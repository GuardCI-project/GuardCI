"""Measurement checkers (DESIGN.md §9.6): thin verdicts over canonical
metrics produced by a build+accept job. Tool-free — they read
``ctx["metrics"]`` (the node's merged canonical metrics) and
``ctx["baseline_metrics"]`` (the baseline root's), and take thresholds
only from clause params. A required metric that is missing or None is
``error`` (fail-closed) unless noted otherwise.
"""

from __future__ import annotations

import math
from typing import ClassVar

from ..core.bounds import resolve_bound
from ..core.models import Cost
from .base import Checker, CheckResult, register


class _MetricChecker(Checker):
    needs: str = ""                  # measurement name
    cost = Cost.HEAVY

    @classmethod
    def needs_for(cls, params):
        """Measurement(s) a clause with ``params`` requires —
        overridable when it depends on the params (metric_bound)."""
        return cls.needs or None

    def metrics(self, ctx) -> dict:
        return ctx.get("metrics") or {}

    def baseline(self, ctx) -> dict:
        return ctx.get("baseline_metrics") or {}

    def _missing(self, ctx, *names) -> list:
        m = self.metrics(ctx)
        return [n for n in names if m.get(n) is None]

    def _missing_baseline(self, ctx, *names) -> list:
        m = self.baseline(ctx)
        return [n for n in names if m.get(n) is None]

    def _require(self, ctx, *names) -> CheckResult | None:
        miss = self._missing(ctx, *names)
        if miss:
            return CheckResult(
                status="error",
                detail=f"required metric(s) missing: {', '.join(miss)}")
        return None


@register
class Lec(_MetricChecker):
    """Structural equivalence: result must be ``identical``."""
    name = "lec"
    needs = "lec"
    cost = Cost.LIGHT
    requires: ClassVar[set] = {"lec"}

    def run(self, ctx, params, backend) -> CheckResult:
        result = self.metrics(ctx).get("lec_result")
        if result is None:
            return CheckResult(status="error",
                               detail="required metric missing: lec_result")
        if result == "identical":
            return CheckResult(status="pass", detail="lec: identical")
        if result == "different":
            n = self.metrics(ctx).get("lec_differences")
            return CheckResult(
                status="fail",
                detail="lec: circuits differ"
                       + (f" ({n} differences)" if n is not None else ""))
        if result == "timeout" and \
                (params or {}).get("on_timeout") == "fail":
            # §9.6: an unproven equivalence is a verdict, not limbo —
            # the clause opts in via `on_timeout: fail`
            t = self.metrics(ctx).get("lec_timeout_s")
            return CheckResult(
                status="fail",
                detail=f"lec: timeout after {t}s "
                       f"(equivalence not proven)"
                if t is not None else
                "lec: timeout (equivalence not proven)")
        return CheckResult(
            status="error", detail=f"lec measurement did not complete: "
                                   f"{result}")


def _equivalence_status(m: dict) -> str | None:
    """§11.5 status: a stored ``equivalence_status`` wins; otherwise
    derived from the one-to-one LEC result (V1)."""
    st = m.get("equivalence_status")
    if st:
        return st
    result = m.get("lec_result")
    if result == "identical":
        return "proven"
    if result in ("different", "timeout"):
        return "unknown"
    return None            # error / missing


def _lec_detail(m: dict) -> str:
    n = m.get("lec_differences")
    result = m.get("lec_result")
    if result == "different":
        return (f"one-to-one LEC: {n} differences"
                if n is not None else "one-to-one LEC: differs")
    if result == "timeout":
        return "one-to-one LEC: timeout"
    return f"one-to-one LEC: {result}"


@register
class Equivalence(_MetricChecker):
    """Invariant (§11.5): fails only on a refuted equivalence."""
    name = "equivalence"
    native_stage = "synth"
    needs = "lec"
    cost = Cost.LIGHT
    kind: ClassVar[str] = "invariant"
    requires: ClassVar[set] = {"lec"}

    def run(self, ctx, params, backend) -> CheckResult:
        m = self.metrics(ctx)
        if m.get("lec_result") in (None, "error") and                 m.get("equivalence_status") is None:
            return CheckResult(
                status="error",
                detail="required metric missing: lec_result")
        st = _equivalence_status(m)
        if st == "refuted":
            return CheckResult(
                status="fail",
                detail=f"equivalence refuted ({_lec_detail(m)})")
        return CheckResult(status="pass",
                           detail=f"equivalence status: {st}")


