"""§9.6 checker unit tests: pass / fail / error-on-missing for each
measurement checker with the contract's thresholds."""

import pytest

from guardci.checks import measurement as M
from guardci.core.models import Cost

GOOD = {
    "clock_period": 0.5,
    "setup_wns": 0.05, "setup_tns": 0.0,
    "hold_wns": 0.02, "hold_tns": 0.0,
    "slew_violations": 2, "cap_violations": 1, "fanout_violations": 0,
    "cells_by_type": {"tapcell": {"count": 10}, "endcap": {"count": 4}},
    "special_nets": ["VDD", "VSS"],
    "pdn_wires_by_layer": {"metal1": 100, "metal4": 40},
    "ir_drop": {"VDD": {"nominal": 1.1, "drop": 0.05}},
    "drc_count": 3, "drc_categories": {"short": 2, "spacing": 1},
    "lec_result": "identical",
    "core_area": 1000.0,
    "cell_area": 800.0,
    "pdn_wires_by_net": {"VDD": 60, "VSS": 80},
    "min_period": 0.4,
}

BASE = dict(GOOD)


def ctx(metrics=None, base=None, golden=None, contract=None):
    return {"metrics": GOOD if metrics is None else metrics,
            "baseline_metrics": BASE if base is None else base,
            "golden": {"golden_period": 0.5} if golden is None
            else golden,
            "contract": contract}


def run(cls, params=None, metrics=None, base=None):
    return cls().run(ctx(metrics, base), params or {}, None)


def test_costs():
    assert M.Lec.cost == Cost.LIGHT
    for cls in (M.StaSetup, M.StaSetupMargin, M.StaHold, M.Electrical,
                M.Drc, M.CellCensus, M.Pdn, M.IrDrop):
        assert cls.cost == Cost.HEAVY


# ---------------------------------------------------------------- lec

def test_lec():
    assert run(M.Lec).status == "pass"
    assert run(M.Lec, metrics={"lec_result": "different",
                               "lec_differences": 4}).status == "fail"
    assert run(M.Lec, metrics={"lec_result": "error"}).status == "error"
    assert run(M.Lec, metrics={}).status == "error"


def test_lec_timeout():
    """§9.6: a timed-out LEC is `fail` only under `on_timeout: fail`;
    `error` (or absent) keeps the old fail-closed behaviour."""
    m = {"lec_result": "timeout", "lec_timeout_s": 1800}
    r = run(M.Lec, params={"on_timeout": "fail"}, metrics=m)
    assert r.status == "fail"
    assert r.detail == "lec: timeout after 1800s " \
        "(equivalence not proven)"
    assert run(M.Lec, params={"on_timeout": "error"},
               metrics=m).status == "error"
    assert run(M.Lec, metrics=m).status == "error"


# ------------------------------------------------------------ sta_setup

def test_sta_setup():
    # GOOD.min_period=0.4, BASE.min_period=0.4
    assert run(M.StaSetup,
               {"max_period": {"ratio": 1.0, "of": "baseline"}}
               ).status == "pass"
    assert run(M.StaSetup, {"max_period": 0.45}).status == "pass"
    r = run(M.StaSetup, {"max_period": 0.3})
    assert r.status == "fail" and \
        r.metrics["gap"]["value"] == 0.4 and \
        r.metrics["gap"]["bound"] == 0.3
    assert run(M.StaSetup, {"max_period": "golden"}).status == "pass"
    # ratio-of-baseline tightens below min_period → fail
    assert run(M.StaSetup,
               {"max_period": {"ratio": 0.5, "of": "baseline"}}
               ).status == "fail"
    # missing baseline min_period → checker error
    assert run(M.StaSetup,
               {"max_period": {"ratio": 1.0, "of": "baseline"}},
               base={"cell_area": 1}).status == "error"
    # timing_error → fail; min_period null → fail
    assert run(M.StaSetup, {"max_period": 1.0},
               metrics={**GOOD, "timing_error": "boom"}).status == "fail"
    r = run(M.StaSetup, {"max_period": 1.0},
            metrics={**GOOD, "min_period": None,
                     "min_period_error": "no achievable period"})
    assert r.status == "fail" and r.metrics["gap"]["distance"] == 1
    # no signoff measurement at all → error (§11 fix)
    assert run(M.StaSetup, {"max_period": 1.0},
               metrics={}).status == "error"


