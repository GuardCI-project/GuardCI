"""Job runner (DESIGN.md §9.1/§9.4/§9.5): build + accept inside one
executor slot. A job is pure w.r.t. the ledger — it may touch only the
cache, the golden dir and ``<project>/artifacts/<sha>/``.

``run_job(view, spec)`` returns a JobResult the scheduler's ``finalize``
turns into check runs, verdicts and a state. Tool-free: everything goes
through the FlowAdapter / Executor / Cache / Backend interfaces.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..artifacts.model import ArtifactKind, ArtifactSet
from ..backends.base import MeasureRequest
from ..executors.base import InfraError
from . import builder as builder_mod

STAGE_TIMEOUT_FACTOR = 10
STAGE_TIMEOUT_FLOOR = 1800          # 30 min


@dataclass
class JobView:
    """Everything a job needs — deliberately no Store (§9.1)."""

    repo: str                        # remote (bare) repo path
    artifacts: Path                  # <project>/artifacts
    flow: object = None
    executor: object = None
    cache: object = None
    protected: list = field(default_factory=list)
    backends: dict = field(default_factory=dict)   # measurement -> Backend
    workroot: Path | None = None     # default <artifacts>/work
    artifacts_keep: str = "all"      # "all" | "reports" (§9.5)
    reset: dict | None = None        # design.reset {port,active,cycles}
    clock_port: str | None = None    # defaults to main_clock's source
    design_name: str | None = None   # top module name for cosim
    predict: dict | None = None      # §11.4 contract `predict:` (off
                                     # when absent)


@dataclass
class JobSpec:
    sha: str
    baseline_sha: str
    upto_stage: str
    measurements: list
    params: dict = field(default_factory=dict)
    attempt: int = 0
    no_predict: bool = False         # request-full completes a
                                     # predicted-open node
    source: str = "rebuild"          # §12.1: artifact | rebuild
    checkpoint_path: str | None = None   # captured checkpoint.json
    keep_workdir: bool = False       # §12.5 delivery bundle keeps the
                                     # rebuilt layout products


@dataclass
class JobResult:
    ok: bool = False
    workdir: Path | None = None      # set when keep_workdir was asked
    error: str | None = None
    failed_stage: str | None = None
    timed_out: bool = False
    stages: list = field(default_factory=list)
    logs: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    measurements: dict = field(default_factory=dict)  # name -> info dict
    build_seconds: float = 0.0
    accept_seconds: float = 0.0
    stages_built: int = 0
    stages_reused: int = 0
    saved_seconds: float = 0.0
    stage_cutoff: list = field(default_factory=list)
    predicted: dict | None = None    # §11.4 predicted open payload
    checkpoint_missing: str | None = None   # §12: unreadable/missing
    baseline_metrics: dict | None = None        # freshly populated
    baseline: dict | None = None


def _git(repo, *args) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args], check=True, capture_output=True,
        text=True).stdout


def _hash(*parts) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(json.dumps(p, sort_keys=True,
                            default=str).encode())
        h.update(b"\n")
    return h.hexdigest()


def memo_key(stage_key: str, measurement: str, tool: str, version: str,
             params: dict, constraints: dict | None = None) -> str:
    """``<stage_key>/<measurement>-<hash>`` — memos live one directory
    per stage key under ``results/`` so a stage-key scan is one
    directory listing (§9.10)."""
    parts = [measurement, tool, version, params]
    if constraints is not None:
        parts.append(constraints)
    return f"{stage_key}/{measurement}-{_hash(*parts)[:24]}"


def _memo_path(view: JobView, key: str) -> Path:
    return view.cache.results_dir() / f"{key}.json"


def memo_get(view: JobView, key: str) -> dict | None:
    p = _memo_path(view, key)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (ValueError, OSError):
        return None


def _memo_put(view: JobView, key: str, memo: dict):
    p = _memo_path(view, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(memo, indent=2, sort_keys=True))
    tmp.rename(p)


_PRUNE_KEEP_EXT = {".json", ".log", ".yml", ".sdc", ".lyrdb", ".txt"}
_PRUNE_KEEP_MAX = 1_000_000          # 1 MB


def prune_artifacts(root: Path, keep: Path | None = None):
    """`artifacts_keep: reports` — under ``artifacts/<sha>`` keep
    ``logs/`` and, per measurement directory, only report files
    (≤ 1 MB, report extensions); drop layouts, streams, netlists,
    SPEF and the ``.export-*`` scratch dirs. The layout hash already
    lives in the metrics (``layout_sha256``), so nothing needed for
    evaluation is lost. ``keep`` (a retained job workdir for the
    delivery bundle, §12.5) is exempt."""
    root = Path(root)
    keep = Path(keep).resolve() if keep is not None else None
    if not root.is_dir():
        return
    for mdir in sorted(root.iterdir()):
        if mdir.name == "logs" or not mdir.is_dir():
            continue
        for sub in list(mdir.iterdir()):
            if sub.is_dir() and sub.name.startswith(".export-"):
                shutil.rmtree(sub, ignore_errors=True)
        for f in mdir.rglob("*"):
            if not f.is_file():
                continue
            if keep is not None and \
                    f.resolve().is_relative_to(keep):
                continue
            if f.suffix in _PRUNE_KEEP_EXT and \
                    f.stat().st_size <= _PRUNE_KEEP_MAX:
                continue
            f.unlink(missing_ok=True)
        for d in sorted((d for d in mdir.rglob("*") if d.is_dir()),
                        key=lambda p: len(p.parts), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass


def memo_by_stage(view: JobView, stage_key: str,
                  measurements: set[str]) -> dict[str, dict]:
    """All memos stored under ``stage_key`` for ``measurements``,
    regardless of params/tool (§9.1 free metric attach)."""
    out: dict[str, dict] = {}
    d = view.cache.results_dir() / stage_key
    if not d.is_dir():
        return out
    for memo_path in sorted(d.glob("*.json"),
                            key=lambda p: p.stat().st_mtime,
                            reverse=True):
        try:
            memo = json.loads(memo_path.read_text())
        except (ValueError, OSError):
            continue
        m = memo.get("measurement")
        if m in measurements and m not in out:
            out[m] = memo
    return out


def golden_constraints_hash(constraints: dict | None) -> str | None:
    if not constraints:
        return None
    return hashlib.sha256(json.dumps(
        constraints, sort_keys=True, default=str).encode()).hexdigest()


def measurement_memo_key(view: JobView, stage_key: str, measurement: str,
                         params: dict,
                         constraints: dict | None = None) -> str | None:
    backend = view.backends.get(measurement)
    if backend is None:
        return None
    # §11.4.3: only the params the backend declares as
    # measurement-affecting enter the key — thresholds (min_wns,
    # min_ratio, accept, …) never invalidate a measurement
    declared = getattr(backend, "memo_params", None)
    if declared is not None:
        params = {k: v for k, v in (params or {}).items()
                  if k in declared}
    return memo_key(stage_key, measurement, backend.tool,
                    backend.version, params,
                    constraints if measurement == "signoff" else None)


def _export_artifacts(view: JobView, workdir: Path, measurement: str,
                      upto_stage: str, dest_dir: Path,
                      golden: dict, slot) -> ArtifactSet:
    """The CI's own artifacts a measurement reads (§9.5)."""
    arts = ArtifactSet()
    if measurement == "lec":
        odb = "6_final.odb" if upto_stage == \
            view.flow.stages()[-1] else "1_synth.odb"
        arts.put(ArtifactKind.NETLIST,
                 view.flow.export(workdir, ArtifactKind.NETLIST,
                                  dest_dir / "netlist.v",
                                  view.executor, slot, odb=odb,
                                  golden=golden.get("librefs")))
        return arts
    arts.put(ArtifactKind.LAYOUT,
             view.flow.export(workdir, ArtifactKind.LAYOUT,
                              dest_dir / "layout.def",
                              view.executor, slot))
    if measurement == "drc":
        arts.put(ArtifactKind.STREAM,
                 view.flow.export_stream(
                     arts.get(ArtifactKind.LAYOUT),
                     golden.get("librefs") or golden,
                     dest_dir / "stream.gds", view.executor, slot))
    return arts


