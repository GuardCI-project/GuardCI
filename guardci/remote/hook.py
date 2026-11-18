"""guardci-remote: post-receive ingest.

Reads ``old new ref`` lines from stdin (git passes them through the hook),
discovers commits not yet recorded under ``refs/guardci/nodes/*`` — so
force-push can never drop a node — upserts each as ``pending`` and
enqueues it.
"""

from __future__ import annotations

import os
import subprocess
import sys

import click

from ..core.models import Node, State
from ..core.project import Project
from . import refs as git_refs


def _git(repo: str, *args: str, check=True) -> str:
    r = subprocess.run(["git", "-C", repo, *args], check=False,
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def _new_commits(repo: str, tip: str) -> list[str]:
    """Commits reachable from ``tip`` minus already-recorded nodes."""
    out = _git(repo, "rev-list", "--reverse", tip,
               "--not", "--glob=refs/guardci/nodes/*")
    return [s for s in out.splitlines() if s]


def _first_parent(repo: str, sha: str) -> str | None:
    r = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet",
                        f"{sha}^"], check=False, capture_output=True,
                        text=True)
    return r.stdout.strip() or None


def ingest(project_dir: str, repo: str, stdin) -> int:
    project = Project(project_dir)
    store = project.store
    queued = 0
    try:
        for line in stdin:
            parts = line.split()
            if len(parts) < 3:
                continue
            _old, new, _ref = parts[0], parts[1], parts[2]
            if set(new) == {"0"}:
                continue  # ref deletion
            newly = []
            for sha in _new_commits(repo, new):
                if store.get_node(sha) is not None:
                    continue
                store.upsert_node(Node(sha=sha,
                                       parent=_first_parent(repo, sha),
                                       state=State.PENDING))
                store.enqueue(sha)
                git_refs.update_ref(repo, f"refs/guardci/nodes/{sha}", sha)
                git_refs.update_ref(repo, f"refs/guardci/pending/{sha}", sha)
                store.log_event("ingest", sha, {"tip": new})
                newly.append(sha)
                print(f"guardci: queued {sha[:12]}", file=sys.stderr)
                queued += 1
            # §12.2: the checkpoint belongs to the push's tip — capture
            # it now, while the workspace still reflects this push
            if newly and new in newly:
                try:
                    from ..core.checkpoint import capture_at_ingest
                    capture_at_ingest(project, new)
                except Exception as exc:  # noqa: BLE001 — never fail
                    store.log_event("checkpoint_error", new,
                                    {"error": str(exc)})
    finally:
        project.close()
    return queued


@click.command()
@click.argument("args", nargs=-1)
@click.option("--project", "project_dir", required=True,
              type=click.Path(exists=True, file_okay=False))
def main(args, project_dir):
    """Entry point of the post-receive hook (subcommand: ingest)."""
    argv = list(args)
    if not argv or argv[0] != "ingest":
        raise SystemExit("usage: guardci-remote ingest --project DIR")
    repo = os.environ.get("GIT_DIR", os.getcwd())
    ingest(os.path.abspath(project_dir), os.path.abspath(repo), sys.stdin)


if __name__ == "__main__":
    main()
