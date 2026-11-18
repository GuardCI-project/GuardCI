"""§12.2 checkpoint capture at push time (ingest) — the recorder runs
when the push lands, not when the scheduler reaches the node.  A
failure must never fail the push: callers catch and log
``checkpoint_error``."""

from __future__ import annotations

from .provenance import evaluate as _eval_provenance


def capture_at_ingest(project, sha: str) -> bool:
    """Capture + persist + evaluate the checkpoint for one newly
    ingested sha.  Returns True when a checkpoint was stored.  Caller
    wraps this in try/except — never raise."""
    verification = project.contract.verification or {}
    if verification.get("source") != "artifact":
        return False
    rec = getattr(project, "recorder", None)
    if rec is None:
        return False
    cp = rec.capture(project, sha)
    if cp is None:
        return False
    from ..recorders import base as rec_base
    cdir = rec_base.persist(project, cp)
    cp_dict = rec_base.load_checkpoint(cdir / "checkpoint.json")
    stage_order = {s: i for i, s in
                   enumerate(project.flow.stages())}
    prov, offenders = _eval_provenance(
        project.paths["remote"], sha, cp_dict, stage_order)
    from .scheduler import _attach_metrics
    _attach_metrics(project, sha, {
        "stage": cp.stage,
        "provenance": prov,
        "provenance_offenders": offenders,
        "_checkpoint": str(cdir / "checkpoint.json")})
    project.store.log_event("checkpoint", sha, {
        "stage": cp.stage,
        "artifacts": sorted(cp.artifacts)})
    return True


def load_stored_checkpoint(project, sha: str) -> dict | None:
    """The checkpoint dict captured at ingest, if any."""
    node = project.store.get_node(sha)
    path = (node.metrics or {}).get("_checkpoint") if node else None
    if not path:
        return None
    from ..recorders import base as rec_base
    try:
        return rec_base.load_checkpoint(path)
    except OSError:
        return None
