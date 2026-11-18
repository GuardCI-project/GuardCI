"""W1 tests (§12): checkpoint recorder, artifact-mode jobs, stage
semantics, library_closure/provenance/recipe_scope, delivery — all with the
fake flow + fake backends (no docker)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import ClassVar

import yaml
from test_pipeline import (
    FakeEquiv,
    FakeFlow,
    _pipe_with,
    baseline_sha,
    commit,
    drain,
    node,
)

from guardci.artifacts.model import ArtifactKind
from guardci.backends import base as backends_base
from guardci.backends.base import Backend
from guardci.core.models import State
from guardci.core.project import Project
from guardci.core.scheduler import node_coverage
from guardci.flows import base as flows_base
from guardci.recorders.base import Checkpoint
from guardci.recorders.workspace import WorkspaceRecorder

# ----------------------------------------------------------- fakes

class WFlow(FakeFlow):
    """FakeFlow + checkpoint hooks (§12.2): workspace artifacts are
    ``out/<stage>.json`` files the test writes itself (the author runs
    stages outside GuardCI).  Stage names are the canonical ones the
    checkers' ``native_stage`` attributes use."""

    name = "wflow"
    light_stage = "synth"
    STAGES: ClassVar[list] = ["synth", "floorplan", "place", "cts",
                              "route", "finish"]

    def closure(self, repo, sha, baseline_sha):
        # all stages read config.mk; reachable = {path: stage_index}
        class _Cl:
            reachable: ClassVar[dict] = {"config.mk": 0}
            unreachable: ClassVar[set] = set()
            baseline_paths: ClassVar[set] = {"config.mk"}
            named: ClassVar[set] = set()
        return _Cl()

    def checkpoint_artifacts(self, workspace):
        out = {}
        for s in self.STAGES:
            p = Path(workspace) / "out" / f"{s}.json"
            if p.exists():
                out[s] = str(p)
        return out


