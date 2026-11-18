from guardci.checks.base import Checker
from guardci.contract.loader import Contract
from guardci.core.models import Clause, Cost, Node, Severity
from guardci.core.router import select_checks


class XChecker(Checker):
    name = "x"
    cost = Cost.LIGHT


class YChecker(Checker):
    name = "y"
    cost = Cost.HEAVY


class RecipeScopeStub(Checker):
    name = "recipe_scope"
    cost = Cost.STATIC


CHECKERS = {"x": XChecker, "y": YChecker,
            "recipe_scope": RecipeScopeStub}


def _contract():
    return Contract(
        name="t",
        protected=["src/**"],
        recipe=["config.mk"],
        clauses=[
            Clause(check="recipe_scope", severity=Severity.HARD),
            Clause(check="x", severity=Severity.HARD),
            Clause(check="y", severity=Severity.HARD),
        ],
        routing={"constraints": ["x"], "unknown": ["heavy"]},
        verification={"mode": "routed"},
    )


def test_full_mode_selects_everything():
    """§11.4 default (no verification key) = full: every clause, no
    routing needed."""
    contract = _contract()
    contract.verification = {}
    node = Node(sha="s" * 40, delta_tags=set())
    got = [c.check for c in select_checks(contract, node, CHECKERS)]
    assert got == ["recipe_scope", "x", "y"]


def test_constraints_tag():
    node = Node(sha="s" * 40, delta_tags={"constraints"})
    got = [c.check for c in select_checks(_contract(), node, CHECKERS)]
    assert got == ["recipe_scope", "x"]


def test_unknown_tag_expands_heavy():
    # heavy implies light (§9.2): the light clause runs too
    node = Node(sha="s" * 40, delta_tags={"unknown"})
    got = [c.check for c in select_checks(_contract(), node, CHECKERS)]
    assert got == ["recipe_scope", "x", "y"]


def test_requested_full():
    node = Node(sha="s" * 40, requested_full=True)
    got = [c.check for c in select_checks(_contract(), node, CHECKERS)]
    assert got == ["recipe_scope", "x", "y"]


def test_unregistered_checker_still_selected():
    contract = _contract()
    contract.clauses.append(Clause(check="ghost", severity=Severity.HARD))
    contract.routing["constraints"] = ["x", "ghost"]
    node = Node(sha="s" * 40, delta_tags={"constraints"})
    got = [c.check for c in select_checks(contract, node, CHECKERS)]
    assert "ghost" in got


def test_unregistered_clause_always_selected():
    # fail-closed: an unregistered checker must never be silently skipped
    contract = _contract()
    contract.clauses.append(Clause(check="foo", severity=Severity.HARD))
    for tags in ({"constraints"}, {"unknown"}, set()):
        node = Node(sha="s" * 40, delta_tags=tags)
        got = [c.check for c in select_checks(contract, node, CHECKERS)]
        assert "foo" in got
