"""W10: equivalence fixes — top detection, boundary mismatch,
partial cosim progress, hierarchical-netlist refusal. All fake."""

import json
from pathlib import Path

import pytest

import guardci.backends.equivalence as equiv
from guardci.artifacts.model import ArtifactKind, ArtifactSet
from guardci.backends.base import MeasureRequest
from guardci.backends.equivalence import CosimBackend, EquivalenceBackend, _rename_module
from guardci.backends.kepler_formal import KeplerFormalBackend, classify_log
from guardci.executors.base import InfraError, RunResult


def test_classify_log_boundary_mismatch():
    log = (b"stuff\nLEC boundary mismatch: design1 has 1383 boundary "
           b"inputs, design2 has 1383, and 1127 match by name.\n")
    assert classify_log(log) == ("boundary_mismatch", 0)


def test_classify_log_unchanged_cases():
    assert classify_log(b"Circuits are IDENTICAL\n") == ("identical", 0)
    assert classify_log(b"Found difference x\n") == ("different", 1)
    assert classify_log(b"random garbage\n") == ("error", 0)


class _KeplerExec:
    """Executor stub: writes lec.log + lec-run.log with canned text."""

    def __init__(self, lec_log=b"", run_log=b""):
        self.lec_log = lec_log
        self.run_log = run_log

    def run(self, cmd, *, mounts=None, workdir=None, env=None,
            slot=None, timeout=None, log=None):
        out = None
        for hp, (cp, _mode) in (mounts or {}).items():
            if cp == equiv.CONTAINER_OUT:
                out = Path(hp)
        if self.lec_log:
            (out / "lec.log").write_bytes(self.lec_log)
        Path(log).write_bytes(self.run_log)
        return RunResult(rc=0, seconds=1.0)


def _lec_req(tmp_path, executor):
    (tmp_path / "a.v").write_text("module m; endmodule\n")
    (tmp_path / "b.v").write_text("module m; endmodule\n")
    arts = ArtifactSet()
    arts.put(ArtifactKind.NETLIST, tmp_path / "b.v")
    return MeasureRequest(
        artifacts=arts,
        golden={"gold_netlist": str(tmp_path / "a.v"),
                "librefs": {"libs": [], "host_root": str(tmp_path)}},
        params={"timeout": 1800}, outdir=tmp_path / "out",
        executor=executor, slot=None, timeout=1800)


def test_kepler_boundary_mismatch_detail(tmp_path):
    lec_log = (b"LEC boundary mismatch: design1 has 1383 boundary "
               b"inputs, design2 has 1383, and 1127 match by name.\n")
    run_log = b"boundary detail tail\n"
    m = KeplerFormalBackend().run(_lec_req(
        tmp_path, _KeplerExec(lec_log=lec_log, run_log=run_log)))
    assert m["lec_result"] == "boundary_mismatch"
    assert m["error"] == run_log[-400:].decode()


def test_kepler_error_detail_is_run_log_tail(tmp_path):
    run_log = bytes(range(256)) * 3
    m = KeplerFormalBackend().run(_lec_req(
        tmp_path, _KeplerExec(run_log=run_log)))
    assert m["lec_result"] == "error"
    assert m["error"] == run_log[-400:].decode(errors="replace")


class _TimedOutExec:
    """Executor stub: executor-level timeout."""

    def run(self, cmd, *, mounts=None, workdir=None, env=None,
            slot=None, timeout=None, log=None):
        Path(log).write_bytes(b"")
        return RunResult(rc=None, seconds=float(timeout or 0),
                         timed_out=True)


def test_kepler_timeout_detail(tmp_path):
    m = KeplerFormalBackend().run(_lec_req(tmp_path, _TimedOutExec()))
    assert m["lec_result"] == "timeout"
    assert m["error"] == "timeout after 1800s"


class FakeTier:
    def __init__(self, result):
        self.result = dict(result)
        self.calls = []

    def run(self, req):
        self.calls.append(req)
        return dict(self.result)


def _req(tmp_path, executor=None, top="design_name"):
    golden = {"gold_netlist": str(tmp_path / "a.v"),
              "top": top, "librefs": {"libs": [], "host_root": None},
              "reset": {"port": "reset", "active": 1, "cycles": 10},
              "clock_port": "clk"}
    return MeasureRequest(artifacts=None, golden=golden, params={},
                          outdir=tmp_path / "out", executor=executor,
                          slot=None, timeout=60)