_LAYOUT_MEASUREMENTS = ("signoff", "drc")


def _layout_sha(view: JobView, stage_key: str | None) -> str | None:
    """layout_sha256 recorded in the index for ``stage_key`` (written
    at first layout export — §11.4.3)."""
    if not stage_key:
        return None
    meta = view.cache.index_get(stage_key)
    return (meta or {}).get("layout_sha256")


def _memo_namespace(view: JobView, stage_key: str | None,
                    measurement: str) -> str | None:
    """Memo slot: ``layout/<sha256>`` for measurements that read the
    layout (§11.4.3) — independent of the finish stage key."""
    if measurement in _LAYOUT_MEASUREMENTS:
        sha = _layout_sha(view, stage_key)
        return f"layout/{sha}" if sha else None
    return stage_key


def memo_lookup(view: JobView, stage_key: str, measurement: str,
                params: dict,
                constraints: dict | None = None) -> dict | None:
    """Exact memo hit, else a memo written under the pre-§11.4.3 key
    (which hashed the full clause params): accepted when its stored
    params agree on the measurement-affecting ones."""
    backend = view.backends.get(measurement)
    if backend is None:
        return None
    if measurement == "signoff":
        # same key the measurement itself uses — injected at run time
        # in measure()/run_artifact_job (§11.3/W7)
        try:
            params = _signoff_params(params, constraints)
        except InfraError:
            return None
    ns = _memo_namespace(view, stage_key, measurement)
    mkey = (measurement_memo_key(view, ns, measurement, params,
                                 constraints) if ns else None)
    if mkey:
        hit = memo_get(view, mkey)
        if hit is not None:
            return hit
    declared = getattr(backend, "memo_params", None)
    if declared is None:
        return None
    want = {k: (params or {}).get(k) for k in declared}
    d = view.cache.results_dir() / stage_key
    if not d.is_dir():
        return None
    for p in sorted(d.glob(f"{measurement}-*.json")):
        try:
            memo = json.loads(p.read_text())
        except (ValueError, OSError):
            continue
        if memo.get("measurement") != measurement:
            continue
        if memo.get("backend") != backend.tool or                 memo.get("backend_version") != backend.version:
            continue
        stored = memo.get("params") or {}
        if all(stored.get(k) == v for k, v in want.items()):
            return memo
    return None


def _signoff_params(params, constraints) -> dict:
    """§11.3: every signoff measurement runs at the golden constraint
    period — the contract bound is a judgment input, never a
    measurement input. Errors when the golden period is unavailable."""
    gp = (constraints or {}).get("golden_period")
    if not isinstance(gp, (int, float)):
        raise InfraError("signoff: golden period unavailable")
    return {**(params or {}), "period": float(gp)}


