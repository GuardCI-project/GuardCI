"""Contract loader with ``extends:`` inheritance (DESIGN.md §4).

Merge rules (child over parent):
- ``protected`` / ``recipe``: append
- ``clauses``: override by ``check`` name; ``{check: X, remove: true}``
  drops a clause inherited from a parent pack
- ``routing``: override by tag
- ``edges``: override by ``metric``

``extends`` may be a path relative to the contract file, or the name of a
builtin pack shipped under ``guardci/contract/packs/`` in the package
(e.g. ``orfs-default``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..core.models import Clause, Edge, Severity
from ..core.protection import PROTECTED_DIMENSIONS, default_edge_params
from . import schema

BUILTIN_DIR = Path(__file__).resolve().parent / "packs"


@dataclass
class Contract:
    """A fully merged contract."""

    name: str
    protected: list[str] = field(default_factory=list)
    recipe: list[str] = field(default_factory=list)
    clauses: list[Clause] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    routing: dict[str, list[str]] = field(default_factory=dict)
    verification: dict = field(default_factory=dict)
    predict: dict | None = None
    protection: dict = field(default_factory=dict)
    tradable: list[str] = field(default_factory=list)

    @property
    def protection_mode(self) -> str:
        return (self.protection or {}).get("mode") or "default"

    @property
    def protection_rel_tol(self) -> float:
        v = (self.protection or {}).get("rel_tol")
        return 0.05 if v is None else float(v)

    @property
    def void_mode(self) -> str:
        return (self.verification or {}).get("void") or "descendants"


def _resolve(ref: str, base: Path) -> Path:
    p = Path(ref)
    if p.is_absolute() and p.exists():
        return p
    cand = (base / ref)
    if cand.exists():
        return cand
    for suffix in ("", ".yaml", ".yml"):
        builtin = BUILTIN_DIR / f"{ref}{suffix}"
        if builtin.exists():
            return builtin
    raise FileNotFoundError(
        f"contract {ref!r} not found (tried {cand} and builtins in "
        f"{BUILTIN_DIR})")


def load(ref: str, base: str | Path = ".") -> Contract:
    """Load a contract (path or builtin name), applying ``extends``."""
    path = _resolve(ref, Path(base))
    # collect the inheritance chain root-first
    chain = []
    seen: set = set()
    cur = path
    while cur is not None:
        doc = yaml.safe_load(cur.read_text())
        schema.validate(doc)
        if cur.resolve() in seen:
            raise schema.ContractError(f"extends cycle at {cur}")
        seen.add(cur.resolve())
        chain.append(doc)
        ext = doc.get("extends")
        cur = _resolve(ext, cur.parent) if ext else None
    chain.reverse()

    contract = Contract(name=chain[-1].get("name") or path.stem)
    clauses: dict[str, Clause] = {}
    edges: dict[str, Edge] = {}
    for doc in chain:
        contract.protected += [g for g in doc.get("protected", [])
                               if g not in contract.protected]
        contract.recipe += [g for g in doc.get("recipe", [])
                            if g not in contract.recipe]
        if doc.get("verification"):
            contract.verification.update(doc["verification"])
        if doc.get("predict") is not None:
            contract.predict = doc["predict"]
        if doc.get("protection"):
            contract.protection.update(doc["protection"])
        for t in doc.get("tradable") or []:
            if t not in contract.tradable:
                contract.tradable.append(t)
        for c in doc.get("clauses", []):
            c = dict(c)
            name = c.pop("check")
            if c.pop("remove", False):
                rid = c.get("id")
                clauses.pop(f"{name}:{rid}" if rid else name, None)
                continue
            sev = Severity(c.pop("severity", "hard"))
            kind = c.pop("kind", None)
            cid = c.pop("id", None)
            key = f"{name}:{cid}" if cid else name
            clauses[key] = Clause(check=name, severity=sev, params=c,
                                  kind=kind, id=cid)
        for e in doc.get("edges", []):
            e = dict(e)
            metric = e.pop("metric")
            sev = Severity(e.pop("severity", "warn"))
            edges[metric] = Edge(metric=metric, severity=sev, params=e)
        for tag, entries in (doc.get("routing") or {}).items():
            contract.routing[tag] = list(entries)
    # keep contract order: later overrides keep first-seen position? we use
    # insertion order of the dict — first definition wins the position,
    # last definition wins the value. That matches "override by name".
    contract.clauses = list(clauses.values())
    unknown = [t for t in contract.tradable
               if t not in PROTECTED_DIMENSIONS]
    if unknown:
        raise schema.ContractError(
            f"tradable: unknown protected dimension(s) "
            f"{unknown} (valid: {sorted(PROTECTED_DIMENSIONS)})")
    if contract.protection_mode == "default":
        # every protected dimension the owner did not declare tradable
        # is an implicit hard no-regression clause; an explicit
        # `edges:` entry on the same metric replaces its params and
        # severity, legacy metrics keep working
        for dim, spec in PROTECTED_DIMENSIONS.items():
            if dim in contract.tradable or dim in edges:
                continue
            edges[dim] = Edge(
                metric=dim, severity=Severity.HARD,
                params=default_edge_params(
                    spec, contract.protection_rel_tol))
    contract.edges = list(edges.values())
    return contract
