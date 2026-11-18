"""Helpers: turn a fixture directory into a scratch git repo."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, check=True, text=True).stdout


def make_repo(tmp_path: Path, fixture: str = "orfs-mini") -> Path:
    src = FIXTURES / fixture
    repo = tmp_path / "repo"
    shutil.copytree(src, repo)
    _git(repo, "init", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-m", "baseline", "--quiet")
    return repo


def head(repo) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def commit_all(repo, message="change") -> str:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-m", message, "--quiet")
    return head(repo)


def edit(repo, path, fn):
    p = Path(repo) / path
    p.write_text(fn(p.read_text()))
    return p