def measure(view: JobView, workdir: Path, upto_stage: str,
            stage_key: str, measurement: str, params: dict,
            artifacts_root: Path, golden: dict, slot,
            use_memo: bool = True) -> dict:
    """One measurement with result memoization (§9.5/§9.10)."""
    backend = view.backends.get(measurement)
    if backend is None:
        raise InfraError(f"no backend bound for {measurement!r}")
    constraints = golden.get("constraints")
    if measurement == "signoff":
        params = _signoff_params(params, constraints)
    ns = _memo_namespace(view, stage_key, measurement)
    mkey = (measurement_memo_key(view, ns, measurement, params,
                                 constraints) if ns else None)
    if use_memo:
        hit = memo_lookup(view, stage_key, measurement, params,
                          constraints)
        if hit is not None:
            return {**hit, "memo_hit": True, "key": mkey}
    outdir = Path(artifacts_root) / measurement
    outdir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    arts = _export_artifacts(view, workdir, measurement, upto_stage,
                             outdir, golden, slot)
    if measurement in _LAYOUT_MEASUREMENTS:
        # first export: record the layout digest in the index so
        # later inline decisions need no export (§11.4.3)
        layout = arts.get(ArtifactKind.LAYOUT)
        if layout is not None and stage_key:
            lsha = _file_sha(Path(layout))
            view.cache.index_put(
                stage_key, {"layout_sha256": lsha,
                            "stage": upto_stage})
            if ns is None:
                ns = f"layout/{lsha}"
                mkey = measurement_memo_key(
                    view, ns, measurement, params, constraints)
                if use_memo:
                    hit = memo_get(view, mkey)
                    if hit is not None:
                        return {**hit, "memo_hit": True, "key": mkey}
    req = MeasureRequest(artifacts=arts, golden=golden, params=params,
                         outdir=outdir, executor=view.executor,
                         slot=slot,
                         timeout=int(params.get("timeout", 3600)))
    metrics = backend.run(req)
    seconds = round(time.monotonic() - t0, 3)
    evidence = sorted(str(p) for p in outdir.rglob("*") if p.is_file())
    info = {"metrics": metrics, "seconds": seconds, "memo_hit": False,
            "backend": backend.tool, "backend_version": backend.version,
            "evidence": evidence, "stage_key": stage_key,
            "measurement": measurement, "params": params or {},
            "key": mkey}
    if mkey:
        _memo_put(view, mkey, info)
    return {**info, "key": mkey}


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _compare_key(backend, da: str, db: str, params: dict,
                 tiers) -> str:
    """Digest-keyed equivalence memo (§11.5): the pair's netlist
    digests + backend versions + measurement-affecting params — the
    stage key is irrelevant to a netlist compare."""
    declared = getattr(backend, "memo_params", None)
    eff = dict(params or {}) if declared is None else         {k: v for k, v in (params or {}).items() if k in declared}
    return "equiv/" + _hash(backend.tool, backend.version,
                            da, db, eff, tiers)[:24]


def _compare_memoized(view: JobView, backend, a: Path, b: Path,
                      params: dict, outdir: Path, golden: dict, slot,
                      tiers=None) -> dict:
    """``backend.compare(a, b)`` memoized by the netlist digests."""
    da, db = _file_sha(Path(a)), _file_sha(Path(b))
    key = _compare_key(backend, da, db, params, tiers)
    hit = memo_get(view, key)
    if hit is not None:
        return {**hit, "memo_hit": True, "key": key}
    golden2 = dict(golden)
    golden2["gold_netlist"] = str(a)
    arts = ArtifactSet()
    arts.put(ArtifactKind.NETLIST, Path(b))
    outdir.mkdir(parents=True, exist_ok=True)
    req = MeasureRequest(artifacts=arts, golden=golden2, params=params,
                         outdir=outdir, executor=view.executor,
                         slot=slot,
                         timeout=int(params.get("timeout", 3600)))
    metrics = backend.compare(Path(a), Path(b), params, req,
                              tiers=tiers)
    evidence = sorted(str(p) for p in outdir.rglob("*") if p.is_file())
    info = {"metrics": metrics, "seconds": metrics.get(
                "equivalence_seconds", 0.0),
            "memo_hit": False, "backend": backend.tool,
            "backend_version": backend.version, "evidence": evidence,
            "pair": {"a": da, "b": db}, "measurement": "lec",
            "params": params or {}, "key": key}
    _memo_put(view, key, info)
    return {**info, "key": key}


def _export_netlist(view: JobView, workdir: Path, upto_stage: str,
                    dest: Path, golden: dict, slot) -> Path:
    odb = "6_final.odb" if upto_stage == \
        view.flow.stages()[-1] else "1_synth.odb"
    return view.flow.export(workdir, ArtifactKind.NETLIST, dest,
                            view.executor, slot, odb=odb,
                            golden=golden.get("librefs"))


def _is_composite(backend) -> bool:
    """The tiered equivalence backend exposes ``compare(a, b, …)``;
    plain measurement backends (kepler_formal etc.) do not."""
    return backend is not None and callable(
        getattr(backend, "compare", None))


def _index_netlist(view: JobView, stage_key: str | None, stage: str,
                   netlist: Path):
    if stage_key and netlist is not None:
        view.cache.index_put(
            stage_key, {"netlist_sha256": _file_sha(Path(netlist)),
                        "stage": stage})


def _lec_chain(view: JobView, workdir: Path, upto_stage: str,
               params: dict, artifacts_root: Path, golden: dict,
               slot, stage_key: str | None = None) -> dict:
    """§11.5 step 1: export the synthesis netlist and
    ``compare(gold, synth)`` with every tier. Returns the ``lec``
    measurement info dict (metrics carry ``equivalence_*``)."""
    backend = view.backends.get("lec")
    lec_dir = Path(artifacts_root) / "lec"
    synth = _export_netlist(view, workdir, upto_stage,
                            lec_dir / "synth_netlist.v", golden, slot)
    _index_netlist(view, stage_key, upto_stage, synth)
    info = _compare_memoized(view, backend, Path(golden["gold_netlist"]),
                             synth, params, lec_dir / "gold_vs_synth",
                             golden, slot)
    info["metrics"]["equivalence_chain"] = "synth"
    return info


def _lec_chain_final(view: JobView, workdir: Path, upto_stage: str,
                     params: dict, artifacts_root: Path, golden: dict,
                     slot, synth_info: dict,
                     stage_key: str | None = None) -> dict:
    """§11.5 step 2: tier-1 ``(synth, final)`` — `identical` inherits
    the synth status (``synth+one_to_one``); otherwise a full
    ``compare(gold, final)`` (``final``)."""
    backend = view.backends.get("lec")
    lec_dir = Path(artifacts_root) / "lec"
    final = _export_netlist(view, workdir, upto_stage,
                            lec_dir / "final_netlist.v", golden, slot)
    _index_netlist(view, stage_key, upto_stage, final)
    synth = lec_dir / "synth_netlist.v"
    t1 = _compare_memoized(
        view, backend, synth, final, params,
        lec_dir / "synth_vs_final", golden, slot,
        tiers=("one_to_one",))
    if (t1["metrics"].get("lec_result") == "identical"):
        info = dict(synth_info)
        info["metrics"] = dict(synth_info["metrics"])
        info["metrics"]["equivalence_chain"] = "synth+one_to_one"
        info["evidence"] = sorted(
            set((info.get("evidence") or [])
                + (t1.get("evidence") or [])))
        info["seconds"] = round(
            (info.get("seconds") or 0) + (t1.get("seconds") or 0), 3)
        return info
    info = _compare_memoized(view, backend,
                             Path(golden["gold_netlist"]), final,
                             params, lec_dir / "gold_vs_final",
                             golden, slot)
    info["metrics"]["equivalence_chain"] = "final"
    # continuity: gold-vs-synth tier 1 numbers stay in lec_*
    for k in ("lec_result", "lec_differences"):
        if k in synth_info["metrics"]:
            info["metrics"][k] = synth_info["metrics"][k]
    return info


