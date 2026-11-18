"""Recorder plugin interface (DESIGN.md §5 plugin E, §12.2).

A recorder captures the author's stage artifacts and the file-change
journal on the sandbox side at ingest — the author cannot modify it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import ClassVar


@dataclass
class Checkpoint:
    """§12.2: what a push at a stage checkpoint consists of."""

    sha: str
    stage: str | None                    # deepest stage-final artifact
    artifacts: dict = field(default_factory=dict)      # stage -> path
    artifact_times: dict = field(default_factory=dict)  # stage -> mtime
    file_blobs: dict = field(default_factory=dict)      # path -> git blob id
    file_times: dict = field(default_factory=dict)      # path -> mtime
    journal: list = field(default_factory=list)
    # [{t, path, op (add|modify|delete), sha256?, writer?}]
    provenance_inputs: dict = field(default_factory=dict)
    # stage -> [closure paths] (flow-supplied; §12.3)


class Recorder:
    """Plugin E base class (§12.2)."""

    type: ClassVar[str] = ""

    def capture(self, project, sha) -> Checkpoint | None:
        raise NotImplementedError


# ---------------------------------------------------------------- registry

_RECORDERS: dict[str, type[Recorder]] = {}
_loaded_eps = False


def register(cls: type[Recorder]) -> type[Recorder]:
    _RECORDERS[cls.type] = cls
    return cls


def registry() -> dict[str, type[Recorder]]:
    global _loaded_eps
    if not _loaded_eps:
        _loaded_eps = True
        for ep in entry_points(group="guardci.recorders"):
            try:
                cls = ep.load()
            except Exception:  # noqa: BLE001,S112
                continue
            _RECORDERS.setdefault(cls.type, cls)
    from . import null as _null  # noqa: F401
    from . import workspace as _ws  # noqa: F401
    return dict(_RECORDERS)


def make(spec: dict | None) -> Recorder:
    """Instantiate the configured recorder (default ``null``)."""
    spec = dict(spec or {"type": "null"})
    rtype = spec.pop("type", "null")
    reg = registry()
    if rtype not in reg:
        raise KeyError(f"unknown recorder {rtype!r} (have {sorted(reg)})")
    return reg[rtype](**spec)


def persist(project, checkpoint: Checkpoint) -> Path:
    """Write ``artifacts/<sha>/checkpoint/checkpoint.json`` plus
    CI-owned copies of the stage artifacts — real byte copies (a tool
    rewriting the source in place must never mutate the CI's copy),
    stored content-addressed so identical artifacts are shared across
    checkpoints.  Returns the checkpoint dir."""
    import hashlib
    import os
    import shutil
    cdir = Path(project.paths["artifacts"]) / checkpoint.sha / "checkpoint"
    adir = cdir / "artifacts"
    blobdir = Path(project.paths["artifacts"]) / "_blobs"
    adir.mkdir(parents=True, exist_ok=True)
    blobdir.mkdir(parents=True, exist_ok=True)
    owned: dict[str, str] = {}
    digests: dict[str, str] = {}
    for stage, src in (checkpoint.artifacts or {}).items():
        src = Path(src)
        # copy first, hash what was actually copied — the author may
        # still be writing the source; naming content B under A's
        # digest would also poison a later genuine A
        tmp = blobdir / f".part.{os.getpid()}.{stage}"
        shutil.copyfile(src, tmp)
        h = hashlib.sha256()
        with open(tmp, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        sha256 = h.hexdigest()
        digests[stage] = sha256
        blob = blobdir / sha256
        if blob.exists():
            tmp.unlink(missing_ok=True)
        else:
            os.replace(tmp, blob)
        dest = adir / src.name
        if not dest.exists() or dest.stat().st_ino != blob.stat().st_ino:
            try:
                if dest.exists():
                    dest.unlink()
                os.link(blob, dest)      # link the CI-owned blob —
            except OSError:              # the blob itself is immutable
                shutil.copy2(blob, dest)
        owned[stage] = str(dest)
    data = {
        "sha": checkpoint.sha,
        "stage": checkpoint.stage,
        "artifacts": owned,
        "artifact_sha256": digests,
        "artifact_times": checkpoint.artifact_times,
        "file_blobs": checkpoint.file_blobs,
        "file_times": checkpoint.file_times,
        "journal": checkpoint.journal,
        "provenance_inputs": checkpoint.provenance_inputs,
    }
    (cdir / "checkpoint.json").write_text(
        json.dumps(data, indent=1, sort_keys=True))
    return cdir


def load_checkpoint(path: Path) -> dict:
    return json.loads(Path(path).read_text())
