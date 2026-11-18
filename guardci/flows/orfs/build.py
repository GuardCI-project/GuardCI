"""ORFS stage machinery (DESIGN.md §9.2/§9.3/§9.10).

Stage list ``synth → floorplan → place → cts → route → finish`` maps to
``make synth / do-floorplan / do-place / do-cts / do-route / do-finish
do-gds``: the ``do-*`` targets run the same steps as the native targets
without mtime dependency checks, so a resumed build executes exactly the
commands of a clean one. ``synth`` is the first stage and therefore only
ever runs on a freshly materialized tree, where the native target's file
dependencies are satisfied.

Container layout (§M2a): the node workdir is mounted rw at ``/work/flow``
(the path recorded episodes used), CI scripts ro at ``/guardci``, output
dirs rw at ``/out``, the golden baseline export ro at ``/golden/flow``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
from collections import OrderedDict
from pathlib import Path

import yaml

from ...backends.toolenv import (  # noqa: F401 — re-exported for adapter.py
    CONTAINER_FLOW,
    CONTAINER_GOLDEN,
    CONTAINER_GUARDCI,
    CONTAINER_OUT,
    EXE_VARS,
    TOOL_ENV,
)
from ...core.diff import read_file
from ...core.paths import match_any
from . import config as cfg_mod
from .sdc import parse_sdc

STAGES = ["synth", "floorplan", "place", "cts", "route", "finish"]
LIGHT_STAGE = "synth"

ADAPTER_VERSION = "v3.3"

STAGE_TARGETS = {
    "synth": ["synth"],
    "floorplan": ["do-floorplan"],
    "place": ["do-place"],
    "cts": ["do-cts"],
    "route": ["do-route"],
    "finish": ["do-finish", "do-gds"],
}

SNAPSHOT_DIRS = ("results", "logs", "reports", "objects")

# intra-stage intermediates are never read by a later stage's do-* chain
# (cross-stage deps are always N_<stage>.odb/.sdc — see the Makefile
# do-step/do-copy table) nor by an export, so snapshots drop them:
# 2_1_floorplan.odb … 5_1_grt.odb, 5_2_route.odb, 6_1_fill.odb,
# 6_1_merged.gds. Stage-final N_<stage>.odb/.sdc, 1_synth.odb and
# 6_final.* stay.
_SNAPSHOT_DROP = re.compile(r"^\d+_\d+_.*\.(?:odb|gds)$")

# ---------------------------------------------------------------- stages

_ORFS_STAGE_ORDER = ["synth", "floorplan", "place", "cts", "grt", "route",
                     "final"]


def _orfs_stage_index(name: str) -> int | None:
    """ORFS variables.yaml stage name → our stage index."""
    m = {"synth": 0, "floorplan": 1, "place": 2, "cts": 3, "grt": 4,
         "route": 4, "final": 5}
    return m.get(name)


_ADAPTER_TAG_STAGE = {"synthesis": 0, "floorplan": 1, "placement": 2,
                      "cts": 3, "routing": 4}


def _script_stage(name: str) -> int | None:
    """scripts/<name> → stage index (§9.3 script table); None → unattributable."""
    if fnmatch.fnmatchcase(name, "synth*.tcl"):
        return 0
    if name in ("floorplan.tcl", "macro_place.tcl", "tapcell.tcl",
                "pdn.tcl") or fnmatch.fnmatchcase(name, "io_placement*.tcl"):
        return 1
    if name in ("resize.tcl", "detail_place.tcl") or \
            fnmatch.fnmatchcase(name, "global_place*.tcl"):
        return 2
    if name == "cts.tcl":
        return 3
    if name in ("global_route.tcl", "detail_route.tcl", "fillcell.tcl"):
        return 4
    if name in ("density_fill.tcl", "final_report.tcl",
                "report_metrics.tcl"):
        return 5
    return None


def _git(repo: str, *args: str, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args],
                          capture_output=True, check=check)


def tree_blobs(repo: str, sha: str) -> dict:
    """{path: blob_sha} for every file in the commit's tree."""
    out = _git(repo, "ls-tree", "-r", sha).stdout.decode("utf-8",
                                                         "replace")
    blobs = {}
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) >= 3 and parts[1] == "blob":
            blobs[path] = parts[2]
    return blobs


