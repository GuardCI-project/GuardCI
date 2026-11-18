"""``workspace`` recorder (§12.2): scans a workspace directory visible
to the CI host; the journal is the difference between scans.

State lives in ``<project>/workspace_scan.json``: per-path
``{size, mtime_ns, sha256}`` so an unchanged tree scans cheap (hash
only when size or mtime_ns moved) and the journal of the last scan.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from ..core.paths import match_any as _match_any
from .base import Checkpoint, Recorder, register


def _blob_id_file(path: Path) -> str:
    """The git blob id of a workspace file — ``sha1(b"blob %d\0" +
    content)``.  Compared against `git ls-tree` output: one git
    subprocess per node, not per file (§12.3)."""
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()





@register
class WorkspaceRecorder(Recorder):
    type = "workspace"

    def __init__(self, path: str, tool_roots: list | None = None):
        self.path = Path(path).resolve()
        self.tool_roots = list(tool_roots or [])

    # ------------------------------------------------------- state file

    def _state_path(self, project) -> Path:
        return Path(project.root) / "workspace_scan.json"

    def _load_state(self, project) -> dict:
        sp = self._state_path(project)
        if sp.exists():
            try:
                return json.loads(sp.read_text())
            except (OSError, json.JSONDecodeError):
                pass
        return {"files": {}, "journal": []}

    # -------------------------------------------------------------- scan

    def _scan_paths(self, project, sha) -> set[str]:
        """Repo-relative paths to hash: protected globs + the recipe
        closure the flow adapter reports for this node."""
        protected = list(project.contract.protected or [])
        closure_stages = self._closure_stages(project, sha)
        paths: set[str] = set(closure_stages)
        # protected paths (baseline or recipe files) — the workspace
        # mirrors the repo tree, so repo paths map 1:1
        if protected:
            import subprocess
            r = subprocess.run(
                ["git", "-C", project.paths["remote"], "ls-tree",
                 "-r", "--name-only", sha],
                capture_output=True, text=True, check=False)
            if r.returncode == 0:
                paths |= {p for p in r.stdout.splitlines()
                          if _match_any(p, protected)}
        return paths

    def _closure_stages(self, project, sha) -> dict:
        """{path: stage_index|None} — the node's recipe closure with
        per-stage attribution (§11.4), plus baseline paths the recipe
        names (they sit inside the recipe closure by definition)."""
        fn = getattr(project.flow, "closure", None)
        baseline_sha = self._baseline_sha(project, sha)
        stages: dict = {}
        if callable(fn):
            try:
                cl = fn(project.paths["remote"], sha, baseline_sha)
                stages = dict(getattr(cl, "reachable", {}) or {})
            except Exception:  # noqa: BLE001 — record, do not crash
                stages = {}
        # ``reachable`` names only paths the baseline lacks (§9.3);
        # provenance needs the WHOLE recipe closure, so add every
        # committed path matching the flow's recipe globs — a recipe
        # file identical to the baseline is still an input whose mtime
        # must precede the artifacts (§12.3)
        schema_fn = getattr(project.flow, "recipe_schema", None)
        globs = list((schema_fn() if callable(schema_fn) else {})
                     .get("recipe_globs") or [])
        if globs:
            import subprocess
            r = subprocess.run(
                ["git", "-C", project.paths["remote"], "ls-tree",
                 "-r", "--name-only", sha],
                capture_output=True, text=True, check=False)
            if r.returncode == 0:
                for p in r.stdout.splitlines():
                    if _match_any(p, globs):
                        stages.setdefault(p, None)
        return stages

    def _baseline_sha(self, project, sha):
        from ..core import tree as tree_mod
        anc = tree_mod.ancestors(project.store, sha)
        b = project.store.baseline_for(sha, anc)
        return b["root_sha"] if b else None

    def _scan(self, project, sha) -> tuple[dict, list, dict]:
        """({path: sha256}, journal_events, closure_stages)."""
        wanted = self._scan_paths(project, sha)
        closure_stages = self._closure_stages(project, sha)
        wanted |= set(closure_stages)
        state = self._load_state(project)
        prev_files = state.get("files") or {}
        prev_hashes = state.get("hashes") or {}
        now = time.time()
        hashes: dict[str, str] = {}
        files_meta: dict[str, list] = {}
        journal: list = []
        for rel in sorted(wanted):
            fp = self.path / rel
            if fp.is_file():
                st = fp.stat()
                old = prev_files.get(rel)
                if old and [st.st_size, st.st_mtime_ns] == list(old[:2]) \
                        and rel in prev_hashes:
                    h = prev_hashes[rel]
                else:
                    h = _blob_id_file(fp)
                files_meta[rel] = [st.st_size, st.st_mtime_ns]
                hashes[rel] = h
                if rel not in prev_files:
                    journal.append({"t": now, "path": rel, "op": "add",
                                    "blob": h})
                elif [st.st_size, st.st_mtime_ns] != list(
                        prev_files.get(rel) or [None, None]):
                    journal.append({"t": now, "path": rel,
                                    "op": "modify", "blob": h})
            else:
                if rel in prev_files:
                    journal.append({"t": now, "path": rel,
                                    "op": "delete"})
        return hashes, journal, closure_stages, files_meta

    # ------------------------------------------------------------ capture

    def capture(self, project, sha) -> Checkpoint | None:
        if not self.path.is_dir():
            return None
        arts_all = project.flow.checkpoint_artifacts(self.path) \
            if hasattr(project.flow, "checkpoint_artifacts") else {}
        stage_order = {s: i for i, s in
                       enumerate(project.flow.stages())}
        arts = {s: str(p) for s, p in arts_all.items()
                if Path(p).is_file()}
        times = {s: Path(p).stat().st_mtime
                 for s, p in arts.items()}
        stage = max((s for s in arts), key=lambda s: stage_order[s],
                    default=None)
        hashes, journal, closure_stages, files_meta = \
            self._scan(project, sha)
        file_times = {p: m[1] / 1e9 for p, m in files_meta.items()}
        state = self._load_state(project)
        state["files"] = files_meta
        state["hashes"] = {**state.get("hashes", {}), **hashes}
        state["journal"] = (state.get("journal") or []) + journal
        # keep the journal bounded but complete since the first scan
        self._state_path(project).write_text(
            json.dumps(state, indent=1, sort_keys=True))
        # provenance inputs: stage -> closure paths attributed ≤ stage
        # (None = unattributable feeds every stage, §12.3)
        prov = {}
        for s in arts:
            i = stage_order[s]
            prov[s] = sorted(
                p for p, st in closure_stages.items()
                if st is None or (isinstance(st, int) and st <= i))
        return Checkpoint(
            sha=sha, stage=stage, artifacts=arts,
            artifact_times=times, file_blobs=hashes,
            file_times=file_times,
            journal=journal, provenance_inputs=prov)

    def journal_since_last(self, project) -> list:
        return (self._load_state(project).get("journal") or [])
