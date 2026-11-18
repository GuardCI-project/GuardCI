"""§9.7 edge/differential-table unit tests on the Store alone."""

import pytest

from guardci.contract.loader import Contract, Edge
from guardci.core import edges
from guardci.core.models import Node, Severity, State
from guardci.core.store import Store


def _contract(edge_params):
    # explicit protection mode: edges: entries keep the legacy
    # raw-metric ratchet semantics (hold_wns is protected in default
    # mode with the clamped extractor)
    return Contract(name="t", protected=[], recipe=[], clauses=[],
                    edges=[Edge(metric="hold_wns", severity=Severity.WARN,
                                params=edge_params)],
                    routing={}, protection={"mode": "explicit"})


def _contract_multi(edges_list):
    return Contract(name="t", protected=[], recipe=[], clauses=[],
                    edges=[Edge(metric=m, severity=s, params=p)
                           for m, s, p in edges_list],
                    routing={}, protection={"mode": "explicit"})


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


def test_nearest_measured_ancestor(store):
    root = _node(store, "r" * 40, None, {"hold_wns": 0.10})
    _node(store, "a" * 40, root.sha, {})            # no metrics → skipped
    leaf = _node(store, "b" * 40, "a" * 40, {"hold_wns": 0.05})
    ref, rows, _eff = edges.compute_rows(store, leaf.sha,
                                         leaf.metrics)
    assert ref == root.sha
    row = rows[0]
    assert row["parent_sha"] == root.sha
    assert row["parent"] == 0.10 and row["node"] == 0.05
    assert row["worse"]                            # lower hold = worse


def test_edge_no_regress_abs_tol(store):
    root = _node(store, "r" * 40, None, {"hold_wns": 0.10})
    contract = _contract({"no_regress": True, "tol": 0.05})
    n = _node(store, "a" * 40, root.sha, {"hold_wns": 0.08})
    _, verdicts = edges.evaluate(store, n.sha, n, contract, 1)
    assert [v.status for v in verdicts] == ["pass"]  # within tol
    n2 = _node(store, "b" * 40, root.sha, {"hold_wns": 0.01})
    _, verdicts = edges.evaluate(store, n2.sha, n2, contract, 1)
    assert len(verdicts) == 1
    assert verdicts[0].status == "fail"
    assert verdicts[0].clause.check == "edge:hold_wns"
    assert verdicts[0].clause.severity == Severity.WARN


def test_edge_rel_tol(store):
    root = _node(store, "r" * 40, None, {"hold_wns": 1.0})
    contract = _contract({"no_regress": True, "rel_tol": 0.1})
    n = _node(store, "a" * 40, root.sha, {"hold_wns": 0.95})
    _, verdicts = edges.evaluate(store, n.sha, n, contract, 1)
    assert [v.status for v in verdicts] == ["pass"]  # -0.05 ≤ 0.1×1.0
    n2 = _node(store, "b" * 40, root.sha, {"hold_wns": 0.8})
    _, verdicts = edges.evaluate(store, n2.sha, n2, contract, 1)
    assert len(verdicts) == 1 and verdicts[0].status == "fail"


def test_edge_cells_by_type_expansion(store):
    root = _node(store, "r" * 40, None,
                 {"cells_by_type": {"tapcell": {"count": 10},
                                    "endcap": {"count": 4}}})
    contract = _contract_multi([
        ("cells_by_type", Severity.WARN,
         {"no_regress": True, "rel_tol": 0.2})])
    n = _node(store, "a" * 40, root.sha,
              {"cells_by_type": {"tapcell": {"count": 9},
                                 "endcap": {"count": 4}}})
    _, verdicts = edges.evaluate(store, n.sha, n, contract, 1)
    assert [v.status for v in verdicts] == ["pass"]  # within 20%
    n2 = _node(store, "b" * 40, root.sha,
               {"cells_by_type": {"tapcell": {"count": 7},
                                  "endcap": {"count": 4}}})
    _, verdicts = edges.evaluate(store, n2.sha, n2, contract, 1)
    assert len(verdicts) == 1
    assert verdicts[0].clause.check == "edge:cells_by_type"
    assert "tapcell" in verdicts[0].detail


def test_no_reference_no_verdict(store):
    n = _node(store, "a" * 40, None, {"hold_wns": 0.01})
    contract = _contract({"no_regress": True})
    run, verdicts = edges.evaluate(store, n.sha, n, contract, 1)
    assert verdicts == []                          # nothing to compare to
    assert run.metrics["reference"] is None


def test_lower_is_better(store):
    root = _node(store, "r" * 40, None, {"drc_count": 0})
    contract = _contract_multi([
        ("drc_count", Severity.WARN, {"no_regress": True})])
    n = _node(store, "a" * 40, root.sha, {"drc_count": 3})
    run, verdicts = edges.evaluate(store, n.sha, n, contract, 1)
    row = run.metrics["rows"][0]
    assert row["worse"] is True                    # more drc = worse
    assert verdicts[0].status == "fail"            # tol 0 → any worsen


def test_replace_within_attempt(store):
    root = _node(store, "r" * 40, None, {"hold_wns": 0.1})
    n = _node(store, "a" * 40, root.sha, {"hold_wns": 0.05})
    contract = _contract({"no_regress": True})
    run, v = edges.evaluate(store, n.sha, n, contract, 1)
    store.replace_check_run(run)
    for x in v:
        store.add_verdict(x)
    # re-evaluation in the same attempt replaces the row in place
    run2, _v2 = edges.evaluate(store, n.sha, n, contract, 1)
    store.replace_check_run(run2)
    runs = [r for r in store.check_runs(n.sha) if r.checker == "edges"]
    assert len(runs) == 1