def test_detected_top_passed_to_all_tiers(tmp_path, monkeypatch):
    """params['top'] (design name) must not reach the tiers — the
    detected gold top does."""
    monkeypatch.setattr(
        equiv, "_top_ports",
        lambda req, netlist, workdir: ("aes_cipher_top", {}))
    tiers = {
        "one_to_one": FakeTier({"lec_result": "different",
                                "lec_differences": 3}),
        "induction": FakeTier({"yosys_result": "not_proven"}),
        "bmc": FakeTier({"bmc_result": "no_counterexample"}),
        "cosim": FakeTier({"sim_result": "match", "sim_cycles": 200,
                           "sim_compared": 200, "sim_x_cycles": 0}),
    }
    EquivalenceBackend(tiers=tiers).compare(
        Path("a.v"), Path("b.v"), {}, _req(tmp_path, top="aes"))
    for name in ("induction", "bmc", "cosim"):
        assert tiers[name].calls[0].params["top"] == "aes_cipher_top"


def test_boundary_mismatch_falls_through_to_tier2(tmp_path, monkeypatch):
    monkeypatch.setattr(
        equiv, "_top_ports",
        lambda req, netlist, workdir: ("real_top", {}))
    tiers = {
        "one_to_one": FakeTier({"lec_result": "boundary_mismatch",
                                "error": "LEC boundary mismatch: ..."}),
        "induction": FakeTier({"yosys_result": "proven"}),
        "bmc": FakeTier({"bmc_result": "no_counterexample"}),
        "cosim": FakeTier({"sim_result": "match"}),
    }
    m = EquivalenceBackend(tiers=tiers).compare(
        Path("a.v"), Path("b.v"), {}, _req(tmp_path))
    assert m["equivalence_status"] == "proven_sequential"
    assert m["equivalence_tiers"][0]["result"] == "boundary_mismatch"
    assert m["equivalence_tiers"][0]["detail"]


def test_rename_module_hierarchical(tmp_path):
    src = tmp_path / "hier.v"
    src.write_text("module leaf; endmodule\nmodule top; endmodule\n")
    with pytest.raises(InfraError, match="hierarchical netlist"):
        _rename_module(src, tmp_path / "out.v", "revised")


def test_rename_module_single(tmp_path):
    src = tmp_path / "flat.v"
    src.write_text("module mytop(a); input a; endmodule\n")
    _rename_module(src, tmp_path / "out.v", "revised")
    assert "module revised(a)" in (tmp_path / "out.v").read_text()


def test_hierarchical_netlist_is_tier_error(tmp_path, monkeypatch):
    """A multi-module revised netlist surfaces as a tier error with
    detail 'hierarchical netlist'."""
    monkeypatch.setattr(
        equiv, "_top_ports",
        lambda req, netlist, workdir: ("real_top", {}))
    hier = tmp_path / "hier.v"
    hier.write_text("module leaf; endmodule\nmodule top; endmodule\n")

    class HierTier:
        def run(self, req):
            _rename_module(
                Path(req.artifacts.get(ArtifactKind.NETLIST)),
                Path(req.outdir) / "r.v", "revised")
            return {}

    tiers = {
        "one_to_one": FakeTier({"lec_result": "different"}),
        "induction": HierTier(),
    }
    m = EquivalenceBackend(tiers=tiers).compare(
        Path("a.v"), hier, {},
        _req(tmp_path), tiers=("one_to_one", "induction"))
    ind = next(t for t in m["equivalence_tiers"]
               if t["tier"] == "induction")
    assert ind["result"] == "error"
    assert "hierarchical netlist" in ind["detail"]


class _SimExec:
    """Executor stub for CosimBackend: answers _top_ports/gen_cells
    with real files, writes a canned sim.log for the bash command."""

    def __init__(self, sim_log="", timed_out=False, top="real_top"):
        self.sim_log = sim_log
        self.timed_out = timed_out
        self.top = top

    def run(self, cmd, *, mounts=None, workdir=None, env=None,
            slot=None, timeout=None, log=None):
        out = None
        for hp, (cp, _mode) in (mounts or {}).items():
            if cp == equiv.CONTAINER_OUT:
                out = Path(hp)
        if any(str(c).endswith("top.ys") for c in cmd):
            (out / "top.json").write_text(json.dumps({"modules": {
                self.top: {"attributes": {"top": "1"},
                           "ports": {"clk": {"direction": "input",
                                             "bits": [1]},
                                     "reset": {"direction": "input",
                                               "bits": [1]},
                                     "y": {"direction": "output",
                                           "bits": [1]}}}}}))
            return RunResult(rc=0, seconds=0.1)
        if any(str(c).endswith("gen.ys") for c in cmd):
            (out / "cells.v").write_text("")
            return RunResult(rc=0, seconds=0.1)
        if log:
            Path(log).write_text(self.sim_log)
        return RunResult(rc=0, seconds=float(timeout or 0),
                         timed_out=self.timed_out)