def test_sta_setup_margin():
    # margin = (max_period − min_period)/max_period: (0.5−0.4)/0.5 = 0.2
    assert run(M.StaSetupMargin,
               {"min_ratio": 0.005, "max_period": "golden"}
               ).status == "pass"
    assert run(M.StaSetupMargin,
               {"min_ratio": 0.5, "max_period": "golden"}
               ).status == "fail"
    # max_period resolved from the contract's sta_setup clause
    from guardci.core.models import Clause
    contract = type("C", (), {"clauses": [
        Clause(check="sta_setup",
               params={"max_period": 0.5})]})()
    r = M.StaSetupMargin().run(
        ctx(contract=contract), {"min_ratio": 0.005}, None)
    assert r.status == "pass"
    assert run(M.StaSetupMargin,
               {"min_ratio": 0.005, "max_period": 0.5},
               metrics={**GOOD, "timing_error": "boom"}).status == "fail"
    assert run(M.StaSetupMargin,
               {"min_ratio": 0.005, "max_period": 0.5},
               metrics={}).status == "error"      # no signoff → error


def test_metric_bound():
    # cell_area 800, baseline 800: max {ratio:1.0} → pass
    assert run(M.MetricBound,
               {"metric": "cell_area",
                "max": {"ratio": 1.0, "of": "baseline"}}).status == "pass"
    r = run(M.MetricBound,
            {"metric": "cell_area",
             "max": {"ratio": 0.5, "of": "baseline"}})
    assert r.status == "fail" and \
        r.metrics["gap"]["bound"] == 400.0
    assert run(M.MetricBound, {"metric": "cell_area",
                               "max": 900}).status == "pass"
    assert run(M.MetricBound, {"metric": "hold_wns",
                               "min": 0.01}).status == "pass"
    assert run(M.MetricBound, {"metric": "hold_wns",
                               "min": 0.05}).status == "fail"
    assert run(M.MetricBound, {"metric": "nope", "max": 1}
               ).status == "error"
    assert M.MetricBound.needs_for({"metric": "drc_count"}) == "drc"
    assert M.MetricBound.needs_for({"metric": "cell_area"}) == "signoff"


def test_sta_hold():
    assert run(M.StaHold, {"min_wns": 0}).status == "pass"
    assert run(M.StaHold, {"min_wns": 0},
               metrics={**GOOD, "hold_wns": -0.01}).status == "fail"
    assert run(M.StaHold, {"min_wns": -1},
               metrics={**GOOD, "hold_tns": -0.5}).status == "fail"
    assert run(M.StaHold, metrics={}).status == "error"
    # timing_error → fail with the error as detail (§9.6)
    r = run(M.StaHold, metrics={**GOOD, "timing_error": "no clock"})
    assert r.status == "fail" and "no clock" in r.detail


# ------------------------------------------------------------ electrical

def test_electrical():
    assert run(M.Electrical, {"max": "baseline"}).status == "pass"
    assert run(M.Electrical, {"max": "baseline"},
               metrics={**GOOD, "slew_violations": 3}).status == "fail"
    assert run(M.Electrical, {"max": 5},
               metrics={**GOOD, "slew_violations": 6}).status == "fail"
    assert run(M.Electrical, {"max": "baseline"},
               metrics={}).status == "error"
    assert run(M.Electrical, {"max": "baseline"},
               base={}).status == "error"


# -------------------------------------------------------------------- drc

def test_drc():
    assert run(M.Drc, {"max": "baseline"}).status == "pass"
    assert run(M.Drc, {"max": "baseline"},
               metrics={**GOOD, "drc_count": 4}).status == "fail"
    # per_category: a single category above baseline fails even if total ok
    m = {**GOOD, "drc_categories": {"short": 3, "spacing": 0}}
    assert run(M.Drc, {"max": "baseline", "per_category": True},
               metrics=m).status == "fail"
    # new category counts from 0
    m = {**GOOD, "drc_categories": {"short": 2, "newcat": 1}}
    assert run(M.Drc, {"max": "baseline", "per_category": True},
               metrics=m).status == "fail"
    assert run(M.Drc, {"max": "baseline"},
               metrics={"drc_unsupported": True}).status == "pass"
    assert run(M.Drc, {"max": "baseline"}, metrics={}).status == "error"
    assert run(M.Drc, {"max": "baseline"}, base={}).status == "error"


# ------------------------------------------------------------ cell_census

