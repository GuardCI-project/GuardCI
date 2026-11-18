"""golden_constraints: parameterization methods + main clock extraction."""

from __future__ import annotations

import pytest

from guardci.flows.orfs.build import golden_constraints, parameterize_sdc

SET_CLK = """\
set clk_name core_clock
set clk_port_name clk
set clk_period 0.25
set clk_port [get_ports $clk_port_name]
create_clock -name $clk_name -period $clk_period $clk_port
set_input_delay [expr $clk_period * 0.2] -clock vclk [all_inputs]
"""

CREATE_CLK = """\
create_clock -name core_clock -period 0.5 [get_ports clk]
set_input_delay 0.1 -clock core_clock [all_inputs]
"""

MULTI_CLK = """\
create_clock -name c1 -period 0.5 [get_ports clk]
create_clock -name c2 -period 0.5 [get_ports clk2]
"""


def test_set_clk_period_method():
    out = golden_constraints(SET_CLK)
    assert out["method"] == "set_clk_period"
    assert "set clk_period $::guardci_period" in out["template"]
    assert out["golden_period"] == 0.25
    mc = out["main_clock"]
    assert mc["name"] == "core_clock"
    assert mc["sources"] == ["clk"]


def test_create_clock_method():
    out = golden_constraints(CREATE_CLK)
    assert out["method"] == "create_clock"
    assert "-period $::guardci_period" in out["template"]
    assert out["golden_period"] == 0.5
    assert out["main_clock"]["name"] == "core_clock"
    assert out["main_clock"]["sources"] == ["clk"]


def test_create_clock_all_method():
    template, method = parameterize_sdc(MULTI_CLK)
    assert method == "create_clock_all"
    assert template.count("$::guardci_period") == 2


def test_unparameterizable_raises():
    with pytest.raises(RuntimeError):
        parameterize_sdc("set_input_delay 0.1 [all_inputs]\n")