class _EquivalenceAccept(_MetricChecker):
    """Exit clause: the equivalence status must be in ``accept``."""
    native_stage = "synth"
    needs = "lec"
    cost = Cost.LIGHT
    default_accept: ClassVar[tuple] = ()

    def run(self, ctx, params, backend) -> CheckResult:
        m = self.metrics(ctx)
        if m.get("lec_result") in (None, "error") and                 m.get("equivalence_status") is None:
            return CheckResult(
                status="error",
                detail="required metric missing: lec_result")
        st = _equivalence_status(m)
        accept = list((params or {}).get("accept")
                      or self.default_accept)
        if st in accept:
            return CheckResult(status="pass",
                               detail=f"equivalence status: {st}")
        gap = {"value": st, "bound": accept, "distance": 1}
        return CheckResult(
            status="fail",
            detail=f"equivalence {st} ({_lec_detail(m)})",
            metrics={"gap": gap})


@register
class EquivalenceProof(_EquivalenceAccept):
    name = "equivalence_proof"
    default_accept: ClassVar[tuple] = ("proven", "proven_sequential",
                                       "bounded")


@register
class EquivalenceStrict(_EquivalenceAccept):
    name = "equivalence_strict"
    default_accept: ClassVar[tuple] = ("proven", "proven_sequential")


def _max_period_bound(ctx, params):
    """``(bound, error)`` for a period bound — the clause's own
    ``max_period`` when present, else the contract's ``sta_setup``
    clause's."""
    bound_raw = (params or {}).get("max_period")
    if bound_raw is None:
        contract = ctx.get("contract")
        clause = next((c for c in (contract.clauses if contract else [])
                       if c.check == "sta_setup"), None)
        if clause is not None:
            bound_raw = clause.params.get("max_period")
    return resolve_bound(bound_raw, "min_period",
                         (ctx.get("baseline_metrics") or {}),
                         ctx.get("golden"))


@register
class StaSetup(_MetricChecker):
    """Setup signoff (§11.3): the measured ``min_period`` must meet the
    owner's ``max_period`` bound."""
    name = "sta_setup"
    native_stage = "finish"
    needs = "signoff"
    requires: ClassVar[set] = {"signoff"}

    def run(self, ctx, params, backend) -> CheckResult:
        m = self.metrics(ctx)
        if "cell_area" not in m and "timing_error" not in m:
            return CheckResult(status="error",
                               detail="required metric missing: signoff")
        if m.get("timing_error"):
            return CheckResult(status="fail",
                               detail=str(m["timing_error"]))
        if m.get("clock_period") is None:
            return CheckResult(
                status="fail", detail="main clock not defined",
                metrics={"gap": {"value": "undefined",
                                 "distance": 1,
                                 "metric": "min_period"}})
        bound, err = _max_period_bound(ctx, params)
        if err:
            return CheckResult(status="error", detail=err)
        if bound is None:
            return CheckResult(status="error",
                               detail="sta_setup: max_period required")
        mp = m.get("min_period")
        if mp is None:
            return CheckResult(
                status="fail",
                detail=m.get("min_period_error") or
                "no closing period found",
                metrics={"gap": {"value": "unbounded",
                                 "bound": bound, "distance": 1,
                                 "metric": "min_period"}})
        if mp > bound:
            return CheckResult(
                status="fail",
                detail=f"min_period {mp:.4g} > max_period {bound:.4g}",
                metrics={"gap": {"value": mp, "bound": bound,
                                 "distance": mp - bound,
                                 "metric": "min_period"}})
        return CheckResult(
            status="pass",
            detail=f"min_period {mp:.4g} <= max_period {bound:.4g}")


