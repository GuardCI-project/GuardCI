"""Executor interface (DESIGN.md §9.9) and registry.

An executor gives the CI a clean environment: run this command with these
mounts on an exclusive CPU set, hand back the result. Tool names live in
the implementations; the signature is tool-free.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Self


@dataclass
class Slot:
    """An exclusive block of host CPUs (flock-backed)."""

    cpus: list[int]
    _release: object = field(default=None, repr=False)

    def release(self):
        if self._release is not None:
            self._release()
            self._release = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args):
        self.release()
        return False


@dataclass
class RunResult:
    """Outcome of one executor run."""

    rc: int | None
    seconds: float
    timed_out: bool = False


class Executor:
    """Plugin D interface (DESIGN.md §9.9)."""

    def slots(self) -> int:
        raise NotImplementedError

    def acquire(self, poll_seconds: float = 5.0) -> Slot:
        """Blocking slot acquisition."""
        raise NotImplementedError

    def try_acquire(self) -> Slot | None:
        """Non-blocking slot acquisition."""
        raise NotImplementedError

    def run(self, cmd, *, mounts=None, workdir=None, env=None, slot=None,
            timeout=None, log=None) -> RunResult:
        """Run ``cmd`` (list of argv words) in the container.

        ``mounts``: {host_path: (container_path, "ro"|"rw")}.
        ``log``: a file-like object (binary) or host path receiving the
        combined stdout/stderr of the command.
        """
        raise NotImplementedError

    def image_id(self) -> str:
        raise NotImplementedError

    @staticmethod
    def _elapsed(t0: float) -> float:
        return round(time.monotonic() - t0, 3)


def default_env() -> dict:
    """Environment variables every GuardCI container gets."""
    return {"HOME": "/tmp/home"}


_EXECUTORS: dict[str, type[Executor]] = {}
_loaded_eps = False


def register(cls: type[Executor]) -> type[Executor]:
    _EXECUTORS[cls.type] = cls
    return cls


def registry() -> dict[str, type[Executor]]:
    global _loaded_eps
    if not _loaded_eps:
        _loaded_eps = True
        for ep in entry_points(group="guardci.executors"):
            try:
                cls = ep.load()
            except Exception:  # noqa: BLE001,S112
                continue
            _EXECUTORS.setdefault(cls.type, cls)
    from . import docker as _docker  # noqa: F401
    return dict(_EXECUTORS)


class InfraError(RuntimeError):
    """Executor / container infrastructure failure — never a build
    failure and never cached (DESIGN.md §9.3)."""


def infra_rc(rc: int | None) -> bool:
    """rc values that mean docker/the image broke, not the build."""
    return rc in (125, 126, 127)
