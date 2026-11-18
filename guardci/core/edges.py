"""Edge clauses and the differential signoff table (DESIGN.md §9.7).

For every node carrying metrics, the reference is — per dimension — the
nearest first-parent ancestor whose node metrics contain that dimension.
The table is stored as the check run ``edges``; contract ``edges:``
entries turn rows into ``edge:<metric>`` verdicts.
"""

from __future__ import annotations

from . import tree as tree_mod
from .models import CheckRun, Clause, State, Verdict
from .protection import PROTECTED_DIMENSIONS


# (dimension, higher_is_better, extractor); order per §9.7
def _cell(name):
    def get(m):
        e = (m.get("cells_by_type") or {}).get(name)
        return (e or {}).get("count") if e else None
    return get


def _pdn_shapes(m):
    p = m.get("pdn_wires_by_layer")
    if not p:
        return None
    return sum(v for v in p.values() if isinstance(v, (int, float)))


def _ir_worst(m):
    drops = [v.get("drop") for v in (m.get("ir_drop") or {}).values()
             if v and v.get("drop") is not None]
    return max(drops) if drops else None


def _plain(name):
    return lambda m: m.get(name)


DIMENSIONS = [
    ("setup_wns", True, _plain("setup_wns")),
    ("setup_tns", True, _plain("setup_tns")),
    ("hold_wns", True, _plain("hold_wns")),
    ("hold_tns", True, _plain("hold_tns")),
    ("slew_violations", False, _plain("slew_violations")),
    ("cap_violations", False, _plain("cap_violations")),
    ("fanout_violations", False, _plain("fanout_violations")),
    ("electrical_violations", False, _plain("electrical_violations")),
    ("drc_count", False, _plain("drc_count")),
    ("antenna_violations", False, _plain("antenna_violations")),
    ("unconstrained_endpoints", False,
     _plain("unconstrained_endpoints")),
    ("tapcell", True, _cell("tapcell")),
    ("endcap", True, _cell("endcap")),
    ("antenna", True, _cell("antenna")),
    ("pdn_shapes", True, _pdn_shapes),
    ("ir_drop_worst", False, _ir_worst),
    ("power_total", False, _plain("power_total")),
    ("cell_area", False, _plain("cell_area")),
]

# contract edge metric -> dimensions it expands over
EDGE_METRICS = {
    "cells_by_type": ["tapcell", "endcap", "antenna"],
}

EDGE_CHECKER = "edges"


def _ancestor_nodes(store, sha):
    for anc in tree_mod.ancestors(store, sha):
        anode = store.get_node(anc)
        if anode is not None:
            yield anode


def reference_sha(store, sha, stage=None) -> str | None:
    """§11.1 ratchet reference: the nearest first-parent ancestor in
    state ``verified``/``warned``, else the baseline root.  §12.5
    same-stage: with checkpoint evidence the reference must carry
    facts at the *same* stage — ancestors that only reached an
    earlier stage are skipped."""
    for anode in _ancestor_nodes(store, sha):
        if anode.state in (State.VERIFIED, State.WARNED) and                 anode.metrics:
            if stage is not None:
                astages = (anode.metrics or {}).get("stages") or {}
                if stage not in astages:
                    continue
            return anode.sha
    root = store.baseline_root()
    if root is not None:
        rnode = store.get_node(root)
        if rnode is not None and rnode.metrics:
            return rnode.sha
    return None


def _stage_effective(metrics: dict, stage: str | None) -> dict:
    """Metrics at ``stage``: the stage facts with the node's top-level
    (signoff) values on top — the same precedence the scheduler uses
    when merging a finish node's metrics."""
    m = metrics or {}
    base = dict((m.get("stages") or {}).get(stage) or {})
    if stage is not None and base and stage != m.get("stage"):
        # an earlier stage of a deeper node: its stage facts only —
        # top-level metrics belong to the node's own (last) stage
        return base
    top = {k: v for k, v in m.items()
           if k != "stages" and not k.startswith("_")}
    return {**base, **top}


