"""OrfsFlow — flow adapter for OpenROAD-flow-scripts projects.

classify_delta maps a Diff onto the §3.4 tag vocabulary using
``variables.yaml`` (variable → tags table shipped with the adapter).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections import OrderedDict
from importlib.resources import files as res_files
from pathlib import Path
from typing import ClassVar

import yaml

from ...core.diff import Diff, list_tree, read_file
from ...core.models import Tag
from ...core.paths import match, match_any
from ..base import ConstraintFiles, DesignIdentity, FlowAdapter, Reference, SdcCommand, register
from . import build as build_mod
from . import config as cfg_mod
from .sdc import parse_sdc

CONFIG_GLOB = "designs/*/*/config.mk"
IMPL_SDC_GLOB = "designs/*/*/impl.sdc"
DESIGN_TCL_GLOB = "designs/*/*/*.tcl"

_REF_KIND = {".v": "rtl", ".sv": "rtl", ".sdc": "sdc", ".lib": "lib",
             ".lef": "lef", ".rules": "rc", ".tcl": "script",
             ".def": "def", ".gds": "gds"}

_var_tags_cache = None
_unknown_hook = None
_unknown_seen: set = set()


def set_unknown_hook(fn):
    """Install a callback fired for each config variable that maps to
    ``unknown`` during classify_delta (replay instrumentation)."""
    global _unknown_hook
    _unknown_hook = fn


def unknown_vars() -> set:
    """Variables seen so far that mapped to `unknown`."""
    return set(_unknown_seen)


def reset_unknown():
    _unknown_seen.clear()


def _var_tags() -> dict:
    """variables.yaml → {VAR: {tag,...}} merged across categories."""
    global _var_tags_cache
    if _var_tags_cache is None:
        doc = yaml.safe_load(
            res_files("guardci.flows.orfs")
            .joinpath("variables.yaml").read_text())
        table = {}
        for category, entries in doc.items():
            if not isinstance(entries, (list, dict)):
                continue
            if isinstance(entries, dict):
                continue  # scripts: section handled separately
            for var in entries:
                var = str(var).split()[0]
                table.setdefault(var, set()).add(category)
        _var_tags_cache = {"vars": table, "scripts": doc.get("scripts", {})}
    return _var_tags_cache


def _config_paths(repo, sha) -> list:
    return [p for p in list_tree(repo, sha, "designs")
            if match(p, CONFIG_GLOB)]


_SCRATCH_UNKNOWN = (".tcl", ".mk", ".sdc", ".script", ".ys", ".py",
                    ".sh", ".def", ".lef", ".lib")
_SCRATCH_RTL = (".v", ".sv", ".vh", ".svh")


def _scratch_tag(path: str) -> str:
    """Tag for a changed path matching no recipe glob (scratch file)."""
    name = path.rsplit("/", 1)[-1]
    if name.startswith("Makefile") or name.endswith(_SCRATCH_UNKNOWN):
        return Tag.UNKNOWN
    if name.endswith(_SCRATCH_RTL):
        return Tag.DESIGN_REFS
    return Tag.MEASUREMENT


_CLOSURE_CACHE: OrderedDict = OrderedDict()
_CLOSURE_CACHE_MAX = 256


def _tcl_var_tags(repo, sha, cpath: str, path: str) -> set:
    """Tags of any *_TCL variable in the built config that references
    path."""
    out = set()
    text = read_file(repo, sha, cpath) if cpath else None
    if text is None:
        return out
    c = cfg_mod.parse_config_mk(text)
    env = cfg_mod.build_env(c, cpath.rsplit("/", 1)[0])
    for var, val in c.items():
        if not var.endswith("_TCL"):
            continue
        resolved = cfg_mod.resolve_value(val, env)
        if path in resolved.split():
            out |= _var_tags()["vars"].get(var, set())
    return out


@register
class OrfsFlow(FlowAdapter):
    name = "orfs"
    capabilities: ClassVar[set] = set()
    light_stage = build_mod.LIGHT_STAGE

    def __init__(self, platform=None, design=None, protected=None):
        self.platform = platform
        self.design = design
        self.protected_globs = list(protected or [])

    def recipe_schema(self) -> dict:
        return {
            "recipe_globs": ["designs/*/*/config.mk", "designs/*/*/impl.sdc",
                             "designs/*/*/*.tcl", "scripts/**", "Makefile"],
            "config_glob": CONFIG_GLOB,
            "impl_sdc_glob": IMPL_SDC_GLOB,
        }

    def _resolver(self, repo, sha, cpath, collected=None):
        """include resolver for config.mk at cpath: operand expanded
        against a bootstrap env, resolved relative to the config dir."""
        ddir = cpath.rsplit("/", 1)[0]

        def resolve(operand):
            rel = cfg_mod.resolve_value(
                operand, {"DESIGN_DIR": ddir, "DESIGN_HOME": "designs",
                          "FLOW_HOME": "."})
            import os.path as osp
            norm = osp.normpath(osp.join(ddir, rel))
            text = read_file(repo, sha, norm)
            return (norm, text) if text is not None else None
        return resolve

    def _config_tags(self, repo, head, entry) -> set:
        """Tags for one changed config.mk via semantic diff."""
        tags = set()
        table = _var_tags()["vars"]
        before = cfg_mod.parse_config_mk(
            entry.before or "",
            self._resolver(repo, head, entry.path))
        after = cfg_mod.parse_config_mk(
            entry.after or "",
            self._resolver(repo, head, entry.path))
        sd = cfg_mod.semantic_diff(before, after)
        changed = set(sd["added"]) | set(sd["removed"]) | set(sd["changed"])
        for var in changed:
            t = table.get(var)
            if t:
                tags |= t
            else:
                tags.add(Tag.UNKNOWN)
                _unknown_seen.add(var)
                if _unknown_hook:
                    _unknown_hook(var)
        return tags

    def _built_config(self, repo, sha, changed=None) -> str | None:
        """designs/<platform>/<design>/config.mk — the config the CI
        builds (§9.3: only this one's variables are parsed)."""
        if self.platform and self.design:
            return f"designs/{self.platform}/{self.design}/config.mk"
        try:
            return self._select_config(repo, sha, changed=changed)
        except (ValueError, IndexError):
            return None

    def classify_delta(self, diff: Diff) -> set:
        tags: set = set()
        script_map = _var_tags()["scripts"]
        baseline_sha = getattr(diff, "baseline", None) or diff.base
        cpath = self._built_config(diff.repo, diff.head,
                                   changed=diff.paths()) \
            if diff.repo else None
        base_blobs = None
        closure = None
        if diff.repo and baseline_sha:
            try:
                base_blobs = build_mod.tree_blobs(diff.repo, baseline_sha)
            except subprocess.CalledProcessError:
                base_blobs = None
        for entry in diff.entries:
            path = entry.path
            if path.startswith(".guardci/"):
                # the CI.s own namespace — never a build
                # input, measured only (§11.3)
                tags.add(Tag.MEASUREMENT)
                continue
            is_new = base_blobs is not None and \
                path not in base_blobs
            # new files are scratch: reachable → tag by extension,
            # unreachable → measurement (§4.2.1 / §9.3)
            if is_new and entry.kind in ("added", "modified"):
                if closure is None:
                    closure = self.closure(diff.repo, diff.head,
                                           baseline_sha)
                if path in closure.reachable:
                    tags.add(_scratch_tag(path))
                else:
                    tags.add(Tag.MEASUREMENT)
                continue
            if is_new and entry.kind == "deleted":
                # a deleted new file matters only if the recipe still
                # names it (the naming change tags itself)
                if closure is None:
                    closure = self.closure(
                        diff.repo, diff.head, baseline_sha,
                        extra_candidates={
                            e.path for e in diff.entries
                            if e.kind == "deleted"
                            and e.path not in base_blobs})
                tags.add(Tag.UNKNOWN if path in closure.named
                         else Tag.MEASUREMENT)
                continue
            if path == cpath or (cpath is None
                                 and match(path, CONFIG_GLOB)):
                tags |= self._config_tags(diff.repo, diff.head, entry)
            elif match(path, IMPL_SDC_GLOB):
                tags.add(Tag.CONSTRAINTS)
            elif match(path, DESIGN_TCL_GLOB):
                t = _tcl_var_tags(diff.repo, diff.head, cpath, path)
                tags |= t if t else {Tag.SCRIPTS, Tag.UNKNOWN}
            elif match_any(path, ["scripts/**"]):
                tags.add(Tag.SCRIPTS)
                fname = path.rsplit("/", 1)[-1]
                st = script_map.get(fname)
                if st:
                    tags |= set(st)
                else:
                    tags.add(Tag.UNKNOWN)
            elif path == "Makefile" or match_any(path, ["Makefile"]) \
                    or entry.kind == "deleted":
                tags.add(Tag.UNKNOWN)
            else:
                tags.add(_scratch_tag(path))
        return tags

    # ------------------------------------------------ normalized reads

    def _select_config(self, repo, sha, design=None, changed=None) -> str:
        """The config.mk path for this design; raises on ambiguity.

        When the flow carries a configured ``platform``/``design``
        (every replay/production use), only the built config
        ``designs/<platform>/<design>/config.mk`` counts — other
        ``designs/*/*/config.mk`` files are scratch (§9.3); discovery
        over candidates remains for unconfigured flows only."""
        if self.platform and self.design:
            built = (f"designs/{self.platform}/{self.design}/"
                     "config.mk")
            if built in list_tree(repo, sha):
                return built
            raise ValueError(
                f"built config {built} missing at {sha[:8]}")
        candidates = _config_paths(repo, sha)
        if design:
            hits = [p for p in candidates
                    if match(p, f"designs/*/{design}/config.mk")]
            if hits:
                return hits[0]
            raise ValueError(
                f"no designs/*/{design}/config.mk at {sha[:8]}")
        if changed:
            touched = [p for p in candidates if p in changed]
            if len(touched) == 1:
                return touched[0]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise ValueError(f"no designs/*/*/config.mk at {sha[:8]}")
        raise ValueError(
            f"multiple designs and none selected: {candidates}")

    def _load(self, repo, sha, cpath):
        collected: list = []
        cfg = cfg_mod.parse_config_mk(
            read_file(repo, sha, cpath) or "",
            self._resolver(repo, sha, cpath), _collected=collected)
        env = cfg_mod.build_env(cfg, cpath.rsplit("/", 1)[0])
        return cfg, env, collected

    def identity(self, repo, sha, design=None, changed=None,
                 protected=None) -> DesignIdentity:
        cpath = self._select_config(repo, sha, design, changed)
        cfg, env, _ = self._load(repo, sha, cpath)
        ddir = cpath.rsplit("/", 1)[0]
        tree = list_tree(repo, sha)

        def paths(var):
            raw = cfg_mod.expand_wildcards(
                cfg_mod.resolve_value(cfg.get(var, ""), env), tree)
            return {t for t in raw.split() if t}

        sdc = cfg_mod.resolve_value(cfg.get("SDC_FILE", ""), env) or None
        if sdc is not None:
            import posixpath
            sdc = posixpath.normpath(sdc)
        if sdc is None:
            role = "missing"
        elif sdc == f"{ddir}/constraint.sdc":
            role = "golden"
        elif (protected and match_any(sdc, protected)) \
                or sdc.startswith("/") or ".." in sdc.split("/") \
                or sdc not in tree:
            role = "other"
        else:
            role = "impl"
        return DesignIdentity(
            platform=cfg_mod.resolve_value(cfg.get("PLATFORM", ""), env),
            top=cfg_mod.resolve_value(cfg.get("DESIGN_NAME", ""), env),
            params=cfg_mod.resolve_value(
                cfg.get("VERILOG_TOP_PARAMS", ""), env),
            rtl_files=paths("VERILOG_FILES"),
            include_dirs=paths("VERILOG_INCLUDE_DIRS"),
            blackboxes=paths("SYNTH_BLACKBOXES"),
            sdc_file=sdc, sdc_role=role, source=cpath)

    def references(self, repo, sha) -> list[Reference]:
        out = []
        tree = set(list_tree(repo, sha))
        if self.platform and self.design:
            # only the built config — variant design dirs are scratch
            cpath = (f"designs/{self.platform}/{self.design}/"
                     "config.mk")
            config_paths = [cpath] if cpath in tree else []
        else:
            config_paths = _config_paths(repo, sha)
        for cpath in config_paths:
            cfg, env, includes = self._load(repo, sha, cpath)
            for var, path in cfg_mod.path_values(cfg, env, tree):
                ext = "." + path.rsplit(".", 1)[-1] if "." in path else ""
                out.append(Reference(var=var, path=path,
                                     kind=_REF_KIND.get(ext, "other")))
            for inc in includes:
                out.append(Reference(var="include", path=inc,
                                     kind="other"))
        return out

    def constraints(self, repo, sha) -> ConstraintFiles:
        cpath = self._select_config(repo, sha)
        cfg, env, _ = self._load(repo, sha, cpath)
        impl = cfg_mod.resolve_value(cfg.get("SDC_FILE", ""), env) or None
        golden = f"{cpath.rsplit('/', 1)[0]}/constraint.sdc"
        return ConstraintFiles(impl=impl, golden=golden)

    def parse_constraints(self, text) -> list[SdcCommand]:
        return [SdcCommand(verb=c["verb"], args=c["params"], raw=c["raw"])
                for c in parse_sdc(text)["commands"]]

    # ------------------------------------------------------ M2: build

    def _design(self, repo=None, sha=None):
        """(platform, design) — configured or resolved from the tree."""
        if self.platform and self.design:
            return self.platform, self.design
        if repo is None or sha is None:
            raise ValueError("platform/design not configured")
        cpath = self._select_config(repo, sha)
        parts = cpath.split("/")
        return parts[1], parts[2]

    def design_config_rel(self, repo=None, sha=None) -> str:
        p, d = self._design(repo, sha)
        return f"./designs/{p}/{d}/config.mk"

    def stages(self) -> list:
        return list(build_mod.STAGES)

    def closure(self, repo, sha, baseline_sha, extra_candidates=()):
        """Recipe closure for (repo, sha, baseline_sha) — shared by
        stage_keys, materialize and classify_delta (§9.3). Cached
        in-process (LRU): it is needed three times per node."""
        key = (str(repo), str(sha), str(baseline_sha), self.platform,
               self.design, tuple(self.protected_globs),
               frozenset(extra_candidates))
        hit = _CLOSURE_CACHE.get(key)
        if hit is not None:
            _CLOSURE_CACHE.move_to_end(key)
            return hit
        keyer = build_mod.StageKeyer(
            repo, sha, baseline_sha, "", self.protected_globs,
            config_path=self._built_config(repo, sha))
        cl = keyer.compute_closure(_var_tags()["vars"],
                                   extra_candidates=extra_candidates)
        if len(_CLOSURE_CACHE) >= _CLOSURE_CACHE_MAX:
            _CLOSURE_CACHE.pop(next(iter(_CLOSURE_CACHE)))
        _CLOSURE_CACHE[key] = cl
        return cl

    def build_tree_new_files(self, repo, sha, baseline_sha) -> list:
        """Sorted reachable new files — the part of the build tree the
        baseline does not provide (§9.3)."""
        return sorted(self.closure(repo, sha, baseline_sha).reachable)

    def _keyer(self, repo, sha, baseline_sha, image_id, protected):
        cpath = self._built_config(repo, sha)
        keyer = build_mod.StageKeyer(
            repo, sha, baseline_sha, image_id, protected,
            config_path=cpath)
        var_tags = _var_tags()["vars"]
        if cpath:
            keyer.attribute_config(cpath, var_tags)
        keyer.attribute_scripts()
        keyer.attribute_rest(_scratch_tag,
                             keyer.compute_closure(var_tags))
        return keyer

    def resolve_keys(self, repo, sha, baseline_sha, image_id,
                     cache=None, protected=None):
        """§11.4.2: ``([(stage, key|None)], keyer)`` — the key chain
        resolved through the cache index; ``None`` from the first
        stage whose predecessor's ``output_digest`` is unrecorded.
        ``keyer`` lets the builder compute later keys once digests are
        known."""
        protected = (protected if protected is not None
                     else self.protected_globs)
        keyer = self._keyer(repo, sha, baseline_sha, image_id,
                            protected)
        digests = None
        if cache is not None:
            digests = []
            prev = None
            for i in range(len(build_mod.STAGES)):
                if i > 0 and prev is None:
                    break
                k = keyer.key(i, prev)
                meta = cache.index_get(k)
                prev = (meta or {}).get("output_digest")
                digests.append(prev)
        return keyer.keys(digests=digests), keyer

    def stage_keys(self, repo, sha, baseline_sha, image_id,
                   protected=None, cache=None) -> list:
        """[(stage, key|None)] — thin wrapper over resolve_keys for
        callers that only need the (resolved) key list."""
        protected = (protected if protected is not None
                     else self.protected_globs)
        pairs, _keyer = self.resolve_keys(repo, sha, baseline_sha,
                                          image_id, cache=cache,
                                          protected=protected)
        return pairs

    def materialize(self, repo, sha, baseline_sha, protected, workdir):
        build_mod.materialize(repo, sha, baseline_sha,
                              protected if protected is not None
                              else self.protected_globs, workdir)
        # unreachable new files are not part of the CI's work tree —
        # what a key does not hash the build cannot read (§9.3)
        cl = self.closure(repo, sha, baseline_sha)
        for p in cl.unreachable:
            f = Path(workdir) / p
            if f.is_file() or f.is_symlink():
                f.unlink()

    def _make_cmd(self, repo, sha, targets, extra=()):
        p, d = self._design(repo, sha)
        cmd = ["make", "--no-print-directory",
               f"DESIGN_CONFIG=./designs/{p}/{d}/config.mk"]
        cmd += [f"{v}={build_mod.TOOL_ENV[v]}" for v in build_mod.EXE_VARS]
        cmd += list(extra) + list(targets)
        return cmd

    def run_stage(self, workdir, stage, executor, slot, timeout, log):
        targets = build_mod.STAGE_TARGETS[stage]
        cmd = self._make_cmd(None, None, targets) \
            if self.platform else self._make_cmd_from_tree(workdir, targets)
        return executor.run(
            cmd,
            mounts={str(workdir): (build_mod.CONTAINER_FLOW, "rw")},
            workdir=build_mod.CONTAINER_FLOW,
            env=build_mod.TOOL_ENV, slot=slot, timeout=timeout, log=log)

    def _make_cmd_from_tree(self, workdir, targets, extra=()):
        """Find designs/*/*/config.mk in a materialized workdir."""
        from pathlib import Path
        hits = sorted(Path(workdir).glob("designs/*/*/config.mk"))
        if len(hits) != 1:
            raise ValueError(f"cannot pick design config: {hits}")
        rel = hits[0].relative_to(workdir)
        cmd = ["make", "--no-print-directory",
               f"DESIGN_CONFIG=./{rel}"]
        cmd += [f"{v}={build_mod.TOOL_ENV[v]}" for v in build_mod.EXE_VARS]
        cmd += list(extra) + list(targets)
        return cmd

    def snapshot(self, workdir, stage, dest, prev=None):
        build_mod.snapshot(workdir, stage, dest, prev=prev)

    def snapshot_digest(self, snap_root):
        """§11.4.2 output digest over the snapshot's results/+objects/."""
        return build_mod.snapshot_digest(snap_root)

    def restore(self, src, workdir):
        build_mod.restore(src, workdir)

    # ----------------------------------------------------- M2: artifacts

    def checkpoint_artifacts(self, workspace) -> dict:
        """§12.2: the author's stage-final ODBs under
        ``results/<platform>/<design>/base/``."""
        p, d = self._design()
        base = Path(workspace) / "results" / p / d / "base"
        out = {}
        names = {"synth": "1_synth.odb", "floorplan": "2_floorplan.odb",
                 "place": "3_place.odb", "cts": "4_cts.odb",
                 "route": "5_route.odb", "finish": "6_final.odb"}
        for stage, fname in names.items():
            cand = base / fname
            if cand.is_file():
                out[stage] = cand
        return out

    def delivery_products(self, workdir) -> dict:
        """§12.5: the rebuilt final database/DEF/netlist (and GDS or
        SPEF when the flow produced them) from the results dir."""
        try:
            res = self._results_dir(workdir)
        except FileNotFoundError:
            return {}
        last_no = len(self.stages())
        out = {}
        for p in sorted(res.iterdir()):
            if not p.is_file():
                continue
            if p.name.startswith(f"{last_no}_") or \
                    p.suffix == ".gds":
                out[p.name] = p
        return out

    def _results_dir(self, workdir):
        p, _d = self._design()
        res = Path(workdir) / "results"
        cands = sorted(res.glob(f"{p}/*/*")) if res.is_dir() else []
        if not cands:
            cands = sorted(res.glob("*/*/*")) if res.is_dir() else []
        if not cands:
            raise FileNotFoundError(f"no results dir under {workdir}")
        return cands[0]

    def export(self, workdir, kind, dest, executor, slot,
               odb=None, golden=None, timeout=600):
        """Export a canonical artifact from a build workdir."""
        from ...artifacts.model import ArtifactKind
        res = self._results_dir(workdir)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if kind == ArtifactKind.LAYOUT:
            shutil.copyfile(res / "6_final.def", dest)
            return dest
        if kind == ArtifactKind.CONSTRAINTS:
            shutil.copyfile(res / "6_final.sdc", dest)
            return dest
        if kind == ArtifactKind.NETLIST:
            odb_name = odb or "6_final.odb"
            if not (res / odb_name).exists():
                odb_name = "1_synth.odb"
            libs = (golden or {}).get("libs") or []
            tcl = res_files("guardci.backends.openroad") \
                .joinpath("write_netlist.tcl")
            outdir = dest.parent / ".export-netlist"
            outdir.mkdir(parents=True, exist_ok=True)
            mounts = {str(workdir): (build_mod.CONTAINER_FLOW, "rw"),
                      str(tcl): (
                          f"{build_mod.CONTAINER_GUARDCI}/write_netlist.tcl",
                          "ro"),
                      str(outdir): (build_mod.CONTAINER_OUT, "rw")}
            if (golden or {}).get("host_root"):
                mounts[str(golden["host_root"])] = (
                    build_mod.CONTAINER_GOLDEN, "ro")
            env = dict(build_mod.TOOL_ENV)
            env["GUARDCI_ODB"] = (
                f"{build_mod.CONTAINER_FLOW}/"
                f"{res.relative_to(workdir)}/{odb_name}")
            env["GUARDCI_LIBS"] = " ".join(libs)
            env["GUARDCI_OUT"] = f"{build_mod.CONTAINER_OUT}/netlist.v"
            rr = executor.run(
                [build_mod.TOOL_ENV["OPENROAD_EXE"], "-no_init",
                 "-no_splash", "-exit",
                 f"{build_mod.CONTAINER_GUARDCI}/write_netlist.tcl"],
                mounts=mounts, workdir="/",
                env=env, slot=slot, timeout=timeout,
                log=outdir / "write_netlist.log")
            out = outdir / "netlist.v"
            if rr.rc != 0 or not out.exists():
                raise RuntimeError(
                    f"write_netlist failed rc={rr.rc} "
                    f"(log {outdir}/write_netlist.log)")
            shutil.copyfile(out, dest)
            return dest
        raise ValueError(f"cannot export {kind}")

    def export_stream(self, layout, golden, dest, executor, slot,
                      timeout=1800):
        """GDS from the CI's DEF via the golden tree's merge."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        host_root = golden["host_root"]
        p, d = self._design()
        out = dest.parent / ".export-stream"
        if out.exists():
            shutil.rmtree(out)
        variant_res = out / "results" / p / d / "base"
        variant_res.mkdir(parents=True)
        for sub in ("logs", "reports", "objects"):
            (out / sub / p / d / "base").mkdir(parents=True,
                                               exist_ok=True)
        shutil.copyfile(layout, variant_res / "6_final.def")
        cmd = self._make_cmd(None, None, ["do-gds"],
                             extra=["WORK_HOME=/out"])
        mounts = {host_root: (build_mod.CONTAINER_GOLDEN, "ro"),
                  str(out): (build_mod.CONTAINER_OUT, "rw")}
        rr = executor.run(cmd, mounts=mounts,
                          workdir=build_mod.CONTAINER_GOLDEN,
                          env=build_mod.TOOL_ENV, slot=slot,
                          timeout=timeout, log=out / "gds.log")
        gds = variant_res / "6_final.gds"
        if rr.rc != 0 or not gds.exists():
            # fallback: hard-linked scratch copy of the golden tree
            scratch = out / "golden-scratch"
            subprocess.run(["cp", "-al", host_root, scratch], check=True)
            scratch_res = scratch / "results" / p / d / "base"
            scratch_res.mkdir(parents=True, exist_ok=True)
            for sub in ("logs", "reports", "objects"):
                (scratch / sub / p / d / "base").mkdir(
                    parents=True, exist_ok=True)
            shutil.copyfile(layout, scratch_res / "6_final.def")
            rr = executor.run(
                self._make_cmd(None, None, ["do-gds"]),
                mounts={str(scratch): (build_mod.CONTAINER_GOLDEN, "rw")},
                workdir=build_mod.CONTAINER_GOLDEN,
                env=build_mod.TOOL_ENV, slot=slot, timeout=timeout,
                log=out / "gds-scratch.log")
            gds = scratch_res / "6_final.gds"
        if not gds.exists():
            raise RuntimeError(f"export_stream failed (logs in {out})")
        shutil.copyfile(gds, dest)
        return dest

    # -------------------------------------------------------- M2: golden

    def golden(self, repo, baseline_sha, protected, dest, executor, slot,
               timeout=600):
        """Export baseline tree + resolve LibRefs via make print-*."""
        dest = Path(dest)
        tree = dest / "tree"
        librefs = dest / "librefs.json"
        if not librefs.exists():
            dest.mkdir(parents=True, exist_ok=True)
            if not tree.exists():
                tmp = dest / ".tree-tmp"
                if tmp.exists():
                    shutil.rmtree(tmp)
                build_mod.export_tree(repo, baseline_sha, tmp)
                tmp.rename(tree)
            names = ("TECH_LEF", "SC_LEF", "ADDITIONAL_LEFS", "LIB_FILES",
                     "RCX_RULES", "KLAYOUT_DRC_FILE", "GDS_FILES",
                     "PLATFORM_DIR", "PWR_NETS_VOLTAGES",
                     "GND_NETS_VOLTAGES", "SET_RC_TCL")
            cmd = self._make_cmd(
                repo, baseline_sha,
                [f"print-{n}" for n in names])
            log = dest / "print.log"
            rr = executor.run(
                cmd, mounts={str(tree): (build_mod.CONTAINER_GOLDEN,
                                         "ro")},
                workdir=build_mod.CONTAINER_GOLDEN,
                env=build_mod.TOOL_ENV, slot=slot, timeout=timeout,
                log=log)
            if rr.rc != 0:
                raise RuntimeError(
                    f"golden print-* failed rc={rr.rc} (log {log})")
            values = build_mod.parse_print_vars(log.read_text(
                errors="replace"))
            abs_ = build_mod._abs_golden
            data = {
                "ok": True,
                "tech_lef": abs_(values.get("TECH_LEF", "")),
                "sc_lefs": abs_(values.get("SC_LEF", "")).split() +
                abs_(values.get("ADDITIONAL_LEFS", "")).split(),
                "libs": abs_(values.get("LIB_FILES", "")).split(),
                "rcx_rules": abs_(values.get("RCX_RULES", "")),
                "klayout_drc_file":
                    abs_(values.get("KLAYOUT_DRC_FILE", "")) or None,
                "platform_dir": abs_(values.get("PLATFORM_DIR", "")),
                "gds_files": abs_(values.get("GDS_FILES", "")).split(),
                "pwr_nets_voltages": values.get("PWR_NETS_VOLTAGES"),
                "gnd_nets_voltages": values.get("GND_NETS_VOLTAGES"),
                "set_rc_tcl": abs_(values.get("SET_RC_TCL", "")),
                "host_root": str(tree),
                "container_root": build_mod.CONTAINER_GOLDEN,
            }
            librefs.write_text(json.dumps(data, indent=2, sort_keys=True))
        return json.loads(librefs.read_text())

    def golden_constraints(self, repo, baseline_sha) -> dict:
        p, d = self._design(repo, baseline_sha)
        text = read_file(repo, baseline_sha,
                         f"designs/{p}/{d}/constraint.sdc")
        if text is None:
            raise ValueError("no golden constraint.sdc at baseline")
        return build_mod.golden_constraints(text)


