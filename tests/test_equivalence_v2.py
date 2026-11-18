"""§11.5 V2: composite equivalence backend + fail-fast job chain.
Uses fake backends; real-tool qualification is documented in docs/VALIDATOR.md."""

from pathlib import Path
from types import SimpleNamespace

import pytest

import guardci.backends.equivalence as equiv
from guardci.backends.base import MeasureRequest
from guardci.backends.equivalence import EquivalenceBackend


@pytest.fixture(autouse=True)
def _fake_top(monkeypatch):
    """compare() resolves the gold top via _top_ports; stub it so the
    fake-tier tests need no docker/yosys."""
    monkeypatch.setattr(equiv, "_top_ports",
                        lambda req, netlist, workdir: ("real_top", {}))


class FakeTier:
    """A tier backend stub: .run(req) returns a canned dict."""

    def __init__(self, result):
        self.result = dict(result)
        self.calls = []

    def run(self, req):
        self.calls.append(req)
        return dict(self.result)


def _req(tmp_path, reset=True):
    golden = {"gold_netlist": str(tmp_path / "a.v"),
              "top": "gcd", "librefs": {"libs": [], "host_root": None}}
    if reset:
        golden["reset"] = {"port": "reset", "active": 1, "cycles": 10}
        golden["clock_port"] = "clk"
    return MeasureRequest(artifacts=None, golden=golden,
                          params={}, outdir=tmp_path / "out",
                          executor=None, slot=None, timeout=60)


def _backend(tiers):
    return EquivalenceBackend(tiers=tiers)


def _lecs(result="identical", diffs=0):
    return FakeTier({"lec_result": result, "lec_differences": diffs})


def _induct(result="not_proven"):
    return FakeTier({"yosys_result": result})


def _bmc(result="no_counterexample"):
    return FakeTier({"bmc_result": result, "bmc_depth": 20})


def _cosim(result="match", compared=200000):
    return FakeTier({"sim_result": result, "sim_cycles": 200000,
                     "sim_compared": compared, "sim_x_cycles": 0,
                     "seed": 1})


def _four(lec=None, ind=None, bmc=None, sim=None):
    return {"one_to_one": lec or _lecs("different", 49),
            "induction": ind or _induct(),
            "bmc": bmc or _bmc(),
            "cosim": sim or _cosim()}


@pytest.mark.parametrize("log,rc,expected", [
    ("Executing equiv_status -assert\n", 0, "not_proven"),
    ("Executing equiv_status -assert\n", 1, "error"),
    ("Equivalence successfully proven!\n", 0, "proven"),
    ("Equivalence successfully proven!\n", 1, "error"),
    ("Equivalence successfully proven!\nERROR: late failure", 0, "error"),
    ("Found 2 unproven $equiv cells\n", 0, "not_proven"),
    ("ERROR: Found 2 unproven $equiv cells\n", 1, "not_proven"),
])
def test_induction_requires_explicit_success(tmp_path, log, rc, expected):
    class Executor:
        def run(self, *args, **kwargs):
            kwargs["log"].write_text(log)
            return SimpleNamespace(rc=rc, timed_out=False)

    req = _req(tmp_path)
    req.executor = Executor()
    backend = equiv.YosysEquivBackend()
    result = backend._induct(
        req, Path("a.v"), Path("b.v"), Path("rev.v"),
        Path("cells.v"), Path("extra.v"), "top", tmp_path)
    assert result["yosys_result"] == expected


def test_bmc_lowers_async_cells_without_forcing_initial_state(tmp_path):
    class Executor:
        def run(self, *args, **kwargs):
            kwargs["log"].write_text("SAT proof finished - no model found: SUCCESS!")
            return SimpleNamespace(rc=0, timed_out=False)

    req = _req(tmp_path)
    req.executor = Executor()
    result = equiv.YosysEquivBackend()._bmc(
        req, Path("a.v"), Path("b.v"), Path("rev.v"),
        Path("cells.v"), Path("extra.v"), "top", tmp_path)
    script = (tmp_path / "miter.ys").read_text()
    assert "async2sync" in script.splitlines()
    assert "dffunmap" in script.splitlines()
    assert "-set-init-zero" not in script
    assert "-set-at 1 in_reset 1" in script
    assert "-set-at 11 in_reset 0" in script
    assert result["bmc_result"] == "no_counterexample"


def test_tier1_identical_is_proven(tmp_path):
    tiers = _four(lec=_lecs("identical"))
    m = _backend(tiers).compare(Path("a.v"), Path("b.v"), {},
                              _req(tmp_path))
    assert m["equivalence_status"] == "proven"
    assert m["lec_result"] == "identical"
    assert [t["tier"] for t in m["equivalence_tiers"]] == ["one_to_one"]
    assert tiers["induction"].calls == [] and tiers["cosim"].calls == []


def test_induction_proven_is_proven_sequential(tmp_path):
    m = _backend(_four(ind=_induct("proven"))).compare(
        Path("a.v"), Path("b.v"), {}, _req(tmp_path))
    assert m["equivalence_status"] == "proven_sequential"
    assert [t["tier"] for t in m["equivalence_tiers"]] == [
        "one_to_one", "induction"]


