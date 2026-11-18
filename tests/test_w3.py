"""W3 — default protection/tradable, void modes, public vocabulary,
paper contract figure, delivery bundle."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from guardci.contract import loader as cl
from guardci.contract.schema import ContractError
from guardci.core import edges
from guardci.core.models import Clause, Edge, Node, Severity, State, public_state
from guardci.core.store import Store

BIN = Path(sys.executable).parent


def _contract_default(edges_list=(), tradable=(), protection=None,
                      **kw):
    """A resolved-contract stand-in (loader output shape)."""
    c = cl.Contract(name="t", protection=protection or {},
                    tradable=list(tradable), **kw)
    c.edges = [Edge(metric=m, severity=s, params=p)
               for m, s, p in edges_list]
    if c.protection_mode == "default":
        from guardci.core.protection import PROTECTED_DIMENSIONS, default_edge_params
        have = {e.metric for e in c.edges} | set(c.tradable)
        for dim, spec in PROTECTED_DIMENSIONS.items():
            if dim not in have:
                c.edges.append(Edge(
                    metric=dim, severity=Severity.HARD,
                    params=default_edge_params(
                        spec, c.protection_rel_tol)))
    return c


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / "s.db")
    yield st
    st.close()


def _node(store, sha, parent, metrics=None, state=State.VERIFIED):
    n = Node(sha=sha, parent=parent, state=state,
             metrics=metrics or {})
    store.upsert_node(n)
    return n


import itertools

_CTR = itertools.count()


def _verdict(store, ref_metrics, node_metrics, metric,
             edge_params=None):
    tag = f"{next(_CTR):02x}"
    root = _node(store, ("r" + tag).ljust(40, "0"), None, ref_metrics)
    n = _node(store, ("n" + tag).ljust(40, "1"), root.sha,
              node_metrics)
    # edge_params None → rely on the implicit default-protection edge;
    # a dict → an explicit ``edges:`` entry that replaces the defaults
    el = [] if edge_params is None else [
        (metric, Severity.HARD, {"no_regress": True, **edge_params})]
    c = _contract_default(edges_list=el)
    run, verdicts = edges.evaluate(store, n.sha, n, c, 1)
    got = [(v.clause.check, v.status) for v in verdicts]
    assert got, f"no edge verdicts: run={run.status} ref={run.metrics}"
    return next(v for v in verdicts
                if v.clause.check == f"edge:{metric}")


# ------------------------------------------------------- §1 comparators

def test_min_period_rel_tol(store):
    v = _verdict(store, {"min_period": 0.4}, {"min_period": 0.41},
                 "min_period", {"rel_tol": 0.05})
    assert v.status == "pass"          # +2.5% within the bound
    v = _verdict(store, {"min_period": 0.4}, {"min_period": 0.5},
                 "min_period", {"rel_tol": 0.05})
    assert v.status == "fail"          # +25% worse
    # an edges: entry without params replaces the defaults — bound 0
    v = _verdict(store, {"min_period": 0.4}, {"min_period": 0.41},
                 "min_period", {})
    assert v.status == "fail"


def test_counts_any_increase_fails(store):
    v = _verdict(store, {"drc_count": 0}, {"drc_count": 1}, "drc_count")
    assert v.status == "fail"
    v = _verdict(store, {"unrouted_nets": 3}, {"unrouted_nets": 3},
                 "unrouted_nets")
    assert v.status == "pass"


def test_ir_drop_bound(store):
    # bound = max(0.005, 0.5 x ref): ref 0.05 → 0.025
    v = _verdict(store, {"ir_drop": {"VDD": {"drop": 0.05}}},
                 {"ir_drop": {"VDD": {"drop": 0.07}}},
                 "ir_drop_worst")
    assert v.status == "pass"          # +0.02 < 0.025
    v = _verdict(store, {"ir_drop": {"VDD": {"drop": 0.05}}},
                 {"ir_drop": {"VDD": {"drop": 0.08}}},
                 "ir_drop_worst")
    assert v.status == "fail"          # +0.03 > 0.025


def test_hold_wns_clamp(store):
    # positive slack is not protected: 0.10 → 0.02 is not a regression
    v = _verdict(store, {"hold_wns": 0.10}, {"hold_wns": 0.02},
                 "hold_wns")
    assert v.status == "pass"
    # 0.02 → -0.01 is a real loss (delta -0.03 > abs tol 0.001)
    v = _verdict(store, {"hold_wns": 0.02}, {"hold_wns": -0.01},
                 "hold_wns")
    assert v.status == "fail"
    # tiny loss within tolerance
    v = _verdict(store, {"hold_wns": 0.0}, {"hold_wns": -0.0005},
                 "hold_wns")
    assert v.status == "pass"


def test_tapcell_density_under_smaller_core(store):
    cbt = lambda n: {"cells_by_type": {"tapcell": {"count": n}}}
    # 10% fewer cells on the same core → density -10% > rel_tol 5%
    v = _verdict(store, {**cbt(46), "core_area": 1000},
                 {**cbt(41), "core_area": 1000}, "tapcell_density")
    assert v.status == "fail"
    # 10% fewer cells on a 20% smaller core → density rises → pass
    v = _verdict(store, {**cbt(46), "core_area": 1000},
                 {**cbt(41), "core_area": 800}, "tapcell_density")
    assert v.status == "pass"


def test_pdn_density_fixed_tol(store):
    # pdn default rel_tol is a fixed 0.10, not the contract's
    pdn = lambda n: {"pdn_wires_by_layer": {"m1": n}}
    v = _verdict(store, {**pdn(100), "core_area": 100},
                 {**pdn(92), "core_area": 100}, "pdn_density")
    assert v.status == "pass"          # -8% < 10%
    v = _verdict(store, {**pdn(100), "core_area": 100},
                 {**pdn(85), "core_area": 100}, "pdn_density")
    assert v.status == "fail"          # -15% > 10%


def test_same_stage_reference(store):
    """The protected comparison reads the reference's facts at the
    *same* stage, not its deepest-stage metrics."""
    ref_m = {"stage": "finish", "cell_area": 900,
             "stages": {"floorplan": {"cell_area": 700},
                        "finish": {"cell_area": 900}}}
    node_m = {"stage": "floorplan",
              "stages": {"floorplan": {"cell_area": 800}},
              "cell_area": 800}
    v = _verdict(store, ref_m, node_m, "cell_area")
    # vs the floorplan reference (700): +14% worse → fail;
    # a top-level comparison (900) would have passed
    assert v.status == "fail"


# -------------------------------------------- §1 contract-level options

def _load(tmp_path, doc, name="c.yaml"):
    p = tmp_path / name
    p.write_text(yaml.safe_dump(doc))
    return cl.load(str(p))


BASE_DOC = {"name": "t", "protected": ["p/**"], "recipe": ["**"],
            "clauses": [{"check": "recipe_scope", "kind": "invariant"}],
            "routing": {}}


def test_tradable_removes_clause(tmp_path):
    c = _load(tmp_path, {**BASE_DOC, "tradable": ["power_total"]})
    metrics = {e.metric for e in c.edges}
    assert "power_total" not in metrics
    assert "min_period" in metrics    # still protected


def test_edges_override_protected(tmp_path):
    c = _load(tmp_path, {**BASE_DOC,
                         "edges": [{"metric": "cell_area",
                                    "rel_tol": 0.5,
                                    "severity": "warn"}]})
    e = next(e for e in c.edges if e.metric == "cell_area")
    assert e.severity == Severity.WARN and e.params["rel_tol"] == 0.5


def test_unknown_tradable_errors(tmp_path):
    with pytest.raises(ContractError, match="tradable"):
        _load(tmp_path, {**BASE_DOC, "tradable": ["bogus_dim"]})


def test_explicit_mode_unchanged(tmp_path):
    c = _load(tmp_path, {**BASE_DOC,
                         "protection": {"mode": "explicit"},
                         "edges": [{"metric": "hold_wns",
                                    "severity": "warn"}]})
    assert [e.metric for e in c.edges] == ["hold_wns"]


def _resolved(c):
    """The paper rules a resolved pack applies, as plain data."""
    return {
        "clauses": [{"check": x.check, "id": x.id, "kind": x.kind,
                     "severity": x.severity.value,
                     "params": x.params} for x in c.clauses],
        "edges": [{"metric": e.metric, "severity": e.severity.value,
                   "params": e.params} for e in c.edges],
        "protected": c.protected,
        "tradable": c.tradable,
        "protection_mode": c.protection_mode,
        "protection_rel_tol": c.protection_rel_tol,
        "void_mode": c.void_mode,
    }


def test_rebuild_packs_judge_by_the_default_rules():
    """A rebuild pack replays under the SAME rules as its default pack
    — the only difference is the evidence source (rebuild vs
    artifact)."""
    for reb, dft in (("orfs-rebuild", "orfs-default"),
                     ("phb-pa80-rebuild", "phb-pa80")):
        c_reb, c_dft = cl.load(reb), cl.load(dft)
        r_reb, r_dft = _resolved(c_reb), _resolved(c_dft)
        assert r_reb == r_dft
        v_reb, v_dft = c_reb.verification, c_dft.verification
        assert {k: v for k, v in v_reb.items()
                if k not in ("source", "fallback")} ==                {k: v for k, v in v_dft.items()
                if k not in ("source", "fallback")}
        assert v_reb["source"] == v_reb["fallback"] == "rebuild"
        assert v_dft["source"] == "artifact"
        assert v_dft["fallback"] == "none"


# ------------------------------------------------- §6 the paper figure

FIGURE = """\
protected: [designs/src/**, platforms/**,
            designs/*/*/constraint.sdc]