def _tree_sha(repo: str, sha: str) -> str:
    return _git(repo, "rev-parse", f"{sha}^{{tree}}").strip()


def ensure_baseline(view: JobView, baseline_sha: str, slot) -> dict:
    """Populate ``<cache>/golden/<baseline tree sha>/`` once (§9.4):
    baseline tree export + LibRefs + golden constraints + the baseline's
    own full build and three measurements + ``gold_netlist.v`` +
    ``baseline.json`` (reference metrics, per-stage seconds)."""
    tree = _tree_sha(view.repo, baseline_sha)
    gdir = view.cache.golden_dir(tree)
    populated = False
    with view.cache.golden_lock(tree):
        bjson = gdir / "baseline.json"
        if not bjson.exists():
            gdir.mkdir(parents=True, exist_ok=True)
            librefs = view.flow.golden(view.repo, baseline_sha,
                                       view.protected, gdir,
                                       view.executor, slot)
            constraints = view.flow.golden_constraints(view.repo,
                                                       baseline_sha)
            (gdir / "constraints.json").write_text(
                json.dumps(constraints, indent=2, sort_keys=True))
            workroot = gdir / "_work"
            try:
                populated = _populate_baseline(
                    view, baseline_sha, gdir, workroot, librefs,
                    constraints, bjson, slot)
            finally:
                shutil.rmtree(workroot, ignore_errors=True)
        baseline = json.loads(bjson.read_text())
        librefs = json.loads((gdir / "librefs.json").read_text())
        cpath = gdir / "constraints.json"
        if cpath.exists():
            constraints = json.loads(cpath.read_text())
        else:
            constraints = view.flow.golden_constraints(view.repo,
                                                       baseline_sha)
        be = view.backends.get("signoff")
        if be is not None and \
                (baseline.get("signoff_backend") != be.version
                 or (baseline.get("metrics") or {})
                 .get("min_period") is None):
            # the baseline predates this signoff backend (e.g. a cache
            # shared across versions): re-measure signoff only —
            # §11.3 needs the baseline's own `min_period`
            workroot = gdir / "_work"
            try:
                _refresh_baseline_signoff(
                    view, baseline_sha, gdir, workroot, librefs,
                    constraints, bjson, baseline, slot)
                baseline = json.loads(bjson.read_text())
            finally:
                shutil.rmtree(workroot, ignore_errors=True)
    return {"dir": gdir, "librefs": librefs, "constraints": constraints,
            "gold_netlist": str(gdir / "gold_netlist.v"),
            "baseline": baseline, "populated": populated}


def _populate_baseline(view, baseline_sha, gdir, workroot, librefs,
                       constraints, bjson, slot) -> bool:
    last = view.flow.stages()[-1]
    build = builder_mod.build(
        view.flow, view.executor, slot, view.repo,
        baseline_sha, baseline_sha, view.protected,
        last, view.cache, workroot, {})
    if not build.ok:
        raise InfraError(
            f"baseline build failed at stage "
            f"{build.failed_stage} (logs {build.logs})")
    golden = {"librefs": librefs, "constraints": constraints,
              "gold_netlist": None,
              "reset": view.reset,
              "clock_port": view.clock_port
              or ((constraints or {}).get("main_clock") or {})
              .get("sources", [None])[0],
              "top": view.design_name}
    gold = view.flow.export(
        build.workdir, ArtifactKind.NETLIST,
        gdir / "gold_netlist.v", view.executor, slot,
        odb="1_synth.odb", golden=librefs)
    golden["gold_netlist"] = str(gold)
    keys = {r.stage: r.key for r in build.stages}
    metrics: dict = {}
    for m in ("signoff", "drc", "lec"):
        # the baseline's own self-LEC gets its own generous budget,
        # independent of any clause `timeout` param
        params = {"timeout": 3600} if m == "lec" else {}
        if m == "signoff":
            # §11.3: the baseline is measured at the golden period and
            # its `min_period` lands in baseline.json
            gp = (constraints or {}).get("golden_period")
            if isinstance(gp, (int, float)):
                params["period"] = gp
        if m == "lec" and _is_composite(view.backends.get("lec")):
            # composite: gold ≡ final netlist must reach `proven`
            final = view.flow.export(
                build.workdir, ArtifactKind.NETLIST,
                gdir / "accept" / "lec" / "netlist.v",
                view.executor, slot, odb="6_final.odb",
                golden=librefs)
            info = _compare_memoized(
                view, view.backends["lec"], gold, final, params,
                gdir / "accept" / "lec" / "compare", golden, slot)
        else:
            info = measure(view, build.workdir, last, keys[last],
                           m, params, gdir / "accept", golden, slot)
        metrics.update(info["metrics"])
    if metrics.get("lec_result") != "identical" and \
            metrics.get("equivalence_status") != "proven":
        raise InfraError(
            "baseline self-LEC is not identical "
            f"({metrics.get('lec_result')})")
    stages_facts = {}
    if view.backends.get("facts") is not None:
        # §12.5: the baseline's own stage facts — ratchet reference and
        # bound ratios evaluate against the same stage
        period = (constraints or {}).get("golden_period")
        for stage, art in view.flow.checkpoint_artifacts(
                build.workdir).items():
            finfo = _artifact_facts(
                view, Path(art), stage, {},
                gdir / "accept" / "facts" / stage, golden, slot,
                period)
            stages_facts[stage] = dict(finfo["metrics"])
    baseline = {
        "metrics": {**metrics,
                    **({"stages": stages_facts} if stages_facts
                       else {})},
        "stage_seconds": {r.stage: r.seconds
                          for r in build.stages},
        "flow_seconds": build.build_seconds,
        "signoff_backend":
            getattr(view.backends.get("signoff"), "version", None),
        "image_id": view.executor.image_id(),
        "tree": _tree_sha(view.repo, baseline_sha),
    }
    bjson.write_text(json.dumps(baseline, indent=2, sort_keys=True))
    return True


