"""FlowAdapter interface and registry (DESIGN.md §5.1).

M0: only ``classify_delta`` is exercised; the remaining methods define the
adapter surface for M1/M2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import ClassVar, Literal


@dataclass
class DesignIdentity:
    """Which design a commit builds — normalized for comparison."""

    platform: str
    top: str                          # DESIGN_NAME / top module
    params: str                       # top-level parameter settings
    rtl_files: set[str] = field(default_factory=set)
    include_dirs: set[str] = field(default_factory=set)
    blackboxes: set[str] = field(default_factory=set)
    sdc_file: str | None = None       # resolved repo-relative path
    sdc_role: Literal["impl", "golden", "other", "missing"] = "missing"
    source: str = ""                  # locator for error messages


@dataclass
class Reference:
    """A path-like value referenced by the recipe config."""

    var: str
    path: str
    kind: Literal["rtl", "sdc", "lib", "lef", "rc", "script", "def",
                  "gds", "other"]


@dataclass
class ConstraintFiles:
    """Where the mutable (recipe) and golden (protected) SDCs live."""

    impl: str | None
    golden: str | None


@dataclass
class SdcCommand:
    """One parsed constraint command."""

    verb: str
    args: dict = field(default_factory=dict)   # '-period': '0.5', '_pos': [...]
    raw: str = ""


class FlowAdapter:
    """Base class for toolchain adapters."""

    name: str = ""
    capabilities: ClassVar[set] = set()

    def recipe_schema(self):
        """Recipe paths; how to semantically diff config."""
        raise NotImplementedError

    def classify_delta(self, diff) -> set:
        """Map a Diff object to delta tags."""
        raise NotImplementedError

    def identity(self, repo, sha, design=None, changed=None,
                 protected=None) -> DesignIdentity:
        """Normalized design identity at ``sha``.

        ``design`` selects among multiple designs when the adapter hosts
        several; ``changed`` (paths) may bias the choice to the one the
        commit touched; ``protected`` (globs) classifies the constraint
        file role. Raises when identity cannot be established.
        """
        raise NotImplementedError

    def references(self, repo, sha) -> list[Reference]:
        """All path-like references the recipe config makes at ``sha``."""
        raise NotImplementedError

    def constraints(self, repo, sha) -> ConstraintFiles:
        """Repo-relative paths of the impl and golden constraint files."""
        raise NotImplementedError

    def parse_constraints(self, text) -> list[SdcCommand]:
        """Parse constraint-file text into normalized commands."""
        raise NotImplementedError

    def stages(self) -> list:
        raise NotImplementedError

    def run(self, recipe, inputs, upto, env):
        raise NotImplementedError

    def export(self, artifacts, kind):
        raise NotImplementedError

    def checkpoint_artifacts(self, workspace) -> dict:
        """§12.2: ``{stage: path}`` of the stage-final databases the
        author's workspace holds (absolute paths; missing files are
        filtered by the recorder)."""
        return {}

    def delivery_products(self, workdir) -> dict:
        """§12.5 delivery bundle: ``{filename: path}`` of the rebuilt
        last-stage products (final database, DEF, GDS if any, final
        netlist). Default: no products."""
        return {}

    def closure(self, repo, sha, baseline_sha, extra_candidates=()):
        """§12.3 recipe closure — ``reachable``: path → stage index
        (None = unattributable). Flows without stage attribution may
        return a flat {path: None} mapping."""
        return {}


_FLOWS: dict[str, type[FlowAdapter]] = {}
_loaded_eps = False


def register(cls: type[FlowAdapter]) -> type[FlowAdapter]:
    _FLOWS[cls.name] = cls
    return cls


def registry() -> dict[str, type[FlowAdapter]]:
    global _loaded_eps
    if not _loaded_eps:
        _loaded_eps = True
        for ep in entry_points(group="guardci.flows"):
            try:
                cls = ep.load()
            except Exception:  # noqa: BLE001,S112
                continue
            _FLOWS.setdefault(cls.name, cls)
    from . import null  # noqa: F401
    from .orfs import adapter as _orfs_adapter  # noqa: F401
    return dict(_FLOWS)
