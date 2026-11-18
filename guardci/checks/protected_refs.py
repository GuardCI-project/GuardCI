"""protected_refs checker (static, severity route).

Every path-like value in the recipe config must resolve inside the repo
and inside `protected ∪ recipe` globs. Absolute paths, ``..`` escapes,
missing files and paths outside the allowed set are "foreign" — a fail
here does not reject; it forces the heavy check set (DESIGN.md §4.2).
"""

from __future__ import annotations

from typing import ClassVar

from ..core.diff import list_tree
from ..core.models import Cost
from ..core.paths import match_any
from .base import Checker, CheckResult, register


@register
class ProtectedRefs(Checker):
    name = "protected_refs"
    cost = Cost.STATIC
    requires: ClassVar[set] = set()

    def run(self, ctx: dict, params: dict, backend) -> CheckResult:
        repo = ctx["repo"]
        head = ctx["node"].sha
        contract = ctx["contract"]
        allowed = list(contract.protected) + list(contract.recipe)
        tree = set(list_tree(repo, head))
        baseline_tree = set(list_tree(repo, ctx["baseline_sha"]))

        foreign = []
        for ref in ctx["flow"].references(repo, head):
            path = ref.path
            if path.startswith("/"):
                foreign.append(f"{ref.var}={path} (absolute path)")
            elif ".." in path.split("/"):
                foreign.append(f"{ref.var}={path} (parent traversal)")
            elif path not in tree:
                foreign.append(f"{ref.var}={path} (not in repo)")
            elif not match_any(path, allowed):
                foreign.append(f"{ref.var}={path} "
                               "(outside protected+recipe)")
            elif match_any(path, contract.protected) \
                    and path not in baseline_tree:
                foreign.append(f"{ref.var}={path} "
                               "(not in protected baseline)")

        if foreign:
            return CheckResult(status="fail",
                               detail="; ".join(foreign),
                               metrics={"foreign_refs": foreign})
        return CheckResult(status="pass", metrics={"foreign_refs": []})
