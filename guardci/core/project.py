"""Project: guardci.yaml parsing, paths, contract/flow/checker loading."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from ..backends import base as backends_base
from ..checks import base as checks_base
from ..contract import loader as contract_loader
from ..executors import base as executors_base
from ..flows import base as flows_base
from .store import Store

DEFAULTS = {
    "flow": "null",
    "contract": "contracts/orfs-default.yaml",
    "backends": {},
    "executor": {"type": "docker", "slots": 4},
    "cache": None,                     # default <project>/artifacts/cache
    "cache_max_gb": 200,
    "remote": "remote.git",
    "state": "state.db",
    "artifacts": "artifacts",
    "t_leaf_seconds": 600,
    "artifacts_keep": "all",         # "all" | "reports" (§9.5)
    "recorder": {"type": "null"},    # §12.2 plugin E
    "webhook": None,
}

# measurement -> default backend tool when the flow is orfs (§9.5)
ORFS_DEFAULT_BACKENDS = {"signoff": "openroad", "drc": "klayout",
                         "lec": "equivalence",
                         "facts": "openroad_facts",
                         "estimate": "openroad_estimate"}


def find_project(start: str | Path | None = None) -> Path | None:
    """Walk upwards from ``start`` looking for guardci.yaml."""
    env = os.environ.get("GUARDCI_PROJECT")
    if env:
        p = Path(env)
        return p if p.is_dir() else p.parent
    d = Path(start or os.getcwd()).resolve()
    for cand in [d, *d.parents]:
        if (cand / "guardci.yaml").exists():
            return cand
    return None


class Project:
    """A GuardCI project directory."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        cfg_file = self.root / "guardci.yaml"
        if not cfg_file.exists():
            raise FileNotFoundError(f"no guardci.yaml under {self.root}")
        cfg = yaml.safe_load(cfg_file.read_text()) or {}
        self.cfg = {**DEFAULTS, **cfg}
        self.paths = {
            "remote": str(self.root / self.cfg["remote"]),
            "state": str(self.root / self.cfg["state"]),
            "artifacts": str(self.root / self.cfg["artifacts"]),
        }
        self._store = None
        self._contract = None
        self._flow = None
        self._checkers = None
        self._executor = None
        self._executor_checked = False
        self._cache = None

    @property
    def store(self) -> Store:
        if self._store is None:
            self._store = Store(self.paths["state"])
        return self._store

    @property
    def contract(self):
        if self._contract is None:
            self._contract = contract_loader.load(
                self.cfg["contract"], base=self.root)
        return self._contract

    @property
    def flow(self):
        if self._flow is None:
            flows = flows_base.registry()
            name = self.cfg["flow"]
            if name not in flows:
                raise KeyError(f"unknown flow {name!r} (have {sorted(flows)})")
            kwargs = {}
            if name == "orfs":
                design = self.cfg.get("design") or {}
                kwargs = {"platform": design.get("platform"),
                          "design": design.get("design"),
                          "protected": self.contract.protected}
            self._flow = flows[name](**kwargs)
        return self._flow

    @property
    def executor(self):
        """Configured executor, or None when the project has none
        (``executor: {type: none}`` or no image configured) — fail-closed:
        non-static clauses cannot run without it."""
        if self._executor is None and not self._executor_checked:
            spec = dict(self.cfg.get("executor") or {})
            etype = spec.pop("type", "docker")
            if etype == "none" or (
                    etype == "docker" and not spec.get("image")):
                self._executor_checked = True
                return None
            registry = executors_base.registry()
            if etype not in registry:
                raise KeyError(
                    f"unknown executor {etype!r} (have {sorted(registry)})")
            self._executor = registry[etype](**spec)
            self._executor_checked = True
        return self._executor

    @property
    def cache(self):
        if self._cache is None:
            from .cache import Cache
            root = self.cfg.get("cache") or \
                str(Path(self.paths["artifacts"]) / "cache")
            if not Path(root).is_absolute():
                root = str(self.root / root)
            self._cache = Cache(root)
        return self._cache

    @property
    def checkers(self):
        if self._checkers is None:
            self._checkers = checks_base.registry()
        return self._checkers

    @property
    def recorder(self):
        """§12.2 plugin E (default ``null``)."""
        if getattr(self, "_recorder", None) is None:
            from ..recorders import base as recorders_base
            self._recorder = recorders_base.make(self.cfg.get("recorder"))
        return self._recorder

    def backend_for(self, measurement: str):
        bound = (self.cfg.get("backends") or {}).get(measurement)
        if not bound and self.cfg.get("flow") == "orfs":
            bound = ORFS_DEFAULT_BACKENDS.get(measurement)
        if not bound:
            return None
        cls = backends_base.registry().get(bound)
        return cls() if cls is not None else None

    def close(self):
        if self._store is not None:
            self._store.close()
            self._store = None
