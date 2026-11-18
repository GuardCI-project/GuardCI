"""recipe_scope checker (static): protected baseline files may not
change.

The delta judged is the cumulative baseline-vs-head path set
(``ctx["baseline_paths"]``), so a commit that *restores* protected
content removed by an ancestor passes — its cumulative delta touches
nothing protected (DESIGN.md §3.2/§4).

Rules (evaluated over every path in the baseline delta):
- under `protected` and the path **exists in the baseline tree**
  (modify/delete of a golden file) → fail
- anything else (new files anywhere included)   → pass; brand-new
  paths are recorded in metrics.scratch_paths
"""

from __future__ import annotations

from typing import ClassVar

from ..core.diff import list_tree
from ..core.models import Cost
from ..core.paths import match_any
from .base import Checker, CheckResult, register


@register
class RecipeScope(Checker):
    name = "recipe_scope"
    cost = Cost.STATIC
    kind: ClassVar[str] = "invariant"

    def run(self, ctx: dict, params: dict, backend) -> CheckResult:
        contract = ctx["contract"]
        protected_hits = []
        scratch = []
        baseline_tree = set(list_tree(ctx["repo"], ctx["baseline_sha"]))
        for path in ctx.get("baseline_paths", ctx["changed_paths"]):
            if path not in baseline_tree:
                scratch.append(path)
            elif match_any(path, contract.protected):
                protected_hits.append(path)
        # §12.3: writes to protected paths in the checkpoint journal,
        # or a protected workspace file whose content differs from the
        # commit blob, are recipe-scope violations too ("(workspace)")
        cp = ctx.get("checkpoint") or {}
        ws_hits = []
        from ..core.provenance import tree_blobs
        commit_tree = tree_blobs(ctx["repo"], ctx["node"].sha)
        base_tree = tree_blobs(ctx["repo"], ctx["baseline_sha"])
        ws_blobs = cp.get("file_blobs") or {}
        # a journal event on a protected path is a violation only if
        # the content it left differs from the baseline blob — a write
        # that restores the golden content is not (§12.3)
        journaled = {}
        for ev in cp.get("journal") or []:
            p = ev.get("path") or ""
            if match_any(p, contract.protected):
                journaled[p] = ev.get("op")
        for p, op in journaled.items():
            # a journal event on a protected path is a violation only
            # if the content it left differs from the baseline blob —
            # restoring the golden content is not flagged; new files
            # under a protected glob absent from the baseline are not
            # violations (same as the commit-diff rule)
            if p in base_tree and (op == "delete"
                                   or ws_blobs.get(p)
                                   != base_tree.get(p)):
                ws_hits.append(f"{p} (workspace)")
        seen = set(ws_hits)
        for path, wh in ws_blobs.items():
            hit = f"{path} (workspace)"
            if match_any(path, contract.protected) and \
                    path in commit_tree and commit_tree[path] != wh \
                    and hit not in seen:
                ws_hits.append(hit)
                seen.add(hit)
        if protected_hits or ws_hits:
            detail = ("touches protected paths: "
                      + ", ".join(protected_hits + ws_hits))
            return CheckResult(status="fail", detail=detail,
                               metrics={"scratch_paths": scratch})
        return CheckResult(
            status="pass",
            metrics={"scratch_paths": scratch})
