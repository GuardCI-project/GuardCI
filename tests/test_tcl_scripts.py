"""Host-side smoke tests for the §12 OpenROAD Tcl scripts — no EDA
tool needed: each script runs under ``tclsh`` (skipped when the host
lacks it) with a stub prelude that defines the tool commands it calls
(`read_db`, `ord::get_db*`, `sta::*`, …) returning canned values,
including a master name containing a quote and an error message with
a newline.  Asserts every JSON output file parses."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / \
    "guardci" / "backends" / "openroad"

pytestmark = pytest.mark.skipif(shutil.which("tclsh") is None,
                                reason="tclsh not available on host")

# Stubs covering every command the new scripts call. `read_db` fails
# when GUARDCI_ODB is the sentinel "broken.odb" (unreadable path).
STUB = r"""
set ::stage_masters [list NAND2_X1 {DFF"X1} filler]
set ::foreign_db 0
proc read_db {p} {
  if {[string match *broken* $p]} {
    error "cannot read database\nsyntax error at line 3"
  }
}
proc read_lef {p} {}
proc read_liberty {p} {}
proc read_def {p} {
  if {[string match *broken* $p]} {
    error "def read failed:\nline 42"
  }
  if {$::foreign_db} { error "unknown master" }
}
proc write_def {p} { set f [open $p w]; puts $f "DEF"; close $f }
proc write_verilog {args} {
  set f [open [lindex $args end] w]; puts $f "module x; endmodule"
  close $f
}
namespace eval ord {
  proc get_db_block {} { return ::blk }
  proc get_db {} { return ::dbobj }
}
proc ::blk {args} {
  if {[lindex $args 0] eq "getInsts"} { return {i1 i2 i3} }
  if {[lindex $args 0] eq "getDbUnitsPerMicron"} { return 2000 }
  if {[lindex $args 0] eq "getDieArea"} { return ::diebox }
  if {[lindex $args 0] eq "getCoreArea"} { return ::corebox }
  if {[lindex $args 0] eq "getRows"} { return {row1} }
  if {[lindex $args 0] eq "getNets"} { return {n1 n2} }
  return {}
}
proc ::diebox {axis} { return 1000 }
proc ::corebox {axis} { if {$axis eq "dx"} {return 800}; return 600 }
proc ::row1 {args} { return row1bbox }
proc row1bbox {args} { return {0 0 800 600} }
proc ::i1 {args} { if {[lindex $args 0] eq "getMaster"} {return m1}
                   return 0 }
proc ::i2 {args} { if {[lindex $args 0] eq "getMaster"} {return m2}
                   return 1 }
proc ::i3 {args} { if {[lindex $args 0] eq "getMaster"} {return m3}
                   return 1 }
proc master_property {name type args} {
  switch -- [lindex $args 0] {
    getName { return $name }
    getType { return $type }
    getWidth { return 400 }
    getHeight { return 2800 }
    default { error "unsupported master API: $args" }
  }
}
proc ::m1 {args} { return [master_property NAND2_X1 CORE {*}$args] }
proc ::m2 {args} { return [master_property {DFF"X1} CORE {*}$args] }
proc ::m3 {args} { return [master_property filler CORE_SPACER {*}$args] }
proc ::n1 {args} {
  if {[lindex $args 0] eq "isSpecial"} { return 1 }
  if {[lindex $args 0] eq "getSWires"} { return {sw1} }
  if {[lindex $args 0] eq "getName"} { return VDD }
  return {} }
proc ::n2 {args} {
  if {[lindex $args 0] eq "isSpecial"} { return 0 }
  return {} }
proc ::sw1 {args} { return {w1} }
proc ::w1 {args} {
  if {[lindex $args 0] eq "isVia"} {return 0}
  if {[lindex $args 0] eq "getTechLayer"} {return L1}
  return {} }
proc ::L1 {args} { return metal1 }
namespace eval dbobj {}
proc ::dbobj {args} {
  if {[lindex $args 0] eq "getDbu"} { return 2000 }
  if {[lindex $args 0] eq "getLibs"} { return {lib1} }
  return {}
}
proc ::lib1 {args} { return {gm1 gm2} }
proc ::gm1 {args} {
  if {[lindex $args 0] eq "getName"} { return NAND2_X1 }
  if {[lindex $args 0] eq "getType"} { return CORE }
  if {[lindex $args 0] eq "getWidth"} { return 400 }
  if {[lindex $args 0] eq "getHeight"} { return 2800 }
  return {} }
proc ::gm2 {args} {
  if {[lindex $args 0] eq "getName"} { return filler }
  if {[lindex $args 0] eq "getType"} { return CORE_SPACER }
  return {} }
proc get_lib_cells {args} { return {lc1} }
proc check_placement {args} {}
proc estimate_parasitics {args} {}
proc set_propagated_clock {args} {}
proc all_clocks {} { return clk }
namespace eval sta {
  proc time_sta_ui {x} { return -0.05 }
  proc worst_slack_cmd {x} { return 0 }
  proc total_negative_slack_cmd {x} { return 0 }
}
"""


def _env(tmp, **kw):
    import os
    env = dict(os.environ)
    defaults = {
        "GUARDCI_ODB": str(tmp / "a.odb"),
        "GUARDCI_LIBS": "lib1.lib lib2.lib",
        "GUARDCI_TECH_LEF": "tech.lef",
        "GUARDCI_SC_LEFS": "sc.lef",
        "GUARDCI_DEF": str(tmp / "in.def"),
        "GUARDCI_DEF_OUT": str(tmp / "out.def"),
        "GUARDCI_OUT": str(tmp / "out.json"),
        "GUARDCI_MASTERS": str(tmp / "masters.json"),
        "GUARDCI_STAGE": "synth",
        "GUARDCI_SDC_TEMPLATE": str(tmp / "sdc.tcl"),
        "GUARDCI_PERIOD": "0.5",
        "GUARDCI_SET_RC_TCL": "",
    }
    (tmp / "sdc.tcl").write_text("# Stub golden timing constraints\n")
    defaults.update(kw)
    env.update({k: str(v) for k, v in defaults.items()})
    return env


def _run(script, tmp, env):
    wrapper = tmp / f"{script.stem}-wrap.tcl"
    wrapper.write_text(STUB + "\nsource [lindex $argv 0]\n")
    return subprocess.run(
        ["tclsh", str(wrapper), str(SCRIPTS / script.name)],
        capture_output=True, text=True, env=env, check=False)


def test_export_artifact_ok(tmp_path):
    env = _env(tmp_path)
    r = _run(Path("export_artifact.tcl"), tmp_path, env)
    assert r.returncode == 0, r.stderr
    data = json.loads((tmp_path / "masters.json").read_text())
    assert "NAND2_X1" in data["masters"]
    assert 'DFF"X1' in data["masters"]     # quoted name escapes cleanly
    assert (tmp_path / "out.def").exists()


def test_export_artifact_unreadable(tmp_path):
    env = _env(tmp_path, GUARDCI_ODB=str(tmp_path / "broken.odb"))
    r = _run(Path("export_artifact.tcl"), tmp_path, env)
    assert r.returncode == 0, r.stderr
    data = json.loads((tmp_path / "masters.json").read_text())
    assert data["unreadable"] == 1
    assert "syntax error" in data["error"]    # newline escaped


def test_facts_unreadable(tmp_path):
    env = _env(tmp_path, GUARDCI_DEF=str(tmp_path / "broken.def"),
               GUARDCI_MASTERS=str(tmp_path / "masters.json"))
    (tmp_path / "masters.json").write_text(
        '{"masters": ["NAND2_X1", "filler"]}')
    r = _run(Path("facts.tcl"), tmp_path, env)
    assert r.returncode == 0, r.stderr
    data = json.loads((tmp_path / "out.json").read_text())
    assert data["unreadable"] == 1


def test_facts_foreign_master(tmp_path):
    (tmp_path / "masters.json").write_text(
        json.dumps({"masters": ["NAND2_X1", 'EVIL"CELL']}))
    env = _env(tmp_path)
    r = _run(Path("facts.tcl"), tmp_path, env)
    assert r.returncode == 0, r.stderr
    data = json.loads((tmp_path / "out.json").read_text())
    assert data["library_closed"] is False
    assert 'EVIL"CELL' in data["foreign_masters"]


def test_facts_ok(tmp_path):
    (tmp_path / "masters.json").write_text(
        '{"masters": ["NAND2_X1", "filler"]}')
    env = _env(tmp_path, GUARDCI_STAGE="place")
    r = _run(Path("facts.tcl"), tmp_path, env)
    assert r.returncode == 0, r.stderr
    data = json.loads((tmp_path / "out.json").read_text())
    assert data["library_closed"] is True
    assert data["instances"] == 2
    assert data["cell_area"] == pytest.approx(0.56)
    assert data["die_area"] == pytest.approx(0.25)
    assert data["core_area"] == pytest.approx(0.12)
    assert data["pdn_wires_by_layer"] == {"metal1": 1}
    for k in ("instances", "cell_area", "die_area", "special_nets",
              "unplaced_instances", "est_setup_wns"):
        assert k in data
