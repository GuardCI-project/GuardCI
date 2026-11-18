"""Protected dimensions — the paper's default no-regression set.

Every measurable dimension is protected against regression unless the
owner declares it ``tradable``.  Each dimension has an extractor (from
a metrics dict — node facts or the ratchet reference's facts at the
same stage), a direction, and a default bound expressed in the
``{tol, rel_tol}`` params form understood by ``edges._regressed``;
``None`` in a default means "use the contract's ``protection.rel_tol``".

Bounds (settled in W3):
- ``min_period`` / ``cell_area`` / ``power_total``: higher is worse,
  rel_tol of the reference.
- ``ir_drop_worst``: higher is worse, delta > max(0.005, 0.5 × ref).
- ``hold_wns``: the protected quantity is ``min(hold_wns, 0)`` —
  positive slack is not protected; abs tol 0.001.
- counts (``electrical_violations`` … ``unrouted_nets``): any
  increase fails (bound 0).
- ``tapcell_density`` = tapcells / core_area; ``endcap_density`` =
  endcaps / sqrt(core_area); ``pdn_density`` = Σ pdn wires /
  sqrt(core_area) — lower is worse, rel_tol (pdn fixed at 0.10).
  Densities rather than raw counts: a smaller legal floorplan
  legitimately removes tap cells.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def _plain(name):
    return lambda m: m.get(name)


def _cell_count(name):
    def get(m):
        e = (m.get("cells_by_type") or {}).get(name)
        return (e or {}).get("count") if e else None
    return get


def _ir_worst(m):
    drops = [v.get("drop") for v in (m.get("ir_drop") or {}).values()
             if v and v.get("drop") is not None]
    return max(drops) if drops else None


def _hold_clamped(m):
    v = m.get("hold_wns")
    return min(v, 0.0) if v is not None else None


def _density(count_of):
    def get(m):
        count = count_of(m)
        core = m.get("core_area")
        if count is None or not core or core <= 0:
            return None
        return count / math.sqrt(core)
    return get


def _pdn_wire_count(m):
    p = m.get("pdn_wires_by_layer")
    if not p:
        return None
    return sum(v for v in p.values() if isinstance(v, (int, float)))


def _tap_density(m):
    count = _cell_count("tapcell")(m)
    core = m.get("core_area")
    if count is None or not core or core <= 0:
        return None
    return count / core


@dataclass(frozen=True)
class DimSpec:
    """One protected dimension."""

    extract: object            # metrics -> float | None
    lower_better: bool         # True → smaller values are better
    bounds: dict               # default _regressed params; values may
                               # be None = contract protection.rel_tol


PROTECTED_DIMENSIONS: dict[str, DimSpec] = {
    "min_period": DimSpec(_plain("min_period"), True,
                          {"rel_tol": None}),
    "cell_area": DimSpec(_plain("cell_area"), True,
                         {"rel_tol": None}),
    "power_total": DimSpec(_plain("power_total"), True,
                           {"rel_tol": None}),
    "ir_drop_worst": DimSpec(_ir_worst, True,
                             {"tol": 0.005, "rel_tol": 0.5}),
    "hold_wns": DimSpec(_hold_clamped, False,
                        {"tol": 0.001}),
    "electrical_violations": DimSpec(_plain("electrical_violations"),
                                     True, {}),
    "drc_count": DimSpec(_plain("drc_count"), True, {}),
    "antenna_violations": DimSpec(_plain("antenna_violations"),
                                  True, {}),
    "unconstrained_endpoints": DimSpec(
        _plain("unconstrained_endpoints"), True, {}),
    "unrouted_nets": DimSpec(_plain("unrouted_nets"), True, {}),
    "tapcell_density": DimSpec(_tap_density, False,
                               {"rel_tol": None}),
    "endcap_density": DimSpec(_density(_cell_count("endcap")), False,
                              {"rel_tol": None}),
    "pdn_density": DimSpec(_density(_pdn_wire_count), False,
                           {"rel_tol": 0.10}),
}

PROTECTION_MODES = ("default", "explicit")


def default_edge_params(spec: DimSpec, rel_tol: float) -> dict:
    """Resolved default bound params for an implicit edge clause."""
    return {k: (rel_tol if v is None else v)
            for k, v in spec.bounds.items()}