clauses:
  # invariants: violated -> violation
  - {check: library_closure,   kind: invariant}
  - {check: equivalence, kind: invariant}
  # exit criteria: unmet -> unfinished
  - {check: sta_setup, kind: exit,
     max_period: {ratio: 0.8, of: baseline}}
  - {check: sta_hold,  kind: exit, min_wns: 0}
  - {check: drc,       kind: exit, max: baseline}
# every other fact is protected by default
tradable: [power_total]
"""


def test_paper_figure_loads_verbatim(tmp_path):
    p = tmp_path / "figure.yaml"
    p.write_text(FIGURE)
    c = cl.load(str(p))
    kinds = {}
    for clause in c.clauses:
        kinds.setdefault(clause.kind or "?", []).append(clause.check)
    assert sorted(kinds["invariant"]) == ["equivalence", "library_closure"]
    assert sorted(kinds["exit"]) == ["drc", "sta_hold", "sta_setup"]
    assert c.tradable == ["power_total"]
    assert c.protection_mode == "default"
    protected = {e.metric for e in c.edges}
    assert "power_total" not in protected
    assert {"min_period", "cell_area", "tapcell_density",
            "pdn_density", "drc_count"} <= protected


# ----------------------------------------------------- §4 vocabulary

def test_public_state_mapping():
    assert public_state(State.REJECTED) == "violation"
    assert public_state(State.TAINTED) == "void"
    assert public_state(State.OPEN) == "unfinished"
    assert public_state(State.VERIFIED) == "accepted"
    assert public_state(State.WARNED) == "accepted"
    assert public_state(State.PENDING) == "pending"
    assert public_state("held") == "pending"    # old ledgers


def test_findings():
    """finding kind per failing clause: invariant→violation,
    exit/build→unfinished, edge:*→regression."""
    import types
    proj = types.SimpleNamespace(
        contract=types.SimpleNamespace(
            clauses=[Clause(check="library_closure", kind="invariant"),
                     Clause(check="sta_hold", kind="exit")]),
        checkers={})
    from guardci.core.status import finding_for
    assert finding_for(proj, "library_closure") == "violation"
    assert finding_for(proj, "sta_hold") == "unfinished"
    assert finding_for(proj, "build") == "unfinished"
    assert finding_for(proj, "edge:tapcell_density") == "regression"


def test_verdict_text():
    from guardci.core.status import verdict_text
    assert verdict_text({"verdict": "accepted",
                         "grade": "signoff"}) == "accepted (signoff)"
    assert verdict_text({"verdict": "accepted", "grade": "stage",
                         "stage": "place"}) == "accepted (stage: place)"
    assert verdict_text({"verdict": "void"}) == "void"


# ------------------------------------------------------ §2 void modes

def _e2e_project(tmp_path, void_mode, tag):
    """Mini pipeline: recipe_scope-only contract in the given
    verification.void mode."""
    src = tmp_path / f"src-{tag}"
    (src / "prot").mkdir(parents=True)
    (src / "prot" / "x.txt").write_text("golden\n")
    (src / "config.mk").write_text("X = 1\n")
    doc = {"name": f"t-{tag}",
           "protected": ["prot/**"],
           "recipe": ["**"],
           "verification": {"void": void_mode},
           "clauses": [{"check": "recipe_scope", "kind": "invariant",
                        "severity": "hard"}],
           "routing": {}}
    contract = tmp_path / f"c-{tag}.yaml"
    contract.write_text(yaml.safe_dump(doc))
    proj = tmp_path / f"proj-{tag}"
    r = subprocess.run(
        [str(BIN / "guardci"), "init", str(proj), "--contract",
         str(contract), "--from", str(src)],
        check=False, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    work = tmp_path / f"work-{tag}"
    subprocess.run(["git", "clone", str(proj / "remote.git"),
                    str(work)], check=True, capture_output=True)
    return proj, work


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True)


def _e2e_commit(work, path, content, msg):
    p = Path(work) / path
    p.write_text(content)
    _git(work, "add", "-A")
    _git(work, "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-qm", msg)
    _git(work, "push", "origin", "HEAD:main")
    return _git(work, "rev-parse", "HEAD").stdout.strip()


def _state(proj, sha):
    r = subprocess.run(
        [str(BIN / "guardci"), "status", sha, "--project", str(proj),
         "--json"], capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def _serve(proj):
    subprocess.run([str(BIN / "guardci"), "serve", "--once",
                    "--project", str(proj)],
                   capture_output=True, text=True, check=True)


@pytest.mark.parametrize("mode,restored", [
    ("descendants", "tainted"),     # void is permanent — a clean
                                    # descendant stays void
    ("until_restored", "verified"), # restoring the content clears it
])
def test_void_modes(tmp_path, mode, restored):
    proj, work = _e2e_project(tmp_path, mode, mode)
    sha_bad = _e2e_commit(work, "prot/x.txt", "hacked\n", "bad")
    _serve(proj)
    assert _state(proj, sha_bad)["state"] == "rejected"
    # descendant whose cumulative delta restores the protected file
    sha_fix = _e2e_commit(work, "prot/x.txt", "golden\n", "restore")
    _serve(proj)
    st = _state(proj, sha_fix)
    assert st["state"] == restored, st
    assert st["verdict"] == ("void" if restored == "tainted"
                             else "accepted")


# ------------------------------------------------------ §5 the bundle

def test_write_bundle(tmp_path, monkeypatch):
    """deliver --out ships the REBUILT products + verdict + manifest
    with verifiable sha256."""
    import types

    from guardci.core import deliver as dmod

    workdir = tmp_path / "wd"
    workdir.mkdir()
    (workdir / "6_final.odb").write_text("ODB")
    (workdir / "6_final.def").write_text("DEF")
    node = Node(sha="n" * 40, parent="p" * 40, state=State.WARNED,
                metrics={"reproducible_exact": True})
    proj = types.SimpleNamespace(
        contract=_contract_default(),
        store=None,
        flow=types.SimpleNamespace(
            delivery_products=lambda wd: {
                p.name: p for p in workdir.iterdir()}),
        paths={"remote": str(tmp_path)})
    monkeypatch.setattr(dmod, "public_status",
                        lambda p, s: {"verdict": "accepted",
                                      "grade": "signoff"})
    monkeypatch.setattr(dmod, "_baseline_sha_for",
                        lambda p, s: "b" * 40)
    res = types.SimpleNamespace(workdir=workdir)
    out = tmp_path / "delivery"
    manifest = dmod.write_bundle(proj, node, res, out)
    assert manifest["node"] == node.sha
    assert manifest["baseline"] == "b" * 40
    assert manifest["contract"]["sha256"]
    import hashlib
    for name, sha in manifest["files"].items():
        assert (out / name).exists()
        assert hashlib.sha256(
            (out / name).read_bytes()).hexdigest() == sha
    verdict = json.loads((out / "verdict.json").read_text())
    assert verdict["verdict"] == "accepted"
    assert (out / "6_final.odb").read_text() == "ODB"


@pytest.mark.parametrize("source", ["artifact", "rebuild"])
def test_cli_bundle_materializes_both_sources(tmp_path, monkeypatch, source):
    import importlib
    import types

    from click.testing import CliRunner

    from guardci.core import deliver as dmod
    from guardci.core import scheduler, status

    cli_mod = importlib.import_module("guardci.cli.main")
    wd = tmp_path / "rebuilt"
    wd.mkdir()
    for name in ("6_final.odb", "6_final.def"):
        (wd / name).write_text(name)
    node = Node(sha="a" * 40, parent=None, state=State.VERIFIED,
                metrics={"reproducible_exact": True})
    project = types.SimpleNamespace(
        contract=_contract_default(verification={"source": source}),
        store=types.SimpleNamespace(all_nodes=lambda: [node], verdicts=lambda sha: []),
        flow=types.SimpleNamespace(delivery_products=lambda work: {
            p.name: p for p in work.iterdir()}),
        paths={"remote": str(tmp_path)}, close=lambda: None)
    calls = []

    def materialize(p, candidates, keep_workdir=False):
        calls.append((p, candidates, keep_workdir))
        return node, {"exact": True, "result": types.SimpleNamespace(workdir=wd)}

    monkeypatch.setattr(cli_mod, "_project", lambda ctx: project)
    monkeypatch.setattr(scheduler, "node_coverage", lambda p, sha: "full")
    monkeypatch.setattr(dmod, "deliverable", materialize)
    monkeypatch.setattr(dmod, "_baseline_sha_for", lambda p, sha: "b" * 40)
    monkeypatch.setattr(status, "public_status", lambda p, sha: {
        "verdict": "accepted", "grade": "signoff"})
    monkeypatch.setattr(dmod, "public_status", status.public_status)
    out = tmp_path / "delivery"
    result = CliRunner().invoke(cli_mod.cli, ["deliver", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert calls == [(project, [node], True)]
    manifest = json.loads((out / "MANIFEST.json").read_text())
    assert {"6_final.odb", "6_final.def", "verdict.json"} == set(manifest["files"])
    assert not wd.exists()


def test_cli_bundle_disallows_no_rebuild(tmp_path):
    from click.testing import CliRunner

    from guardci.cli.main import cli

    result = CliRunner().invoke(cli, ["deliver", "--no-rebuild", "--out", str(tmp_path)])
    assert result.exit_code != 0
    assert "--no-rebuild" in result.output and "--out" in result.output


def test_bundle_requires_build_products(tmp_path):
    import types

    from guardci.core.deliver import write_bundle

    project = types.SimpleNamespace(flow=types.SimpleNamespace(delivery_products=lambda wd: {}))
    with pytest.raises(ValueError, match="products"):
        write_bundle(project, None, types.SimpleNamespace(workdir=tmp_path), tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_export_without_accepted_node_fails(tmp_path, monkeypatch):
    import importlib
    import types

    from click.testing import CliRunner

    cli_mod = importlib.import_module("guardci.cli.main")
    project = types.SimpleNamespace(
        store=types.SimpleNamespace(all_nodes=list), close=lambda: None)
    monkeypatch.setattr(cli_mod, "_project", lambda ctx: project)
    out = tmp_path / "delivery"
    result = CliRunner().invoke(cli_mod.cli, ["deliver", "--out", str(out)])
    assert result.exit_code != 0
    assert "no accepted nodes" in result.output
    assert not out.exists()
