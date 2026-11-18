"""`guardci rejudge` (DESIGN.md §11.7): re-evaluate every node's stored
metrics under the current contract without building anything.

For every node in topological (parent-first) order a new attempt is
started, the static checkers re-run, and — when the node carries stored
measurement metrics — every contract clause is judged from those
metrics (no executor, no job, no memo lookup). Taint is recomputed from
scratch. Idempotent: a second run changes no state.
"""

from __future__ import annotations

from . import scheduler as sched_mod


def _topo_order(store) -> list[str]:
    """Parent-first order over all recorded nodes."""
    order: list[str] = []
    seen: set[str] = set()
    roots = [n.sha for n in store.all_nodes() if n.parent is None]
    stack = list(reversed(roots))
    while stack:
        sha = stack.pop()
        if sha in seen:
            continue
        seen.add(sha)
        order.append(sha)
        for ch in reversed(store.children(sha)):
            if ch not in seen:
                stack.append(ch)
    # nodes not reachable from a root (shouldn't happen): append last
    for n in store.all_nodes():
        if n.sha not in seen:
            order.append(n.sha)
    return order


def rejudge(project) -> dict:
    """Re-judge every node; returns counts ``{old_state: {new: n}}``."""
    store = project.store
    counts: dict = {}
    project.rejudging = True
    try:
        for sha in _topo_order(store):
            node = store.get_node(sha)
            if node is None:
                continue
            old = node.state
            sched_mod._process_sha(project, sha)
            new = store.get_node(sha).state
            counts.setdefault(old.value, {})[new.value] = \
                counts.get(old.value, {}).get(new.value, 0) + 1
            store.log_event("rejudged", sha,
                            {"old": old.value, "new": new.value})
    finally:
        project.rejudging = False
    return counts