def _refresh_baseline_signoff(view, baseline_sha, gdir, workroot,
                              librefs, constraints, bjson,
                              baseline, slot) -> None:
    """Re-run only the baseline's signoff measurement (cached stage
    entries make the rebuild cheap) and merge its metrics into
    ``baseline.json`` — used when the recorded baseline predates the
    current signoff backend version."""
    last = view.flow.stages()[-1]
    build = builder_mod.build(
        view.flow, view.executor, slot, view.repo,
        baseline_sha, baseline_sha, view.protected,
        last, view.cache, workroot, {})
    if not build.ok:
        raise InfraError(
            f"baseline build failed at stage "
            f"{build.failed_stage} (logs {build.logs})")
    golden = {"librefs": librefs, "constraints": constraints,
              "gold_netlist": str(gdir / "gold_netlist.v"),
              "reset": view.reset,
              "clock_port": view.clock_port
              or ((constraints or {}).get("main_clock") or {})
              .get("sources", [None])[0],
              "top": view.design_name}
    keys = {r.stage: r.key for r in build.stages}
    params: dict = {}
    gp = (constraints or {}).get("golden_period")
    if isinstance(gp, (int, float)):
        params["period"] = gp
    info = measure(view, build.workdir, last, keys[last],
                   "signoff", params, gdir / "accept", golden, slot)
    (baseline.setdefault("metrics", {})).update(info["metrics"])
    baseline["signoff_backend"] = getattr(
        view.backends.get("signoff"), "version", None)
    bjson.write_text(json.dumps(baseline, indent=2, sort_keys=True))


def _run_estimate(view: JobView, workdir, stage: str, period,
                  artifacts_root: Path, golden_req: dict, slot) -> dict:
    """§11.4 predict: run the `estimate` measurement on the given
    stage's ODB. Returns est_* metrics."""
    be = view.backends.get("estimate")
    if be is None:
        raise InfraError("no backend bound for 'estimate'")
    res_dir = view.flow._results_dir(workdir)         if callable(getattr(view.flow, "_results_dir", None))         else None
    odb = None
    if res_dir is not None:
        cand = sorted(Path(res_dir).glob(f"*_{stage}.odb"))
        if cand:
            odb = cand[0]
    if odb is None:
        raise InfraError(f"estimate: no {stage} ODB found")
    outdir = Path(artifacts_root) / "estimate"
    outdir.mkdir(parents=True, exist_ok=True)
    req = MeasureRequest(
        golden=golden_req, params={"period": period},
        artifacts={ArtifactKind.LAYOUT: str(odb)},
        outdir=str(outdir), executor=view.executor, slot=slot,
        timeout=1800)
    return be.run(req)


def _predict_check(view: JobView, build, stage: str, period,
                   skip_below: float, artifacts_root: Path,
                   golden_req: dict, slot):
    """Return the predicted payload when est_setup_wns/period is
    below skip_below, else None (continue the job)."""
    if stage not in {r.stage: r for r in build.stages}:
        return None
    est = _run_estimate(view, build.workdir, stage, period,
                        artifacts_root, golden_req, slot)
    wns = est.get("est_setup_wns")
    if wns is None or not period or not isinstance(
            period, (int, float)):
        return {"est": est, "skip": False}
    if float(wns) / float(period) < skip_below:
        return {"est": est, "skip": True, "period": period}
    return {"est": est, "skip": False}


