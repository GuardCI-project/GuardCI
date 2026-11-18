"""Bound resolution (DESIGN.md §11.3): the owner states requirements in
the contract; bound forms are shared across exit clauses.

Forms: a number; ``golden`` (for periods: the golden SDC's period; for
other metrics: the golden/baseline metric value); ``{ratio: r, of:
baseline}`` = ``r`` × the baseline's measured value of that metric.
"""

from __future__ import annotations

_PERIOD_METRICS = {"period", "max_period", "min_period",
                   "clock_period"}


def resolve_bound(bound, metric: str, baseline: dict,
                  golden: dict | None = None):
    """``(value, error)`` — value is a float or None. ``error`` is a
    short detail string when the bound cannot be resolved."""
    if bound is None:
        return None, None
    if isinstance(bound, bool):
        return None, f"invalid bound {bound!r}"
    if isinstance(bound, (int, float)):
        return float(bound), None
    if isinstance(bound, str):
        if bound == "golden":
            if metric in _PERIOD_METRICS:
                v = (golden or {}).get("golden_period")
                if isinstance(v, (int, float)):
                    return float(v), None
                return None, "golden period unavailable"
            v = baseline.get(metric)
            if isinstance(v, (int, float)):
                return float(v), None
            return None, f"baseline metric missing: {metric}"
        if bound == "baseline":
            v = baseline.get(metric)
            if isinstance(v, (int, float)):
                return float(v), None
            return None, f"baseline metric missing: {metric}"
        try:
            return float(bound), None
        except ValueError:
            return None, f"invalid bound {bound!r}"
    if isinstance(bound, dict) and bound.get("of") == "baseline":
        ratio = bound.get("ratio")
        v = baseline.get(metric)
        if not isinstance(ratio, (int, float)):
            return None, f"invalid ratio bound {bound!r}"
        if not isinstance(v, (int, float)):
            return None, f"baseline metric missing: {metric}"
        return float(ratio) * float(v), None
    return None, f"invalid bound {bound!r}"
