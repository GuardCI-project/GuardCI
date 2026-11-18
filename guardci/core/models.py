"""Core data model for GuardCI (DESIGN.md §3)."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class State(str, enum.Enum):
    """Node states (DESIGN.md §3.2)."""

    PENDING = "pending"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    WARNED = "warned"
    REJECTED = "rejected"
    OPEN = "open"
    TAINTED = "tainted"


class Severity(str, enum.Enum):
    """Clause severity (DESIGN.md §4.1)."""

    HARD = "hard"
    WARN = "warn"
    INFO = "info"
    ROUTE = "route"


class Cost(str, enum.Enum):
    """Checker cost class (DESIGN.md §3.3)."""

    STATIC = "static"
    LIGHT = "light"
    HEAVY = "heavy"


class Tag:
    """Delta-tag vocabulary (DESIGN.md §3.4)."""

    PROTECTED = "protected"
    CONSTRAINTS = "constraints"
    SYNTHESIS = "synthesis"
    FLOORPLAN = "floorplan"
    PLACEMENT = "placement"
    CTS = "cts"
    ROUTING = "routing"
    PHYSICAL_INTEGRITY = "physical_integrity"
    DESIGN_REFS = "design_refs"
    LIBRARY_REFS = "library_refs"
    SCRIPTS = "scripts"
    MEASUREMENT = "measurement"
    UNKNOWN = "unknown"

    ALL = frozenset({
        PROTECTED, CONSTRAINTS, SYNTHESIS, FLOORPLAN, PLACEMENT, CTS,
        ROUTING, PHYSICAL_INTEGRITY, DESIGN_REFS, LIBRARY_REFS, SCRIPTS,
        MEASUREMENT, UNKNOWN,
    })


# The paper's public vocabulary (W3 §4): internal `State` values map
# to the words a user or agent sees — violation / void / unfinished /
# accepted / pending.  Ledgers and the DB keep the internal names;
# only this mapping may be used at a user-facing boundary.
PUBLIC_STATE = {
    "rejected": "violation",
    "tainted": "void",
    "open": "unfinished",
    "verified": "accepted",
    "warned": "accepted",
    "pending": "pending",
    "verifying": "pending",
    "held": "pending",          # old ledgers: proposals are gone
}

PUBLIC_STATES = ("pending", "accepted", "unfinished", "violation",
                 "void")


def public_state(state) -> str:
    """Map an internal State (or its string) to the paper's word."""
    raw = state.value if isinstance(state, State) else str(state)
    return PUBLIC_STATE.get(raw, raw)


@dataclass
class Clause:
    """One contract clause: a checker binding plus severity and params.

    ``kind``: ``invariant`` (violation rejects the node) or ``exit``
    (violation leaves the node ``open``); ``None`` = take the checker
    class default (DESIGN.md §11.1).
    """

    check: str
    severity: Severity = Severity.HARD
    params: dict[str, Any] = field(default_factory=dict)
    kind: str | None = None
    id: str | None = None           # instance id for multi-instance
                                    # checks; identity = check + id

    @property
    def name(self) -> str:
        """Clause identity (verdict/gap name): ``check:id`` when the
        clause carries an instance id."""
        return f"{self.check}:{self.id}" if self.id else self.check


@dataclass
class Edge:
    """Contract edge clause: metric comparison against the parent."""

    metric: str
    severity: Severity = Severity.WARN
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckRun:
    """One executed (or attempted) check on a node."""

    sha: str
    checker: str
    backend: str | None
    cost: Cost | None
    status: str            # pass | fail | error | unavailable
    seconds: float = 0.0
    metrics: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    error: str | None = None
    id: int | None = None
    attempt: int = 0


@dataclass
class Verdict:
    """Per-clause verdict on a node."""

    sha: str
    clause: Clause
    status: str            # pass | fail | error | unavailable | skipped
    detail: str = ""
    id: int | None = None
    attempt: int = 0


@dataclass
class Node:
    """A commit node in the verification DAG (DESIGN.md §3.1)."""

    sha: str
    parent: str | None = None
    state: State = State.PENDING
    delta_tags: set[str] = field(default_factory=set)
    metrics: dict[str, Any] = field(default_factory=dict)
    baseline_id: int | None = None
    requested_full: bool = False
    attempt: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