def run_job(view: JobView, spec: JobSpec) -> JobResult:
    """ensure_baseline → build → measurements → cleanup (§9.1/§9.5)."""
    res = JobResult()
    slot = view.executor.acquire()
    try:
        golden = ensure_baseline(view, spec.baseline_sha, slot)
        baseline = golden["baseline"]
        res.baseline = baseline
        if golden["populated"]:
            res.baseline_metrics = baseline["metrics"]

        stage_secs = baseline.get("stage_seconds") or {}
        timeouts = {s: max(STAGE_TIMEOUT_FLOOR,
                           STAGE_TIMEOUT_FACTOR * secs)
                    for s, secs in stage_secs.items()}

        artifacts_root = Path(view.artifacts) / spec.sha
        workroot = view.workroot or (artifacts_root / "work")
        # §11.5 fail fast: with the composite equivalence backend a
        # full-depth job builds the light stage first and compares
        # gold≡synth before any physical stage runs
        eq_be = view.backends.get("lec")
        _last = view.flow.stages()[-1]
        _light = getattr(view.flow, "light_stage", None)
        two_phase = (_is_composite(eq_be)
                     and "lec" in spec.measurements
                     and spec.upto_stage == _last
                     and _light is not None and _light != _last)
        upto1 = _light if two_phase else spec.upto_stage
        try:
            build = builder_mod.build(
                view.flow, view.executor, slot, view.repo, spec.sha,
                spec.baseline_sha, view.protected, upto1,
                view.cache, workroot, timeouts)
            res.stages = [{"stage": r.stage, "key": r.key,
                           "reused": r.reused, "seconds": r.seconds,
                           "rc": r.rc, "cutoff": r.cutoff}
                          for r in build.stages]
            res.stage_cutoff = list(build.stage_cutoff)
            # preserve stage logs before the workdir is deleted; the
            # `build` verdict's detail/evidence point at these copies
            logdir = artifacts_root / "logs"
            for stage, lpath in build.logs.items():
                logdir.mkdir(parents=True, exist_ok=True)
                dst = logdir / f"{stage}.log"
                try:
                    shutil.copyfile(lpath, dst)
                except OSError:
                    dst = Path(lpath)
                res.logs[stage] = str(dst)
            res.build_seconds = build.build_seconds
            res.stages_built = build.stages_built
            res.stages_reused = build.stages_reused
            res.saved_seconds = round(sum(
                stage_secs.get(r.stage, 0.0)
                for r in build.stages if r.reused), 3)
            if not build.ok:
                res.failed_stage = build.failed_stage
                res.timed_out = build.timed_out
                return res

            keys = {r["stage"]: r["key"] for r in res.stages}
            accept_t0 = time.monotonic()
            pred = (None if spec.no_predict else view.predict) or {}
            pstage = pred.get("stage")
            if (pstage and pstage in keys
                    and pstage != spec.upto_stage):
                period = (spec.params.get("signoff") or {}
                          ).get("period")
                if period is None:
                    period = ((golden["constraints"] or {})
                              .get("golden_period"))
                try:
                    p = _predict_check(
                        view, build, pstage, period,
                        float(pred.get("skip_below", 0) or 0),
                        artifacts_root,
                        {"librefs": golden["librefs"],
                         "constraints": golden["constraints"]}, slot)
                except InfraError:
                    p = None          # estimate unavailable → run on
                if p is not None:
                    res.metrics.update(
                        {k: v for k, v in
                         (p.get("est") or {}).items()
                         if isinstance(v, (int, float))})
                    res.metrics["_predicted"] = True
                    if p.get("skip"):
                        res.predicted = {
                            "stage": pstage,
                            "est_setup_wns": (p.get("est") or {})
                            .get("est_setup_wns"),
                            "period": p.get("period"),
                            "gap": {"value": (p.get("est") or {})
                                    .get("est_setup_wns"),
                                    "bound": 0,
                                    "distance": abs((p.get("est") or {})
                                    .get("est_setup_wns") or 0),
                                    "estimated": True}}
                        res.accept_seconds = round(
                            time.monotonic() - accept_t0, 3)
                        res.ok = True
                        return res
            golden_req = {"librefs": golden["librefs"],
                          "constraints": golden["constraints"],
                          "gold_netlist": golden["gold_netlist"],
                          "reset": view.reset,
                          "clock_port": view.clock_port
                          or ((golden["constraints"] or {})
                              .get("main_clock") or {})
                          .get("sources", [None])[0],
                          "top": view.design_name}
            lec_refuted = False
            for m in spec.measurements:
                if m == "lec" and _is_composite(eq_be):
                    info = _lec_chain(view, build.workdir, upto1,
                                      spec.params.get(m) or {},
                                      artifacts_root, golden_req, slot,
                                      stage_key=keys.get(upto1))
                    res.measurements[m] = info
                    res.metrics.update(info["metrics"])
                    lec_refuted = info["metrics"].get(
                        "equivalence_status") == "refuted"
                    continue
                if two_phase:
                    continue
                info = measure(view, build.workdir, spec.upto_stage,
                               keys.get(spec.upto_stage), m,
                               spec.params.get(m) or {},
                               artifacts_root, golden_req, slot)
                res.measurements[m] = info
                res.metrics.update(info["metrics"])
            if lec_refuted:
                # §11.5 fail fast: refuted at the synthesis netlist —
                # no physical stages, no other measurement
                res.accept_seconds = round(
                    time.monotonic() - accept_t0, 3)
                res.ok = True
                return res
            if two_phase:
                # continue the build to the last stage, then the
                # synth≡final one-to-one check / gold-vs-final fallback
                build2 = builder_mod.build(
                    view.flow, view.executor, slot, view.repo,
                    spec.sha, spec.baseline_sha, view.protected,
                    spec.upto_stage, view.cache, workroot, timeouts)
                # job-level stage records: the light stages ran in
                # phase 1, the rest are build2's. The digest-chain
                # cut-off spans both phases — recompute it on the
                # merged record (a stage that ran in phase 1 whose
                # output digest made later keys hit the cache).
                merged = [{"stage": r.stage, "key": r.key,
                           "reused": r.reused, "seconds": r.seconds,
                           "rc": r.rc, "cutoff": False}
                          for r in build.stages]
                merged += [{"stage": r.stage, "key": r.key,
                            "reused": r.reused, "seconds": r.seconds,
                            "rc": r.rc, "cutoff": False}
                           for r in build2.stages[len(build.stages):]]
                last_built = max(
                    (i for i, s in enumerate(merged) if not s["reused"]),
                    default=-1)
                stage_cutoff = []
                if last_built >= 0:
                    for s in merged[last_built + 1:]:
                        if s["reused"]:
                            s["cutoff"] = True
                            stage_cutoff.append(s["stage"])
                res.stages = merged
                res.stage_cutoff = stage_cutoff
                for stage, lpath in build2.logs.items():
                    logdir.mkdir(parents=True, exist_ok=True)
                    dst = logdir / f"{stage}.log"
                    try:
                        shutil.copyfile(lpath, dst)
                    except OSError:
                        dst = Path(lpath)
                    res.logs[stage] = str(dst)
                res.build_seconds += build2.build_seconds
                res.stages_built += build2.stages_built
                res.stages_reused += build2.stages_reused
                res.saved_seconds = round(res.saved_seconds + sum(
                    stage_secs.get(r.stage, 0.0)
                    for r in build2.stages if r.reused), 3)
                if not build2.ok:
                    res.failed_stage = build2.failed_stage
                    res.timed_out = build2.timed_out
                    res.accept_seconds = round(
                        time.monotonic() - accept_t0, 3)
                    return res
                build = build2
                keys = {r["stage"]: r["key"] for r in res.stages}
                info = _lec_chain_final(
                    view, build.workdir, spec.upto_stage,
                    spec.params.get("lec") or {}, artifacts_root,
                    golden_req, slot, res.measurements["lec"],
                    stage_key=keys.get(spec.upto_stage))
                res.measurements["lec"] = info
                res.metrics.update(info["metrics"])
                # remaining non-lec measurements ran above? They were
                # skipped in the first loop for two_phase; run them now
                for m in spec.measurements:
                    if m == "lec" or m in res.measurements:
                        continue
                    info = measure(view, build.workdir, spec.upto_stage,
                                   keys.get(spec.upto_stage), m,
                                   spec.params.get(m) or {},
                                   artifacts_root, golden_req, slot)
                    res.measurements[m] = info
                    res.metrics.update(info["metrics"])
            res.accept_seconds = round(time.monotonic() - accept_t0, 3)
            res.ok = True
            return res
        finally:
            wd = locals().get("build") and build.workdir or workroot
            if spec.keep_workdir:
                res.workdir = wd
            else:
                shutil.rmtree(wd, ignore_errors=True)
                if workroot == artifacts_root / "work":
                    shutil.rmtree(workroot, ignore_errors=True)
            if view.artifacts_keep == "reports":
                prune_artifacts(
                    artifacts_root,
                    keep=wd if spec.keep_workdir else None)
                for info in res.measurements.values():
                    info["evidence"] = [
                        e for e in info.get("evidence") or []
                        if Path(e).exists()]
                    if info.get("key") and not info.get("memo_hit"):
                        _memo_put(view, info["key"], info)
    finally:
        slot.release()



