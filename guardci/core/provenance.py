"""Provenance evaluation (§12.3, tool-free).

``consistent``  — every recipe-closure file's workspace content equals
                  the commit blob, and every stage artifact is newer
                  than the closure inputs attributed to stages ≤ s and
                  than the previous stage's artifact;
``stale``       — one of those fails (the artifact was not produced
                  from the committed recipe);
``unknown``     — no journal / no checkpoint;
``rebuilt``     — the CI built the node itself (rebuild source).
"""

from __future__ import annotations

import subprocess


def tree_blobs(repo: str, sha: str) -> dict:
    """``{path: blob id}`` for a commit — one ``git ls-tree`` call
    regardless of file count (§12.3)."""
    r = subprocess.run(["git", "-C", repo, "ls-tree", "-r", sha],
                       capture_output=True, check=False, text=True)
    if r.returncode != 0:
        return {}
    out = {}
    for line in r.stdout.splitlines():
        # "<mode> blob <blobid>\t<path>"
        try:
            meta, path = line.split("\t", 1)
            blob = meta.split()[2]
        except (ValueError, IndexError):
            continue
        out[path] = blob
    return out


def evaluate(repo: str, sha: str, checkpoint: dict,
             stage_order: dict) -> tuple[str, list]:
    """``(status, offenders)`` — offenders name the paths/stages that
    break the relation."""
    if not checkpoint:
        return "unknown", []
    file_blobs = checkpoint.get("file_blobs") or {}
    file_times = checkpoint.get("file_times") or {}
    inputs = checkpoint.get("provenance_inputs") or {}
    times = checkpoint.get("artifact_times") or {}
    if not file_blobs and not inputs:
        return "unknown", []
    offenders: list[str] = []
    # relation 1: workspace content of every hashed closure/protected
    # file equals the commit blob — one ls-tree for the whole tree
    tree = tree_blobs(repo, sha)
    for path, wh in file_blobs.items():
        if tree.get(path) != wh:
            offenders.append(path)
    # relation 2: stage artifact mtime > mtime of every closure file
    # attributed to stages ≤ s (and unattributable) and > previous
    # stage artifact mtime
    prev_t = 0.0
    for stage in sorted(inputs, key=lambda s: stage_order.get(s, 0)):
        t = times.get(stage)
        if t is None:
            continue
        stale_src = next(
            (p for p in inputs[stage]
             if (file_times.get(p) or 0.0) >= t), None)
        if stale_src is not None:
            offenders.append(f"{stage} (input {stale_src} newer)")
        if t <= prev_t:
            offenders.append(f"{stage} (not newer than previous stage)")
        prev_t = t
    return ("stale" if offenders else "consistent"), offenders