def _sim_req(tmp_path, executor):
    for name in ("a.v", "b.v"):
        (tmp_path / name).write_text(
            "module real_top(input clk, input reset, output y); "
            "assign y = clk & reset; endmodule\n")
    arts = ArtifactSet()
    arts.put(ArtifactKind.NETLIST, tmp_path / "b.v")
    return MeasureRequest(
        artifacts=arts,
        golden={"gold_netlist": str(tmp_path / "a.v"),
                "librefs": {"libs": [], "host_root": str(tmp_path)},
                "reset": {"port": "reset", "active": 1, "cycles": 2},
                "clock_port": "clk"},
        params={"sim_cycles": 200000, "sim_seconds": 600},
        outdir=tmp_path / "out", executor=executor, slot=None,
        timeout=600)


def test_cosim_timeout_with_progress_is_partial_match(tmp_path):
    log = ("PROGRESS cycle=1000 compared=900 xcycles=100\n"
           "PROGRESS cycle=12000 compared=11000 xcycles=1000\n")
    m = CosimBackend().run(_sim_req(
        tmp_path, _SimExec(sim_log=log, timed_out=True)))
    assert m["sim_result"] == "match"
    assert m["sim_partial"] is True
    assert m["sim_cycles"] == 12000
    assert m["sim_compared"] == 11000
    assert m["sim_x_cycles"] == 1000


def test_cosim_timeout_without_progress_stays_timeout(tmp_path):
    m = CosimBackend().run(_sim_req(
        tmp_path, _SimExec(sim_log="garbage\n", timed_out=True)))
    assert m["sim_result"] == "timeout"


def test_cosim_result_beats_timeout_flag(tmp_path):
    log = ("PROGRESS cycle=1000 compared=900 xcycles=100\n"
           "RESULT mismatch=2 cycle=1500 compared=1200 xcycles=150\n")
    m = CosimBackend().run(_sim_req(
        tmp_path, _SimExec(sim_log=log, timed_out=True)))
    assert m["sim_result"] == "mismatch"
    assert m["sim_cycles"] == 1500
    assert "sim_partial" not in m


class _FlatExec:
    """Executor stub for _prepare_revised: answers _top_ports and
    writes flat.v for the flat.ys run."""

    def run(self, cmd, *, mounts=None, workdir=None, env=None,
            slot=None, timeout=None, log=None):
        out = None
        for hp, (cp, _mode) in (mounts or {}).items():
            if cp == equiv.CONTAINER_OUT:
                out = Path(hp)
        if any(str(c).endswith("top.ys") for c in cmd):
            (out / "top.json").write_text(json.dumps({"modules": {
                "hier_top": {"attributes": {"top": "1"},
                             "ports": {}}}}))
        elif any(str(c).endswith("flat.ys") for c in cmd):
            (out / "flat.v").write_text(
                "module hier_top(a); input a; endmodule\n")
        return RunResult(rc=0, seconds=0.1)


def test_prepare_revised_flattens_hierarchical(tmp_path, monkeypatch):
    from guardci.backends.equivalence import _prepare_revised
    hier = tmp_path / "hier.v"
    hier.write_text("module leaf; endmodule\nmodule hier_top; "
                    "leaf l(); endmodule\n")
    req = MeasureRequest(artifacts=None, golden={}, params={},
                         outdir=tmp_path / "o", executor=_FlatExec(),
                         slot=None, timeout=60)
    out = _prepare_revised(req, hier, tmp_path / "c.v",
                           tmp_path / "e.v", tmp_path / "o")
    text = out.read_text()
    assert "module revised(a)" in text
    # top detected with yosys, flattened, then renamed by name
    assert (tmp_path / "o/.flat/flat.ys").exists()
