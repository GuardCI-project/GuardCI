"""design_identity checker (static, hard, invariant).

The recipe must implement *the same design* as the golden baseline —
compared by **content** (§11.1/§11.2): same platform and top, and every
golden RTL blob must be present among the blobs of the resolved RTL set.
Extra RTL/techmap files are allowed (reported as ``extra_rtl``); a
resolved path that leaves the repository or does not exist fails.
``./designs/src/gcd/gcd.v`` or a redefined ``DESIGN_HOME`` resolving to
the same file passes because the blob hash is identical.
"""

from __future__ import annotations

import posixpath
import subprocess
from typing import ClassVar

from ..core.models import Cost
from .base import Checker, CheckResult, register


def _norm(path: str) -> str | None:
    """Repo-relative normalized path, or None when it escapes/abs."""
    if path.startswith("/"):
        return None
    p = posixpath.normpath(path)
    if p.startswith("..") or p == ".":
        return None
    return p


def _blobs(repo: str, sha: str, paths: set) -> tuple[dict, list]:
    """({path: blob sha}, missing paths) for resolved repo paths."""
    out: dict = {}
    bad = []
    for path in sorted(paths):
        r = subprocess.run(["git", "-C", repo, "rev-parse",
                            f"{sha}:{path}"],
                           check=False, capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout.strip():
            bad.append(path)
        else:
            out[path] = r.stdout.strip()
    return out, bad


@register
class DesignIdentity(Checker):
    name = "design_identity"
    cost = Cost.STATIC
    kind: ClassVar[str] = "invariant"
    requires: ClassVar[set] = set()

    def run(self, ctx: dict, params: dict, backend) -> CheckResult:
        repo = ctx["repo"]
        flow = ctx["flow"]
        design = ctx.get("design")
        changed = ctx.get("changed_paths")
        sha = ctx["node"].sha
        baseline_sha = ctx["baseline_sha"]
        try:
            protected = list(ctx["contract"].protected)
            head_id = flow.identity(repo, sha, design,
                                    changed, protected)
            base_id = flow.identity(repo, baseline_sha, design,
                                    changed, protected)
        except ValueError as exc:
            return CheckResult(
                status="fail",
                detail=f"cannot establish design identity: {exc}")

        # resolve file sets to repo-relative paths; escaping or missing
        # paths fail immediately
        head_files, bad = {}, []
        for f in head_id.rtl_files | head_id.blackboxes:
            p = _norm(f)
            if p is None:
                bad.append(f)
            else:
                head_files[f] = p
        if bad:
            return CheckResult(
                status="fail",
                detail="RTL path(s) leave the repository: "
                       + ", ".join(sorted(bad)))
        base_files = {f: _norm(f)
                      for f in base_id.rtl_files | base_id.blackboxes}
        if any(p is None for p in base_files.values()):
            return CheckResult(
                status="fail",
                detail="golden RTL path(s) leave the repository")

        head_map, missing = _blobs(repo, sha,
                                   set(head_files.values()))
        if missing:
            return CheckResult(
                status="fail",
                detail="rtl_files resolved path does not exist: "
                       + ", ".join(sorted(missing)))
        head_blobs = set(head_map.values())
        base_map, base_missing = _blobs(
            repo, baseline_sha, {p for p in base_files.values() if p})
        if base_missing:
            return CheckResult(
                status="fail",
                detail="golden RTL file(s) missing at baseline: "
                       + ", ".join(sorted(base_missing)))
        base_blobs = set(base_map.values())
        base_blob_names = {b: p for p, b in base_map.items()}

        metrics = {
            "platform": head_id.platform,
            "top": head_id.top,
            "rtl_files": len(head_id.rtl_files),
            "sdc_role": head_id.sdc_role,
            "sdc_file": head_id.sdc_file,
            "source": head_id.source,
        }
        mismatches = []
        for f in ("platform", "top", "params"):
            hv, bv = getattr(head_id, f), getattr(base_id, f)
            if hv != bv:
                mismatches.append(f"{f}: {bv!r} -> {hv!r}")
        missing_blobs = base_blobs - head_blobs
        if missing_blobs:
            miss = sorted(base_blob_names.get(b, b[:12])
                          for b in missing_blobs)
            mismatches.append(
                "golden RTL missing from the resolved set: "
                + ", ".join(miss))
        extra = head_blobs - base_blobs
        if extra:
            metrics["extra_rtl"] = len(extra)

        if head_id.sdc_role not in ("impl", "golden"):
            mismatches.append(
                f"SDC_FILE: {head_id.sdc_file!r} resolves to role "
                f"{head_id.sdc_role!r} (expected impl/golden in "
                f"{head_id.source})")

        if mismatches:
            return CheckResult(status="fail",
                               detail="; ".join(mismatches),
                               metrics=metrics)
        return CheckResult(status="pass", metrics=metrics)