def test_cosim_match_enough_cycles_is_bounded(tmp_path):
    m = _backend(_four()).compare(Path("a.v"), Path("b.v"), {},
                                 _req(tmp_path))
    assert m["equivalence_status"] == "bounded"
    assert m["sim_compared"] == 200000


def test_cosim_mismatch_is_refuted(tmp_path):
    sim = _cosim("mismatch")
    sim.result["counterexample"] = "/tmp/cex.json"
    m = _backend(_four(sim=sim)).compare(Path("a.v"), Path("b.v"), {},
                                         _req(tmp_path))
    assert m["equivalence_status"] == "refuted"
    assert m["counterexample"] == "/tmp/cex.json"


def test_bmc_counterexample_alone_does_not_refute(tmp_path):
    """§11.5: a bounded-check counterexample is only a hint — it must
    be reproduced by co-simulation. Clean sim → bounded, not refuted."""
    m = _backend(_four(bmc=_bmc("counterexample"))).compare(
        Path("a.v"), Path("b.v"), {}, _req(tmp_path))
    assert m["bmc_result"] == "counterexample"
    assert m["equivalence_status"] == "bounded"


def test_too_few_comparable_cycles_is_unknown(tmp_path):
    m = _backend(_four(sim=_cosim("match", compared=500))).compare(
        Path("a.v"), Path("b.v"), {}, _req(tmp_path))
    assert m["equivalence_status"] == "unknown"


def test_no_reset_skips_cosim_to_unknown(tmp_path):
    tiers = _four()
    m = _backend(tiers).compare(Path("a.v"), Path("b.v"), {},
                                _req(tmp_path, reset=False))
    assert m["equivalence_status"] == "unknown"
    assert tiers["cosim"].calls == []          # tier skipped entirely
    assert m["equivalence_tiers"][-1]["result"] == "skipped"


def test_tier1_timeout_still_runs_later_tiers(tmp_path):
    """A tier that times out/errors is recorded and the next runs."""
    tiers = _four(lec=_lecs("timeout"), ind=_induct("proven"))
    m = _backend(tiers).compare(Path("a.v"), Path("b.v"), {},
                                _req(tmp_path))
    assert m["lec_result"] == "timeout"
    assert m["equivalence_status"] == "proven_sequential"


def test_tier_infra_error_recorded_next_runs(tmp_path):
    class Boom(FakeTier):
        def run(self, req):
            from guardci.executors.base import InfraError
            raise InfraError("boom")

    tiers = _four(ind=Boom({}))
    m = _backend(tiers).compare(Path("a.v"), Path("b.v"), {},
                                _req(tmp_path))
    ind = [t for t in m["equivalence_tiers"] if t["tier"] == "induction"]
    assert ind[0]["result"] == "error"
    assert m["equivalence_status"] == "bounded"


def test_tiers_param_restricts_to_one_to_one(tmp_path):
    """The job's synth≡final step runs tier 1 only."""
    tiers = _four(lec=_lecs("identical"))
    m = _backend(tiers).compare(Path("a.v"), Path("b.v"), {},
                                _req(tmp_path), tiers=("one_to_one",))
    assert [t["tier"] for t in m["equivalence_tiers"]] == ["one_to_one"]
    assert tiers["bmc"].calls == [] and tiers["cosim"].calls == []


# ------------------------------------------------- verify.py digest memo

def test_compare_memoized_by_netlist_digests(tmp_path):
    """Two compares over byte-identical netlists → one backend run."""
    from guardci.core import verify as V

    class FakeCache:
        def results_dir(self):
            d = tmp_path / "results"
            d.mkdir(exist_ok=True)
            return d

    class Counting(EquivalenceBackend):
        def __init__(self):
            super().__init__(tiers=_four(lec=_lecs("identical")))
            self.compare_calls = 0

        def compare(self, a, b, params, req, tiers=None):
            self.compare_calls += 1
            return super().compare(a, b, params, req, tiers=tiers)

    a = tmp_path / "gold.v"
    a.write_text("module gold; endmodule\n")
    b = tmp_path / "rev.v"
    b.write_text("module rev; endmodule\n")
    be = Counting()
    view = type("V", (), {"cache": FakeCache(), "executor": None,
                          "backends": {"lec": be}})()
    golden = {"librefs": {}, "reset": {"port": "reset", "active": 1}}
    V._compare_memoized(view, be, a, b, {},
                        tmp_path / "o1", golden, None)
    info2 = V._compare_memoized(view, be, a, b, {},
                                tmp_path / "o2", golden, None)
    assert be.compare_calls == 1
    assert info2["memo_hit"] is True
    # different content → different digest → new compare
    b.write_text("module rev2; endmodule\n")
    V._compare_memoized(view, be, a, b, {}, tmp_path / "o3", golden, None)
    assert be.compare_calls == 2
    # tier-scoped memo: same pair, different tier set → no false hit
    V._compare_memoized(view, be, a, b, {}, tmp_path / "o4", golden,
                        None, tiers=("one_to_one",))
    assert be.compare_calls == 3
