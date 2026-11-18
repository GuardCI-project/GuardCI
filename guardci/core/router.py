"""Check selection (DESIGN.md §3.3):

    checks(node) = static clauses
                 ∪ ⋃_{tag ∈ delta_tags} routing[tag]
                 ∪ (all clauses if requested_full)

The literal ``heavy`` inside a routing list means "all clauses whose
checker cost is heavy".  Checker costs come from the checker registry.

Fail-closed: a clause whose checker is **not registered** is always
selected — the scheduler records it ``unavailable`` and the node stays
``verifying`` rather than silently skipping a contract requirement.
"""

from __future__ import annotations

from .models import Clause, Cost, Node


def _checker_cost(checkers, name: str) -> Cost | None:
    cls = checkers.get(name)
    if cls is None:
        return None
    return getattr(cls, "cost", None)


def select_checks(contract, node: Node, checkers) -> list[Clause]:
    """Return the ordered, deduplicated clause set for a node."""
    by_name = {c.check: c for c in contract.clauses}

    selected: dict[str, Clause] = {}

    # static clauses run on every commit; unregistered checkers are
    # always selected (fail-closed — see module docstring)
    for clause in contract.clauses:
        cost = _checker_cost(checkers, clause.check)
        if cost == Cost.STATIC or cost is None:
            selected[clause.check] = clause

    # §11.4 mode full (default): the full check set on every commit;
    # mode routed keeps the §3.3 tag-routing behaviour
    mode = (contract.verification or {}).get("mode", "full")
    if mode != "routed":
        for clause in contract.clauses:
            selected[clause.check] = clause
        order = {c.check: i for i, c in enumerate(contract.clauses)}
        return sorted(selected.values(),
                      key=lambda c: order.get(c.check, 1 << 30))

    # routing expansion over delta tags
    for tag in sorted(node.delta_tags):
        for entry in contract.routing.get(tag, []):
            if entry == "heavy":
                # heavy implies light (§9.2): light clauses run too,
                # on the final artifacts
                for clause in contract.clauses:
                    cost = _checker_cost(checkers, clause.check)
                    if cost in (Cost.HEAVY, Cost.LIGHT) or cost is None:
                        selected[clause.check] = clause
            elif entry in by_name:
                selected[entry] = by_name[entry]

    # full verification on request
    if node.requested_full:
        for clause in contract.clauses:
            selected[clause.check] = clause

    # preserve contract order
    order = {c.check: i for i, c in enumerate(contract.clauses)}
    return sorted(selected.values(), key=lambda c: order.get(c.check, 1 << 30))
