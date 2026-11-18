"""Pure DAG logic over the store — no git calls here (DESIGN.md §3.2)."""

from __future__ import annotations

from .models import State


def descendants(store, sha: str) -> list[str]:
    """All transitive children of sha (excluding sha itself)."""
    out = []
    stack = list(store.children(sha))
    seen = set(stack)
    while stack:
        cur = stack.pop()
        out.append(cur)
        for ch in store.children(cur):
            if ch not in seen:
                seen.add(ch)
                stack.append(ch)
    return out


def ancestors(store, sha: str) -> list[str]:
    """First-parent chain from sha upwards (excluding sha itself)."""
    out = []
    node = store.get_node(sha)
    while node is not None and node.parent:
        out.append(node.parent)
        node = store.get_node(node.parent)
    return out


def propagate_taint(store, sha: str, kind: str, clean_fn=None):
    """Spread the consequences of a rejected node to its descendants.

    kind == "protected": every *dirty* descendant is marked ``tainted``.
        ``clean_fn(sha) -> bool`` decides whether a descendant's delta
        restored the violated protected content (DESIGN.md §3.2:
        tainted "until a descendant's delta restores the protected
        content"); clean descendants keep their state. When clean_fn is
        None every descendant taints (M0 behaviour).

    kind == "metric": descendants are not tainted and are not
        re-verified (DESIGN.md §3.2: a node's verdict depends only on
        its own content plus edge clauses; edge re-evaluation handles
        reference changes). This branch is a no-op.
    """
    if kind == "metric":
        return
    if kind == "void":
        # verification.void: descendants — a node rejected by any
        # invariant voids every descendant, permanently
        for desc in descendants(store, sha):
            node = store.get_node(desc)
            if node is None:
                continue
            if node.state not in (State.REJECTED, State.TAINTED):
                store.set_state(desc, State.TAINTED)
        return
    if kind != "protected":
        raise ValueError(f"unknown taint kind: {kind!r}")
    for desc in descendants(store, sha):
        node = store.get_node(desc)
        if node is None:
            continue
        if clean_fn is not None and clean_fn(desc):
            continue  # delta restored the protected content
        if node.state not in (State.REJECTED, State.TAINTED):
            store.set_state(desc, State.TAINTED)
