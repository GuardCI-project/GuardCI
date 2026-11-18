"""External view of the ledger: refs/guardci/* and git notes (§8.1)."""

from __future__ import annotations

import json
import subprocess

from ..core.models import PUBLIC_STATES, public_state

STATES = PUBLIC_STATES


def update_ref(repo: str, ref: str, sha: str):
    subprocess.run(["git", "-C", repo, "update-ref", ref, sha],
                   check=True, capture_output=True)


def delete_ref(repo: str, ref: str):
    subprocess.run(["git", "-C", repo, "update-ref", "-d", ref],
                   check=False, capture_output=True)


def set_state_ref(repo: str, sha: str, state: str):
    """Move ``refs/guardci/<public state>/<sha>`` — remove it from
    other states. ``state`` may be an internal or public name."""
    state = public_state(state)
    for st in STATES:
        if st != state:
            delete_ref(repo, f"refs/guardci/{st}/{sha}")
    update_ref(repo, f"refs/guardci/{state}/{sha}", sha)


def state_refs(repo: str) -> dict:
    """Map refs/guardci/<state>/<sha> -> {state: [sha]}."""
    out = subprocess.run(
        ["git", "-C", repo, "for-each-ref", "--format=%(refname)",
         "refs/guardci/"], capture_output=True, text=True,
        check=True).stdout
    result: dict[str, list[str]] = {}
    for line in out.splitlines():
        parts = line.strip().split("/")
        # refs / guardci / <state> / <sha>
        if len(parts) == 4 and parts[2] in STATES:
            result.setdefault(parts[2], []).append(parts[3])
    return result


def write_note(repo: str, sha: str, payload: dict):
    """Annotate a node commit with a JSON note under refs/notes/guardci."""
    subprocess.run(
        ["git", "-C", repo, "notes", "--ref=guardci", "add", "-f",
         "-m", json.dumps(payload, sort_keys=True), sha],
        check=True, capture_output=True)