@register
class StaSetupMargin(_MetricChecker):
    """§11.3: ``(max_period − min_period) / max_period ≥ min_ratio``."""
    name = "sta_setup_margin"
    native_stage = "finish"
    needs = "signoff"
    requires: ClassVar[set] = {"signoff"}

    def run(self, ctx, params, backend) -> CheckResult:
        m = self.metrics(ctx)
        if "cell_area" not in m and "timing_error" not in m:
            return CheckResult(status="error",
                               detail="required metric missing: signoff")
        if m.get("timing_error"):
            return CheckResult(status="fail",
                               detail=str(m["timing_error"]))
        if m.get("clock_period") is None:
            return CheckResult(status="fail",
                               detail="main clock not defined")
        bound, err = _max_period_bound(ctx, params)
        if err:
            return CheckResult(status="error", detail=err)
        mp = m.get("min_period")
        if mp is None or bound is None or not bound:
            return CheckResult(
                status="error",
                detail="min_period or max_period unavailable")
        ratio = (bound - mp) / bound
        need = params.get("min_ratio", 0)
        if ratio < need:
            return CheckResult(
                status="fail",
                detail=f"setup margin {ratio:.4f} < {need}",
                metrics={"gap": {"value": ratio, "bound": need,
                                 "distance": need - ratio,
                                 "metric": "min_period"}})
        return CheckResult(status="pass",
                           detail=f"setup margin {ratio:.4f}")


@register
class MetricBound(_MetricChecker):
    """Generic exit clause (§11.3): ``{check: metric_bound, id: …,
    metric: <metric>, max|min: <bound>}`` — several instances may
    coexist (clause identity = check + id)."""
    name = "metric_bound"
    native_stage = "finish"
    needs = "signoff"
    requires: ClassVar[set] = {"signoff", "drc"}

    @classmethod
    def needs_for(cls, params):
        if (params or {}).get("metric") == "drc_count":
            return "drc"
        return "signoff"

    def run(self, ctx, params, backend) -> CheckResult:
        metric = (params or {}).get("metric")
        if not metric:
            return CheckResult(status="error",
                               detail="metric_bound: no metric")
        if "max" in params:
            bound_raw, direction = params["max"], "max"
        elif "min" in params:
            bound_raw, direction = params["min"], "min"
        else:
            return CheckResult(
                status="error",
                detail="metric_bound: needs max or min")
        bound, err = resolve_bound(bound_raw, metric,
                                   self.baseline(ctx), ctx.get("golden"))
        if err:
            return CheckResult(status="error", detail=err)
        if bound is None:
            return CheckResult(status="error",
                               detail="metric_bound: unresolvable bound")
        v = self.metrics(ctx).get(metric)
        if v is None:
            return CheckResult(
                status="error",
                detail=f"required metric missing: {metric}")
        bad = v > bound if direction == "max" else v < bound
        if bad:
            return CheckResult(
                status="fail",
                detail=f"{metric} {v:.4g} > {bound:.4g}"
                if direction == "max" else
                f"{metric} {v:.4g} < {bound:.4g}",
                metrics={"gap": {"value": v, "bound": bound,
                                 "distance": (v - bound
                                              if direction == "max"
                                              else bound - v),
                                 "metric": metric}})
        return CheckResult(
            status="pass",
            detail=f"{metric} {v:.4g} within {direction} {bound:.4g}")


@register
class StaHold(_MetricChecker):
    name = "sta_hold"
    native_stage = "finish"
    needs = "signoff"
    requires: ClassVar[set] = {"signoff"}

    def run(self, ctx, params, backend) -> CheckResult:
        m = self.metrics(ctx)
        if m.get("timing_error"):
            return CheckResult(status="fail",
                               detail=str(m["timing_error"]))
        err = self._require(ctx, "hold_wns", "hold_tns")
        if err:
            return err
        fails = []
        if m["hold_wns"] < params.get("min_wns", 0):
            fails.append(f"hold_wns {m['hold_wns']:.4g} < "
                         f"{params.get('min_wns', 0)}")
        if m["hold_tns"] < 0:
            fails.append(f"hold_tns {m['hold_tns']:.4g} < 0")
        if fails:
            gap = {"value": min(m["hold_wns"], m["hold_tns"]),
                   "bound": params.get("min_wns", 0),
                   "distance": max(params.get("min_wns", 0)
                                   - m["hold_wns"], -m["hold_tns"], 0)}
            return CheckResult(status="fail", detail="; ".join(fails),
                               metrics={"gap": gap})
        return CheckResult(
            status="pass",
            detail=f"hold_wns {m['hold_wns']:.4g}, "
                   f"hold_tns {m['hold_tns']:.4g}")


_ELECTRICAL = ("slew_violations", "cap_violations", "fanout_violations")


