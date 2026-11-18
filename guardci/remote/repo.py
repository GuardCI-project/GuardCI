"""Bare remote repository setup: git init --bare + post-receive hook."""

from __future__ import annotations

import shutil
import stat
import subprocess
import sys
from pathlib import Path


def _guardci_remote_bin() -> str:
    """Absolute path of the guardci-remote console script.

    The hook must call it by absolute path: git invokes hooks with a
    minimal environment and PATH does not include the venv's bin dir.
    """
    found = shutil.which("guardci-remote")
    if found:
        return str(Path(found).resolve())
    # do not resolve() sys.executable: in a venv it is a symlink to the
    # base interpreter whose directory does not contain the script
    cand = Path(sys.executable).parent / "guardci-remote"
    if cand.exists():
        return str(cand.resolve())
    return "guardci-remote"  # last resort; will fail loudly in the hook


def init_bare(path: str | Path, project: str | Path) -> Path:
    """Create a bare repo at ``path`` and install the ingest hook."""
    path = Path(path).resolve()
    project = Path(project).resolve()
    subprocess.run(["git", "init", "--bare", "-b", "main", str(path)],
                   check=True, capture_output=True)
    # notes need a committer identity in the bare repo itself
    subprocess.run(["git", "-C", str(path), "config", "user.name",
                    "GuardCI"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email",
                    "guardci@localhost"], check=True, capture_output=True)
    hook = path / "hooks" / "post-receive"
    hook.write_text(
        "#!/bin/sh\n"
        f"exec {_guardci_remote_bin()} ingest --project {project}\n")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
               | stat.S_IXOTH)
    return path
