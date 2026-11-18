"""Public status payload — the paper's vocabulary at the user-facing
boundary: verdict / grade / findings (violation | unfinished |
regression).  Internal `State` values stay in the store."""

from __future__ import annotations

from .models import Severity, State, public_state

_FINDING_KIND = {"invariant": "violation", "exit": "unfinished"}


def finding_for(project, clause_name: str) -> str:
    """The paper's finding kind for a failing clause name."""
    if clause_name.startswith("edge:"):
        return "regression"
    if clause_name == "build":
        return "unfinished"
    clause = next((c for c in project.contract.clauses
                   if c.name == clause_name), None)
    kind = clause.kind if clause and clause.kind else None
    if kind is None:
        cls = project.checkers.get(
            clause_name.split(":", 1)[0] if clause is None
            else clause.check)
        kind = getattr(cls, "kind", "exit") if cls else "exit"
    return _FINDING_KIND.get(kind, "unfinished")


def public_status(project, sha: str) -> dict | None:
    """Public-vocabulary status for a node (None when unknown)."""
    node = project.store.get_node(sha)
    if node is None:
        return None
    from .scheduler import node_coverage
    coverage = node_coverage(project, node.sha)
    verdict = public_state(node.state)
    accepted = node.state in (State.VERIFIED, State.WARNED)
    grade = None
    if accepted:
        grade = "signoff" if coverage == "full" else "stage"
    fails = [v for v in project.store.verdicts(node.sha)
             if v.status == "fail"]
    warnings = sorted({v.clause.name for v in fails
                       if v.clause.severity == Severity.WARN})
    # findings are the failures that decide the verdict; failing
    # warn clauses are reported under `warnings` only
    findings = [{"clause": v.clause.name,
                 "finding": finding_for(project, v.clause.name),
                 "severity": v.clause.severity.value,
                 "detail": v.detail} for v in fails
                if v.clause.severity != Severity.WARN]
    return {
        "sha": node.sha,
        "state": node.state.value,
        "verdict": verdict,
        "grade": grade,
        "stage": (node.metrics or {}).get("stage"),
        "coverage": coverage,
        "warnings": warnings,
        "findings": findings,
    }


def verdict_text(payload: dict) -> str:
    """One-line public verdict: ``accepted (signoff)`` /
    ``accepted (stage: place)`` / ``violation`` / ``unfinished`` …"""
    v = payload.get("verdict") or payload.get("state") or "unknown"
    if v == "accepted":
        g = payload.get("grade")
        if g == "signoff":
            return "accepted (signoff)"
        if g == "stage":
            stage = payload.get("stage")
            return (f"accepted (stage: {stage})"
                    if stage else "accepted (stage)")
    return v
