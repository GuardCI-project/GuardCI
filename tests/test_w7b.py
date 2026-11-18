"""W7b — every signoff measurement runs at the golden constraint
period (§11.3): the contract bound is a judgment input, never a
measurement input, and job params no longer carry contract values."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import test_pipeline as tp
import test_w1_checkpoint as w1
import yaml
from test_pipeline import (
    CONTRACT,
    FakeSignoff,
    _pipe_with,
    commit,
    drain,
    metrics_json,
    node,
    store,
)

from guardci.core import verify as verify_mod
from guardci.core.project import Project
from guardci.core.scheduler import _measurements_for
from guardci.executors.base import InfraError
from guardci.flows import base as flows_base

BIN = Path(sys.executable).parent

GOLDEN = 0.5          # FakeFlow.golden_constraints()["golden_period"]

# a contract whose signoff-needing clauses carry contract params that
# must never reach the measurement (the W7a bug's condition)
STA_CONTRACT = dict(CONTRACT)
STA_CONTRACT["clauses"] = list(CONTRACT["clauses"]) + [
    {"check": "sta_setup", "severity": "hard",
     "max_period": 0.45},
]


def _signoff_params_seen(monkeypatch):
    """Wrap FakeSignoff.run, recording every request's params."""
    seen = []
    orig = FakeSignoff.run

    def spy(self, req):
        seen.append(dict(req.params or {}))
        return orig(self, req)

    monkeypatch.setattr(FakeSignoff, "run", spy)
    return seen


def _signoff_job_params(proj, sha):
    return [j["params"].get("signoff")
            for j in store(proj).jobs_for(sha)
            if "signoff" in j.get("params", {})]


def test_signoff_params_helper():
    assert verify_mod._signoff_params({}, {"golden_period": 0.5}) == {
        "period": 0.5}
    assert verify_mod._signoff_params(
        {"period": 9.0, "timeout": 5},
        {"golden_period": 0.5}) == {"period": 0.5, "timeout": 5}
    with pytest.raises(InfraError, match="golden period"):
        verify_mod._signoff_params({}, {})
    with pytest.raises(InfraError, match="golden period"):
        verify_mod._signoff_params({}, {"golden_period": "x"})


def test_measurements_for_no_contract_leak(tmp_path):
    """The queued signoff params contain no contract-derived values,
    whether or not baseline metrics exist yet (the W7a condition)."""
    proj, _work = _pipe_with(tmp_path, STA_CONTRACT)
    p = Project(proj)
    try:
        clauses = [c for c in p.contract.clauses
                   if c.check in ("fake_heavy", "sta_setup")]
        needs, _ = _measurements_for(p, clauses, sha=None)
        assert needs.get("signoff") == {}
    finally:
        p.close()


def test_rebuild_signoff_at_golden_period(tmp_path, monkeypatch):
    """Rebuild path: job params carry no contract values; the backend
    receives golden_period — before and after baseline metrics exist."""
    seen = _signoff_params_seen(monkeypatch)
    proj, work = _pipe_with(tmp_path, STA_CONTRACT)
    sha = commit(work, {"config.mk": "X = 2\n", "heavy_x.txt": "x\n",
                        "metrics.json": metrics_json(
                            "n1", signoff={"min_period": 0.4})}, "n1")
    drain(proj, work)
    for params in _signoff_job_params(proj, sha):
        assert "period" not in params
        assert "max_period" not in params
        assert "min_wns" not in params
    # every signoff call (baseline's own + the node's) ran at golden
    assert seen and all(p.get("period") == GOLDEN for p in seen)
    n = node(proj, sha)
    assert (n.metrics or {}).get("clock_period") == GOLDEN


