"""Checkpoint-evidence checkers (§12.3)."""

from __future__ import annotations

from ..core.models import Cost
from .base import Checker, CheckResult, register


@register
class LibraryClosure(Checker):
    """Invariant: the artifact is closed under the baseline libraries against golden LEFs/liberty —
    foreign masters reject (no taint).  ``unreadable`` is handled by
    the job/fallback path, not here."""

    name = "library_closure"
    kind = "invariant"
    cost = Cost.LIGHT
    native_stage = "synth"

    def run(self, ctx, params, backend) -> CheckResult:
        facts = ctx.get("metrics") or {}
        if facts.get("library_closed") is False:
            fm = facts.get("foreign_masters") or []
            return CheckResult(
                status="fail",
                detail="foreign masters: " + ", ".join(fm[:8]),
                metrics={"gap": {"library_closure":
                                 {"foreign_masters": fm}}})
        return CheckResult(status="pass")


@register
class Provenance(Checker):
    """Exit check: ``consistent``/``rebuilt`` pass, ``stale`` fails
    (severity usually ``warn``), ``unknown`` passes with a detail."""

    name = "provenance"
    kind = "exit"
    cost = Cost.LIGHT
    # the node's own provenance is decided at every stage — `synth`
    # keeps it out of the pending path (its facts are top-level
    # metrics, not stage-scoped)
    native_stage = "synth"

    def run(self, ctx, params, backend) -> CheckResult:
        m = ctx.get("metrics") or {}
        prov = m.get("provenance", "rebuilt")
        if prov == "stale":
            offenders = m.get("provenance_offenders") or []
            return CheckResult(
                status="fail",
                detail="stale: " + ", ".join(offenders),
                metrics={"gap": {"provenance":
                                 {"offenders": offenders}}})
        if prov == "unknown":
            return CheckResult(status="pass",
                               detail="provenance unknown (no recorder)")
        return CheckResult(status="pass")


@register
class Reproducible(Checker):
    """Exit check decided by ``guardci deliver`` (§12.5): pending until
    the artifact candidate is rebuilt; native stage ``delivery``."""

    name = "reproducible"
    kind = "exit"
    cost = Cost.LIGHT
    native_stage = "delivery"

    def run(self, ctx, params, backend) -> CheckResult:
        m = ctx.get("metrics") or {}
        if "stages" not in m:
            # source: rebuild — the CI built the layout itself;
            # reproducibility holds by construction (§12.4)
            return CheckResult(status="pass",
                               detail="rebuilt by the CI")
        exact = m.get("reproducible_exact")
        if exact is None:
            return CheckResult(status="pending",
                               detail="decided at delivery")
        if exact:
            return CheckResult(status="pass")
        return CheckResult(status="fail",
                           detail="rebuild does not reproduce the "
                                  "artifact",
                           metrics={"gap": {"reproducible": {}}})