def compute_rows(store, sha, metrics) -> tuple[str | None, list, dict]:
    """(reference_sha, rows, ref_stage_metrics) — the node's
    differential table against its single ratchet reference (§11.1).
    Protected dimensions compare the node's stage-effective facts
    against the reference's facts *at the same stage*."""
    stage = (metrics or {}).get("stage")
    ref = reference_sha(store, sha, stage=stage)
    ref_metrics = {}
    if ref is not None:
        rnode = store.get_node(ref)
        ref_metrics = (rnode.metrics or {}) if rnode else {}
    ref_eff = _stage_effective(ref_metrics, stage)
    node_eff = _stage_effective(metrics, stage)
    rows = []
    for dim, higher, extract in DIMENSIONS:
        node_v = extract(metrics or {})
        parent_v = extract(ref_metrics)
        if node_v is None or parent_v is None:
            continue
        delta = node_v - parent_v
        worse = (delta < 0) if higher else (delta > 0)
        rows.append({"dimension": dim, "parent_sha": ref,
                     "parent": parent_v, "node": node_v,
                     "delta": delta, "worse": worse})
    shown = {r["dimension"] for r in rows}
    for dim, spec in PROTECTED_DIMENSIONS.items():
        node_v = spec.extract(node_eff)
        parent_v = spec.extract(ref_eff)
        if node_v is None or parent_v is None:
            continue
        delta = node_v - parent_v
        worse = (delta < 0) if not spec.lower_better else (delta > 0)
        if dim not in shown:
            rows.append({"dimension": dim, "parent_sha": ref,
                         "parent": parent_v, "node": node_v,
                         "delta": delta, "worse": worse})
            shown.add(dim)
    return ref, rows, ref_eff


def _regressed(row, params) -> bool:
    if not row.get("worse"):
        return False
    bound = max(params.get("tol") or 0,
                (params.get("rel_tol") or 0) * abs(row["parent"]))
    return abs(row["delta"]) > bound


def evaluate(store, sha, node, contract, attempt: int,
             verdicts_allowed: bool = True):
    """(CheckRun, [Verdict]) for the node's edge clauses.

    The differential table is always stored; ratchet *verdicts* are
    produced only when ``verdicts_allowed`` — §11.1: only on a node
    whose hard exit clauses all pass."""
    metrics = node.metrics or {}
    ref, rows, ref_eff = compute_rows(store, sha, metrics)
    run = CheckRun(sha=sha, attempt=attempt, checker=EDGE_CHECKER,
                   backend=None, cost=None, status="pass",
                   metrics={"reference": ref, "rows": rows})
    by_dim = {r["dimension"]: r for r in rows}
    node_eff = _stage_effective(metrics, metrics.get("stage"))
    default_mode = getattr(contract, "protection_mode",
                           "explicit") == "default"
    verdicts = []
    if not verdicts_allowed:
        return run, verdicts
    for edge in contract.edges:
        spec = (PROTECTED_DIMENSIONS.get(edge.metric)
                if default_mode else None)
        if spec is not None:
            nv, rv = spec.extract(node_eff), spec.extract(ref_eff)
            if nv is None or rv is None:
                continue      # no reference or dimension missing
            bad = [{"dimension": edge.metric, "parent": rv,
                    "node": nv, "delta": nv - rv,
                    "worse": (nv - rv < 0) if not spec.lower_better
                             else (nv - rv > 0)}]
            bad = [r for r in bad if _regressed(r, edge.params)]
            dims = None
        else:
            dims = EDGE_METRICS.get(edge.metric, [edge.metric])
            hits = [by_dim[d] for d in dims if d in by_dim]
            if not hits:
                continue      # no reference or dimension missing
            bad = [r for r in hits if _regressed(r, edge.params)]
        clause = Clause(check=f"edge:{edge.metric}",
                        severity=edge.severity,
                        params=dict(edge.params))
        if bad:
            detail = "; ".join(
                f"{r['dimension']} {r['parent']:.4g} → {r['node']:.4g} "
                f"(Δ{r['delta']:+.4g})" for r in bad)
            verdicts.append(Verdict(sha=sha, attempt=attempt,
                                    clause=clause, status="fail",
                                    detail=detail))
        else:
            verdicts.append(Verdict(sha=sha, attempt=attempt,
                                    clause=clause, status="pass",
                                    detail=""))
    return run, verdicts