def test_artifact_signoff_at_golden_period(tmp_path, monkeypatch):
    """Checkpoint (artifact) path: same golden period, same stripped
    params."""
    seen = _signoff_params_seen(monkeypatch)
    contract = dict(w1.ACONTRACT)
    contract["clauses"] = list(w1.ACONTRACT["clauses"]) + [
        {"check": "sta_setup", "severity": "hard",
         "max_period": 0.45},
    ]
    proj, work = w1._wpipe(tmp_path, contract)
    sha = commit(work, {"config.mk": "X = 3\n"}, "a1")
    for s in w1.WFlow.STAGES:
        extra = {"signoff_metrics": tp._signoff(
                     min_period=0.4, clock_period=GOLDEN)} \
            if s == w1.WFlow.STAGES[-1] else {}
        w1._write_artifact(
            work, s, stage_facts={**w1._facts(), "min_period": 0.4,
                                  "clock_period": GOLDEN}, **extra)
    drain(proj, work)
    for params in _signoff_job_params(proj, sha):
        assert "period" not in params
        assert "max_period" not in params
    assert seen and all(p.get("period") == GOLDEN
                        and "max_period" not in p for p in seen)
    n = node(proj, sha)
    assert (n.metrics or {}).get("clock_period") == GOLDEN


def test_rejudge_under_new_bound_uses_no_measurement(tmp_path):
    """Changing the contract bound and rejudging needs no new
    measurement and changes no fact — only the verdict."""
    contract = dict(CONTRACT)
    contract["clauses"] = list(CONTRACT["clauses"]) + [
        {"check": "sta_setup", "severity": "hard",
         "max_period": 0.45},
    ]
    proj, work = _pipe_with(tmp_path, contract)
    sha = commit(work, {"config.mk": "X = 4\n", "heavy_x.txt": "x\n",
                        "metrics.json": metrics_json(
                            "n2", signoff={"min_period": 0.4})}, "n2")
    drain(proj, work)
    before = node(proj, sha)
    before_metrics = dict(before.metrics or {})

    # tighten the bound: 0.4 <= 0.45 held, 0.4 <= 0.3 fails
    contract_b = dict(contract)
    contract_b["clauses"] = [
        {**c, "max_period": 0.3}
        if c["check"] == "sta_setup" else c
        for c in contract["clauses"]]
    cb = tmp_path / "contract-b.yaml"
    cb.write_text(yaml.safe_dump(contract_b))
    # same as `guardci rejudge --contract`: replace the contract copy,
    # then re-decide in-process (the fake checkers only exist here)
    import shutil

    from guardci.core.rejudge import rejudge as core_rejudge
    p = Project(proj)
    try:
        shutil.copyfile(cb, Path(p.root) / p.cfg["contract"])
        p._contract = None
        core_rejudge(p)
    finally:
        p.close()

    after = node(proj, sha)
    facts = {k: v for k, v in (after.metrics or {}).items()
             if not k.startswith("_")}
    assert facts == {k: v for k, v in before_metrics.items()
                     if not k.startswith("_")}
    # rejudging under the new bound ran no measurement — across all
    # attempts the signoff measurement ran exactly once
    st = store(proj)
    fresh = [r for r in st.check_runs(sha, all_attempts=True)
             if r.checker == "measure:signoff"
             and (r.metrics or {}).get("memo_hit") is not True]
    assert len(fresh) == 1
    vs = {v.clause.name: v.status
          for v in store(proj).verdicts(sha)}
    assert vs["sta_setup"] == "fail"


class NoGoldenPeriodFlow(tp.FakeFlow):
    name = "no_golden_period"

    def golden_constraints(self, repo, baseline_sha):
        c = dict(super().golden_constraints(repo, baseline_sha))
        c.pop("golden_period")
        return c


flows_base.register(NoGoldenPeriodFlow)


def test_missing_golden_period_is_an_error(tmp_path):
    """No golden period → job error naming the cause, never a silent
    measurement at the search period."""
    proj, work = _pipe_with(tmp_path, STA_CONTRACT)
    cfg = yaml.safe_load((proj / "guardci.yaml").read_text())
    cfg["flow"] = "no_golden_period"
    (proj / "guardci.yaml").write_text(yaml.safe_dump(cfg))
    sha = commit(work, {"config.mk": "X = 5\n"}, "n3")
    drain(proj, work)
    jobs = store(proj).jobs_for(sha)
    assert jobs and jobs[-1]["state"] == "error"
    assert "golden period" in (jobs[-1].get("error") or "")
    # the node got no signoff facts from a fallback period
    n = node(proj, sha)
    assert (n.metrics or {}).get("hold_wns") is None
