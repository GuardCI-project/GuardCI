"""Unit tests for OrfsFlow.classify_delta tag mapping."""

from guardci.core.diff import Diff, DiffEntry
from guardci.flows.orfs.adapter import OrfsFlow

BASE_CFG = """\
export DESIGN_NAME = gcd
export PLATFORM = nangate45
export VERILOG_FILES = $(DESIGN_HOME)/src/$(DESIGN_NICKNAME)/gcd.v
export SDC_FILE = $(DESIGN_DIR)/impl.sdc
export CORE_UTILIZATION = 55
"""


def _flow():
    return OrfsFlow()


def _diff(entries):
    return Diff(base="b", head="h", entries=entries, repo=None)


def _cfg_change(before, after):
    return DiffEntry(path="designs/nangate45/gcd/config.mk",
                     kind="modified", before=before, after=after)


def _var_change(var, old, new):
    return _diff([_cfg_change(BASE_CFG + f"export {var} = {old}\n",
                              BASE_CFG + f"export {var} = {new}\n")])


def test_core_utilization_tags():
    tags = _var_change("CORE_UTILIZATION", "55", "60")
    assert _flow().classify_delta(tags) == {"floorplan",
                                            "physical_integrity"}


def test_hold_slack_margin():
    tags = _var_change("HOLD_SLACK_MARGIN", "0.05", "0.1")
    assert _flow().classify_delta(tags) == {"physical_integrity"}


def test_lib_files():
    tags = _var_change("LIB_FILES", "$(PLATFORM_DIR)/lib/a.lib",
                       "$(PLATFORM_DIR)/lib/b.lib")
    assert _flow().classify_delta(tags) == {"library_refs"}


def test_verilog_files():
    tags = _var_change("VERILOG_FILES", "designs/src/gcd/gcd.v",
                       "designs/src/gcd/other.v")
    assert _flow().classify_delta(tags) == {"design_refs"}


def test_unknown_var():
    tags = _var_change("FOO", "1", "2")
    assert _flow().classify_delta(tags) == {"unknown"}


def test_script_cts():
    d = _diff([DiffEntry(path="scripts/cts.tcl", kind="modified",
                       before="x", after="y")])
    assert _flow().classify_delta(d) == {"scripts", "cts"}


def test_script_unknown():
    d = _diff([DiffEntry(path="scripts/mystery.tcl", kind="added",
                       after="y")])
    assert _flow().classify_delta(d) == {"scripts", "unknown"}


def test_impl_sdc():
    d = _diff([DiffEntry(path="designs/nangate45/gcd/impl.sdc",
                         kind="modified", before="a", after="b")])
    assert _flow().classify_delta(d) == {"constraints"}


def test_makefile_and_recipe_unknowns():
    d = _diff([DiffEntry(path="Makefile", kind="modified",
                         before="a", after="b")])
    assert _flow().classify_delta(d) == {"unknown"}


def test_scratch_mk_tagged_unknown():
    d = _diff([DiffEntry(path="designs/nangate45/gcd/exp.mk",
                         kind="added", before=None, after="X = 1\n")])
    assert _flow().classify_delta(d) == {"unknown"}


def test_scratch_verilog_tagged_design_refs():
    d = _diff([DiffEntry(path="designs/nangate45/gcd/extra.v",
                         kind="added", before=None, after="module m;")])
    assert _flow().classify_delta(d) == {"design_refs"}


def test_scratch_doc_tagged_measurement():
    d = _diff([DiffEntry(path="NOTES.md", kind="added", before=None,
                         after="# notes\n")])
    assert _flow().classify_delta(d) == {"measurement"}


# ---- closure-aware classification (real repo, §9.3) ---------------------

from gitrepo import commit_all, head, make_repo

from guardci.core.diff import build_diff

PROTECTED = [
    "platforms/**", "designs/src/**", "designs/*/*/constraint.sdc",
]


def _repo(tmp_path):
    repo = make_repo(tmp_path / "r")
    base = head(repo)
    return repo, base


def _tags(repo, base, flow):
    
    return flow.classify_delta(build_diff(str(repo), head(repo), base))


def test_unreachable_new_files_tagged_measurement(tmp_path):
    """config_v2.mk + run_sweep.sh unreferenced → {measurement}."""
    repo, base = _repo(tmp_path)
    d = repo / "designs/nangate45/gcd"
    (d / "config_v2.mk").write_text("export X = 1\n")
    (d / "run_sweep.sh").write_text("#!/bin/sh\n")
    commit_all(repo)
    assert _tags(repo, base, _flow()) == {"measurement"}


def test_reachable_new_tcl_tagged_by_ext(tmp_path):
    """A new file the recipe names (PRE_CTS_TCL hook) is reachable and
    tags by extension (.tcl → unknown); an unreferenced file is
    measurement."""
    repo, base = _repo(tmp_path)
    d = repo / "designs/nangate45/gcd"
    (d / "hook.tcl").write_text("puts hi\n")
    (d / "config.mk.bak").write_text("x\n")  # unreferenced → measurement
    cfg = d / "config.mk"
    cfg.write_text(cfg.read_text()
                   + "\nexport PRE_CTS_TCL = $(DESIGN_DIR)/hook.tcl\n")
    commit_all(repo)
    tags = _tags(repo, base, _flow())
    # PRE_CTS_TCL variable tag (cts) + reachable .tcl (unknown)
    assert {"cts", "unknown"} <= tags
    # .bak unreachable → measurement
    assert "measurement" in tags


def test_deleting_unreachable_new_file_is_measurement(tmp_path):
    repo, base = _repo(tmp_path)
    d = repo / "designs/nangate45/gcd"
    (d / "sweep.sh").write_text("echo 1\n")
    mid = commit_all(repo)
    (d / "sweep.sh").unlink()
    head_sha = commit_all(repo)
    diff = build_diff(str(repo), head_sha, mid)
    diff.baseline = base          # sweep.sh is new vs the baseline
    assert _flow().classify_delta(diff) == {"measurement"}
