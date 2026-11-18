"""Backend base class, MeasureRequest and registry (DESIGN.md §5.2, §9.5)."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any


@dataclass
class MeasureRequest:
    """One measurement on CI-built artifacts under golden inputs."""

    artifacts: Any                    # ArtifactSet
    golden: dict = field(default_factory=dict)   # LibRefs + constraints
    params: dict = field(default_factory=dict)
    outdir: Path | None = None
    executor: Any = None
    slot: Any = None
    timeout: int = 3600


class Backend:
    """A tool backend bound to a measurement (DESIGN.md §5.2, §9.5)."""

    provides: str = ""    # measurement name: signoff | drc | lec
    tool: str = ""        # e.g. "kepler_formal"
    version: str = "1"    # bump when scripts change
    # clause params that affect the measurement result — only these
    # enter the memo key (§11.4.3); thresholds never do. ``None`` means
    # "all params are measurement-affecting" (conservative default).
    memo_params: set | None = None

    def run(self, req: MeasureRequest) -> dict:
        """Canonical metrics for this measurement."""
        raise NotImplementedError


_BACKENDS: dict[str, type[Backend]] = {}
_loaded_eps = False


def register(cls: type[Backend]) -> type[Backend]:
    _BACKENDS[cls.tool] = cls
    return cls


def registry() -> dict[str, type[Backend]]:
    global _loaded_eps
    if not _loaded_eps:
        _loaded_eps = True
        for ep in entry_points(group="guardci.backends"):
            try:
                cls = ep.load()
            except Exception:  # noqa: BLE001,S112
                continue
            _BACKENDS.setdefault(cls.tool, cls)
    from . import equivalence, kepler_formal, klayout, openroad  # noqa: F401
    return dict(_BACKENDS)