@register
class Electrical(_MetricChecker):
    name = "electrical"
    native_stage = "finish"
    needs = "signoff"
    requires: ClassVar[set] = {"signoff"}

    def run(self, ctx, params, backend) -> CheckResult:
        err = self._require(ctx, *_ELECTRICAL)
        if err:
            return err
        m = self.metrics(ctx)
        bound = params.get("max", 0)
        fails = []
        gaps = []
        for name in _ELECTRICAL:
            if bound == "baseline":
                b = self.baseline(ctx).get(name)
                if b is None:
                    return CheckResult(
                        status="error",
                        detail=f"baseline metric missing: {name}")
            else:
                b = bound
            if m[name] > b:
                fails.append(f"{name} {m[name]} > {b}")
                gaps.append({"metric": name, "value": m[name],
                             "bound": b, "distance": m[name] - b})
        if fails:
            return CheckResult(status="fail", detail="; ".join(fails),
                               metrics={"gap": max(
                                   gaps, key=lambda g: g["distance"])})
        total = sum(m[n] for n in _ELECTRICAL)
        return CheckResult(status="pass",
                           detail=f"electrical violations {total}")


@register
class Drc(_MetricChecker):
    name = "drc"
    needs = "drc"
    requires: ClassVar[set] = {"drc"}

    def run(self, ctx, params, backend) -> CheckResult:
        m = self.metrics(ctx)
        if m.get("drc_unsupported"):
            return CheckResult(
                status="pass",
                detail="platform has no DRC deck (unsupported)")
        count = m.get("drc_count")
        if count is None:
            return CheckResult(
                status="error",
                detail="required metric missing: drc_count")
        bound = params.get("max", 0)
        base = self.baseline(ctx)
        if bound == "baseline":
            bound = base.get("drc_count")
            if bound is None:
                return CheckResult(
                    status="error",
                    detail="baseline metric missing: drc_count")
        fails = []
        gaps = []
        if count > bound:
            fails.append(f"drc_count {count} > {bound}")
            gaps.append({"metric": "drc_count", "value": count,
                         "bound": bound, "distance": count - bound})
        if params.get("per_category"):
            base_cats = base.get("drc_categories") or {}
            for cat, n in (m.get("drc_categories") or {}).items():
                if n > base_cats.get(cat, 0):
                    fails.append(
                        f"drc category {cat}: {n} > "
                        f"{base_cats.get(cat, 0)}")
                    gaps.append({"metric": f"drc:{cat}", "value": n,
                                 "bound": base_cats.get(cat, 0),
                                 "distance": n - base_cats.get(cat, 0)})
        if fails:
            return CheckResult(status="fail", detail="; ".join(fails),
                               metrics={"gap": max(
                                   gaps, key=lambda g: g["distance"])})
        return CheckResult(status="pass", detail=f"drc_count {count}")


@register
class CellCensus(_MetricChecker):
    name = "cell_census"
    native_stage = "floorplan"
    needs = "signoff"
    requires: ClassVar[set] = {"signoff"}

    def run(self, ctx, params, backend) -> CheckResult:
        err = self._require(ctx, "cells_by_type")
        if err:
            return err
        base = self.baseline(ctx)
        base_cbt = base.get("cells_by_type")
        if base_cbt is None:
            return CheckResult(
                status="error",
                detail="baseline metric missing: cells_by_type")
        m = self.metrics(ctx)
        ratio = params.get("min_ratio", 0.0)
        # §11.2 floorplan changes are legal: compare densities, not
        # counts — tap cells per core area, end caps per sqrt(core area)
        n_area = m.get("core_area")
        b_area = base.get("core_area")
        if not n_area or not b_area:
            return CheckResult(
                status="error",
                detail="core_area missing (node or baseline)")
        fails = []
        gaps = []
        for t in params.get("types", []):
            bcount = (base_cbt.get(t) or {}).get("count")
            if not bcount:
                continue          # type absent from baseline — skip
            norm = math.sqrt if t == "endcap" else (lambda a: a)
            bdens = bcount / norm(b_area)
            ncount = (m["cells_by_type"].get(t) or {}).get("count", 0)
            ndens = ncount / norm(n_area)
            bound = math.ceil(ratio * bdens * norm(n_area))
            if ncount == 0 or ndens < ratio * bdens:
                fails.append(
                    f"{t} density {ndens:.4g} < {ratio}×{bdens:.4g}")
                gaps.append({"metric": t, "value": ncount,
                             "bound": bound,
                             "distance": max(bound - ncount, 0)})
        if fails:
            return CheckResult(status="fail", detail="; ".join(fails),
                               metrics={"gap": max(
                                   gaps, key=lambda g: g["distance"])})
        return CheckResult(status="pass", detail="cell census ok")


