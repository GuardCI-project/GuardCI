"""sdc_delta checker (static, severity info).

Classifies the difference between the implementation constraints
(recipe, free) and the golden signoff constraints (protected) into the
DESIGN.md §4.2 buckets: tighten / loosen / exception / clock-redefine /
structural. Always passes — this is metadata, not a gate. Works on the
adapter-normalized ``SdcCommand`` list; knows nothing about SDC file
formats itself.
"""

from __future__ import annotations

from typing import ClassVar

from ..core.diff import read_file
from ..core.models import Cost
from .base import Checker, CheckResult, register

_EXCEPTION_VERBS = {"set_false_path", "set_multicycle_path",
                    "set_disable_timing", "set_case_analysis",
                    "set_clock_groups"}
_REDEFINE_VERBS = {"create_clock", "create_generated_clock",
                   "set_clock_latency", "set_propagated_clock",
                   "set_clock_transition"}
_STRUCTURAL_VERBS = {"set_dont_touch", "set_dont_use", "set_size_only",
                     "set_ideal_network"}
_IO_VERBS = {"set_input_delay", "set_output_delay"}
_RULE_VERBS = {"set_max_transition", "set_max_capacitance",
               "set_max_fanout"}
_CLASSIFIED = _EXCEPTION_VERBS | _REDEFINE_VERBS | _STRUCTURAL_VERBS \
    | _IO_VERBS | _RULE_VERBS | {"set_clock_uncertainty"}


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _periods(commands):
    """clock name -> period for create_clock commands."""
    out = {}
    for c in commands:
        if c.verb == "create_clock":
            name = c.args.get("-name") \
                or (c.args.get("_pos") or [None])[0]
            out[name or "?"] = _num(c.args.get("-period"))
    return out


def _scalar(commands, verb):
    for c in commands:
        if c.verb == verb:
            pos = c.args.get("_pos") or []
            return _num(pos[0]) if pos else None
    return None


def _raws(commands, verbs):
    return [c.raw for c in commands if c.verb in verbs]


def _direction(smaller_tighter: bool, golden, impl) -> str:
    if golden == impl:
        return "same"
    if impl is None or golden is None:
        return "tighter" if impl is None else "looser"
    tighter = impl < golden if smaller_tighter else impl > golden
    return "tighter" if tighter else "looser"


def classify(impl_cmds, golden_cmds) -> dict:
    """Classify impl-vs-golden constraint differences into §4.2 metrics.

    Both arguments are ``list[SdcCommand]`` as returned by
    ``FlowAdapter.parse_constraints``.
    """
    impl, golden = list(impl_cmds), list(golden_cmds)
    summary = {"tighter": 0, "looser": 0, "exceptions": 0,
               "redefines": 0}
    classes: dict = {}

    # clock period (create_clock -period), smaller = tighter
    gp, ip = _periods(golden), _periods(impl)
    rows = []
    for clk in sorted(set(gp) | set(ip)):
        d = _direction(True, gp.get(clk), ip.get(clk))
        rows.append({"clock": clk, "golden": gp.get(clk),
                     "impl": ip.get(clk), "direction": d})
        if d != "same":
            summary[d] += 1
    classes["clock_period"] = rows

    # clock uncertainty (set_clock_uncertainty), larger = tighter
    gu, iu = _scalar(golden, "set_clock_uncertainty"), \
        _scalar(impl, "set_clock_uncertainty")
    if gu != iu:
        d = _direction(False, gu, iu)
        classes["clock_uncertainty"] = [{"golden": gu, "impl": iu,
                                         "direction": d}]
        summary[d] += 1

    # io delays
    gi, ii = _raws(golden, _IO_VERBS), _raws(impl, _IO_VERBS)
    classes["io_delay"] = {
        "added": [r for r in ii if r not in gi],
        "removed": [r for r in gi if r not in ii],
        "changed": [],
    }

    # design rules, smaller = tighter
    rules = []
    for verb in sorted(_RULE_VERBS):
        gv, iv = _scalar(golden, verb), _scalar(impl, verb)
        if gv == iv:
            continue
        d = _direction(True, gv, iv)
        rules.append({"cmd": verb, "golden": gv, "impl": iv,
                      "direction": d})
        summary[d] += 1
    classes["design_rule"] = rules

    g_set = {c.raw for c in golden}

    # exceptions present in impl only
    ge, ie = _raws(golden, _EXCEPTION_VERBS), _raws(impl,
                                                  _EXCEPTION_VERBS)
    classes["exception"] = [r for r in ie if r not in ge]
    summary["exceptions"] = len(classes["exception"])

    # clock redefinition
    gr, ir = _raws(golden, _REDEFINE_VERBS), _raws(impl,
                                                 _REDEFINE_VERBS)
    classes["clock_redefine"] = [r for r in ir if r not in gr]
    summary["redefines"] = len(classes["clock_redefine"])

    # structural
    gs, is_ = _raws(golden, _STRUCTURAL_VERBS), \
        _raws(impl, _STRUCTURAL_VERBS)
    classes["structural"] = [r for r in is_ if r not in gs]

    # everything else that differs
    classes["other"] = sorted(
        c.raw for c in impl
        if c.raw not in g_set and c.verb not in _CLASSIFIED)

    return {"classes": classes, "summary": summary}


@register
class SdcDelta(Checker):
    name = "sdc_delta"
    cost = Cost.STATIC
    requires: ClassVar[set] = set()

    def run(self, ctx: dict, params: dict, backend) -> CheckResult:
        repo, head = ctx["repo"], ctx["node"].sha
        flow = ctx["flow"]
        files = flow.constraints(repo, head)

        impl_text = read_file(repo, head, files.impl) if files.impl else None
        golden_text = read_file(repo, ctx["baseline_sha"], files.golden) \
            if files.golden else None

        if files.impl and files.impl == files.golden:
            return CheckResult(status="pass",
                               metrics={"same_as_golden": True,
                                        "sdc_file": files.impl})
        if impl_text is None or golden_text is None:
            return CheckResult(
                status="pass",
                metrics={"note": "constraint file missing",
                         "sdc_file": files.impl})

        metrics = classify(flow.parse_constraints(impl_text),
                           flow.parse_constraints(golden_text))
        metrics["sdc_file"] = files.impl
        metrics["golden_sdc"] = files.golden
        return CheckResult(status="pass", metrics=metrics)
