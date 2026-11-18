"""Canonical artifact model. M0 defines the vocabulary only; nothing
produces or consumes artifacts yet."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ArtifactKind(str, enum.Enum):
    NETLIST = "netlist"             # structural Verilog
    LAYOUT = "layout"               # DEF or OASIS
    STREAM = "stream"               # GDS/OASIS stream for DRC
    CONSTRAINTS = "constraints"     # SDC
    CONFIG = "config"               # adapter config + normalized k/v map
    LIB_REFS = "lib_refs"           # resolved liberty/LEF paths
    METRICS = "metrics"             # metrics.json, canonical names


@dataclass
class ArtifactSet:
    """kind -> path mapping plus a metrics dict."""

    paths: dict[ArtifactKind, Path] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)

    def get(self, kind: ArtifactKind) -> Path | None:
        return self.paths.get(kind)

    def put(self, kind: ArtifactKind, path: Path):
        self.paths[kind] = Path(path)
