"""§12.5 delivery: rebuild an artifact-judged candidate and decide
``reproducible`` — ``mode: digest`` requires the rebuilt layout digest
to equal the checkpoint artifact's; ``mode: contract`` (default)
requires the rebuilt layout's own metrics to pass every hard clause."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from .models import Cost, Severity, State
from .scheduler import (
    _attach_metrics,
    _baseline_metrics,
    _baseline_sha_for,
    _job_view,
    _measurements_for,
    _set_state,
)
from .status import public_status
from .verify import JobSpec, run_job


def _reproducible_clause(project):
    return next((c for c in project.contract.clauses
                 if c.check == "reproducible"), None)


def deliverable(project, candidates, keep_workdir: bool = False):
    """Newest signoff-reliable candidate whose artifact reproduces;
    sets a failing candidate back to ``open`` with a ``reproducible``
    gap and tries the next one.  Returns ``(node, {"exact": bool,
    "result": JobResult})`` — ``(node, None)`` when reproducibility
    stays undecided (no executor, or ``--no-rebuild`` handled by the
    caller)."""
    clause = _reproducible_clause(project)
    mode = ((clause.params or {}).get("mode")
            if clause else None) or "contract"
    for node in candidates:
        metrics = node.metrics or {}
        exact, res = _rebuild_and_check(project, node, mode,
                                        keep_workdir=keep_workdir)
        if exact is None:
            return node, None              # no executor → pending
        if exact:
            _attach_metrics(project, node.sha,
                            {"reproducible_exact": True})
            return project.store.get_node(node.sha), {"exact": True, "result": res}
        _attach_metrics(project, node.sha, {
            "reproducible_exact": False,
            "_gaps": {**(metrics.get("_gaps") or {}),
                      "reproducible": {"mode": mode}}})
        _set_state(project, node.sha, State.OPEN)
        project.store.log_event("open", node.sha,
                                {"gaps": {"reproducible":
                                          {"mode": mode}}})
    return None, None


def _rebuild_and_check(project, node, mode,
                       keep_workdir: bool = False):
    """Rebuild the candidate's recipe on the existing build path and
    decide reproducibility; ``(None, res)`` when no executor is
    configured. ``keep_workdir`` retains the rebuilt workdir so the
    caller can ship its products (``deliver --out``)."""
    if project.executor is None:
        return None, None
    view = _job_view(project)
    baseline_sha = _baseline_sha_for(project, node.sha)
    nonstatic = [c for c in project.contract.clauses
                 if project.checkers.get(c.check) is not None
                 and getattr(project.checkers[c.check], "cost", None)
                 in (Cost.LIGHT, Cost.HEAVY)]
    needs, upto = _measurements_for(project, nonstatic, node.sha)
    spec = JobSpec(sha=node.sha, baseline_sha=baseline_sha,
                   upto_stage=upto, measurements=sorted(needs),
                   params=needs, attempt=node.attempt,
                   no_predict=True, keep_workdir=keep_workdir)
    res = run_job(view, spec)
    if not res.ok:
        return False, res
    if mode == "digest":
        art_sha = _artifact_sha(project, node)
        rebuilt = res.metrics.get("layout_sha256")
        return (art_sha is not None and rebuilt == art_sha), res
    # mode: contract — the rebuilt layout's own metrics must pass every
    # hard non-static clause (statics judge the recipe, which is
    # unchanged; ``reproducible`` itself is decided here)
    ctx = {
        "project": project, "node": node,
        "metrics": res.metrics,
        # the golden baseline is usually already populated — its
        # metrics live in the store, not on the job result
        "baseline_metrics": res.baseline_metrics
            or _baseline_metrics(project, baseline_sha),
        "repo": project.paths["remote"],
        "contract": project.contract,
    }
    for c in project.contract.clauses:
        if c.severity != Severity.HARD or c.check == "reproducible":
            continue
        cls = project.checkers.get(c.check)
        if cls is None or getattr(cls, "cost", None) == Cost.STATIC:
            continue
        try:
            r = cls().run(ctx, c.params,
                          project.backend_for(getattr(cls, "needs", "")
                                              or ""))
            if r.status in ("fail", "error"):
                return False, res
        except Exception:  # noqa: BLE001
            return False, res
    return True, res


def _artifact_sha(project, node) -> str | None:
    """sha256 of the candidate's deepest checkpoint artifact."""
    metrics = node.metrics or {}
    cp_path = metrics.get("_checkpoint")
    if not cp_path:
        return None
    try:
        cp = json.loads(Path(cp_path).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    art = (cp.get("artifacts") or {}).get(metrics.get("stage")
                                          or project.flow.stages()[-1])
    if not art or not Path(art).exists():
        return None
    return hashlib.sha256(Path(art).read_bytes()).hexdigest()


def _contract_sha256(project) -> str:
    """sha256 of the resolved (merged) contract."""
    c = project.contract
    doc = {
        "name": c.name,
        "protected": list(c.protected),
        "recipe": list(c.recipe),
        "clauses": [{"check": cl.check, "severity": cl.severity.value,
                     "kind": cl.kind, "id": cl.id,
                     "params": cl.params}
                    for cl in c.clauses],
        "edges": [{"metric": e.metric, "severity": e.severity.value,
                   "params": e.params} for e in c.edges],
        "routing": c.routing,
        "verification": c.verification,
        "protection": c.protection,
        "tradable": list(c.tradable),
        "predict": c.predict,
    }
    return hashlib.sha256(
        json.dumps(doc, sort_keys=True, default=str).encode()
    ).hexdigest()


def write_bundle(project, node, res, out_dir) -> dict:
    """§12.5 delivery bundle under ``out_dir``: the CI's REBUILT
    last-stage products (never the author's artifact), ``verdict.json``
    in the public vocabulary, and ``MANIFEST.json`` with sha256 of
    every shipped file.  Returns the manifest."""
    workdir = getattr(res, "workdir", None)
    products = project.flow.delivery_products(Path(workdir)) if workdir else {}
    if not products:
        raise ValueError("no rebuilt delivery products available; refusing metadata-only export")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, src in products.items():
        dst = out / name
        shutil.copyfile(src, dst)
        files[name] = hashlib.sha256(dst.read_bytes()).hexdigest()
    payload = public_status(project, node.sha) or {}
    verdict = dict(payload)
    verdict["node"] = node.sha
    verdict["reproducible_exact"] = (
        (node.metrics or {}).get("reproducible_exact"))
    vname = "verdict.json"
    (out / vname).write_text(json.dumps(verdict, indent=1,
                                       sort_keys=True))
    files[vname] = hashlib.sha256(
        (out / vname).read_bytes()).hexdigest()
    manifest = {
        "node": node.sha,
        "baseline": _baseline_sha_for(project, node.sha),
        "contract": {"name": project.contract.name,
                     "sha256": _contract_sha256(project)},
        "files": files,
    }
    (out / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=1, sort_keys=True))
    return manifest