class FakeFacts(Backend):
    """Reads the artifact JSON (written by the test), emits facts:
    copies ``netlist`` text to netlist.v, writes exported.def."""

    provides = "facts"
    tool = "ffacts"
    version = "w1.0"
    calls: ClassVar[list] = []

    def run(self, req):
        art = Path(req.artifacts.get(ArtifactKind.LAYOUT))
        self.calls.append((req.params.get("stage"), art.name))
        data = json.loads(art.read_text())
        if data.get("unreadable"):
            return {"unreadable": True, "artifact_sha256": "x"}
        outdir = Path(req.outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        net = outdir / "netlist.v"
        net.write_text(data.get("netlist", "module x; endmodule\n"))
        defp = outdir / "exported.def"
        defp.write_text(art.read_text())     # metrics JSON as "DEF"
        import hashlib
        facts = dict(data.get("facts") or {})
        facts["library_closed"] = data.get("library_closed", True)
        if data.get("foreign_masters"):
            facts["library_closed"] = False
            facts["foreign_masters"] = data["foreign_masters"]
        facts["netlist_sha256"] = hashlib.sha256(
            net.read_bytes()).hexdigest()
        facts["def_path"] = str(defp)
        facts["netlist_path"] = str(net)
        return facts


backends_base.register(FakeFacts)
flows_base.register(WFlow)


def artifact(stage_facts, netlist="module x; endmodule\n", **kw):
    """The JSON blob a fake artifact file carries."""
    d = {"netlist": netlist, "facts": stage_facts}
    d.update(kw)
    return json.dumps(d)


ACONTRACT = {
    "lec_tool": "fequiv",            # composite backend for the chain
    "name": "wfake",
    "protected": ["prot/**"],
    "recipe": ["**"],
    "clauses": [
        {"check": "recipe_scope", "kind": "invariant",
         "severity": "hard"},
        {"check": "library_closure", "kind": "invariant",
         "severity": "hard"},
        {"check": "fake_light", "severity": "hard"},
        {"check": "fake_heavy", "severity": "hard", "min_wns": 0},
        {"check": "provenance", "kind": "exit", "severity": "warn"},
        {"check": "reproducible", "kind": "exit", "mode": "contract",
         "severity": "hard"},
    ],
    "verification": {"mode": "full", "source": "artifact",
                     "fallback": "rebuild"},
}


def _wpipe(tmp_path, contract=None):
    proj, work = _pipe_with(tmp_path, contract or ACONTRACT)
    cfg = yaml.safe_load((proj / "guardci.yaml").read_text())
    cfg["flow"] = "wflow"
    cfg["recorder"] = {"type": "workspace", "path": str(work)}
    cfg["backends"]["facts"] = "ffacts"
    (proj / "guardci.yaml").write_text(yaml.safe_dump(cfg))
    return proj, work


_ART_T = [time.time()]


def _write_artifact(work, stage, **kw):
    """Write a stage artifact with a strictly increasing mtime — the
    author ran the stage after the previous one finished."""
    p = Path(work) / "out" / f"{stage}.json"
    p.parent.mkdir(exist_ok=True)
    p.write_text(artifact(**kw))
    _ART_T[0] = max(_ART_T[0] + 0.01, time.time() + 0.01)
    os.utime(p, (_ART_T[0], _ART_T[0]))
    return p


def _facts():
    return {"instances": 3, "cells_by_type": {"CORE": 3},
            "cell_area": 1.0, "netlist_sha256": "x",
            "setup_wns": 0.1, "lec_result": "identical",
            "drc_count": 0}


# ----------------------------------------------------------- recorder

def test_recorder_scan_journal_hashcache(tmp_path):
    proj, work = _wpipe(tmp_path)
    p = Project(proj)
    try:
        rec = p.recorder
        assert isinstance(rec, WorkspaceRecorder)
        sha = baseline_sha(proj)
        _write_artifact(work, "synth", stage_facts=_facts())
        cp = rec.capture(p, sha)
        assert isinstance(cp, Checkpoint)
        assert cp.stage == "synth"
        assert "synth" in cp.artifacts
        assert "config.mk" in cp.file_blobs
        # second scan: no journal events, hash cache hit
        cp2 = rec.capture(p, sha)
        assert cp2.journal == []
        # modify → journal records it
        (Path(work) / "config.mk").write_text("X = 3\n")
        cp3 = rec.capture(p, sha)
        ops = {(e["path"], e["op"]) for e in cp3.journal}
        assert ("config.mk", "modify") in ops
    finally:
        p.close()


def test_checkpoint_persist_copy(tmp_path):
    proj, work = _wpipe(tmp_path)
    p = Project(proj)
    try:
        sha = baseline_sha(proj)
        art = _write_artifact(work, "synth", stage_facts=_facts())
        cp = p.recorder.capture(p, sha)
        from guardci.recorders import base as rec_base
        cdir = rec_base.persist(p, cp)
        cj = cdir / "checkpoint.json"
        assert cj.exists()
        data = json.loads(cj.read_text())
        assert data["stage"] == "synth"
        copy = cdir / "artifacts" / "synth.json"
        assert copy.exists()
        # CI-owned copy: same content, but not a link to the author's
        # still-rewritable file — content-addressed under _blobs
        assert copy.read_bytes() == art.read_bytes()
        assert os.stat(copy).st_ino != os.stat(art).st_ino
        assert data["artifact_sha256"]["synth"]
        blob = (Path(proj) / "artifacts" / "_blobs"
                / data["artifact_sha256"]["synth"])
        assert blob.exists()
    finally:
        p.close()


def test_artifact_mode_synth_only(tmp_path):
    """push 1: synth artifact only → facts once, lec vs gold, pending
    later clauses, stage coverage."""
    proj, work = _wpipe(tmp_path)
    FakeFacts.calls = []
    sha = commit(work, {"config.mk": "X = 2\n"}, "a1")
    _write_artifact(work, "synth", stage_facts=_facts())
    drain(proj, work)
    n = node(proj, sha)
    assert n.metrics["stage"] == "synth"
    assert node_coverage(Project(proj), sha) == "stage"
    # one facts call at stage a; one gold-vs-first lec compare
    assert [(s, _) for s, _ in FakeFacts.calls] == [("synth", "synth.json")]
    verdicts = {v.clause.name: v.status
                for v in Project(proj).store.verdicts(sha)}
    assert verdicts["library_closure"] == "pass"
    assert verdicts["fake_heavy"] == "pending"   # native finish > a
    assert verdicts["reproducible"] == "pending"  # delivery
    # stage-reliable node is verified below the last stage
    assert n.state == State.VERIFIED


def test_artifact_push2_reuses_synth(tmp_path):
    """push 2: same synth artifact + place → one new facts call, one
    tier-1 pair check, no repeated gold comparison."""
    proj, work = _wpipe(tmp_path)
    commit(work, {"config.mk": "X = 2\n"}, "a1")
    _write_artifact(work, "synth", stage_facts=_facts(),
                    netlist="module a; endmodule\n")
    drain(proj, work)
    FakeFacts.calls = []
    FakeEquiv.calls = []
    sha2 = commit(work, {"config.mk": "X = 2\n# note\n"}, "b1")
    _write_artifact(work, "place", stage_facts=_facts(),
                    netlist="module b; endmodule\n")
    drain(proj, work)
    n2 = node(proj, sha2)
    assert n2.metrics["stage"] == "place"
    assert [s for s, _ in FakeFacts.calls] == ["place"]   # synth memoized
    # tier-1 pair check on (a-netlist, b-netlist), no gold re-compare
    t1 = [c for c in FakeEquiv.calls if c[2] == ("one_to_one",)]
    assert len(t1) == 1
    assert not any(c[0] == "gold_netlist.v"
                   for c in t1)


def test_foreign_master_rejected(tmp_path):
    proj, work = _wpipe(tmp_path)
    sha = commit(work, {"config.mk": "X = 4\n"}, "fm")
    _write_artifact(work, "synth", stage_facts=_facts(),
                    foreign_masters=["EVIL_CELL"])
    drain(proj, work)
    n = node(proj, sha)
    assert n.state == State.REJECTED        # invariant fail, not tainted
    assert n.state != State.TAINTED


def test_unreadable_fallback_none(tmp_path):
    contract = dict(ACONTRACT)
    contract["verification"] = {"mode": "full", "source": "artifact",
                                "fallback": "none"}
    proj, work = _wpipe(tmp_path, contract)
    sha = commit(work, {"config.mk": "X = 5\n"}, "u")
    p = Path(work) / "out"
    p.mkdir(exist_ok=True)
    (p / "synth.json").write_text(artifact(
        stage_facts=_facts(), unreadable=True))
    drain(proj, work)
    n = node(proj, sha)
    assert n.state == State.OPEN
    assert "checkpoint" in (n.metrics.get("_gaps") or {})


def test_journal_protected_write_rejects(tmp_path):
    """§12.3 / B.3: a workspace write to a protected path violates only
    when the content it left differs from the baseline blob — a write
    that restores the golden content is not flagged."""
    import subprocess as _sp

    from guardci.checks.recipe_scope import RecipeScope
    from guardci.contract.loader import Contract
    work = tmp_path / "repo"
    work.mkdir()
    def g(*a):
        return _sp.run(["git", "-C", str(work), *a], check=True,
                       capture_output=True, text=True).stdout.strip()
    g("init", "-q")
    (work / "prot").mkdir()
    (work / "prot/golden.txt").write_text("x\n")
    g("add", "-A")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
    sha = g("rev-parse", "HEAD")
    base = sha
    contract = Contract(name="t")
    contract.protected = ["prot/**"]
    contract.recipe = ["**"]
    checker = RecipeScope()
    def run_cp(cp):
        ctx = {"contract": contract, "repo": str(work),
               "baseline_sha": base, "changed_paths": [],
               "baseline_paths": [],
               "node": SimpleNamespace(sha=sha), "checkpoint": cp}
        return checker.run(ctx, {}, None)
    # tampered content vs baseline blob → "(workspace)" violation
    import hashlib
    from types import SimpleNamespace
    def blob(t):
        b = t.encode()
        return hashlib.sha1(b"blob %d\0" % len(b) + b).hexdigest()
    r = run_cp({"journal": [{"op": "modify", "path": "prot/golden.txt"}],
                "file_blobs": {"prot/golden.txt": blob("tampered\n")}})
    assert r.status == "fail" and "(workspace)" in r.detail
    # restore to baseline content → not a violation
    r = run_cp({"journal": [{"op": "modify", "path": "prot/golden.txt"}],
                "file_blobs": {"prot/golden.txt": blob("x\n")}})
    assert r.status == "pass", r.detail
    # delete → violation
    r = run_cp({"journal": [{"op": "delete", "path": "prot/golden.txt"}],
                "file_blobs": {}})
    assert r.status == "fail"
    # new protected path absent from baseline → not a violation
    r = run_cp({"journal": [{"op": "add", "path": "prot/new.txt"}],
                "file_blobs": {"prot/new.txt": blob("n\n")}})
    assert r.status == "pass", r.detail


def test_stale_artifact_warned(tmp_path):
    proj, work = _wpipe(tmp_path)
    sha = commit(work, {"config.mk": "X = 6\n"}, "stale")
    old = time.time() - 100
    art = _write_artifact(work, "synth", stage_facts=_facts())
    os.utime(art, (old, old))          # artifact older than sources
    drain(proj, work)
    n = node(proj, sha)
    assert n.state == State.WARNED
    assert n.metrics["provenance"] == "stale"
    offenders = n.metrics.get("provenance_offenders") or []
    assert any("config.mk" in o for o in offenders)


def test_pending_and_signoff_reliable(tmp_path):
    """final-stage artifact → all clauses decided, coverage full."""
    proj, work = _wpipe(tmp_path)
    sha = commit(work, {"config.mk": "X = 7\n"}, "full")
    for s in ("synth", "place", "finish"):
        _write_artifact(work, s, stage_facts=_facts(),
                        netlist=f"module {s}; endmodule\n")
    drain(proj, work)
    n = node(proj, sha)
    assert n.metrics["stage"] == "finish"
    assert node_coverage(Project(proj), sha) == "full"
    assert n.state == State.VERIFIED
    vs = {v.clause.name: v.status
          for v in Project(proj).store.verdicts(sha)}
    # reproducible stays pending until deliver; everything else decided
    assert vs["reproducible"] == "pending"
    assert vs["fake_heavy"] == "pass"


def test_deliver_contract_mode(tmp_path, monkeypatch):
    proj, work = _wpipe(tmp_path)
    sha = commit(work, {"config.mk": "X = 8\n"}, "d")
    for s in ("synth", "place", "finish"):
        _write_artifact(work, s, stage_facts=_facts(),
                        netlist=f"module {s}; endmodule\n")
    drain(proj, work)
    from guardci.core import deliver as dmod
    p = Project(proj)
    try:
        n = p.store.get_node(sha)
        best, repro = dmod.deliverable(p, [n])
        assert best.sha == n.sha
        assert repro["exact"] is True
        assert repro["result"].ok
        assert best.metrics["reproducible_exact"] is True
        n2 = p.store.get_node(sha)
        assert n2.metrics["reproducible_exact"] is True
    finally:
        p.close()


def test_deliver_fails_next_candidate(tmp_path):
    proj, work = _wpipe(tmp_path)
    sha1 = commit(work, {"config.mk": "X = 9\n"}, "d1")
    sha2 = commit(work, {"config.mk": "X = 10\n"}, "d2")
    for s in ("synth", "place", "finish"):
        _write_artifact(work, s, stage_facts=_facts(),
                        netlist=f"module {s}; endmodule\n")
    drain(proj, work)
    from guardci.core import deliver as dmod
    p = Project(proj)
    try:
        nodes = [p.store.get_node(sha2), p.store.get_node(sha1)]
        # sha2's rebuild fails reproducibility → next candidate wins
        orig = dmod._rebuild_and_check
        dmod._rebuild_and_check = \
            lambda p, n, mode, keep_workdir=False: (n.sha != sha2,
                                                  None)
        try:
            best, _repro = dmod.deliverable(p, nodes)
        finally:
            dmod._rebuild_and_check = orig
        assert best.sha == sha1
        assert p.store.get_node(sha2).state == State.OPEN
        assert "reproducible" in (p.store.get_node(sha2).metrics
                                  .get("_gaps") or {})
    finally:
        p.close()


def test_rebuild_source_unchanged(tmp_path):
    """`source: rebuild` stays byte-for-byte: no checkpoint captured,
    coverage stays full/partial vocabulary."""
    contract = dict(ACONTRACT)
    contract["verification"] = {"mode": "full", "source": "rebuild",
                                "fallback": "rebuild"}
    proj, work = _wpipe(tmp_path, contract)
    sha = commit(work, {"config.mk": "X = 11\n"}, "rb")
    drain(proj, work)
    n = node(proj, sha)
    assert "_checkpoint" not in (n.metrics or {})
    assert (n.metrics or {}).get("provenance") is None
    assert not (proj / "artifacts" / sha / "checkpoint"
                / "checkpoint.json").exists()


def test_rejudge_idempotent_stages(tmp_path):
    proj, work = _wpipe(tmp_path)
    sha = commit(work, {"config.mk": "X = 12\n"}, "rj")
    _write_artifact(work, "synth", stage_facts=_facts())
    drain(proj, work)
    # pending verdicts survive a rejudge attempt unchanged
    from guardci.core.rejudge import rejudge as core_rejudge
    p = Project(proj)
    try:
        core_rejudge(p)
    finally:
        p.close()
    vs = {v.clause.name: v.status
          for v in Project(proj).store.verdicts(sha)}
    assert vs["fake_heavy"] == "pending"
    assert node_coverage(Project(proj), sha) == "stage"


def test_unreadable_fallback_rebuild(tmp_path):
    """unreadable artifact + fallback: rebuild → today's build job."""
    proj, work = _wpipe(tmp_path)   # ACONTRACT fallback=rebuild
    sha = commit(work, {"config.mk": "X = 5\n"}, "u")
    p = Path(work) / "out"
    p.mkdir(exist_ok=True)
    (p / "synth.json").write_text(artifact(
        stage_facts=_facts(), unreadable=True))
    drain(proj, work)
    n = node(proj, sha)
    # fallback job built the node → full rebuild path verdicts
    assert n.state == State.VERIFIED
    jobs = Project(proj).store.jobs_for(sha)
    assert [j["source"] for j in jobs] == ["artifact", "rebuild"]


def test_stage_gate_place(tmp_path):
    """A `stage:` clause evaluates on that stage's facts and can leave
    the node open at the gate stage (§12.4)."""
    contract = dict(ACONTRACT)
    contract["clauses"] = list(ACONTRACT["clauses"]) + [
        {"check": "metric_bound", "id": "gate",
         "metric": "est_setup_wns", "min": -0.01,
         "stage": "place", "kind": "exit", "severity": "hard"}]
    proj, work = _wpipe(tmp_path, contract)
    facts = dict(_facts())
    facts["est_setup_wns"] = -0.5
    sha = commit(work, {"config.mk": "X = 13\n"}, "gate")
    _write_artifact(work, "synth", stage_facts=_facts())
    _write_artifact(work, "place", stage_facts=facts)
    drain(proj, work)
    n = node(proj, sha)
    assert n.state == State.OPEN
    assert n.metrics["stage"] == "place"
    vs = {v.clause.name: v.status
          for v in Project(proj).store.verdicts(sha)}
    assert vs["metric_bound:gate"] == "fail"
    assert vs["fake_heavy"] == "pending"      # beyond the node stage


def test_same_stage_ratchet_reference(tmp_path):
    """§12.5: the ratchet reference is the nearest accepted ancestor
    with facts at the *same* stage — a synth-only ancestor is skipped
    when judging a place node."""
    contract = dict(ACONTRACT)
    contract["edges"] = [{"metric": "cell_area", "no_regress": True,
                          "severity": "warn"}]
    proj, work = _wpipe(tmp_path, contract)
    sha1 = commit(work, {"config.mk": "X = 20\n"}, "r1")
    _write_artifact(work, "synth", stage_facts=_facts())
    drain(proj, work)
    assert node(proj, sha1).state == State.VERIFIED
    sha2 = commit(work, {"config.mk": "X = 21\n"}, "r2")
    _write_artifact(work, "place", stage_facts=_facts())
    drain(proj, work)
    n2 = node(proj, sha2)
    assert n2.metrics["stage"] == "place"
    runs = [r for r in Project(proj).store.check_runs(sha2)
            if r.checker == "edges"]
    assert runs
    # sha1 only reached synth → skipped; the reference falls back to
    # the baseline root
    assert runs[-1].metrics["reference"] == baseline_sha(proj)



def test_provenance_constant_git_calls(tmp_path, monkeypatch):
    """§12.3: provenance evaluation issues ONE git call regardless of
    the number of hashed files (ls-tree over `git show` per file)."""
    from guardci.core import provenance as prov_mod
    calls = []
    orig = prov_mod.subprocess.run

    def counting(*a, **kw):
        calls.append(a[0] if a else kw.get("args"))
        return orig(*a, **kw)

    monkeypatch.setattr(prov_mod.subprocess, "run", counting)
    cp = {"file_blobs": {f"f{i}": "x" for i in range(5000)},
          "provenance_inputs": {}, "artifact_times": {},
          "file_times": {}}
    prov_mod.evaluate("/nonexistent", "deadbeef", cp, {})
    assert len(calls) == 1
    assert calls[0][3] == "ls-tree"


# ------------------------------------------------- chain status (W2.1)

class ChainEquiv(Backend):
    """Per-content control of the §11.5 chain compares: `gold_status`
    maps a *netlist text* to the full-tier status the gold-vs-net
    compare returns; `link` maps (prev text, net text) to the
    one-to-one `lec_result`."""

    provides = "lec"
    tool = "chaineq"
    version = "w21.1"
    gold_status: ClassVar[dict] = {}
    link: ClassVar[dict] = {}
    calls: ClassVar[list] = []

    def compare(self, a, b, params, req, tiers=None):
        at = Path(a).read_text()
        bt = Path(b).read_text()
        self.calls.append((at.strip(), bt.strip(), tiers))
        if tiers == ("one_to_one",):
            r = self.link.get((at, bt), "identical")
            return {"lec_result": r,
                    "lec_differences": 0 if r == "identical" else 3}
        st = self.gold_status.get(bt, "proven")
        return {"equivalence_status": st,
                "lec_result": ("identical" if st in
                               ("proven", "proven_sequential")
                               else "different"),
                "lec_differences": 0, "equivalence_tiers": [],
                "equivalence_seconds": 0.01}


backends_base.register(ChainEquiv)

ACONTRACT_EQ = dict(ACONTRACT, lec_tool="chaineq",
                    clauses=list(ACONTRACT["clauses"]) + [
                        {"check": "equivalence", "kind": "invariant",
                         "severity": "hard"}])


def _chain_pipe(tmp_path):
    proj, work = _wpipe(tmp_path, ACONTRACT_EQ)
    cfg = yaml.safe_load((proj / "guardci.yaml").read_text())
    cfg["backends"]["lec"] = "chaineq"
    (proj / "guardci.yaml").write_text(yaml.safe_dump(cfg))
    ChainEquiv.calls = []
    ChainEquiv.gold_status = {}
    ChainEquiv.link = {}
    return proj, work


def _push2(proj, work, n1, n2, link="different",
           s1="proven", s2="proven"):
    ChainEquiv.gold_status = {n1: s1, n2: s2}
    ChainEquiv.link = {(n1, n2): link}
    commit(work, {"config.mk": "X = 2\n"}, "a1")
    _write_artifact(work, "synth", stage_facts=_facts(), netlist=n1)
    _write_artifact(work, "place", stage_facts=_facts(), netlist=n2)
    sha = commit(work, {"config.mk": "X = 3\n"}, "b1")
    drain(proj, work)
    return sha


def test_chain_weakest_status_bounded(tmp_path):
    proj, work = _chain_pipe(tmp_path)
    n1 = "module a; endmodule\n"
    n2 = "module b; endmodule\n"
    sha = _push2(proj, work, n1, n2, s1="proven", s2="bounded")
    m = node(proj, sha).metrics
    assert m["equivalence_status"] == "bounded"
    assert m["equivalence_by_stage"] == {"synth": "proven",
                                       "place": "bounded"}


def test_chain_unknown_first_stays_unknown(tmp_path):
    proj, work = _chain_pipe(tmp_path)
    n1 = "module a; endmodule\n"
    n2 = "module b; endmodule\n"
    sha = _push2(proj, work, n1, n2, s1="unknown", s2="proven")
    m = node(proj, sha).metrics
    # weakest link wins: an undecided gold-vs-synth is not masked
    # by a proven later stage
    assert m["equivalence_status"] == "unknown"


def test_chain_identical_links_proven(tmp_path):
    proj, work = _chain_pipe(tmp_path)
    n1 = "module a; endmodule\n"
    n2 = "module b; endmodule\n"
    sha = _push2(proj, work, n1, n2, link="identical",
                 s1="proven", s2="proven")
    m = node(proj, sha).metrics
    assert m["equivalence_status"] == "proven"
    assert m["equivalence_by_stage"]["place"] == "proven"


def test_chain_refuted_later_stage(tmp_path):
    proj, work = _chain_pipe(tmp_path)
    n1 = "module a; endmodule\n"
    n2 = "module b; endmodule\n"
    sha = _push2(proj, work, n1, n2, s1="proven", s2="refuted")
    n = node(proj, sha)
    assert n.metrics["equivalence_status"] == "refuted"
    assert n.state == State.REJECTED