def _hash_entries(entries) -> str:
    h = hashlib.sha256()
    for e in entries:
        h.update(json.dumps(e, sort_keys=True,
                            separators=(",", ":")).encode())
        h.update(b"\n")
    return h.hexdigest()


def protected_set_hash(blobs: dict, protected) -> str:
    entries = sorted([p, b] for p, b in blobs.items()
                     if match_any(p, protected or []))
    return _hash_entries(entries)


class Closure:
    """Recipe closure result (§9.3 "New files"): which files absent
    from the baseline tree the built recipe names."""

    def __init__(self, reachable: dict, unreachable: set, named: set):
        self.reachable = reachable        # {path: stage_index | None}
        self.unreachable = unreachable    # new paths in no key
        self.named = named                # extra candidates still named


_SCAN_EXTS = (".mk", ".tcl", ".sh", ".py", ".sdc", ".script", ".ys",
              ".yaml", ".json", ".cfg", ".txt")
_SCAN_MAX = 1 << 20
_COMP_RE = re.compile(r"[A-Za-z0-9_.\-]+")
_BLOB_TEXT: dict = {}
_BLOB_TEXT_MAX = 4096
_BLOB_COMPONENTS: OrderedDict = OrderedDict()
_BLOB_COMPONENTS_MAX = 20000
_VAR_USE_RE = re.compile(
    r"env\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)"
    r"|\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
_BLOB_VAR_USES: OrderedDict = OrderedDict()
_BLOB_VAR_USES_MAX = 20000


def _scannable(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.startswith("Makefile") or name.endswith(_SCAN_EXTS)


def _blob_text(repo: str, blob_sha: str) -> str | None:
    """Text of a git blob ≤ 1 MB (cached per blob sha — a blob is
    content-addressed, so this serves baseline and head alike)."""
    if blob_sha in _BLOB_TEXT:
        return _BLOB_TEXT[blob_sha]
    r = _git(repo, "cat-file", "blob", blob_sha, check=False)
    if r.returncode != 0:
        _BLOB_TEXT[blob_sha] = None
        return None
    data = r.stdout
    if len(data) > _SCAN_MAX:
        _BLOB_TEXT[blob_sha] = None
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if len(_BLOB_TEXT) >= _BLOB_TEXT_MAX:
        _BLOB_TEXT.clear()
    _BLOB_TEXT[blob_sha] = text
    return text


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _blob_components(repo: str, blob_sha: str) -> frozenset | None:
    """The token set of a text blob — one regex pass per blob sha,
    cached (content-addressed). A new file is *named* by a build-tree
    text iff its base name is one of these tokens (§9.3 "New files"):
    `/`, `$`, `)`, whitespace … all delimit, so
    `source $::env(DESIGN_DIR)/helper.tcl` names `helper.tcl` while
    `foo.tcl.bak` and `xfoo.tcl` do not."""
    if blob_sha in _BLOB_COMPONENTS:
        _BLOB_COMPONENTS.move_to_end(blob_sha)
        return _BLOB_COMPONENTS[blob_sha]
    text = _blob_text(repo, blob_sha)
    comps = None if text is None else \
        frozenset(_COMP_RE.findall(text))
    if len(_BLOB_COMPONENTS) >= _BLOB_COMPONENTS_MAX:
        _BLOB_COMPONENTS.pop(next(iter(_BLOB_COMPONENTS)))
    _BLOB_COMPONENTS[blob_sha] = comps
    return comps


def _blob_var_uses(repo: str, blob_sha: str) -> frozenset | None:
    """Variable names *used* in a text blob as ``env(VAR)`` /
    ``$VAR`` / ``${VAR}`` (§11.4.2) — unlike the token set, a bare
    name in a printed string or comment does not count."""
    if blob_sha in _BLOB_VAR_USES:
        _BLOB_VAR_USES.move_to_end(blob_sha)
        return _BLOB_VAR_USES[blob_sha]
    text = _blob_text(repo, blob_sha)
    uses = None if text is None else frozenset(
        v for m in _VAR_USE_RE.finditer(text)
        for v in (m.group(1), m.group(2)) if v)
    if len(_BLOB_VAR_USES) >= _BLOB_VAR_USES_MAX:
        _BLOB_VAR_USES.pop(next(iter(_BLOB_VAR_USES)))
    _BLOB_VAR_USES[blob_sha] = uses
    return uses


class StageKeyer:
    """§9.3 attribution for one (repo, sha, baseline_sha) triple."""

    def __init__(self, repo, sha, baseline_sha, image_id, protected,
                 design=None, config_path=None):
        self.repo = repo
        self.sha = sha
        self.baseline_sha = baseline_sha
        self.image_id = image_id
        self.protected = protected or []
        self.design = design
        self.config_path = config_path
        self.blobs = tree_blobs(repo, sha)
        base = tree_blobs(repo, baseline_sha) if baseline_sha else {}
        self.base_blobs = base
        self.baseline_stages = self._baseline_stages()
        # inputs: stage index -> list of entry tuples
        self.attributed: dict[int, list] = {i: [] for i in
                                            range(len(STAGES))}
        self.unattributable: list = []
        self._handled: set = set()
        self._mention_stages: dict[str, frozenset] = {}

    def _baseline_stages(self) -> dict:
        text = read_file(self.repo, self.baseline_sha,
                         "scripts/variables.yaml") if self.baseline_sha \
            else None
        if not text:
            return {}
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError:
            return {}
        out = {}
        all_idx = frozenset(range(len(STAGES)))
        if isinstance(doc, dict):
            for var, spec in doc.items():
                stages = (spec or {}).get("stages") if isinstance(
                    spec, dict) else None
                if not stages:
                    continue
                if "All stages" in stages or \
                        "generate_abstract" in stages:
                    out[str(var)] = all_idx
                    continue
                idxs = {_orfs_stage_index(s) for s in stages}
                idxs = frozenset(i for i in idxs if i is not None)
                if idxs:
                    out[str(var)] = idxs
        return out

    # ------------------------------------------------------ attribution

    _SCRIPT_MENTION_EXTS = (".tcl", ".sh", ".py")

    def var_mention_stages(self, var: str) -> frozenset:
        """Stages whose flow scripts mention ``var`` as ``env(VAR)`` /
        ``$VAR`` / ``${VAR}`` (§11.4.2): baseline ``scripts/`` text,
        the head text when the script was modified. A mention in an
        unmapped ``scripts/*.tcl|sh|py`` counts for every stage."""
        if var in self._mention_stages:
            return self._mention_stages[var]
        out: set = set()
        for p, b in self.blobs.items():
            if p not in self.base_blobs or \
                    not p.startswith("scripts/"):
                continue
            name = p.rsplit("/", 1)[-1]
            if not name.endswith(self._SCRIPT_MENTION_EXTS):
                continue
            uses = _blob_var_uses(self.repo, b)
            if uses is None or var not in uses:
                continue
            st = _script_stage(name)
            if st is None:
                out.update(range(len(STAGES)))
            else:
                out.add(st)
        fs = frozenset(out)
        self._mention_stages[var] = fs
        return fs

    def stages_for_var(self, var: str, adapter_tags: set) -> frozenset:
        """Stage set a config variable feeds (§11.4.2): ORFS-declared
        stages ∪ script-mention stages; a variable with neither feeds
        every stage; ``measurement``-tagged feeds none."""
        if "measurement" in adapter_tags:
            return frozenset()
        out = set(self.baseline_stages.get(var, ()))
        out |= set(self.var_mention_stages(var))
        if not out:
            idxs = [_ADAPTER_TAG_STAGE[t] for t in adapter_tags
                    if t in _ADAPTER_TAG_STAGE]
            if idxs:
                return frozenset(idxs)
            out = set(range(len(STAGES)))
        return frozenset(out)

    def var_stage(self, var: str, adapter_tags: set) -> int | None:
        """Earliest stage index a variable feeds; None = excluded."""
        stages = self.stages_for_var(var, adapter_tags)
        return min(stages) if stages else None

    def add_var(self, var: str, forms: list, value: str, stages):
        for st in stages:
            self.attributed[st].append(["var", var, forms, value])

    def add_file(self, path: str, stages):
        """A file/dir referenced by a variable — at the variable's
        stages (§11.4.2)."""
        if path in self.blobs:
            entry = ["file", path, self.blobs[path]]
            for st in stages:
                self.attributed[st].append(entry)
            self._handled.add(path)
            return
        prefix = path + "/"
        sub = sorted([p, b] for p, b in self.blobs.items()
                     if p.startswith(prefix))
        if sub:
            entry = ["dir", path, sub]
            for st in stages:
                self.attributed[st].append(entry)
            self._handled.update(p for p, _ in sub)
        else:
            for st in stages:
                self.attributed[st].append(["missing", path])

    def attribute_config(self, cpath: str, var_tags: dict):
        text = read_file(self.repo, self.sha, cpath)
        self._handled.add(cpath)
        if text is None:
            return
        ddir = cpath.rsplit("/", 1)[0]

        def resolver(operand):
            import posixpath as pp
            rel = cfg_mod.resolve_value(
                operand, {"DESIGN_DIR": ddir, "DESIGN_HOME": "designs",
                          "FLOW_HOME": "."})
            norm = pp.normpath(pp.join(ddir, rel))
            t = read_file(self.repo, self.sha, norm)
            if t is not None:
                self._handled.add(norm)
            return (norm, t) if t is not None else None

        # a line the parser cannot account for — in the config or in a
        # resolved include — means its variable set is incomplete:
        # the whole config text is unattributable (and stays a namer)
        unparsed: list = []
        forms: dict = {}
        cfg = cfg_mod.parse_config_mk(text, resolver,
                                      _unparsed=unparsed,
                                      _forms=forms)
        if unparsed:
            self.unattributable.append(
                ["text", cpath,
                 hashlib.sha256(text.encode()).hexdigest()])
            return
        env = cfg_mod.build_env(cfg, ddir)
        files = sorted(self.blobs)
        path_vars: dict[str, frozenset] = {}
        for var, raw in cfg.items():
            tags = var_tags.get(var, set())
            stages = self.stages_for_var(var, tags)
            if not stages:
                continue
            resolved = cfg_mod.resolve_value(raw, env)
            resolved = cfg_mod.expand_wildcards(resolved, files)
            self.add_var(var, forms.get(var, []), resolved, stages)
            path_vars[var] = stages
        for var, p in cfg_mod.path_values(cfg, env, files):
            stages = path_vars.get(var)
            if stages:
                self.add_file(p, stages)

    def attribute_scripts(self):
        # baseline scripts only — a new scripts/ file is scratch and
        # enters keys only through the recipe closure (§9.3)
        for p in self.blobs:
            if not p.startswith("scripts/") or \
                    p not in self.base_blobs:
                continue
            self._handled.add(p)
            stage = _script_stage(p.rsplit("/", 1)[-1])
            if stage is None:
                self.unattributable.append(["file", p, self.blobs[p]])
            else:
                self.attributed[stage].append(["file", p, self.blobs[p]])

    def attribute_rest(self, scratch_tag, closure=None):
        """Makefile, util/**, modified/deleted baseline files and new
        files via the recipe closure (§9.3)."""
        reachable = closure.reachable if closure else {}
        unreachable = closure.unreachable if closure else set()
        for p, b in sorted(self.blobs.items()):
            if p in self._handled:
                continue
            if p in self.base_blobs:
                # baseline file: protected set is covered by the
                # protected hash; unchanged files are constant and
                # contribute nothing to key distinctness
                if match_any(p, self.protected):
                    continue
                if self.base_blobs[p] == b:
                    continue
                if scratch_tag(p) == "measurement":
                    continue
                self.unattributable.append(["file", p, b])
                continue
            # new file (absent from the baseline tree)
            if p in unreachable:
                continue                    # scratch — in no key
            stages = reachable.get(p)
            if not stages or all(s_ is None for s_ in stages):
                self.unattributable.append(["file", p, b])
            else:
                for st in stages:
                    if st is not None:
                        self.attributed[st].append(["file", p, b])
        for p in sorted(set(self.base_blobs) - set(self.blobs)):
            if match_any(p, self.protected):
                continue
            if scratch_tag(p) == "measurement":
                continue
            self.unattributable.append(["deleted", p])

    def header(self) -> dict:
        return {"flow": "orfs", "v": ADAPTER_VERSION,
                "image": self.image_id,
                "protected": protected_set_hash(self.base_blobs,
                                                  self.protected)}

    def key(self, stage_index: int, prev_digest: str | None) -> str:
        """Content-chained stage key (§11.4.2):
        ``H(header, output_digest(s−1), inputs(s), unattributable)`` —
        per-stage inputs, not cumulative."""
        parts = [dict(self.header(), stage=STAGES[stage_index])]
        if prev_digest is not None:
            parts.append(["output_digest", prev_digest])
        parts += sorted(self.attributed[stage_index])
        parts += sorted(self.unattributable)
        return _hash_entries(parts)

    def keys(self, digests=None) -> list:
        """[(stage, key|None)] in stage order. ``digests``: recorded
        output_digest per stage (index entries); a stage whose
        predecessor's digest is unknown gets key ``None`` — and so do
        all later stages."""
        prev = None
        out = []
        for i, stage in enumerate(STAGES):
            if i > 0 and prev is None:
                out.append((stage, None))
                continue
            out.append((stage, self.key(i, prev)))
            if digests is not None:
                prev = digests[i] if i < len(digests) else None
        return out

    # ------------------------------------------------------ closure

    def compute_closure(self, var_tags: dict,
                        extra_candidates=()) -> Closure:
        """Recipe closure over new files (§9.3 "New files").

        ``reachable``: new path → stage index (None = unattributable);
        ``unreachable``: new paths the recipe never names;
        ``named``: extra candidates (e.g. deleted new files) whose name
        still occurs in a build-tree text.
        """
        new_paths = {p for p in self.blobs if p not in self.base_blobs}
        extra = set(extra_candidates) - set(self.blobs)
        result: dict[str, int | None] = {}
        cpath = self.config_path
        ddir = cpath.rsplit("/", 1)[0] if cpath else None

        cfg, env, includes = {}, {}, []
        config_supported = False
        if cpath and cpath in self.blobs:
            text = read_file(self.repo, self.sha, cpath)
            if text is not None:
                collected: list = []
                unparsed: list = []

                def resolver(operand):
                    rel = cfg_mod.resolve_value(
                        operand, {"DESIGN_DIR": ddir,
                                  "DESIGN_HOME": "designs",
                                  "FLOW_HOME": "."})
                    import posixpath as pp
                    norm = pp.normpath(pp.join(ddir, rel))
                    t = read_file(self.repo, self.sha, norm)
                    if t is not None:
                        collected.append(norm)
                    return (norm, t) if t is not None else None

                cfg = cfg_mod.parse_config_mk(text, resolver,
                                              _unparsed=unparsed)
                if not unparsed:
                    config_supported = True
                    env = cfg_mod.build_env(cfg, ddir)
                    includes = collected
                else:
                    cfg = {}

        files = sorted(self.blobs)
        # (a) variable values + includes — the variable's stage set
        ref_stage: dict[str, frozenset] = {}
        for var, p in cfg_mod.path_values(cfg, env, files):
            stages = self.stages_for_var(var, var_tags.get(var, set()))
            if not stages:
                continue
            ref_stage[p] = ref_stage.get(p, frozenset()) | stages
        for p in new_paths:
            if p in ref_stage:
                result[p] = ref_stage[p]
                continue
            for d, st in ref_stage.items():
                if p.startswith(d + "/"):
                    result[p] = result.get(p, frozenset()) | st
                    break
        for inc in includes:
            if inc in new_paths:
                result[inc] = result.get(inc, frozenset()) | {0}
        ref_dirs = {d: s for d, s in ref_stage.items()
                    if any(q.startswith(d + "/") for q in self.blobs)}

        def namer_stage(p: str) -> frozenset | None:
            """Stage set the namer attributes to; None = unattributable."""
            if p == cpath:
                # an unparseable config (unsupported make constructs)
                # still names files, but gives no stage attribution
                return frozenset({0}) if config_supported else None
            if cpath and p in includes:
                return frozenset({0})
            if p in ref_stage:
                return ref_stage[p]
            for d, st in ref_dirs.items():
                if p.startswith(d + "/"):
                    return st
            if p.startswith("scripts/"):
                st = _script_stage(p.rsplit("/", 1)[-1])
                return frozenset({st}) if st is not None else None
            return None        # Makefile, util, protected, unmapped …

        # namers: baseline text files present at head (+ decided new
        # files at the fixpoint), each pre-reduced to its token set —
        # one set lookup per (candidate, namer) instead of a regex.
        namers: dict[str, tuple] = {}
        for p, b in self.blobs.items():
            if p not in self.base_blobs or not _scannable(p):
                continue
            comps = _blob_components(self.repo, b)
            if comps is None:
                continue
            namers[p] = (comps, namer_stage(p))

        # new files already reached via variables/includes are part of
        # the build tree too — they can name further files
        for p in list(result):
            if _scannable(p):
                comps = _blob_components(self.repo, self.blobs[p])
                if comps is not None:
                    namers[p] = (comps, result[p])

        remaining = ({p for p in new_paths if p not in result}
                     | (extra - set(result)))
        named_extra: set = set()
        while remaining:
            decided = {}
            for cand in remaining:
                stages: set = set()
                unattributable = False
                base = _basename(cand)
                for np_, (comps, st) in namers.items():
                    if np_ == cand:
                        continue
                    if base in comps:
                        if st is None:
                            unattributable = True
                        else:
                            stages |= set(st)
                if unattributable or stages:
                    decided[cand] = (None if unattributable
                                     else frozenset(stages))
            if not decided:
                break
            for cand, st in decided.items():
                remaining.discard(cand)
                if cand in extra:
                    named_extra.add(cand)
                    continue
                result[cand] = st
                # a decided new file joins the build tree: it can name
                # further files in later fixpoint rounds
                if _scannable(cand):
                    comps = _blob_components(self.repo,
                                             self.blobs[cand])
                    if comps is not None:
                        namers[cand] = (comps, st)

        result = {p: (frozenset(v) if isinstance(v, (set, frozenset))
                      else frozenset({v}))
                  for p, v in result.items()}
        unreachable = {p for p in new_paths if p not in result}
        self._closure_namers = namers
        self._closure_refs = ref_stage
        self._closure_ddir = ddir
        return Closure(reachable=result, unreachable=unreachable,
                       named=named_extra)


# ------------------------------------------------------------ filesystem

def materialize(repo, sha, baseline_sha, protected, workdir):
    """tree(sha) into workdir; protected paths overwritten from baseline."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    archive = subprocess.Popen(["git", "-C", repo, "archive", sha],
                               stdout=subprocess.PIPE)
    untar = subprocess.run(["tar", "-x", "-C", str(workdir)],
                           stdin=archive.stdout, capture_output=True,
                           check=False)
    archive.wait()
    if archive.returncode != 0 or untar.returncode != 0:
        raise RuntimeError(
            f"git archive {sha[:12]} failed: {untar.stderr[:300]!r}")
    if not protected or not baseline_sha:
        return
    base = tree_blobs(repo, baseline_sha)
    head = tree_blobs(repo, sha)
    for path in sorted(base):
        if not match_any(path, protected):
            continue
        if head.get(path) == base[path]:
            continue          # already the baseline blob — skip git show
        dest = workdir / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        blob = subprocess.run(
            ["git", "-C", repo, "show", f"{baseline_sha}:{path}"],
            capture_output=True, check=True)
        dest.write_bytes(blob.stdout)


def _same_file(a: Path, b: Path) -> bool:
    try:
        sa, sb = a.stat(), b.stat()
    except OSError:
        return False
    return sa.st_size == sb.st_size and sa.st_mtime_ns == sb.st_mtime_ns


def _snapshot_dir(src: Path, dst: Path, prev: Path | None):
    """Copy src → dst, hard-linking files identical to prev (cp -al
    semantics). Cache files are immutable, so dst may link to prev."""
    if not src.is_dir():
        return
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        out_root = dst if rel == "." else dst / rel
        out_root.mkdir(parents=True, exist_ok=True)
        prev_root = (prev / rel) if prev is not None and rel != "." \
            else prev
        for name in files:
            s = Path(root) / name
            d = out_root / name
            p = (prev_root / name) if prev_root is not None else None
            if p is not None and p.is_file() and _same_file(s, p):
                os.link(p, d)
            else:
                shutil.copy2(s, d)


def snapshot(workdir, stage, dest, prev=None):
    """Cumulative results/ logs/ reports/ objects/ snapshot, minus
    intra-stage intermediates (``_SNAPSHOT_DROP``)."""
    dest = Path(dest)
    prev = Path(prev) if prev else None
    for d in SNAPSHOT_DIRS:
        src = Path(workdir) / d
        if d == "results" and src.is_dir():
            # drop sub-step databases/streams: hard-link-friendly copy
            # that skips them instead of pruning the workdir
            tmp = dest / d
            for root, dirs, files in os.walk(src):
                rel = os.path.relpath(root, src)
                out_root = tmp if rel == "." else tmp / rel
                out_root.mkdir(parents=True, exist_ok=True)
                prev_root = (prev / d / rel) if prev is not None else None
                for name in files:
                    if _SNAPSHOT_DROP.match(name):
                        continue
                    s = Path(root) / name
                    dd = out_root / name
                    p = (prev_root / name) if prev_root is not None \
                        else None
                    if p is not None and p.is_file() and \
                            _same_file(s, p):
                        os.link(p, dd)
                    else:
                        shutil.copy2(s, dd)
            continue
        _snapshot_dir(src, dest / d, (prev / d) if prev else None)


def restore(src, workdir):
    """Copy a snapshot back into a workdir (content copies — the build
    must never be able to mutate a cached file)."""
    src = Path(src)
    for d in SNAPSHOT_DIRS:
        s = src / d
        if s.is_dir():
            shutil.copytree(s, Path(workdir) / d, dirs_exist_ok=True)


# --------------------------------------------------------------- golden

def export_tree(repo, sha, dest):
    """git archive sha → dest (a plain directory)."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.Popen(["git", "-C", repo, "archive", sha],
                               stdout=subprocess.PIPE)
    untar = subprocess.run(["tar", "-x", "-C", str(dest)],
                           stdin=archive.stdout, capture_output=True,
                           check=False)
    archive.wait()
    if archive.returncode != 0 or untar.returncode != 0:
        raise RuntimeError(f"git archive failed: {untar.stderr[:300]!r}")
    return dest


def _abs_golden(value: str) -> str:
    if not value:
        return value
    toks = []
    for tok in value.split():
        if tok.startswith("/"):
            toks.append(tok)
        else:
            toks.append(f"{CONTAINER_GOLDEN}/{tok.lstrip('./')}")
    return " ".join(toks)


def parse_print_vars(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Z_][A-Z0-9_]*):\s*(.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


# ------------------------------------------------------- SDC golden spec

def parameterize_sdc(sdc_text: str):
    """(new_text, method) replacing the clock period with
    ``$::guardci_period`` — port of phb reference.parameterize_sdc."""
    matches = re.findall(r"^set\s+clk_period\s+\S+", sdc_text, re.MULTILINE)
    if len(matches) == 1:
        return (re.sub(r"^set\s+clk_period\s+\S+",
                       "set clk_period $::guardci_period", sdc_text,
                       flags=re.MULTILINE), "set_clk_period")
    clocks = re.findall(
        r"create_clock\s+[^\n]*?-period\s+([0-9.]+)", sdc_text)
    if len(clocks) == 1:
        return (re.sub(
            r"(create_clock\s+[^\n]*?-period\s+)[0-9.]+",
            r"\g<1>$::guardci_period", sdc_text, count=1), "create_clock")
    if len(clocks) > 1 and len(set(clocks)) == 1:
        return (re.sub(
            r"(create_clock\s+[^\n]*?-period\s+)[0-9.]+",
            r"\g<1>$::guardci_period", sdc_text), "create_clock_all")
    raise RuntimeError(
        f"cannot parameterize SDC: {len(matches)} 'set clk_period' lines, "
        f"{len(clocks)} numeric create_clock periods")


_PORT_RE = re.compile(r"get_(?:ports|pins|cells)\s+([^\s\]]+)")


def _sdc_main_clock(parsed: dict):
    """name + resolved source ports of the first create_clock."""
    vars_ = parsed.get("vars", {})

    def resolve(tok: str) -> str:
        tok = tok.strip("[]")
        if tok.startswith("$"):
            return resolve(vars_.get(tok[1:], tok))
        return tok

    def expand(text: str, depth=0) -> str:
        if depth > 5:
            return text
        out = re.sub(r"\$(\w+)|\$\{(\w+)\}",
                     lambda m: vars_.get(m.group(1) or m.group(2),
                                         m.group(0)), text)
        return expand(out, depth + 1) if out != text else out

    for cmd in parsed.get("commands", []):
        if cmd.get("verb") != "create_clock":
            continue
        params = cmd.get("params", {})
        name = resolve(str(params.get("-name", ""))) or None
        joined = expand(" ".join(str(t) for t in
                                 params.get("_pos", [])))
        sources = [resolve(m.group(1)) for m in _PORT_RE.finditer(joined)]
        return {"name": name, "sources": sources}
    return None


def golden_constraints(sdc_text: str) -> dict:
    """{template, method, main_clock, golden_period} from the golden SDC."""
    template, method = parameterize_sdc(sdc_text)
    parsed = parse_sdc(sdc_text)
    period = None
    if method == "set_clk_period":
        raw = parsed["vars"].get("clk_period")
        try:
            period = float(raw)
        except (TypeError, ValueError):
            period = None
    else:
        m = re.search(r"create_clock\s+[^\n]*?-period\s+([0-9.]+)",
                      sdc_text)
        if m:
            period = float(m.group(1))
    return {"template": template, "method": method,
            "main_clock": _sdc_main_clock(parsed) or
            {"name": None, "sources": []},
            "golden_period": period}


def find_results_dir(workdir, platform=None, design=None) -> Path | None:
    """results/<platform>/<design>/<variant> under workdir."""
    res = Path(workdir) / "results"
    if not res.is_dir():
        return None
    for p in res.glob("*/*/*"):
        if p.is_dir():
            return p
    return None


# §11.4.2: files that are not reproducible between two identical
# builds and that no later stage or artifact export reads —
# ``6_final.gds``/``6_final.spef`` embed run metadata; exports read
# ``.def``/``.sdc``/``.odb`` only. Everything else must be identical.
_DIGEST_EXCLUDE = re.compile(r"(\.gds|\.spef)$")


def snapshot_digest(root) -> str:
    """§11.4.2 output digest: sha256 over (relative path, content hash)
    of every file in the stage's snapshot under ``results/`` and
    ``objects/`` (not ``logs/``/``reports/``; ``_DIGEST_EXCLUDE``
    aside)."""
    entries = []
    root = Path(root)
    for d in ("results", "objects"):
        base = root / d
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*")):
            if f.is_file() and not _DIGEST_EXCLUDE.search(f.name):
                entries.append(
                    [str(f.relative_to(root)),
                     hashlib.sha256(f.read_bytes()).hexdigest()])
    return _hash_entries(entries)