# ============================================================ §12 artifact mode

def _artifact_facts(view: JobView, art_path: Path, stage: str,
                    params: dict, outdir: Path, golden: dict, slot,
                    required_period) -> dict:
    """`facts` measurement on a captured artifact — memo keyed on the
    artifact's sha256 (+ golden tree, backend version, period)."""
    backend = view.backends.get("facts")
    if backend is None:
        raise InfraError("no backend bound for 'facts'")
    asha = _file_sha(art_path)
    ns = f"artifact/{asha}"
    fparams = {**(params or {}), "stage": stage}
    if required_period is not None and stage in ("place", "cts"):
        # the required period only affects the estimate facts.tcl
        # emits for place/cts — for other stages it must not enter
        # the memo key (§11.4.3: only measurement-affecting params)
        fparams.setdefault("period", required_period)
    mkey = measurement_memo_key(view, ns, "facts", fparams,
                                golden.get("constraints"))
    hit = memo_get(view, mkey)
    if hit is not None:
        return {**hit, "memo_hit": True, "key": mkey}
    outdir.mkdir(parents=True, exist_ok=True)
    arts = ArtifactSet()
    arts.put(ArtifactKind.LAYOUT, art_path)
    t0 = time.monotonic()
    req = MeasureRequest(artifacts=arts, golden=golden,
                         params=fparams, outdir=outdir,
                         executor=view.executor, slot=slot,
                         timeout=int((params or {}).get("timeout",
                                                       3600)))
    metrics = backend.run(req)
    seconds = round(time.monotonic() - t0, 3)
    evidence = sorted(str(p) for p in outdir.rglob("*") if p.is_file())
    info = {"metrics": metrics, "seconds": seconds, "memo_hit": False,
            "backend": backend.tool,
            "backend_version": backend.version,
            "evidence": evidence, "measurement": "facts",
            "params": fparams, "key": mkey}
    _memo_put(view, mkey, info)
    return info


