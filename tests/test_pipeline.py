"""M2 pipeline tests (§9): fake flow + fake executor + fake backends +
fake checkers driving the REAL Scheduler/verify/builder on a real bare
git repo. No docker, no EDA tools.

The fake flow has stages a→b→c (light_stage=a). `metrics.json` in a
commit decides the measurements the fake backends return. `config.mk`
is the recipe file: only non-comment content enters the stage keys, so
a comment-only edit shares its parent's keys.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import ClassVar

import pytest
import yaml

from guardci.artifacts.model import ArtifactKind
from guardci.backends import base as backends_base
from guardci.backends.base import Backend
from guardci.checks import base as checks_base
from guardci.checks.base import Checker, CheckResult
from guardci.core.diff import read_file
from guardci.core.models import Cost, State
from guardci.core.project import Project
from guardci.core.scheduler import Scheduler
from guardci.core.store import Store
from guardci.executors import base as executors_base
from guardci.executors.base import RunResult, Slot
from guardci.flows import base as flows_base

BIN = Path(sys.executable).parent


# ---------------------------------------------------------------- fakes

def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, check=True,
                          text=True).stdout


def _norm(text: str | None) -> str:
    """Content minus comment lines — a comment-only edit normalizes
    away and shares the stage key."""
    return "\n".join(l for l in (text or "").splitlines()
                     if not l.strip().startswith("#"))


class FakeFlow:
    """3-stage fake flow (light_stage=a)."""

    name = "fake"
    light_stage = "a"
    STAGES: ClassVar[list] = ["a", "b", "c"]

    # test knobs (class-level so the Project-instantiated adapter shares
    # them with the test)
    ran: ClassVar[list] = []       # (sha, stage) of real stage runs
    fail_at: str | None = None
    infra_at: str | None = None
    exports: ClassVar[list] = []   # (kind, odb) recorded by export()

    @classmethod
    def reset(cls, fail_at=None, infra_at=None):
        cls.ran, cls.exports = [], []
        cls.fail_at, cls.infra_at = fail_at, infra_at

    # -- §6 classify -----------------------------------------------------

    def classify_delta(self, diff) -> set:
        tags = set()
        changed = False
        for e in diff.entries:
            if (e.kind in ("added", "deleted", "renamed")
                    or _norm(e.before) != _norm(e.after)):
                changed = True
            if e.path.startswith("heavy"):
                tags.add("scripts")
        if changed and "scripts" not in tags:
            tags.add("synthesis")
        return tags

    # -- §9.10 -------------------------------------------------------------

    def stages(self):
        return list(self.STAGES)

    def _tree_fingerprint(self, repo, sha) -> str:
        paths = _git(repo, "ls-tree", "-r", "--name-only",
                     sha).splitlines()
        h = hashlib.sha256()
        for p in sorted(paths):
            h.update(p.encode())
            h.update(b"\0")
            h.update(_norm(read_file(repo, sha, p)).encode())
            h.update(b"\0")
        return h.hexdigest()

    def stage_keys(self, repo, sha, baseline_sha, image_id):
        fp = self._tree_fingerprint(repo, sha)
        return [(s, hashlib.sha256(
            f"{image_id}|{s}|{fp}".encode()).hexdigest())
                for s in self.STAGES]

    def materialize(self, repo, sha, baseline_sha, protected, workdir):
        workdir = Path(workdir)
        arc = subprocess.run(["git", "-C", repo, "archive", sha],
                             capture_output=True, check=True)
        with tarfile.open(fileobj=io.BytesIO(arc.stdout),
                          mode="r:") as tf:
            tf.extractall(workdir)
        (workdir / ".guardci-sha").write_text(sha)

    def run_stage(self, workdir, stage, executor, slot, timeout, log):
        sha = (Path(workdir) / ".guardci-sha").read_text().strip()
        self.ran.append((sha, stage))
        Path(log).write_text(f"{stage} log")
        if stage == self.infra_at:
            return RunResult(rc=125, seconds=0.01)
        if stage == self.fail_at:
            return RunResult(rc=2, seconds=0.01)
        (Path(workdir) / "out").mkdir(exist_ok=True)
        (Path(workdir) / "out" / stage).write_text(stage)
        # stamp the stage into metrics.json so light-stage vs final
        # exports differ byte-wise (real netlists differ per stage);
        # still valid JSON for the fake backends
        mj = Path(workdir) / "metrics.json"
        if mj.exists():
            data = json.loads(mj.read_text())
            data["stage"] = stage
            mj.write_text(json.dumps(data, indent=2))
        return RunResult(rc=0, seconds=0.01)

    def snapshot(self, workdir, stage, dest, prev=None):
        src = Path(workdir) / "out"
        if src.is_dir():
            shutil.copytree(src, dest / "out")

    def restore(self, src, workdir):
        if (Path(src) / "out").is_dir():
            shutil.copytree(Path(src) / "out", Path(workdir) / "out",
                            dirs_exist_ok=True)

    def export(self, workdir, kind, dest, executor, slot,
               odb=None, golden=None, timeout=None):
        self.exports.append((kind, odb))
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(workdir) / "metrics.json", dest)
        return dest

    def export_stream(self, layout, golden, dest, executor, slot):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(layout, dest)
        return dest

    def golden(self, repo, baseline_sha, protected, dest, executor,
               slot):
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "tree").mkdir(exist_ok=True)
        librefs = {"lefs": ["sc.lef"], "libs": ["sc.lib"]}
        (dest / "librefs.json").write_text(json.dumps(librefs))
        return librefs

    def golden_constraints(self, repo, baseline_sha):
        return {"template": "create_clock -period $::guardci_period clk",
                "golden_period": 0.5,
                "main_clock": {"name": "clk", "sources": ["clk"]}}


class FakeExecutor:
    type = "fake"

    def __init__(self, image="img-1", slots=1, **_kw):
        self.image = image
        self.nslots = int(slots)

    def slots(self):
        return self.nslots

    def acquire(self, poll_seconds=5.0):
        return Slot(cpus=[0])

    def try_acquire(self):
        return Slot(cpus=[0])

    def run(self, cmd, *, mounts=None, workdir=None, env=None,
            slot=None, timeout=None, log=None):
        raise AssertionError("fake executor must not run commands")

    def image_id(self):
        return self.image


class _JsonBackend(Backend):
    """Reads the fake-flow-exported metrics.json and returns the
    ``<name>_metrics`` section. ``OVERRIDE`` (class var) replaces the
    result wholesale (used to flip metrics between attempts)."""

    OVERRIDE: ClassVar = None
    calls: ClassVar[list] = []

    def run(self, req):
        data = json.loads(
            req.artifacts.get(ArtifactKind.LAYOUT).read_text())
        self.calls.append(data.get("id"))
        if self.OVERRIDE is not None:
            return dict(self.OVERRIDE)
        return dict(data.get(f"{self.provides}_metrics") or {})


class FakeSignoff(_JsonBackend):
    provides = "signoff"
    tool = "fsign"
    calls: ClassVar[list] = []


class FakeDrc(Backend):
    provides = "drc"
    tool = "fdrc"
    calls: ClassVar[list] = []

    def run(self, req):
        data = json.loads(
            req.artifacts.get(ArtifactKind.STREAM).read_text())
        self.calls.append(data.get("id"))
        return dict(data.get("drc_metrics") or {"drc_count": 0})


class FakeLec(Backend):
    provides = "lec"
    tool = "flec"
    calls: ClassVar[list] = []

    def run(self, req):
        data = json.loads(
            req.artifacts.get(ArtifactKind.NETLIST).read_text())
        self.calls.append(data.get("id"))
        return dict(data.get("lec_metrics")
                    or {"lec_result": "identical"})


class FakeStatic(Checker):
    name = "fake_static"
    cost = Cost.STATIC

    def run(self, ctx, params, backend):
        return CheckResult(status="pass", detail="static ok")


class FakeLight(Checker):
    name = "fake_light"
    cost = Cost.LIGHT
    needs = "lec"

    def run(self, ctx, params, backend):
        if ctx["metrics"].get("lec_result") != "identical":
            return CheckResult(status="fail", detail="lec not identical")
        return CheckResult(status="pass", detail="lec identical")


class FakeHeavy(Checker):
    name = "fake_heavy"
    cost = Cost.HEAVY
    needs = "signoff"

    def run(self, ctx, params, backend):
        wns = ctx["metrics"].get("setup_wns")
        if wns is None:
            return CheckResult(status="error",
                               detail="setup_wns missing")
        if wns < params.get("min_wns", 0):
            return CheckResult(status="fail",
                               detail=f"setup_wns {wns} < 0")
        return CheckResult(status="pass", detail="setup ok")


class FakeWarn(FakeHeavy):
    name = "fake_warn"

    def run(self, ctx, params, backend):
        if ctx["metrics"].get("warn_me"):
            return CheckResult(status="fail", detail="warned by test")
        return CheckResult(status="pass", detail="warn ok")


class FakeErr(FakeHeavy):
    name = "fake_err"

    def run(self, ctx, params, backend):
        if ctx["metrics"].get("want_error"):
            return CheckResult(status="error", detail="checker error")
        return super().run(ctx, params, backend)


for _cls in (FakeFlow,):
    flows_base.register(_cls)
for _cls in (FakeExecutor,):
    executors_base.register(_cls)
class FakeEquiv(Backend):
    """Composite `equivalence` stand-in for the §11.5 job chain
    (fail-fast + synth/final one-to-one + gold-vs-final fallback).
    ``tool`` is ``fequiv`` so the ``lec: fequiv`` binding resolves;
    `_is_composite` recognizes it by the ``compare`` method."""
    provides = "lec"
    tool = "fequiv"
    version = "fake.1"
    memo_params: ClassVar[set] = {"timeout"}
    calls: ClassVar[list] = []          # (a.name, b.name, tiers)
    SYNTH_STATUS: ClassVar[str] = "proven"
    FINAL_T1: ClassVar[str] = "identical"
    FINAL_STATUS: ClassVar[str] = "proven"

    def compare(self, a, b, params, req, tiers=None):
        from pathlib import Path as _P
        a_name, b_name = _P(a).name, _P(b).name
        self.calls.append((a_name, b_name, tiers))
        if b_name == "netlist.v":               # baseline self-check
            st = "proven"
        elif tiers == ("one_to_one",):          # synth ≡ final
            return {"lec_result": self.FINAL_T1, "lec_differences": 0}
        elif b_name == "synth_netlist.v":       # gold ≡ synth
            st = self.SYNTH_STATUS
        else:                                   # gold ≡ final
            st = self.FINAL_STATUS
        return {"equivalence_status": st,
                "lec_result": ("identical" if st in
                               ("proven", "proven_sequential")
                               else "different"),
                "lec_differences": 0, "equivalence_tiers": [],
                "seed": 1, "bmc_depth": 20, "equivalence_seconds": 0.01}


for _cls in (FakeSignoff, FakeDrc, FakeLec, FakeEquiv):
    backends_base.register(_cls)
for _cls in (FakeStatic, FakeLight, FakeHeavy, FakeWarn, FakeErr):
    checks_base.register(_cls)


# ------------------------------------------------------------ fixture

CONTRACT = {
    "name": "fake",
    "protected": ["prot/**"],
    "recipe": ["**"],
    "clauses": [
        {"check": "fake_static", "severity": "hard"},
        {"check": "fake_light", "severity": "hard"},
        {"check": "fake_heavy", "severity": "hard", "min_wns": 0},
        {"check": "fake_warn", "severity": "warn"},
        {"check": "fake_err", "severity": "hard"},
    ],
    "verification": {"mode": "routed"},
    "routing": {
        "synthesis": ["fake_light"],
        "scripts": ["heavy"],
    },
    "edges": [],
}

BASE_METRICS = {
    "id": "baseline",
    "signoff_metrics": {
        "clock_period": 0.5, "setup_wns": 0.1, "setup_tns": 0.0,
        "hold_wns": 0.05, "hold_tns": 0.0,
        "slew_violations": 1, "cap_violations": 0,
        "fanout_violations": 0,
        "cells_by_type": {"tapcell": {"count": 10},
                          "endcap": {"count": 4}},
        "pdn_wires_by_layer": {"metal1": 100},
        "special_nets": ["VDD"],
        "ir_drop": {"VDD": {"nominal": 1.1, "drop": 0.05}},
        "power_total": 1.0, "cell_area": 100.0,
    },
    "drc_metrics": {"drc_count": 0, "drc_categories": {}},
    "lec_metrics": {"lec_result": "identical"},
}


def _signoff(**over):
    m = dict(BASE_METRICS["signoff_metrics"])
    m.update(over)
    return m


def metrics_json(node_id, signoff=None, drc=None, lec=None, extra=None):
    data = {"id": node_id,
            "signoff_metrics": _signoff(**(signoff or {})),
            "drc_metrics": drc or {"drc_count": 0},
            "lec_metrics": lec or {"lec_result": "identical"}}
    data.update(extra or {})
    return json.dumps(data, indent=2)


def guardci(*args):
    return subprocess.run([str(BIN / "guardci"), *args],
                          capture_output=True, text=True, check=True)


def _pipe_with(tmp_path, contract_dict):
    contract_dict = dict(contract_dict)
    lec_tool = contract_dict.pop("lec_tool", "flec")   # fixture-only
    src = tmp_path / "src"
    src.mkdir()
    (src / "config.mk").write_text("X = 1\n")
    (src / "metrics.json").write_text(metrics_json("baseline"))

    contract = tmp_path / "contract.yaml"
    contract.write_text(yaml.safe_dump(contract_dict))

    proj = tmp_path / "proj"
    guardci("init", str(proj), "--contract", str(contract),
            "--flow", "fake", "--from", str(src),
            "--cache", str(tmp_path / "cache"),
            "--cache-max-gb", "1")

    cfg = yaml.safe_load((proj / "guardci.yaml").read_text())
    cfg["executor"] = {"type": "fake", "image": "img-1", "slots": 1}
    cfg["backends"] = {"signoff": "fsign", "drc": "fdrc",
                       "lec": lec_tool}
    cfg["t_leaf_seconds"] = 0
    (proj / "guardci.yaml").write_text(yaml.safe_dump(cfg))

    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(proj / "remote.git"),
                    str(work)], check=True, capture_output=True)
    FakeFlow.reset()
    FakeEquiv.calls = []
    FakeEquiv.SYNTH_STATUS, FakeEquiv.FINAL_T1 = "proven", "identical"
    FakeEquiv.FINAL_STATUS = "proven"
    FakeSignoff.calls, FakeDrc.calls, FakeLec.calls = [], [], []
    _JsonBackend.OVERRIDE = None
    return proj, work


@pytest.fixture
def pipe(tmp_path):
    """init project + clone; returns (project_dir, clone_dir)."""
    return _pipe_with(tmp_path, CONTRACT)


@pytest.fixture
def pipe_info_err(tmp_path):
    """pipe() but the erroring heavy checker runs at severity=info."""
    contract = dict(CONTRACT)
    contract["clauses"] = [
        {**c, "severity": "info"} if c["check"] == "fake_err" else c
        for c in CONTRACT["clauses"]]
    return _pipe_with(tmp_path, contract)


def commit(work, files: dict, msg="c"):
    for p, text in files.items():
        f = Path(work) / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    _git(work, "add", "-A")
    _git(work, "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-qm", msg)
    return _git(work, "rev-parse", "HEAD").strip()


def push(work, ref="HEAD:main"):
    return _git(work, "push", "origin", ref)


def serve(proj, idle_heavy=False):
    p = Project(proj)
    try:
        Scheduler(p, idle_heavy=idle_heavy).run(once=True,
                                              interval=0.05)
    finally:
        p.close()


def node(proj, sha):
    p = Project(proj)
    try:
        return p.store.get_node(sha)
    finally:
        p.close()


def store(proj):
    return Store(proj / "state.db")


def baseline_sha(proj):
    p = Project(proj)
    try:
        return p.store.baselines()[0]["root_sha"]
    finally:
        p.close()


def drain(proj, work):
    """Push everything and serve until done."""
    push(work)
    # §12.2: capture happens at ingest (post-receive hook).  The hook
    # runs in a subprocess where the test's fake flow/backends are not
    # registered, so its capture attempt fails harmlessly; mimic it
    # here for the pushed tip.
    try:
        from guardci.core.checkpoint import capture_at_ingest
        from guardci.core.project import Project
        p = Project(proj)
        try:
            capture_at_ingest(p, _git(work, "rev-parse", "HEAD").strip())
        finally:
            p.close()
    except Exception:  # noqa: BLE001,S110 — tests without a
        pass             # recorder/flow legitimately skip capture
    serve(proj)


# ------------------------------------------------------------- tests

def test_light_only_node(pipe, tmp_path):
    proj, work = pipe
    sha = commit(work, {"config.mk": "X = 2\n"}, "light")
    drain(proj, work)
    n = node(proj, sha)
    assert n.state == State.VERIFIED
    assert n.metrics.get("lec_result") == "identical"
    assert "setup_wns" not in n.metrics           # no signoff metrics
    st = store(proj)
    try:
        job = st.jobs_for(sha)[0]
        assert job["upto_stage"] == "a"           # depth = synth
        assert job["measurements"] == ["lec"]
        names = {r.checker for r in st.check_runs(sha)}
        assert "measure:lec" in names \
            and "measure:signoff" not in names
        assert ("build" in {v.clause.check for v in st.verdicts(sha)})
    finally:
        st.close()
    # LEC ran on the synth netlist
    assert (ArtifactKind.NETLIST, "1_synth.odb") in FakeFlow.exports


def test_heavy_node(pipe, tmp_path):
    proj, work = pipe
    sha = commit(work, {"heavy_x.txt": "x\n",
                        "config.mk": "X = 9\n"}, "heavy")
    drain(proj, work)
    n = node(proj, sha)
    assert n.state == State.VERIFIED
    assert n.metrics["setup_wns"] == 0.1
    st = store(proj)
    try:
        job = st.jobs_for(sha)[0]
        assert job["upto_stage"] == "c"           # last stage
        assert set(job["measurements"]) == {"lec", "signoff"}
        names = {r.checker for r in st.check_runs(sha)}
        assert {"measure:lec", "measure:signoff",
                "edges", "build"} <= names
        rows = [r for r in st.check_runs(sha) if r.checker == "edges"]
        assert rows and "rows" in rows[0].metrics
    finally:
        st.close()


def test_heavy_implies_light_lec_on_final(pipe):
    proj, work = pipe
    sha = commit(work, {"heavy_x.txt": "x\n"}, "heavy")
    drain(proj, work)
    # heavy implies light: lec ran on the final netlist
    assert (ArtifactKind.NETLIST, "6_final.odb") in FakeFlow.exports
    assert node(proj, sha).state == State.VERIFIED


def test_build_failure(pipe):
    proj, work = pipe
    # populate the baseline golden dir first so fail_at hits the node
    commit(work, {"config.mk": "X = 4\n"}, "warmup")
    drain(proj, work)
    FakeFlow.fail_at = "b"
    sha = commit(work, {"heavy_bad.txt": "x\n"}, "bad")
    drain(proj, work)
    n = node(proj, sha)
    assert n.state == State.OPEN        # §11.1 exit-clause fail
    assert n.metrics["_gaps"]["build"] == {"stage": "b"}
    st = store(proj)
    try:
        vs = {v.clause.check: v for v in st.verdicts(sha)}
        assert vs["build"].status == "fail"
        assert "b" in vs["build"].detail
    finally:
        st.close()
    # child is not tainted and still builds
    FakeFlow.fail_at = None
    sha2 = commit(work, {"heavy_ok.txt": "y\n",
                         "config.mk": "X = 3\n"}, "child")
    drain(proj, work)
    n2 = node(proj, sha2)
    assert n2.state == State.VERIFIED


def test_rejudge_carries_build_failure(pipe):
    """§11.7 fix: rejudge re-emits the stored `build` verdict — a node
    whose job failed stays `open` with the build gap, and no
    measurement clause is evaluated or awaited."""
    from guardci.core.rejudge import rejudge
    proj, work = pipe
    commit(work, {"config.mk": "X = 4\n"}, "warmup")
    drain(proj, work)
    FakeFlow.fail_at = "b"
    sha = commit(work, {"heavy_bad.txt": "x\n"}, "bad")
    drain(proj, work)
    assert node(proj, sha).state == State.OPEN
    p = Project(proj)
    try:
        rejudge(p)
    finally:
        p.close()
    FakeFlow.fail_at = None
    n = node(proj, sha)
    assert n.state == State.OPEN
    assert n.metrics["_gaps"]["build"] == {"stage": "b"}
    st = store(proj)
    try:
        vs = {v.clause.check: v for v in st.verdicts(sha)}
        assert set(vs) == {"build", "fake_static"}   # statics re-run
        assert vs["build"].status == "fail"
        assert vs["build"].status == "fail"
        runs = {r.checker for r in st.check_runs(sha)}
        assert "build" in runs
        assert not any(r.startswith("measure:") for r in runs)
    finally:
        st.close()


def test_hard_metric_fail_does_not_requeue_descendant(pipe):
    proj, work = pipe
    # sha1 is light-verified; its committed metrics.json carries a bad
    # signoff setup_wns that only a heavy job will see
    sha1 = commit(work, {"config.mk": "X = 2\n",
                         "metrics.json": metrics_json(
                             "badp", signoff={"setup_wns": -0.2})},
                  "light-parent")
    drain(proj, work)
    assert node(proj, sha1).state == State.VERIFIED
    sha2 = commit(work, {"config.mk": "X = 3\n"}, "light-child")
    drain(proj, work)
    assert node(proj, sha2).state == State.VERIFIED
    # upgrading the parent to full verification fails the hard check →
    # open (§11.1 exit fail); metric taint does NOT re-queue
    # descendants (§3.2)
    p = Project(proj)
    try:
        p.store.set_requested_full(sha1)
        p.store.set_state(sha1, State.PENDING)
        p.store.enqueue(sha1)
    finally:
        p.close()
    serve(proj)
    assert node(proj, sha1).state == State.OPEN
    n2 = node(proj, sha2)
    assert n2.attempt == 1                       # untouched, not re-queued
    assert n2.state == State.VERIFIED


def test_warn_node(pipe):
    proj, work = pipe
    sha = commit(work, {"heavy_w.txt": "w\n",
                        "metrics.json": metrics_json(
                            "warn1", signoff={"warn_me": True})}, "w")
    drain(proj, work)
    assert node(proj, sha).state == State.WARNED


def test_checker_error_stays_verifying(pipe):
    proj, work = pipe
    sha = commit(work, {"heavy_e.txt": "e\n",
                        "metrics.json": metrics_json(
                            "err1", signoff={"want_error": True})}, "e")
    drain(proj, work)
    n = node(proj, sha)
    assert n.state == State.VERIFYING
    st = store(proj)
    try:
        evs = [json.loads(e["payload"]) for e in st.events(sha)
               if e["kind"] == "awaiting"]
        assert any("fake_err" in ev["checkers"] for ev in evs)
    finally:
        st.close()


def test_info_severity_error_does_not_block(pipe_info_err):
    """An error on an info-severity clause is recorded but cannot
    block the verdict — fail-closed applies only to clauses that can
    change the state."""
    proj, work = pipe_info_err
    sha = commit(work, {"heavy_e.txt": "e\n",
                        "metrics.json": metrics_json(
                            "err1", signoff={"want_error": True})},
                 "e")
    drain(proj, work)
    n = node(proj, sha)
    assert n.state == State.VERIFIED
    st = store(proj)
    try:
        vs = [v for v in st.verdicts(sha)
              if v.clause.check == "fake_err"]
        assert vs and vs[-1].status == "error"
        evs = [json.loads(e["payload"]) for e in st.events(sha)
               if e["kind"] == "awaiting"]
        assert not any("fake_err" in ev.get("checkers", [])
                       for ev in evs)
    finally:
        st.close()


def test_infra_error_retried_once(pipe):
    proj, work = pipe
    commit(work, {"config.mk": "X = 4\n"}, "warmup")
    drain(proj, work)                    # baseline populated first
    FakeFlow.infra_at = "b"
    sha = commit(work, {"heavy_i.txt": "i\n"}, "infra")
    drain(proj, work)
    n = node(proj, sha)
    assert n.state == State.VERIFYING            # fail-closed
    st = store(proj)
    try:
        job = st.jobs_for(sha)[-1]
        assert job["state"] == "error" and job["retries"] == 1
        assert any(e["kind"] == "job_error" for e in st.events(sha))
    finally:
        st.close()
    runs = [s for s_, s in FakeFlow.ran if s_ == sha]
    # b fails rc=125 twice (one retry); a is a cache hit on the retry
    assert runs == ["a", "b", "b"]


def test_comment_only_inherits_metrics(pipe):
    proj, work = pipe
    commit(work, {"heavy_a.txt": "a\n"}, "base-heavy")
    sha1 = _git(work, "rev-parse", "HEAD").strip()
    drain(proj, work)
    before = len(FakeFlow.ran)
    calls = len(FakeSignoff.calls)
    sha2 = commit(work, {"config.mk": "X = 1\n# just a comment\n"},
                  "comment")
    drain(proj, work)
    n1, n2 = node(proj, sha1), node(proj, sha2)
    assert n2.state == State.VERIFIED
    assert n2.metrics["setup_wns"] == n1.metrics["setup_wns"]
    st = store(proj)
    try:
        assert st.jobs_for(sha2) == []           # zero jobs
    finally:
        st.close()
    assert len(FakeFlow.ran) == before           # nothing built
    assert len(FakeSignoff.calls) == calls       # no backend call


def test_memo_hit_no_backend_call(pipe):
    proj, work = pipe
    commit(work, {"heavy_a.txt": "a\n"}, "h1")
    drain(proj, work)
    calls = len(FakeSignoff.calls)
    # clause-bearing commit with identical content keys: comment-only
    # edit inside a heavy-tag file → routed heavy, same stage keys
    sha2 = commit(work, {"heavy_a.txt": "a\n# comment\n"}, "h2")
    drain(proj, work)
    n2 = node(proj, sha2)
    assert n2.state == State.VERIFIED
    assert n2.metrics["setup_wns"] == 0.1
    st = store(proj)
    try:
        assert st.jobs_for(sha2) == []           # memo hit → no job
        evs = {e["kind"] for e in st.events(sha2)}
        assert "memo_hit" in evs
    finally:
        st.close()
    assert len(FakeSignoff.calls) == calls       # backend never ran


def test_priority_order(pipe, tmp_path):
    proj, work = pipe
    # c1 (has children) — class 'rest'; c2 requested_full — class top;
    # s_a/s_b sibling leaves — class 'leaf'
    sha1 = commit(work, {"heavy_a.txt": "a\n"}, "c1")
    _git(work, "push", "origin", "HEAD:main")
    sha2 = commit(work, {"heavy_b.txt": "b\n"}, "c2")
    _git(work, "push", "origin", "HEAD:refs/heads/c2")
    _git(work, "checkout", "-qb", "s_a", sha1)
    sha_sa = commit(work, {"heavy_sa.txt": "sa\n"}, "sa")
    _git(work, "push", "origin", "HEAD:refs/heads/sa")
    _git(work, "checkout", "-qb", "s_b", sha1)
    sha_sb = commit(work, {"heavy_sb.txt": "sb\n"}, "sb")
    _git(work, "push", "origin", "HEAD:refs/heads/sb")
    # mark c2 requested_full before its static phase
    p = Project(proj)
    try:
        p.store.set_requested_full(sha2)
    finally:
        p.close()
    FakeFlow.reset()
    serve(proj)
    order = []
    for sha, _stage in FakeFlow.ran:
        if sha != baseline_sha(proj) and sha not in order:
            order.append(sha)
    # leaves dispatch newest-first; the superseded parent last (defer)
    assert order == [sha2, sha_sb, sha_sa, sha1]


def test_idle_heavy_upgrades_leaf(pipe):
    proj, work = pipe
    sha = commit(work, {"config.mk": "X = 7\n"}, "light-leaf")
    drain(proj, work)
    n = node(proj, sha)
    assert n.state == State.VERIFIED
    assert "setup_wns" not in n.metrics
    serve(proj, idle_heavy=True)
    n = node(proj, sha)
    assert "setup_wns" in n.metrics              # upgraded
    assert n.attempt >= 2


def test_attempt_increments_latest_only(pipe):
    proj, work = pipe
    sha = commit(work, {"heavy_a.txt": "a\n"}, "h1")
    drain(proj, work)
    p = Project(proj)
    try:
        p.store.set_requested_full(sha)
        p.store.set_state(sha, State.PENDING)
        p.store.enqueue(sha)
    finally:
        p.close()
    serve(proj)
    st = store(proj)
    try:
        latest = st.verdicts(sha)
        allv = st.verdicts(sha, all_attempts=True)
        assert all(v.attempt == 2 for v in latest)
        assert len(allv) > len(latest)
    finally:
        st.close()
    assert node(proj, sha).attempt == 2


def test_baseline_built_once(pipe):
    proj, work = pipe
    bsha = baseline_sha(proj)
    commit(work, {"heavy_a.txt": "a\n"}, "c1")
    drain(proj, work)
    commit(work, {"heavy_b.txt": "b\n"}, "c2")
    drain(proj, work)
    baseline_runs = [s for s_, s in FakeFlow.ran if s_ == bsha]
    assert baseline_runs == ["a", "b", "c"]      # exactly once
    assert node(proj, bsha).metrics["setup_wns"] == 0.1


def test_no_executor_fail_closed(pipe, tmp_path):
    proj, work = pipe
    cfg = yaml.safe_load((proj / "guardci.yaml").read_text())
    cfg["executor"] = {"type": "none"}
    (proj / "guardci.yaml").write_text(yaml.safe_dump(cfg))
    sha = commit(work, {"heavy_a.txt": "a\n"}, "noexec")
    drain(proj, work)
    n = node(proj, sha)
    assert n.state == State.VERIFYING
    st = store(proj)
    try:
        evs = [json.loads(e["payload"]) for e in st.events(sha)
               if e["kind"] == "awaiting"]
        assert any("no executor" in str(e.get("reason")) for e in evs)
        assert st.jobs_for(sha) == []
    finally:
        st.close()


def test_shared_cache_baseline_metrics(pipe, tmp_path):
    """Two projects, one cache dir, same baseline tree: the second
    never builds the baseline — its root gets metrics from
    golden/<tree>/baseline.json and its nodes get verdicts (§9.4)."""
    proj, work = pipe
    commit(work, {"heavy_a.txt": "a\n"}, "h1")
    drain(proj, work)                       # populates golden + memos
    bsha1 = baseline_sha(proj)
    assert node(proj, bsha1).metrics["setup_wns"] == 0.1

    # second project: own remote seeded from the same tree, same cache
    proj2 = tmp_path / "proj2"
    guardci("init", str(proj2), "--contract",
            str(tmp_path / "contract.yaml"), "--flow", "fake",
            "--from", str(tmp_path / "src"),
            "--cache", str(tmp_path / "cache"))
    cfg2 = yaml.safe_load((proj2 / "guardci.yaml").read_text())
    cfg2["executor"] = {"type": "fake", "image": "img-1", "slots": 1}
    cfg2["backends"] = {"signoff": "fsign", "drc": "fdrc", "lec": "flec"}
    cfg2["t_leaf_seconds"] = 0
    (proj2 / "guardci.yaml").write_text(yaml.safe_dump(cfg2))
    work2 = tmp_path / "work2"
    subprocess.run(["git", "clone", str(proj2 / "remote.git"),
                    str(work2)], check=True, capture_output=True)

    commit(work2, {"config.mk": "X = 2\n"}, "light")
    drain(proj2, work2)

    bsha2 = baseline_sha(proj2)
    assert bsha2 != bsha1                    # different commit …
    assert node(proj2, bsha2).metrics["setup_wns"] == 0.1   # same tree
    # the baseline was not built a second time
    assert not [s_ for s_, s in FakeFlow.ran if s_ == bsha2]
    # and proj2's node was decided with verdicts
    tip = _git(work2, "rev-parse", "HEAD").strip()
    assert node(proj2, tip).state == State.VERIFIED


def test_lec_timeout_fail_and_memo(tmp_path):
    """§9.6: lec_result=timeout under `on_timeout: fail` is a hard-fail
    verdict (rejected, not forever-verifying), and a re-queued attempt
    takes the memoized result without a backend call."""
    contract = dict(CONTRACT)
    contract["clauses"] = CONTRACT["clauses"] + [
        {"check": "lec", "gold": "synth_of_golden_rtl",
         "timeout": 1800, "on_timeout": "fail", "severity": "hard"}]
    proj_dir, work = _pipe_with(tmp_path, contract)

    files = {"heavy_x.txt": "x\n",
             "metrics.json": metrics_json(
                 "n1", lec={"lec_result": "timeout",
                            "lec_timeout_s": 1800})}
    sha = commit(work, files, "t")
    drain(proj_dir, work)
    n = node(proj_dir, sha)
    assert n.state == State.OPEN        # exit-clause hard fail
    st = store(proj_dir)
    try:
        vd = [v for v in st.verdicts(sha) if v.clause.check == "lec"]
        assert vd and vd[0].status == "fail"
        assert vd[0].detail == \
            "lec: timeout after 1800s (equivalence not proven)"
    finally:
        st.close()

    calls = len(FakeLec.calls)
    p = Project(proj_dir)
    try:
        p.store.set_requested_full(sha)
        p.store.set_state(sha, State.PENDING)
        p.store.enqueue(sha)
    finally:
        p.close()
    serve(proj_dir)
    n2 = node(proj_dir, sha)
    assert n2.attempt == 2 and n2.state == State.OPEN
    assert len(FakeLec.calls) == calls          # memo hit, no backend run


def test_artifacts_keep_reports(pipe):
    """`artifacts_keep: reports` prunes big binary evidence after the
    job (memos stay whole); logs and small reports remain, and
    `guardci status` still works."""
    proj, work = pipe
    cfg_path = Path(proj) / "guardci.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["artifacts_keep"] = "reports"
    cfg_path.write_text(yaml.safe_dump(cfg))
    sha = commit(work, {"heavy_a.txt": "a\n"}, "h1")
    drain(proj, work)
    assert node(proj, sha).state == State.VERIFIED

    adir = Path(proj) / "artifacts" / sha
    kept = [p for p in adir.rglob("*") if p.is_file()]
    assert kept
    rel = {str(p.relative_to(adir)) for p in kept}
    assert any(p.startswith("logs/") for p in rel)
    assert "signoff/layout.def" not in rel
    assert "lec/netlist.v" not in rel
    assert "drc/stream.gds" not in rel
    assert not [p for p in rel if ".export-" in p]
    # memo evidence filtered to existing paths
    st = store(proj)
    try:
        runs = [r for r in st.check_runs(sha)
                if r.checker.startswith("measure:")]
        for r in runs:
            for e in r.evidence:
                assert Path(e).exists(), e
    finally:
        st.close()
    r = subprocess.run(
        [str(BIN / "guardci"), "--project", str(proj), "status",
         "--json", sha], check=False,
        capture_output=True, text=True)
    assert r.returncode == 0
    assert json.loads(r.stdout)["state"] == "verified"


def test_artifacts_keep_all(pipe):
    """Default `all` keeps every evidence file."""
    proj, work = pipe
    sha = commit(work, {"heavy_a.txt": "a\n"}, "h1")
    drain(proj, work)
    adir = Path(proj) / "artifacts" / sha
    assert (adir / "signoff" / "layout.def").exists()
    assert (adir / "lec" / "netlist.v").exists()


# -------------------------------------------------- §11.5 V2 job chain

EQ_CONTRACT = {
    **CONTRACT,
    "lec_tool": "fequiv",
    "clauses": CONTRACT["clauses"] + [
        {"check": "equivalence", "kind": "invariant",
         "severity": "hard"}],
}


@pytest.fixture
def pipe_eq(tmp_path):
    return _pipe_with(tmp_path, EQ_CONTRACT)


def test_refuted_at_synth_stops_build(pipe_eq):
    """§11.5 fail fast: refuted gold≡synth kills the job — no physical
    stage runs, node is `rejected` by the equivalence invariant."""
    proj, work = pipe_eq
    FakeEquiv.SYNTH_STATUS = "refuted"
    sha = commit(work, {"heavy_x.txt": "x\n",
                        "metrics.json": metrics_json("n1")}, "refuted")
    drain(proj, work)
    n = node(proj, sha)
    assert n.state == State.REJECTED
    stages = {s for _sha, s in FakeFlow.ran if _sha == sha}
    assert "a" in stages and "b" not in stages and "c" not in stages
    st = store(proj)
    try:
        vs = {v.clause.check: v for v in st.verdicts(sha)}
        assert vs["equivalence"].status == "fail"
    finally:
        st.close()


def test_chain_inherits_synth_status(pipe_eq):
    """synth≡final one-to-one identical → final inherits the synth
    status and the chain is tagged `synth+one_to_one`."""
    proj, work = pipe_eq
    FakeEquiv.SYNTH_STATUS = "bounded"
    FakeEquiv.FINAL_T1 = "identical"
    sha = commit(work, {"heavy_x.txt": "x\n",
                        "metrics.json": metrics_json("n1")}, "chain")
    drain(proj, work)
    n = node(proj, sha)
    # fake_light still fails on `lec_result != identical` — that is
    # correct: bounded legitimately means tier-1 `different`. What we
    # assert is the chain itself, not the node state.
    assert n.state == State.OPEN
    assert n.metrics["equivalence_chain"] == "synth+one_to_one"
    assert n.metrics["equivalence_status"] == "bounded"
    pairs = {(a, b, t) for a, b, t in FakeEquiv.calls}
    assert ("synth_netlist.v", "final_netlist.v",
            ("one_to_one",)) in pairs
    # no full gold-vs-final compare ran
    assert not any(b == "final_netlist.v" and t is None
                   for a, b, t in FakeEquiv.calls)


def test_chain_fallback_gold_vs_final(pipe_eq):
    """synth≡final differs → full compare(gold, final), chain `final`."""
    proj, work = pipe_eq
    FakeEquiv.SYNTH_STATUS = "bounded"
    FakeEquiv.FINAL_T1 = "different"
    FakeEquiv.FINAL_STATUS = "refuted"
    sha = commit(work, {"heavy_x.txt": "x\n",
                        "metrics.json": metrics_json("n1")}, "fallback")
    drain(proj, work)
    n = node(proj, sha)
    # refuted at the FINAL compare also fails the invariant → rejected
    assert n.state == State.REJECTED
    assert n.metrics["equivalence_chain"] == "final"
    assert n.metrics["equivalence_status"] == "refuted"
    assert any(b == "final_netlist.v" and t is None
               for a, b, t in FakeEquiv.calls)


def test_equiv_memo_by_netlist_digest(pipe_eq):
    """Two commits exporting byte-identical netlists hit one compare —
    the memo key is the pair's digests, not the stage key."""
    proj, work = pipe_eq
    commit(work, {"heavy_x.txt": "x\n",
                  "metrics.json": metrics_json("same")}, "c1")
    drain(proj, work)
    sha2 = commit(work, {"heavy_y.txt": "y\n",
                         "metrics.json": metrics_json("same")}, "c2")
    drain(proj, work)
    synth_calls = [c for c in FakeEquiv.calls if c[1] == "synth_netlist.v"]
    assert len(synth_calls) == 1        # sha2 reused the digest memo
    n2 = node(proj, sha2)
    assert n2.metrics["equivalence_status"] == "proven"