def test_cell_census():
    """§11.2 density form: taps per core area, endcaps per √area."""
    p = {"types": ["tapcell", "endcap"], "min_ratio": 0.8}
    assert run(M.CellCensus, p).status == "pass"
    # smaller core with proportionally fewer taps → still passes
    m = {**GOOD, "core_area": 500.0,
         "cells_by_type": {"tapcell": {"count": 5},
                           "endcap": {"count": 3}}}
    assert run(M.CellCensus, p, metrics=m).status == "pass"
    m = {**GOOD, "cells_by_type": {"tapcell": {"count": 7},
                                 "endcap": {"count": 4}}}
    assert run(M.CellCensus, p, metrics=m).status == "fail"
    r = run(M.CellCensus, p, metrics=m)
    assert r.metrics["gap"]["value"] == 7
    m = {**GOOD, "cells_by_type": {"endcap": {"count": 4}}}
    assert run(M.CellCensus, p, metrics=m).status == "fail"   # count 0
    # type absent in baseline is skipped
    p2 = {"types": ["antenna"], "min_ratio": 0.8}
    assert run(M.CellCensus, p2).status == "pass"
    assert run(M.CellCensus, p, metrics={}).status == "error"
    assert run(M.CellCensus, p, base={}).status == "error"


# -------------------------------------------------------------------- pdn

def test_sta_no_signoff_is_error_not_no_clock():
    """No signoff measurement at all → error (missing measurement);
    `main clock not defined` is reserved for a signoff run that found
    no clock. Gap values are numbers/short enums, never free text."""
    r = run(M.StaSetup, metrics={})
    assert r.status == "error"
    m = {"cell_area": 1.0, "timing_error": None, "clock_period": None}
    r = run(M.StaSetup, metrics=m)
    assert r.status == "fail" and "clock" in r.detail
    assert r.metrics["gap"]["value"] == "undefined"
    r = run(M.StaSetupMargin, metrics={})
    assert r.status == "error"


def test_pdn():
    """§11.1 connectivity form: layer sets and counts never matter."""
    p = {}
    assert run(M.Pdn, p).status == "pass"
    # layer/via set and count changes are legal
    m = {**GOOD, "pdn_wires_by_layer": {"metal1": 5, "metal9": 900}}
    assert run(M.Pdn, p, metrics=m).status == "pass"
    # different supply-net set fails both ways
    m = {**GOOD, "special_nets": ["VDD"]}
    assert run(M.Pdn, p, metrics=m).status == "fail"
    m = {**GOOD, "special_nets": ["VDD", "VSS", "VDDH"]}
    assert run(M.Pdn, p, metrics=m).status == "fail"
    # a supply net without PDN shapes fails
    m = {**GOOD, "pdn_wires_by_net": {"VDD": 0, "VSS": 80}}
    assert run(M.Pdn, p, metrics=m).status == "fail"
    # a baseline supply net with no IR result fails
    m = {**GOOD, "ir_drop": {}}
    assert run(M.Pdn, p, metrics=m).status == "fail"
    # legacy metrics without per-net shapes: net set still checked
    m = {**GOOD}
    del m["pdn_wires_by_net"]
    assert run(M.Pdn, p, metrics=m).status == "pass"
    m["pdn_wires_by_layer"] = {}
    assert run(M.Pdn, p, metrics=m).status == "fail"
    assert run(M.Pdn, p, metrics={}).status == "error"
    assert run(M.Pdn, p, base={}).status == "error"


# ---------------------------------------------------------------- ir_drop

def test_ir_drop():
    p = {"max_growth": 1.5, "min_delta": 0.005}
    assert run(M.IrDrop, p).status == "pass"
    # 0.09 > 1.5×0.05 and 0.09−0.05=0.04 > 0.005×1.1 → fail
    m = {**GOOD, "ir_drop": {"VDD": {"nominal": 1.1, "drop": 0.09}}}
    assert run(M.IrDrop, p, metrics=m).status == "fail"
    # above growth but tiny absolute delta → pass
    m = {**GOOD, "ir_drop": {"VDD": {"nominal": 1.1, "drop": 0.0554}}}
    assert run(M.IrDrop, p, metrics=m).status == "pass"
    # big delta but below growth bound → pass
    m = {**GOOD, "ir_drop": {"VDD": {"nominal": 1.1, "drop": 0.07}}}
    assert run(M.IrDrop, p, metrics=m).status == "pass"
    assert run(M.IrDrop, p, metrics={}).status == "error"
    assert run(M.IrDrop, p, base={}).status == "error"


@pytest.mark.parametrize("cls", [M.StaHold,
                                 M.Electrical, M.Drc, M.CellCensus,
                                 M.Pdn, M.IrDrop])
def test_missing_metric_is_error(cls):
    p = {"max": "baseline", "types": ["tapcell"],
         "max_layer_change": 0.3, "max_growth": 1.5,
         "min_delta": 0.005, "min_ratio": 0.005}
    assert run(cls, p, metrics={}).status == "error"
