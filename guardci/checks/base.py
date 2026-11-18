"""Checker base class, CheckResult, and the checker registry.

Checkers are discovered from the ``guardci.checkers`` entry-point group
and can also be registered manually with ``@register``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, ClassVar

from ..core.models import Cost


@dataclass
class CheckResult:
    status: str                       # pass | fail | error
    metrics: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    seconds: float = 0.0
    detail: str = ""


class Checker:
    """Base class for checkers (DESIGN.md §5.2).

    ``ctx`` is a dict carrying at least: ``project``, ``node``,
    ``changed_paths`` (list of repo-relative paths changed by the commit),
    ``contract`` and ``artifacts`` (an ArtifactSet or None in M0).
    """

    name: str = ""
    requires: ClassVar[set] = set()
    cost = Cost.STATIC
    # §11.1 clause kind when the contract does not set it: ``invariant``
    # (violation → rejected) or ``exit`` (violation → open/warned)
    kind: ClassVar[str] = "exit"
    # §12.4: the stage whose facts decide this check ("delivery" for
    # reproducible); clause params["stage"] overrides it
    native_stage: ClassVar[str] = ""

    def run(self, ctx: dict, params: dict, backend) -> CheckResult:  # pragma: no cover
        raise NotImplementedError

    def timed(self, fn, *a, **kw) -> CheckResult:
        t0 = time.monotonic()
        r = fn(*a, **kw)
        r.seconds = time.monotonic() - t0
        return r


_CHECKERS: dict[str, type[Checker]] = {}
_loaded_eps = False


def register(cls: type[Checker]) -> type[Checker]:
    """Class decorator: register a checker under its ``name``."""
    _CHECKERS[cls.name] = cls
    return cls


def registry() -> dict[str, type[Checker]]:
    """All checkers: manually registered + entry-point discovered."""
    global _loaded_eps
    if not _loaded_eps:
        _loaded_eps = True
        for ep in entry_points(group="guardci.checkers"):
            try:
                cls = ep.load()
            except Exception:  # noqa: BLE001,S112
                continue
            _CHECKERS.setdefault(cls.name, cls)
    # make sure builtins are registered even if entry points are missing
    from . import checkpoint, design_identity, measurement, protected_refs, recipe_scope, sdc_delta  # noqa: F401
    return dict(_CHECKERS)
