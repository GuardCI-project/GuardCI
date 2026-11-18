"""Unit tests for SDC golden-vs-impl classification."""

from guardci.checks.sdc_delta import classify
from guardci.flows.orfs.adapter import OrfsFlow

FLOW = OrfsFlow()

GOLDEN = """\
set clk_period 1.0
create_clock [get_ports clk] -name clk -period $clk_period
set_clock_uncertainty 0.05 [get_clocks clk]
set_input_delay 0.2 -clock clk [all_inputs]
set_output_delay 0.2 -clock clk [all_outputs]
set_max_transition 0.5 [current_design]
"""


def test_tighter_period():
    impl = GOLDEN.replace("-period $clk_period", "-period 0.8") \
        .replace("set clk_period 1.0", "set clk_period 0.8")
    m = classify(FLOW.parse_constraints(impl),
                          FLOW.parse_constraints(GOLDEN))
    row = m["classes"]["clock_period"][0]
    assert row["golden"] == 1.0 and row["impl"] == 0.8
    assert row["direction"] == "tighter"
    assert m["summary"]["tighter"] == 1


def test_added_false_path_is_exception():
    impl = GOLDEN + "set_false_path -from [get_ports rst]\n"
    m = classify(FLOW.parse_constraints(impl),
                          FLOW.parse_constraints(GOLDEN))
    assert any("set_false_path" in r for r in m["classes"]["exception"])
    assert m["summary"]["exceptions"] == 1


def test_clock_latency_is_redefine():
    impl = GOLDEN + "set_clock_latency 0.3 [get_clocks clk]\n"
    m = classify(FLOW.parse_constraints(impl),
                          FLOW.parse_constraints(GOLDEN))
    assert any("set_clock_latency" in r
               for r in m["classes"]["clock_redefine"])
    assert m["summary"]["redefines"] == 1


def test_looser_max_transition():
    impl = GOLDEN.replace("set_max_transition 0.5",
                          "set_max_transition 1.0")
    m = classify(FLOW.parse_constraints(impl),
                          FLOW.parse_constraints(GOLDEN))
    row = m["classes"]["design_rule"][0]
    assert row["cmd"] == "set_max_transition"
    assert row["golden"] == 0.5 and row["impl"] == 1.0
    assert row["direction"] == "looser"
    assert m["summary"]["looser"] == 1
