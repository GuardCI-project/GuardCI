"""Cache + builder unit tests (no docker): fake flow, fake executor."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from guardci.core.builder import InfraError, build
from guardci.core.cache import Cache
from guardci.executors.base import RunResult, Slot

# ------------------------------------------------------------- fakes

class FakeExecutor:
    def __init__(self):
        self.image = "img-1"

    def image_id(self):
        return self.image


class FakeFlow:
    """STAGES a→b→c; run_stage behaviour scripted via ``fail_at``/
    ``infra_at``; snapshots copy the workdir's ``out/`` dir."""

    stages_list = ("a", "b", "c")

    def __init__(self, keys=None, fail_at=None, infra_at=None):
        self.keys = keys or {"a": "ka", "b": "kb", "c": "kc"}
        self.fail_at = fail_at
        self.infra_at = infra_at
        self.ran = []
        self.restored = []

    def stage_keys(self, repo, sha, baseline_sha, image_id):
        return [(s, self.keys[s]) for s in self.stages_list]

    def materialize(self, repo, sha, baseline_sha, protected, workdir):
        (Path(workdir) / "out").mkdir(parents=True, exist_ok=True)
        (Path(workdir) / "out" / "recipe.txt").write_text(str(sha))

    def run_stage(self, workdir, stage, executor, slot, timeout, log):
        self.ran.append(stage)
        Path(log).write_text(f"log {stage}")
        if stage == self.infra_at:
            return RunResult(rc=125, seconds=0.1)
        if stage == self.fail_at:
            return RunResult(rc=2, seconds=0.5)
        (Path(workdir) / "out" / f"{stage}.done").write_text(stage)
        return RunResult(rc=0, seconds=1.0)

    def snapshot(self, workdir, stage, dest, prev=None):
        import shutil
        shutil.copytree(Path(workdir) / "out", dest / "out")

    def restore(self, src, workdir):
        import shutil
        shutil.copytree(Path(src) / "out", Path(workdir) / "out",
                        dirs_exist_ok=True)
        self.restored.append(str(src))


@pytest.fixture
def cache(tmp_path):
    return Cache(tmp_path / "cache")


def _build(flow, cache, tmp_path, upto="c", sha="s1"):
    return build(flow, FakeExecutor(), Slot(cpus=[0]), "repo", sha,
                 "base", [], upto, cache, tmp_path / "work",
                 stage_timeouts={"a": 10, "b": 10, "c": 10})


# ------------------------------------------------------------- cache

def test_cache_publish_lookup(tmp_path):
    c = Cache(tmp_path)
    assert c.lookup("k1") is None
    c.publish("k1", lambda d: (d / "snap").write_text("x"),
              {"stage": "a", "status": "ok", "seconds": 1.0, "rc": 0})
    e = c.lookup("k1")
    assert e.ok and e.stage == "a" and (e.dir / "snap").read_text() == "x"


def test_cache_negative_entry(tmp_path):
    c = Cache(tmp_path)
    c.publish("k", lambda d: None, {"stage": "b", "status": "failed"})
    e = c.lookup("k")
    assert e is not None and e.status == "failed" and not e.ok


def test_cache_populate_failure_leaves_no_entry(tmp_path):
    c = Cache(tmp_path)

    def boom(d):
        (d / "partial").write_text("x")
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        c.publish("k", boom, {"stage": "a", "status": "ok"})
    assert c.lookup("k") is None
    assert not c.entry_dir("k").exists()


def test_cache_two_threads_build_once(tmp_path):
    c = Cache(tmp_path)
    calls = []

    def populate(d):
        calls.append(1)
        import time
        time.sleep(0.05)
        (d / "meta-payload").write_text("v")

    def worker():
        with c.lock("k"):
            if c.lookup("k") is None:
                c.publish("k", populate, {"stage": "a", "status": "ok"})

    ts = [threading.Thread(target=worker) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert calls == [1]
    assert c.lookup("k").ok


# ------------------------------------------------------------- builder

def test_builder_clean_build(tmp_path, cache):
    flow = FakeFlow()
    r = _build(flow, cache, tmp_path)
    assert r.ok and r.stages_built == 3 and r.stages_reused == 0
    assert flow.ran == ["a", "b", "c"]
    assert [s.key for s in r.stages] == ["ka", "kb", "kc"]
    for s in ("a", "b", "c"):
        assert cache.lookup(flow.keys[s]).ok


def test_builder_prefix_reuse(tmp_path, cache):
    first = FakeFlow()
    _build(first, cache, tmp_path, sha="s1")
    second = FakeFlow()
    r = _build(second, cache, tmp_path / "w2", sha="s1")
    assert r.ok and r.stages_built == 0 and r.stages_reused == 3
    assert second.ran == [] and second.restored


def test_builder_failed_stage_negative_entry(tmp_path, cache):
    flow = FakeFlow(fail_at="b")
    r = _build(flow, cache, tmp_path)
    assert not r.ok and r.failed_stage == "b"
    assert cache.lookup("kb").status == "failed"
    # second build reuses the failure without running anything
    flow2 = FakeFlow()
    r2 = _build(flow2, cache, tmp_path / "w2", sha="s1")
    assert not r2.ok and r2.failed_stage == "b"
    assert flow2.ran == []


def test_builder_infra_error_not_cached(tmp_path, cache):
    flow = FakeFlow(infra_at="b")
    with pytest.raises(InfraError):
        _build(flow, cache, tmp_path)
    assert cache.lookup("kb") is None
    assert cache.lookup("ka").ok       # the good stage was published


def test_builder_cost_fields(tmp_path, cache):
    flow = FakeFlow()
    r = _build(flow, cache, tmp_path)
    assert r.build_seconds >= 0
    assert isinstance(r.logs, dict) and r.logs["a"].exists()
    assert r.workdir is not None


def test_builder_snapshot_immutable_after_reuse(tmp_path, cache):
    """A resumed build mutating its workdir must not corrupt the cache."""
    flow = FakeFlow()
    _build(flow, cache, tmp_path, sha="s1")
    entry = cache.lookup("ka")
    payload = (entry.snapshot / "out" / "a.done").read_bytes()
    flow2 = FakeFlow()
    r2 = _build(flow2, cache, tmp_path / "w2", sha="s1")
    # simulate the resumed build scribbling on restored files
    f = Path(r2.workdir) / "out" / "a.done"
    assert f.exists()
    f.write_text("corrupted")
    assert (entry.snapshot / "out" / "a.done").read_bytes() == payload
