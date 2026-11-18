from guardci.core.models import Node, State
from guardci.core.store import Store
from guardci.core.tree import descendants, propagate_taint


def _chain(store):
    """a -> b -> c, plus branch b -> d. Returns shas."""
    for sha, parent in (("a" * 40, None), ("b" * 40, "a" * 40),
                        ("c" * 40, "b" * 40), ("d" * 40, "b" * 40)):
        store.upsert_node(Node(sha=sha, parent=parent))
    return "a" * 40, "b" * 40, "c" * 40, "d" * 40


def test_protected_taint(tmp_path):
    store = Store(tmp_path / "s.db")
    a, b, c, d = _chain(store)
    propagate_taint(store, b, "protected")
    assert store.get_node(c).state == State.TAINTED
    assert store.get_node(d).state == State.TAINTED
    assert store.get_node(a).state == State.PENDING
    store.close()


def test_metric_taint_is_a_noop(tmp_path):
    """§3.2: a metric failure does not taint or re-verify descendants —
    a node's verdict depends on its own content plus edge clauses."""
    store = Store(tmp_path / "s.db")
    _a, b, c, d = _chain(store)
    store.set_state(c, State.VERIFIED)
    store.set_state(d, State.WARNED)
    propagate_taint(store, b, "metric")
    assert store.get_node(c).state == State.VERIFIED
    assert store.get_node(d).state == State.WARNED
    assert not store.queued()
    store.close()


def test_descendants(tmp_path):
    store = Store(tmp_path / "s.db")
    a, b, c, d = _chain(store)
    assert set(descendants(store, a)) == {b, c, d}
    assert set(descendants(store, b)) == {c, d}
    assert descendants(store, c) == []
    store.close()
