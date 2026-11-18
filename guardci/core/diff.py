"""Diff model and git-backed construction (DESIGN.md §5.1 ``Diff``)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field


@dataclass
class DiffEntry:
    path: str
    kind: str                      # added | modified | deleted | renamed
    before: str | None = None
    after: str | None = None
    binary: bool = False
    old_path: str | None = None


@dataclass
class Diff:
    base: str | None
    head: str
    entries: list[DiffEntry] = field(default_factory=list)
    repo: str | None = None      # set by build_diff; adapters may need it
    baseline: str | None = None  # baseline root sha (set by the caller)

    def paths(self) -> list[str]:
        return [e.path for e in self.entries]

    def entry(self, path: str) -> DiffEntry | None:
        for e in self.entries:
            if e.path == path or e.old_path == path:
                return e
        return None


def _git(repo: str, *args: str, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args],
                          capture_output=True, check=check)


def read_file(repo: str, sha: str, path: str) -> str | None:
    """File content at <sha>:<path> as text; None if absent or non-UTF-8."""
    r = _git(repo, "show", f"{sha}:{path}", check=False)
    if r.returncode != 0:
        return None
    try:
        return r.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _read_bytes(repo: str, sha: str, path: str):
    r = _git(repo, "show", f"{sha}:{path}", check=False)
    if r.returncode != 0:
        return None, False
    try:
        return r.stdout.decode("utf-8"), False
    except UnicodeDecodeError:
        return None, True


def list_tree(repo: str, sha: str, prefix: str = "") -> list[str]:
    """All paths in the commit's tree (optionally under ``prefix``)."""
    args = ["ls-tree", "-r", "--name-only", sha]
    if prefix:
        args += ["--", prefix]
    out = _git(repo, *args).stdout.decode("utf-8", "replace")
    return [p for p in out.splitlines() if p]


def build_diff(repo: str, head: str, base: str | None) -> Diff:
    """name-status diff base..head with file contents attached."""
    args = ["diff-tree", "-r", "-M", "--name-status", "--no-commit-id"]
    if base is None:
        args.append("--root")
    out = _git(repo, *args, head).stdout.decode("utf-8", "replace")
    entries = []
    for line in out.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        code = fields[0]
        if code.startswith(("R", "C")):
            kind = "renamed"
            old_path, path = fields[1], fields[2]
        else:
            kind = {"A": "added", "M": "modified", "D": "deleted",
                    "T": "modified"}.get(code[0], "modified")
            old_path, path = None, fields[1]
        before = after = None
        binary = False
        if base is not None:
            src = old_path or path
            before, b = _read_bytes(repo, base, src)
            binary = binary or b
        if kind != "deleted":
            after, b = _read_bytes(repo, head, path)
            binary = binary or b
        entries.append(DiffEntry(path=path, kind=kind, before=before,
                                 after=after, binary=binary,
                                 old_path=old_path))
    return Diff(base=base, head=head, entries=entries, repo=repo)