@register
class Pdn(_MetricChecker):
    name = "pdn"
    native_stage = "floorplan"
    needs = "signoff"
    requires: ClassVar[set] = {"signoff"}

    def run(self, ctx, params, backend) -> CheckResult:
        err = self._require(ctx, "special_nets")
        if err:
            return err
        base = self.baseline(ctx)
        if base.get("special_nets") is None:
            return CheckResult(
                status="error",
                detail="baseline metric missing: special_nets")
        # §11.1 V1 connectivity form: a different supply-net set, a
        # supply net with no PDN shapes, or a baseline supply net with
        # no IR result fails; layer sets and counts do not matter
        m = self.metrics(ctx)
        fails = []
        gaps = []
        node_nets = set(m["special_nets"])
        base_nets = set(base["special_nets"])
        if node_nets != base_nets:
            fails.append(
                f"special nets differ: {sorted(node_nets)} "
                f"vs baseline {sorted(base_nets)}")
            gaps.append({"metric": "special_nets",
                         "value": sorted(node_nets),
                         "bound": sorted(base_nets),
                         "distance": len(base_nets - node_nets)})
        shapes = m.get("pdn_wires_by_net")
        for net in sorted(node_nets & base_nets):
            if shapes is not None:
                if not shapes.get(net):
                    fails.append(f"supply net {net} has no PDN shapes")
                    gaps.append({"metric": f"pdn:{net}", "value": 0,
                                 "bound": 1, "distance": 1})
            elif not any(isinstance(v, (int, float)) and v > 0
                         for v in (m.get("pdn_wires_by_layer")
                                   or {}).values()):
                # legacy metrics without per-net shapes: only a
                # globally empty grid can be detected
                fails.append("no PDN shapes on any supply net")
                gaps.append({"metric": "pdn", "value": 0,
                             "bound": 1, "distance": 1})
                break
        ir = m.get("ir_drop")
        if ir is not None:
            bir = base.get("ir_drop") or {}
            for net in sorted(base_nets):
                if bir.get(net) is not None and ir.get(net) is None:
                    fails.append(f"no IR result for supply net {net}")
                    gaps.append({"metric": f"ir:{net}", "value": None,
                                 "bound": "drop measured",
                                 "distance": 1})
        if fails:
            return CheckResult(status="fail", detail="; ".join(fails),
                               metrics={"gap": max(
                                   gaps, key=lambda g: g["distance"])})
        return CheckResult(status="pass",
                           detail="pdn connectivity ok")


@register
class IrDrop(_MetricChecker):
    name = "ir_drop"
    native_stage = "finish"
    needs = "signoff"
    requires: ClassVar[set] = {"signoff"}

    def run(self, ctx, params, backend) -> CheckResult:
        err = self._require(ctx, "ir_drop")
        if err:
            return err
        base = self.baseline(ctx).get("ir_drop")
        if base is None:
            return CheckResult(
                status="error", detail="baseline metric missing: ir_drop")
        m = self.metrics(ctx)
        max_growth = params.get("max_growth", 1.0)
        min_delta = params.get("min_delta", 0.0)
        nominals = [v.get("nominal") or 0 for v in base.values()]
        nominal_max = max([n for n in nominals if n > 0], default=1.0)
        fails = []
        gaps = []
        for net, cur in m["ir_drop"].items():
            b = base.get(net)
            if not b:
                continue                       # net present in both only
            drop = cur.get("drop")
            bdrop = b.get("drop")
            if drop is None or bdrop is None:
                continue
            if drop > max_growth * bdrop and \
                    drop - bdrop > min_delta * nominal_max:
                fails.append(
                    f"{net}: drop {drop:.4g} > {max_growth}×{bdrop:.4g}"
                    f" and grew {drop - bdrop:.4g} > "
                    f"{min_delta}×{nominal_max:.4g}")
                gaps.append({"metric": f"ir:{net}", "value": drop,
                             "bound": max_growth * bdrop,
                             "distance": drop - max_growth * bdrop})
        if fails:
            return CheckResult(status="fail", detail="; ".join(fails),
                               metrics={"gap": max(
                                   gaps, key=lambda g: g["distance"])})
        return CheckResult(status="pass", detail="ir drop within bounds")
