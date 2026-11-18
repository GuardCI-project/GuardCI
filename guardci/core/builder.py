"""Stage-wise recipe builder (DESIGN.md §9.3, §11.4.2).

Stage keys are content-chained:
``key(s) = H(header, output_digest(s−1), inputs(s), unattributable)``.
The builder walks the chain: a stage whose key resolves through
recorded ``output_digest``s is looked up; on a miss the deepest
pending hit is restored, the stage runs, its snapshot's output digest
is computed and published (entry meta + ``cache/index/``). A stage
whose key is new but whose output digest matches a cached one makes
every downstream key resolve — the remaining stages come out of the
cache and the build stops running (early cut-off, recorded in
``BuildResult.stage_cutoff``).

Tool-free: everything goes through the FlowAdapter / Executor / Cache
interfaces. Flows without ``resolve_keys`` fall back to the static
``stage_keys`` list (no chaining). Per-key locks are held from a
stage's lookup until its publish.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..executors.base import InfraError, infra_rc


@dataclass
class StageRecord:
    stage: str
    key: str
    reused: bool
    seconds: float = 0.0
    rc: int | None = None
    cutoff: bool = False         # satisfied through the digest chain
                                 # after the last stage that ran


@dataclass
class BuildResult:
    ok: bool = False
    failed_stage: str | None = None
    timed_out: bool = False
    stages: list[StageRecord] = field(default_factory=list)
    build_seconds: float = 0.0
    stages_built: int = 0
    stages_reused: int = 0
    workdir: Path | None = None
    logs: dict = field(default_factory=dict)     # stage -> log path
    stage_cutoff: list = field(default_factory=list)

    def stage_log(self, stage: str):
        return self.logs.get(stage)


def _reused_record(name, key, entry, cutoff=False) -> StageRecord:
    return StageRecord(stage=name, key=key, reused=True,
                       seconds=entry.seconds if entry else 0.0,
                       rc=entry.rc if entry else None, cutoff=cutoff)


class _StaticKeyer:
    """Shim for flows without ``resolve_keys``: fixed per-stage keys,
    no digest chaining."""

    def __init__(self, pairs):
        self._keys = dict(pairs)

    def key(self, stage_index, prev_digest):
        return list(self._keys.values())[stage_index]


def _resolve(flow, repo, sha, baseline_sha, image_id, cache,
             protected):
    """``(pairs, keyer)`` — chained resolution when the flow supports
    it, static keys otherwise."""
    if callable(getattr(flow, "resolve_keys", None)):
        return flow.resolve_keys(repo, sha, baseline_sha, image_id,
                                 cache=cache, protected=protected)
    pairs = flow.stage_keys(repo, sha, baseline_sha, image_id)
    return pairs, _StaticKeyer(pairs)


def _snapshot_digest(flow, snap_root: Path) -> str:
    """Output digest of a published snapshot (§11.4.2): the flow
    decides which files later stages read; default is every file."""
    fn = getattr(flow, "snapshot_digest", None)
    if callable(fn):
        return fn(snap_root)
    entries = []
    for f in sorted(Path(snap_root).rglob("*")):
        if f.is_file():
            entries.append([str(f.relative_to(snap_root)),
                            hashlib.sha256(f.read_bytes()).hexdigest()])
    h = hashlib.sha256()
    import json as _json
    for e in entries:
        h.update(_json.dumps(e, separators=(",", ":")).encode())
        h.update(b"\n")
    return h.hexdigest()


def _entry_digest(flow, cache, key, entry) -> str | None:
    """output_digest of a cached entry — from meta/index, or computed
    (and indexed) from the snapshot when missing."""
    d = (entry.meta or {}).get("output_digest")
    if d:
        return d
    meta = cache.index_get(key)
    if meta and meta.get("output_digest"):
        return meta["output_digest"]
    snap = entry.snapshot
    if snap.is_dir():
        d = _snapshot_digest(flow, snap)
        cache.index_put(key, {"output_digest": d})
        return d
    return None


def build(flow, executor, slot, repo, sha, baseline_sha, protected_globs,
          upto_stage, cache, workroot, stage_timeouts=None,
          meta_extra=None) -> BuildResult:
    """Build ``sha`` up to ``upto_stage`` following §9.3 + §11.4.2."""
    stage_timeouts = stage_timeouts or {}
    pairs, keyer = _resolve(flow, repo, sha, baseline_sha,
                            executor.image_id(), cache,
                            protected_globs)
    names = [s for s, _ in pairs]
    if upto_stage not in names:
        raise ValueError(f"unknown stage {upto_stage!r} (have {names})")
    upto = names.index(upto_stage)
    resolved = dict(pairs)

    workdir = Path(workroot) / f"{sha[:12]}-{upto_stage}"
    if workdir.exists():
        import shutil
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    flow.materialize(repo, sha, baseline_sha, protected_globs, workdir)
    logs_dir = workdir / ".guardci-logs"
    logs_dir.mkdir()

    result = BuildResult(ok=True, workdir=workdir)
    t0 = time.monotonic()

    pending = None                    # entry not yet restored
    prev_entry_dir = None
    prev_digest = None
    last_built = -1

    for i in range(upto + 1):
        stage = names[i]
        key = resolved.get(stage) or keyer.key(i, prev_digest)
        resolved[stage] = key
        with cache.lock(key):
            entry = cache.lookup(key)
            if entry is not None:
                if entry.status == "failed":
                    result.stages.append(
                        _reused_record(stage, key, entry))
                    result.stages_reused += 1
                    result.ok = False
                    result.failed_stage = stage
                    result.build_seconds = round(
                        time.monotonic() - t0, 3)
                    return result
                result.stages.append(_reused_record(stage, key, entry))
                result.stages_reused += 1
                pending = entry
                prev_entry_dir = entry.dir
                prev_digest = _entry_digest(flow, cache, key, entry)
                continue
            if pending is not None:
                flow.restore(pending.snapshot, workdir)
                pending = None
            log = logs_dir / f"{stage}.log"
            result.logs[stage] = log
            try:
                rr = flow.run_stage(workdir, stage, executor, slot,
                                    stage_timeouts.get(stage), log)
            except Exception as exc:
                raise InfraError(f"stage {stage}: {exc}") from exc
            if infra_rc(rr.rc):
                raise InfraError(f"stage {stage}: executor rc={rr.rc}")
            rec = StageRecord(stage=stage, key=key, reused=False,
                              seconds=rr.seconds, rc=rr.rc)
            result.stages.append(rec)
            result.stages_built += 1
            last_built = i
            meta = {"stage": stage, "seconds": rr.seconds, "rc": rr.rc,
                    "sha": sha, **(meta_extra or {})}
            if rr.rc != 0 or rr.timed_out:
                cache.publish(key, lambda tmp: None,
                              {**meta, "status": "failed"})
                cache.index_put(key, {"stage": stage, "status": "failed",
                                      "seconds": rr.seconds})
                result.ok = False
                result.failed_stage = stage
                result.timed_out = rr.timed_out
                break
            digest_holder = [None]
            prev_dir_holder = [prev_entry_dir]
            pmeta = {**meta, "status": "ok"}

            def _populate(tmp, stage=stage, prev=prev_dir_holder,
                          holder=digest_holder, pmeta=pmeta):
                flow.snapshot(workdir, stage, tmp / "snapshot",
                              prev=prev[0])
                holder[0] = _snapshot_digest(flow, tmp / "snapshot")
                pmeta["output_digest"] = holder[0]

            cache.publish(key, _populate, pmeta)
            digest = digest_holder[0]
            cache.index_put(key, {"stage": stage, "status": "ok",
                                  "seconds": rr.seconds,
                                  "output_digest": digest})
            prev_entry_dir = cache.entry_dir(key)
            prev_digest = digest

    # stages hit after the last one that ran are the digest-chain
    # cut-off (§11.4.2)
    if last_built >= 0 and result.ok:
        for rec in result.stages[last_built + 1:]:
            if rec.reused:
                rec.cutoff = True
                result.stage_cutoff.append(rec.stage)

    # every stage was a cache hit: the workdir still needs the build
    # outputs for artifact export
    if result.ok and pending is not None:
        flow.restore(pending.snapshot, workdir)
    result.build_seconds = round(time.monotonic() - t0, 3)
    return result