def run_artifact_job(view: JobView, spec: JobSpec) -> JobResult:
    """§12.4: judge a captured checkpoint — facts + equivalence chain +
    (last stage) signoff/drc on the exported DEF.  No build."""
    res = JobResult()
    slot = view.executor.acquire()
    try:
        golden = ensure_baseline(view, spec.baseline_sha, slot)
        res.baseline = golden["baseline"]
        if golden["populated"]:
            res.baseline_metrics = golden["baseline"]["metrics"]
        artifacts_root = Path(view.artifacts) / spec.sha
        t0 = time.monotonic()
        cp = {}
        if spec.checkpoint_path:
            try:
                cp = json.loads(Path(spec.checkpoint_path).read_text())
            except (OSError, json.JSONDecodeError):
                cp = {}
        if not cp:
            res.checkpoint_missing = "no checkpoint record"
            return res
        arts = {s: Path(p) for s, p in
                (cp.get("artifacts") or {}).items()}
        stage_order = view.flow.stages()
        last = stage_order[-1]
        upto = spec.upto_stage
        order = {s: i for i, s in enumerate(stage_order)}
        have = sorted([s for s in arts if order.get(s, -1) >= 0
                       and arts[s].exists() and
                       order[s] <= order.get(upto, order[last])],
                      key=lambda s: order[s])
        if not have:
            res.checkpoint_missing = "no usable stage artifacts"
            return res
        period = ((spec.params.get("signoff") or {}).get("period"))
        if period is None:
            period = ((golden["constraints"] or {})
                      .get("golden_period"))
        golden_req = {"librefs": golden["librefs"],
                      "constraints": golden["constraints"],
                      "gold_netlist": golden["gold_netlist"],
                      "reset": view.reset,
                      "clock_port": view.clock_port
                      or ((golden["constraints"] or {})
                          .get("main_clock") or {})
                      .get("sources", [None])[0],
                      "top": view.design_name}
        eq_be = view.backends.get("lec")
        stages_facts: dict = {}
        status_by_stage: dict = {}   # stage -> s_i (§12 equivalence)
        detail_by_stage: dict = {}   # stage -> producing compare info
        prev_net: Path | None = None
        library_closed_ok = True
        lec_params = spec.params.get("lec") or {}
        lec_secs = 0.0
        chain_metrics: dict = {}
        lec_dir = artifacts_root / "lec"
        lec_dir.mkdir(parents=True, exist_ok=True)
        # the required period for the artifact's own estimate facts
        # is the design's constraint period — the baseline's stage
        # facts are measured under golden_period too, so this keeps
        # node and baseline estimates (and memos) like-for-like
        facts_period = ((golden["constraints"] or {})
                        .get("golden_period")) or period
        for i, stage in enumerate(have):
            info = _artifact_facts(
                view, arts[stage], stage,
                spec.params.get("facts") or {},
                artifacts_root / "facts" / stage, golden_req, slot,
                facts_period)
            res.measurements[f"facts:{stage}"] = info
            facts = dict(info["metrics"])
            stages_facts[stage] = facts
            if facts.get("unreadable"):
                res.checkpoint_missing = (
                    f"artifact unreadable at {stage}: "
                    + str(facts.get("unreadable_error", "")))
                res.metrics["stages"] = stages_facts
                res.accept_seconds = round(
                    time.monotonic() - t0, 3)
                return res
            if facts.get("library_closed") is False:
                library_closed_ok = False
                break
            net_p = facts.get("netlist_path")
            if "lec" in spec.measurements and eq_be is not None \
                    and net_p:
                net = Path(net_p)
                # §11.5 chain: s_0 = tiers(gold, n_0); s_i = s_{i-1}
                # when the netlist is unchanged or the one-to-one
                # link is identical, else tiers(gold, n_i)
                refuted = None
                if i == 0:
                    cinfo = _compare_memoized(
                        view, eq_be, Path(golden["gold_netlist"]),
                        net, lec_params, lec_dir / "gold_vs_first",
                        golden_req, slot)
                    lec_secs += cinfo.get("seconds") or 0
                    status_by_stage[stage] = cinfo["metrics"].get(
                        "equivalence_status")
                    detail_by_stage[stage] = cinfo
                    refuted = cinfo
                elif prev_net is not None:
                    pa = _file_sha(prev_net)
                    pb = _file_sha(net)
                    inherited = pa == pb
                    if not inherited:
                        t1 = _compare_memoized(
                            view, eq_be, prev_net, net, lec_params,
                            lec_dir / f"{have[i-1]}_vs_{stage}",
                            golden_req, slot,
                            tiers=("one_to_one",))
                        lec_secs += t1.get("seconds") or 0
                        if t1["metrics"].get("lec_result") \
                                == "identical":
                            inherited = True
                        else:
                            fb = _compare_memoized(
                                view, eq_be,
                                Path(golden["gold_netlist"]), net,
                                lec_params,
                                lec_dir / f"gold_vs_{stage}",
                                golden_req, slot)
                            lec_secs += fb.get("seconds") or 0
                            status_by_stage[stage] =                                 fb["metrics"].get(
                                    "equivalence_status")
                            detail_by_stage[stage] = fb
                            refuted = fb
                    if inherited:
                        prev_stage = next(
                            s for s in reversed(have[:i])
                            if s in status_by_stage)
                        status_by_stage[stage] =                             status_by_stage[prev_stage]
                        detail_by_stage[stage] =                             detail_by_stage[prev_stage]
                if refuted is not None and refuted["metrics"].get(
                        "equivalence_status") == "refuted":
                    chain_metrics = dict(refuted["metrics"])
                    chain_metrics["equivalence_by_stage"] = dict(
                        status_by_stage)
                    res.measurements["lec"] = refuted
                    res.metrics["stages"] = stages_facts
                    res.metrics.update(chain_metrics)
                    res.accept_seconds = round(
                        time.monotonic() - t0, 3)
                    # the JOB succeeded — `refuted` is a verdict
                    # for the checkers, not a job failure
                    res.ok = True
                    return res
            prev_net = Path(net_p) if net_p else prev_net
        if status_by_stage:
            # node status = weakest s_i (refuted < unknown < bounded
            # < proven_sequential < proven); detail from the first
            # stage that produced it
            rank = {"refuted": 0, "bounded": 2,
                    "proven_sequential": 3, "proven": 4}
            wr = min(rank.get(s, 1)
                     for s in status_by_stage.values())
            first_weak = next(
                s for s in have
                if s in status_by_stage
                and rank.get(status_by_stage[s], 1) == wr)
            chain_metrics = dict(
                (detail_by_stage.get(first_weak) or {})
                .get("metrics") or {})
            chain_metrics["equivalence_by_stage"] = dict(
                status_by_stage)
            res.measurements["lec"] = detail_by_stage[first_weak]
        res.metrics["stages"] = stages_facts
        res.metrics.update(chain_metrics)
        res.metrics["stage"] = have[-1]
        # top-level metrics = deepest stage facts (existing checkers
        # read them unchanged)
        res.metrics.update(
            {k: v for k, v in stages_facts.get(have[-1],
                                               {}).items()})
        if "lec" in res.measurements:
            res.measurements["lec"]["metrics"][
                "equivalence_chain"] = "artifact"
            res.measurements["lec"]["seconds"] = lec_secs
        # last-stage artifact → signoff + drc on the exported DEF
        if have[-1] == last and library_closed_ok:
            def_p = stages_facts[last].get("def_path")
            if def_p:
                arts_dir = artifacts_root
                for m in spec.measurements:
                    if m in ("facts", "lec") or m in res.measurements:
                        continue
                    be = view.backends.get(m)
                    if be is None:
                        continue
                    outdir = arts_dir / m
                    outdir.mkdir(parents=True, exist_ok=True)
                    aset = ArtifactSet()
                    aset.put(ArtifactKind.LAYOUT, Path(def_p))
                    if m == "drc":
                        aset.put(
                            ArtifactKind.STREAM,
                            view.flow.export_stream(
                                Path(def_p),
                                golden.get("librefs") or golden,
                                outdir / "stream.gds",
                                view.executor, slot))
                    mparams = spec.params.get(m) or {}
                    if m == "signoff":
                        mparams = _signoff_params(
                            mparams, golden.get("constraints"))
                    mkey = measurement_memo_key(
                        view, f"artifact/{_file_sha(Path(def_p))}", m,
                        mparams, golden.get("constraints"))
                    hit = memo_get(view, mkey) if mkey else None
                    if hit is not None:
                        info = {**hit, "memo_hit": True}
                    else:
                        req = MeasureRequest(
                            artifacts=aset, golden=golden_req,
                            params=mparams,
                            outdir=outdir, executor=view.executor,
                            slot=slot,
                            timeout=int(mparams.get("timeout", 3600)))
                        m0 = time.monotonic()
                        mets = be.run(req)
                        info = {
                            "metrics": mets,
                            "seconds": round(
                                time.monotonic() - m0, 3),
                            "memo_hit": False,
                            "backend": be.tool,
                            "backend_version": be.version,
                            "evidence": sorted(
                                str(p) for p in outdir.rglob("*")
                                if p.is_file()),
                            "measurement": m,
                            "params": mparams,
                            "key": mkey}
                        if mkey:
                            _memo_put(view, mkey, info)
                    res.measurements[m] = info
                    res.metrics.update(info["metrics"])
        res.accept_seconds = round(time.monotonic() - t0, 3)
        res.ok = True
        return res
    finally:
        slot.release()
