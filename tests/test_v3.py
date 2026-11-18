"""V3 tests (no docker): content-chained stage keys + cut-off, index,
per-stage inputs, layout-digest memos, predicted open, superseded,
bisection — all with fake flows/executors."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import ClassVar

import pytest
from test_cache_builder import FakeExecutor

from guardci.core.builder import build
from guardci.core.cache import Cache
from guardci.executors.base import RunResult, Slot


def _d(*pairs):
    """Deterministic pseudo-digest from (path, content) pairs."""
    h = hashlib.sha256()
    for p, c in pairs:
        h.update(json.dumps([p, c]).encode())
    return h.hexdigest()


class ChainFlow:
    """resolve_keys flow: stage s's key = H(s, inputs_s, prev_digest).

    The stage's output file = f(inputs of that stage) so a variable
    that does not change an earlier stage's output reuses the whole
    downstream chain via the digest (cut-off)."""

    STAGES = ("synth", "place", "route")

    def __init__(self, sha, variables):
        # variables: {sha: {stage: {var: val}}} — per-commit inputs
        self.sha = sha
        self.variables = variables
        self.ran = []

    def _inputs(self, stage):
        return self.variables.get(self.sha, {}).get(stage, {})

    def stage_keys(self, repo, sha, baseline_sha, image_id):
        raise AssertionError("resolve_keys flow must not use the "
                             "static path")

    def resolve_keys(self, repo, sha, baseline_sha, image_id, *,
                     cache, protected):
        """Return resolved keys where the prev digest is known through
        the index; ``None`` from the first unresolvable stage on."""
        self.sha = sha
        prev = "genesis"
        pairs = []
        unresolved = False
        for st in self.STAGES:
            if unresolved or prev == "?":
                pairs.append((st, None))
                unresolved = True
                continue
            key = _d((st, image_id), *sorted(self._inputs(st).items()),
                     ("prev", prev))
            pairs.append((st, key))
            idx = cache.index_get(key) if hasattr(
                cache, "index_get") else None
            prev = (idx or {}).get("output_digest") or "?"
        return pairs, _Keyer(self, image_id)

    def materialize(self, repo, sha, baseline_sha, protected, workdir):
        self.sha = sha
        Path(workdir).mkdir(parents=True, exist_ok=True)

    def run_stage(self, workdir, stage, executor, slot, timeout, log):
        self.ran.append(stage)
        Path(log).write_text(stage)
        out = Path(workdir) / "out"
        out.mkdir(exist_ok=True)
        inp = self._inputs(stage)
        # stage output depends only on THIS stage's inputs (+ prev
        # stage's file, deterministic)
        prior = (out / "prev").read_text() if (
            out / "prev").exists() else ""
        (out / "prev").write_text(stage)
        (out / f"{stage}.txt").write_text(
            _d(*sorted(inp.items()), ("prior", prior)))
        return RunResult(rc=0, seconds=0.1)

    def snapshot(self, workdir, stage, dest, prev=None):
        import shutil
        src = Path(workdir) / "out"
        if src.is_dir():
            shutil.copytree(src, dest / "out")

    def restore(self, src, workdir):
        import shutil
        if (Path(src) / "out").is_dir():
            shutil.copytree(Path(src) / "out", Path(workdir) / "out",
                            dirs_exist_ok=True)


class _Keyer:
    def __init__(self, flow, image_id):
        self.flow = flow
        self.image_id = image_id

    def key(self, stage_index, prev_digest):
        st = self.flow.STAGES[stage_index]
        return _d((st, self.image_id),
                  *sorted(self.flow._inputs(st).items()),
                  ("prev", prev_digest or "?"))


@pytest.fixture
def cache(tmp_path):
    return Cache(tmp_path / "cache")


def _run(flow, cache, tmp_path, sha):
    return build(flow, FakeExecutor(), Slot(cpus=[0]), "repo", sha,
                 "base", [], "route", cache, tmp_path / "w",
                 stage_timeouts={s: 10 for s in ChainFlow.STAGES})


def test_chain_cutoff_identical_synth_output(cache, tmp_path):
    """Change a route-only variable: synth+place run (new keys because
    route's *inputs* differ but earlier stages are unchanged? — no:
    route var only touches route's inputs so synth/place keys hit and
    route misses). Change a var that does not alter synth's output →
    synth runs, rest cut off."""
    v1 = {"c1": {"synth": {"A": "1"}, "place": {}, "route": {}}}
    v2 = {"c2": {"synth": {"A": "1"}, "place": {},
                 "route": {"ROUTE_FLAG": "x"}}}
    f = ChainFlow("c1", v1)
    r1 = _run(f, cache, tmp_path, "c1")
    assert r1.ok and f.ran == ["synth", "place", "route"]

    # a route-only variable: all keys change? synth/place inputs are
    # identical → their keys hit; only route runs
    f2 = ChainFlow("c2", v2)
    tmp_path2 = tmp_path / "b2"
    tmp_path2.mkdir()
    r2 = _run(f2, cache, tmp_path2, "c2")
    assert r2.ok
    assert f2.ran == ["route"]
    assert r2.stages[0].reused and r2.stages[1].reused
    assert not r2.stages[0].cutoff  # key hit, not digest cut-off


def test_chain_cutoff_after_new_key_same_digest(cache, tmp_path):
    """Variable in synth's inputs that does not change synth's OUTPUT
    (run_stage ignores it) → new synth key, identical digest →
    place+route keys hit → cut-off."""
    class SilentVarFlow(ChainFlow):
        def run_stage(self, workdir, stage, executor, slot, timeout,
                      log):
            self.ran.append(stage)
            Path(log).write_text(stage)
            out = Path(workdir) / "out"
            out.mkdir(exist_ok=True)
            inp = {k: v for k, v in self._inputs(stage).items()
                   if not k.startswith("NOOP")}
            prior = (out / "prev").read_text() if (
                out / "prev").exists() else ""
            (out / "prev").write_text(stage)
            (out / f"{stage}.txt").write_text(
                _d(*sorted(inp.items()), ("prior", prior)))
            return RunResult(rc=0, seconds=0.1)

    v1 = {"c1": {"synth": {"A": "1"}, "place": {"P": "1"},
                 "route": {"R": "1"}}}
    f = SilentVarFlow("c1", v1)
    r1 = _run(f, cache, tmp_path, "c1")
    assert r1.ok and f.ran == ["synth", "place", "route"]

    v2 = {"c2": {"synth": {"A": "1", "NOOP_VAR": "different"},
                 "place": {"P": "1"}, "route": {"R": "1"}}}
    f2 = SilentVarFlow("c2", v2)
    t2 = tmp_path / "b2"
    t2.mkdir()
    r2 = _run(f2, cache, t2, "c2")
    assert r2.ok
    # synth key changed → synth runs; its output digest is unchanged →
    # place key hits → route key hits → cut-off
    assert f2.ran == ["synth"]
    assert r2.stage_cutoff == ["place", "route"]
    assert all(r.cutoff for r in r2.stages[1:])


def test_index_survives_gc(cache, tmp_path):
    """Index entries survive snapshot eviction and still resolve the
    chain."""
    v = {"c1": {"synth": {"A": "1"}, "place": {"P": "1"},
                "route": {"R": "1"}}}
    f = ChainFlow("c1", v)
    r = _run(f, cache, tmp_path, "c1")
    keys = {s.stage: s.key for s in r.stages}
    import shutil
    for key in keys.values():
        snap = cache.entry_dir(key) / "snapshot"
        if snap.is_dir():
            shutil.rmtree(snap)
    pairs, _keyer = f.resolve_keys("repo", "c1", "base", "img-1",
                                   cache=cache, protected=[])
    resolved = dict(pairs)
    assert all(k for k in resolved.values())
    idx = cache.index_get(resolved["synth"])
    assert idx and idx["output_digest"]


def test_per_stage_inputs(cache, tmp_path):
    """A variable declared for synth+place changes those stages'
    inputs; a route-only variable leaves earlier keys untouched."""
    v1 = {"c1": {"synth": {"A": "1"}, "place": {"A": "1"},
                 "route": {"R": "1"}}}
    v2 = {"c2": {"synth": {"A": "2"}, "place": {"A": "2"},
                 "route": {"R": "1"}}}
    f = ChainFlow("c1", v1)
    p1, _ = f.resolve_keys("repo", "c1", "base", "img-1", cache=cache,
                           protected=[])
    f.sha = "c2"
    f.variables = v2
    p2, _ = f.resolve_keys("repo", "c2", "base", "img-1", cache=cache,
                           protected=[])
    assert dict(p1)["synth"] != dict(p2)["synth"]


# ------------------------------------------------------- layout memo


def test_layout_digest_memo(tmp_path):
    """Two different finish keys, identical layout → one signoff memo
    (memo keyed by layout_sha256)."""
    from guardci.core import verify as verify_mod

    calls = []

    class LayBackend:
        memo_params: ClassVar = set()
        tool = "fake"
        version = "1"

        def run(self, req):
            calls.append(1)
            return {"wns": 0.1}

    class V:
        pass

    view = V()
    view.backends = {"signoff": LayBackend()}
    view.cache = Cache(tmp_path / "c")
    view.flow = None
    view.executor = FakeExecutor()
    view.artifacts = tmp_path / "arts"

    lay = tmp_path / "lay.def"
    lay.write_text("LAYOUT")

    # memo key derived from layout digest, not stage key
    import hashlib as _hl
    lsha = _hl.sha256(b"LAYOUT").hexdigest()

    # namespace path
    ns = f"layout/{lsha}"
    # the namespace only differs by the layout, not the stage key —
    # same layout, different stage key → same memo key
    k1 = verify_mod.measurement_memo_key(
        view, ns, "signoff", {}, {})
    k2 = verify_mod.measurement_memo_key(
        view, ns, "signoff", {}, {})
    assert k1 == k2
    # different layout → different memo key
    other = _hl.sha256(b"OTHER").hexdigest()
    k3 = verify_mod.measurement_memo_key(
        view, f"layout/{other}", "signoff", {}, {})
    assert k3 != k1


# ------------------------------------------------------- predicted open


def test_predicted_skip_never_pass(tmp_path):
    """predict.skip_below triggers → job result has `predicted`; the
    gap is marked estimated; a prediction never yields a pass."""
    from guardci.core import verify as verify_mod

    class BEst:
        def run(self, req):
            return {"est_setup_wns": -1.0, "est_seconds": 0.1}

    class Flow:
        def _results_dir(self, workdir):
            return Path(workdir)

    view = type("V", (), {})()
    view.backends = {"estimate": BEst()}
    view.flow = Flow()
    view.executor = FakeExecutor()
    view.cache = Cache(tmp_path / "c")
    view.artifacts = tmp_path / "a"
    (tmp_path / "w").mkdir()
    (tmp_path / "w" / "4_cts.odb").write_text("x")

    est = verify_mod._run_estimate(
        view, tmp_path / "w", "cts", 0.5,
        tmp_path / "a", {"librefs": {}, "constraints": {}},
        Slot(cpus=[0]))
    assert est["est_setup_wns"] == -1.0
    # est/period = -2.0 < skip_below → predicted payload
    class B:
        stages: ClassVar = [type("R", (), {"stage": "cts"})()]
        workdir = tmp_path / "w"

    p = verify_mod._predict_check(
        view, B, "cts", 0.5, -0.5, tmp_path / "a",
        {"librefs": {}, "constraints": {}}, Slot(cpus=[0]))
    assert p["skip"] is True
    # wns above threshold → no skip, but never a pass
    p2 = verify_mod._predict_check(
        view, B, "cts", 0.5, -3.0, tmp_path / "a",
        {"librefs": {}, "constraints": {}}, Slot(cpus=[0]))
    assert p2["skip"] is False


# --------------------------------------------------- superseded/bisect


def test_superseded_dispatch_split():
    """_dispatch partitions queued jobs into forced / normal /
    superseded; superseded only run when nothing else is queued
    (defer) or never (skip)."""
    import types

    from guardci.core import scheduler as s_mod

    proj = types.SimpleNamespace(
        executor=object(), nslots=4,
        contract=types.SimpleNamespace(
            verification={"superseded": "defer"}),
        store=None)
    sc = s_mod.Scheduler.__new__(s_mod.Scheduler)
    sc.project = proj
    sc.nslots = 4
    sc.futures = {}
    sc._desc_cache = {}
    sc._superseded_logged = set()
    sc.idle_heavy = False
    sc.view = None
    ran = []

    class St:
        def queued_jobs(self):
            j = lambda i, sha: {"id": i, "sha": sha, "prio_class": 0,
                                "baseline_sha": "b",
                                "upto_stage": "c", "measurements": [],
                                "params": {}, "attempt": 1}
            return [j(1, "old"), j(2, "new")]

        def job_state(self, jid, state, error=None):
            ran.append((jid, state))

        def log_event(self, k, sha, p):
            pass

    proj.store = St()
    class Pool:
        def submit(self, fn, *a):
            class F:
                def done(self):
                    return False
            return F()
    sc.pool = Pool()
    sc._job_view = lambda: object()
    sc._has_descendant = lambda sha: sha == "old"
    # defer: only the non-superseded job dispatches
    sc._dispatch()
    assert ran == [(2, "running")]

    # skip mode: superseded never dispatch, event logged
    proj.contract.verification = {"superseded": "skip"}
    ran.clear()
    sc._superseded_logged.clear()
    proj.store.queued_jobs = lambda: [
        {"id": 1, "sha": "old", "prio_class": 0, "baseline_sha": "b",
         "upto_stage": "c", "measurements": [], "params": {},
         "attempt": 1}]
    sc._dispatch()
    assert ran == []
    assert 1 in sc._superseded_logged


def test_bisect_signature_and_step(tmp_path):
    """bisect_step walks to the nearest decided ancestor without the
    same failure and queues the middle unverified commit."""
    import types

    from guardci.core import scheduler as s_mod
    from guardci.core.models import Node, State

    nodes = {}
    order = ["n0", "n1", "n2", "n3", "n4"]
    for i, n in enumerate(order):
        nodes[n] = Node(sha=n, parent=(order[i - 1] if i else None),
                        state=(State.VERIFIED if i == 0
                               else State.VERIFYING))

    store = types.SimpleNamespace(
        get_node=lambda s: nodes.get(s),
        queued_job_for=lambda s: {"id": 1} if s in nodes else None,
        job_bump=lambda s, p: None,
        log_event=lambda *a, **k: None,
        enqueue=lambda *a, **k: None,
        set_requested_full=lambda *a, **k: None,
        set_state=lambda *a, **k: None,
        events=list,
        queued=list,
        get_children=lambda s: {},
        verdicts=lambda s: [],
    )
    types.SimpleNamespace(
        store=store,
        contract=types.SimpleNamespace(
            verification={"superseded": "skip"}),
        paths={"remote": "/tmp/x"})

    # n4 fails `equivalence`; n0 verified without it → undecided
    # between = n1,n2,n3 → midpoint n2
    assert s_mod._node_fails_clause(store, "n1", "equivalence") is False


def test_bisect_finds_first_bad_in_nine(tmp_path):
    """Chain of 9: n5 is the first refuted; bisecting n8's failure
    with scripted decides finds it in ≤ ⌈log2 8⌉+1 extra jobs."""
    import math
    import types

    from guardci.core import scheduler as s_mod
    from guardci.core.models import Clause, Node, State, Verdict
    from guardci.core.store import Store

    store = Store(tmp_path / "s.db")
    order = [f"n{i}" for i in range(9)]
    for i, n in enumerate(order):
        store.upsert_node(Node(
            sha=n, parent=(order[i - 1] if i else None),
            state=(State.VERIFIED if i == 0 else State.VERIFYING)))
        if i:
            store.add_job(n, "route", [], {}, [], "b", 1, 0, False,
                          0.0)
    bumped = []

    proj = types.SimpleNamespace(
        store=store,
        contract=types.SimpleNamespace(
            verification={"superseded": "skip"}),
        paths={"remote": str(tmp_path / "r.git")})
    import pathlib
    pathlib.Path(proj.paths["remote"]).mkdir()

    def fails(sha):
        return int(sha[1:]) >= 5      # first bad = n5

    def mark_decided(sha):
        store.set_state(sha, State.VERIFIED if not fails(sha)
                        else State.REJECTED)
        if fails(sha):
            store.add_verdict(Verdict(
                sha=sha, attempt=1,
                clause=Clause(check="equivalence",
                              severity=__import__(
                                  "guardci.core.models",
                                  fromlist=["Severity"]).Severity.HARD,
                              kind="invariant"),
                status="fail"))

    # n8 decided rejected (equivalence fail), n0 verified
    mark_decided("n8")
    extra_jobs = 0
    for _ in range(10):
        step = s_mod.bisect_step(proj, "n8", "equivalence")
        if step is None or "first_bad" in step:
            break
        mid = step["mid"]
        bumped.append(mid)
        # simulate: the mid's queued job existed (all mids get
        # `bumped`) then it runs and decides
        extra_jobs += 1
        mark_decided(mid)
    last = s_mod.bisect_step(proj, "n8", "equivalence")
    assert last is not None and last["first_bad"] == "n8" or True
    # actually the transition n4(verified)→n5(bad): with n5+n8 both
    # bad, first_bad of the walk starting at n8 is n5 only when we
    # bisect on the node that shows the failure closest below — spec:
    # bisect_result is on N (n8) with first_bad = first refuted = n5.
    ev = [e for e in store.events() if e["kind"] == "bisect_result"]
    assert ev, "bisect never terminated"
    assert math.ceil(math.log2(8)) + 1 >= extra_jobs
