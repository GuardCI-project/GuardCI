"""OpenROAD signoff backend (DESIGN.md §9.5/§11.3): measurement of the
CI-built layout under golden libraries and the golden SDC template —
never the recipe's own constraints.

``measure.tcl`` loads the layout, extracts parasitics once, searches
the smallest period at which the golden template closes
(``min_period``), then reports all period-dependent metrics at the
required period (``params["period"]``, the resolved owner bound; when
absent, at ``min_period``). Ports of phb measure.tcl and
remeasure.sanitize_def_ndr.
"""

from __future__ import annotations

import hashlib
import json
import re
from importlib.resources import files as res_files
from pathlib import Path
from typing import ClassVar

from ...artifacts.model import ArtifactKind
from ...executors.base import InfraError, infra_rc
from ..base import Backend, MeasureRequest, register
from ..toolenv import (
    CONTAINER_GOLDEN,
    CONTAINER_GUARDCI,
    CONTAINER_OUT,
    TOOL_ENV,
)

_NDR_DEF_RE = re.compile(r"^\s*-\s*(\S+)")
_NDR_REF_RE = re.compile(r"\+\s*NONDEFAULTRULE\s+(\S+)")


def sanitize_def_ndr(lines, out):
    """Strip `+ NONDEFAULTRULE <name>` refs not defined in this DEF's
    NONDEFAULTRULES section (port of remeasure.sanitize_def_ndr)."""
    defined = set()
    stripped = set()
    nets = 0
    in_defs = False
    in_nets = False
    for line in lines:
        s = line.strip()
        if s.startswith("NONDEFAULTRULES"):
            in_defs = True
            out.write(line)
            continue
        if in_defs:
            if s.startswith("END"):
                in_defs = False
            elif s.startswith("-"):
                m = _NDR_DEF_RE.match(line)
                if m:
                    defined.add(m.group(1))
            out.write(line)
            continue
        if s.startswith(("NETS", "SPECIALNETS")):
            in_nets = True
            out.write(line)
            continue
        if in_nets:
            if s.startswith("END"):
                in_nets = False
                out.write(line)
                continue

            def repl(m):
                nonlocal nets
                if m.group(1) in defined:
                    return m.group(0)
                stripped.add(m.group(1))
                nets += 1
                return ""

            new = _NDR_REF_RE.sub(repl, line)
            if new.strip() or not s:
                out.write(new)
            continue
        out.write(line)
    return {"names": sorted(stripped), "nets": nets}


_CANON_CELL = {"CORE_WELLTAP": "tapcell", "CORE_ANTENNACELL": "antenna",
               "CORE_SPACER": "filler", "CORE": "core"}


def canonical_cell_type(mtype: str) -> str:
    if mtype in _CANON_CELL:
        return _CANON_CELL[mtype]
    if mtype.startswith("ENDCAP"):
        return "endcap"
    return mtype


def _ir_drop(ir_list):
    """{net: {nominal, drop}} — power net drop = nominal − min; ground
    net drop = max (phb signals.ir_drop_worse convention)."""
    out = {}
    for entry in ir_list or []:
        net = entry.get("net")
        if net is None:
            continue
        nominal = entry.get("voltage")
        if nominal is None:
            continue
        nominal = float(nominal)
        if nominal > 0:
            drop = nominal - (entry.get("min") or nominal)
        else:
            drop = (entry.get("max") or 0.0) - nominal
        out[net] = {"nominal": nominal, "drop": drop}
    return out


def map_metrics(raw: dict, period, timing_error=None) -> dict:
    """Raw measure.json → the §9.5 canonical metric names."""
    raw = raw or {}
    cbt = {}
    for mtype, stats in (raw.get("cells_by_type") or {}).items():
        canon = canonical_cell_type(mtype)
        cur = cbt.setdefault(canon, {"count": 0, "area": 0.0})
        cur["count"] += stats.get("count", 0)
        cur["area"] += stats.get("area_um2", 0.0)
    slew = raw.get("max_slew_violations")
    cap = raw.get("max_cap_violations")
    fanout = raw.get("max_fanout_violations")
    wns = raw.get("setup_wns")
    margin = None
    if isinstance(wns, (int, float)) and period:
        margin = wns / period
    t_err = timing_error if timing_error is not None \
        else raw.get("timing_error")
    return {
        "clock_period": period,
        "setup_wns": wns,
        "setup_tns": raw.get("setup_tns"),
        "setup_margin_ratio": margin,
        "hold_wns": raw.get("hold_wns"),
        "hold_tns": raw.get("hold_tns"),
        "slew_violations": slew,
        "cap_violations": cap,
        "fanout_violations": fanout,
        "electrical_violations": (
            None if any(v is None for v in (slew, cap, fanout))
            else slew + cap + fanout),
        "cells_by_type": cbt,
        "pdn_wires_by_layer": raw.get("pdn_wires_by_layer"),
        "pdn_wires_by_net": raw.get("pdn_wires_by_net"),
        "special_nets": raw.get("special_nets"),
        "ir_drop": _ir_drop(raw.get("ir_drop_worst_v")),
        "power_total": raw.get("power_total"),
        "cell_area": raw.get("cell_area_um2"),
        "die_area": raw.get("die_area_um2"),
        "core_area": raw.get("core_area_um2"),
        "instances": raw.get("instances"),
        "antenna_violations": raw.get("antenna_violations"),
        "unconstrained_endpoints": raw.get("unconstrained_endpoints"),
        "placement_legal": raw.get("check_placement") == "pass",
        "timing_error": t_err,
        "min_period": raw.get("min_period"),
        "min_period_trials": raw.get("min_period_trials"),
        "min_period_error": raw.get("min_period_error"),
    }


def _script(name: str) -> str:
    return str(res_files("guardci.backends.openroad").joinpath(name))


def _run_openroad(req, mounts, script, env, log):
    """One ``openroad -exit`` container run of a mounted Tcl script."""
    return req.executor.run(
        [TOOL_ENV["OPENROAD_EXE"], "-no_init", "-no_splash",
         "-exit", f"{CONTAINER_GUARDCI}/{script}"],
        mounts=mounts, workdir="/",
        env={**TOOL_ENV, **env}, slot=req.slot,
        timeout=req.timeout, log=log)


@register
class OpenRoadBackend(Backend):
    memo_params: ClassVar[set] = {"period"}
    provides = "signoff"
    tool = "openroad"
    version = "v5.0"

    def _env_base(self, librefs, def_c):
        return {
            "GUARDCI_TECH_LEF": librefs["tech_lef"],
            "GUARDCI_SC_LEFS": " ".join(librefs["sc_lefs"]),
            "GUARDCI_LIBS": " ".join(librefs["libs"]),
            "GUARDCI_DEF": def_c,
            "GUARDCI_RCX_RULES": librefs.get("rcx_rules") or "",
            "PLATFORM_DIR": librefs.get("platform_dir") or "",
            "GUARDCI_PWR_NETS_VOLTAGES":
                librefs.get("pwr_nets_voltages") or "",
            "GUARDCI_GND_NETS_VOLTAGES":
                librefs.get("gnd_nets_voltages") or "",
        }

    def _openroad(self, req, mounts, script, env, log):
        return _run_openroad(req, mounts, script, env, log)

    def run(self, req: MeasureRequest) -> dict:
        librefs = req.golden.get("librefs") or req.golden
        constraints = req.golden.get("constraints") or {}
        outdir = Path(req.outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        layout = Path(req.artifacts.get(ArtifactKind.LAYOUT))

        # NDR sanitize: strip NDR refs the ODB writer left undefined.
        san_def = outdir / "layout.sanitized.def"
        with open(layout, errors="replace") as src, \
                open(san_def, "w") as dst:
            strip_info = sanitize_def_ndr(src, dst)
        (outdir / "ndr_sanitize.json").write_text(
            json.dumps(strip_info, indent=2))
        def_c = f"{CONTAINER_OUT}/layout.sanitized.def"

        (outdir / "measure.sdc").write_text(constraints["template"])

        mounts = {
            golden_host(librefs): (CONTAINER_GOLDEN, "ro"),
            str(outdir): (CONTAINER_OUT, "rw"),
            _script("measure.tcl"): (f"{CONTAINER_GUARDCI}/measure.tcl",
                                     "ro"),
        }

        # §11.3: the required period is the owner's bound resolved by
        # the core — a float — or absent (measure at min_period)
        period_spec = (req.params or {}).get("period")
        required = float(period_spec) \
            if isinstance(period_spec, (int, float)) else None
        golden_period = constraints.get("golden_period")
        timing_error = None

        menv = self._env_base(librefs, def_c)
        menv.update({
            "GUARDCI_SDC_TEMPLATE": f"{CONTAINER_OUT}/measure.sdc",
            "GUARDCI_OUT": f"{CONTAINER_OUT}/measure.json",
            "GUARDCI_NETLIST_OUT": "",
        })
        if required is not None:
            menv["GUARDCI_PERIOD"] = repr(required)
        if golden_period is not None:
            menv["GUARDCI_GOLDEN_PERIOD"] = repr(float(golden_period))
        raw = None
        rr = self._openroad(req, mounts, "measure.tcl", menv,
                            outdir / "measure.log")
        if infra_rc(rr.rc):
            raise InfraError(f"measure run rc={rr.rc}")
        mj = outdir / "measure.json"
        if mj.exists():
            try:
                raw = json.loads(mj.read_text())
            except ValueError:
                raw = None
        if raw is None:
            timing_error = f"measure.tcl produced no JSON (rc={rr.rc})"

        clock_period = required if required is not None \
            else (raw or {}).get("min_period")
        metrics = map_metrics(raw, clock_period,
                              timing_error=timing_error)
        metrics["layout_sha256"] = hashlib.sha256(
            layout.read_bytes()).hexdigest()
        return metrics


def golden_host(librefs) -> str:
    return librefs["host_root"]


def run_estimate(req) -> dict:
    """§11.4 predict: time the CTS database under golden constraints
    with estimated parasitics. ``req.artifacts`` must carry
    ``ArtifactKind.LAYOUT`` = the build's ``4_cts.odb``."""
    librefs = req.golden.get("librefs") or req.golden
    constraints = req.golden.get("constraints") or {}
    outdir = Path(req.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copyfile(req.artifacts.get(ArtifactKind.LAYOUT),
                    outdir / "4_cts.odb")
    sdc = outdir / "estimate.sdc"
    sdc.write_text(constraints["template"])
    env = {
        "GUARDCI_CTS_ODB": f"{CONTAINER_OUT}/4_cts.odb",
        "GUARDCI_LIBS": " ".join(librefs["libs"]),
        "GUARDCI_SDC_TEMPLATE": f"{CONTAINER_OUT}/estimate.sdc",
        "GUARDCI_PERIOD": repr(float(req.params["period"])),
        "GUARDCI_SET_RC_TCL": librefs.get("set_rc_tcl") or "",
        "GUARDCI_OUT": f"{CONTAINER_OUT}/estimate.json",
    }
    rr = req.executor.run(
        [TOOL_ENV["OPENROAD_EXE"], "-no_init", "-no_splash",
         "-exit", f"{CONTAINER_GUARDCI}/cts_estimate.tcl"],
        mounts={
            golden_host(librefs): (CONTAINER_GOLDEN, "ro"),
            str(outdir): (CONTAINER_OUT, "rw"),
            _script("cts_estimate.tcl"):
                (f"{CONTAINER_GUARDCI}/cts_estimate.tcl", "ro")},
        workdir="/", env={**TOOL_ENV, **env}, slot=req.slot,
        timeout=req.timeout, log=outdir / "estimate.log")
    if infra_rc(rr.rc):
        raise InfraError(f"estimate run rc={rr.rc}")
    est = {}
    ej = outdir / "estimate.json"
    if ej.exists():
        try:
            est = json.loads(ej.read_text())
        except ValueError:
            est = {}
    return {"est_setup_wns": est.get("est_setup_wns"),
            "est_setup_tns": est.get("est_setup_tns"),
            "est_hold_wns": est.get("est_hold_wns"),
            "est_seconds": rr.seconds}


@register
class EstimateBackend(Backend):
    """§11.4 predict: CTS-stage timing estimate (not a verdict
    measurement — bound as `backends.estimate`). The registry keys
    on `tool`, so this needs its own name (sharing "openroad" would
    shadow OpenRoadBackend)."""
    provides = "estimate"
    tool = "openroad_estimate"
    version = "m3d.1"
    memo_params: ClassVar[set] = {"period"}

    def run(self, req: MeasureRequest) -> dict:
        return run_estimate(req)


@register
class FactsBackend(Backend):
    """§12.3/§12.4 stage-artifact facts (checkpoint verification):
    export the captured ODB under the golden Liberty (DEF + functional
    Verilog + master list), then reload the DEF against the golden
    LEFs and measure canonical facts.  ``req.artifacts`` carries
    ``ArtifactKind.LAYOUT`` = the captured ODB; ``req.params``
    carries ``stage`` and, for place/cts, ``period``."""

    provides = "facts"
    tool = "openroad_facts"
    version = "w5.0"
    memo_params: ClassVar[set] = {"period", "stage"}

    def run(self, req: MeasureRequest) -> dict:
        librefs = req.golden.get("librefs") or req.golden
        constraints = req.golden.get("constraints") or {}
        outdir = Path(req.outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        odb = Path(req.artifacts.get(ArtifactKind.LAYOUT))
        stage = (req.params or {}).get("stage") or "finish"
        artifact_sha = hashlib.sha256(odb.read_bytes()).hexdigest()

        mounts = {
            golden_host(librefs): (CONTAINER_GOLDEN, "ro"),
            str(odb.parent): ("/mnt/artifact", "ro"),
            str(outdir): (CONTAINER_OUT, "rw"),
        }
        for name in ("export_artifact.tcl", "facts.tcl"):
            mounts[_script(name)] = (f"{CONTAINER_GUARDCI}/{name}", "ro")

        env = {
            **TOOL_ENV,
            "GUARDCI_LIBS": " ".join(
                p.replace(golden_host(librefs), CONTAINER_GOLDEN)
                for p in librefs["libs"]),
            "GUARDCI_ODB": f"/mnt/artifact/{odb.name}",
            "GUARDCI_DEF_OUT": f"{CONTAINER_OUT}/exported.def",
            "GUARDCI_OUT": f"{CONTAINER_OUT}/netlist.v",
            "GUARDCI_MASTERS": f"{CONTAINER_OUT}/masters.json",
        }
        rr = _run_openroad(req, mounts, "export_artifact.tcl", env,
                           outdir / "export.log")
        if infra_rc(rr.rc):
            raise InfraError(f"export_artifact rc={rr.rc}")
        mj = outdir / "masters.json"
        meta = {}
        if mj.exists():
            try:
                meta = json.loads(mj.read_text())
            except ValueError:
                meta = {}
        if meta.get("unreadable"):
            return {"unreadable": True,
                    "unreadable_error": meta.get("error", ""),
                    "artifact_sha256": artifact_sha}
        def_p = outdir / "exported.def"
        net_p = outdir / "netlist.v"
        if not def_p.exists() or not net_p.exists():
            return {"unreadable": True,
                    "unreadable_error": "export produced no DEF/netlist",
                    "artifact_sha256": artifact_sha}
        netlist_sha = hashlib.sha256(net_p.read_bytes()).hexdigest()

        fenv = {
            **TOOL_ENV,
            "GUARDCI_TECH_LEF": librefs["tech_lef"].replace(
                golden_host(librefs), CONTAINER_GOLDEN),
            "GUARDCI_SC_LEFS": " ".join(
                p.replace(golden_host(librefs), CONTAINER_GOLDEN)
                for p in librefs["sc_lefs"]),
            "GUARDCI_LIBS": " ".join(
                p.replace(golden_host(librefs), CONTAINER_GOLDEN)
                for p in librefs["libs"]),
            "GUARDCI_DEF": f"{CONTAINER_OUT}/exported.def",
            "GUARDCI_MASTERS": f"{CONTAINER_OUT}/masters.json",
            "GUARDCI_STAGE": stage,
            "GUARDCI_SDC_TEMPLATE": f"{CONTAINER_OUT}/facts.sdc",
            "GUARDCI_SET_RC_TCL": (
                (librefs.get("set_rc_tcl") or "").replace(
                    golden_host(librefs), CONTAINER_GOLDEN)),
            "GUARDCI_OUT": f"{CONTAINER_OUT}/facts.json",
        }
        (outdir / "facts.sdc").write_text(
            (constraints or {}).get("template") or "")
        period = (req.params or {}).get("period")
        if isinstance(period, (int, float)):
            fenv["GUARDCI_PERIOD"] = repr(float(period))
        fr = _run_openroad(req, mounts, "facts.tcl", fenv,
                           outdir / "facts.log")
        if infra_rc(fr.rc):
            raise InfraError(f"facts.tcl rc={fr.rc}")
        facts = {}
        fj = outdir / "facts.json"
        if fj.exists():
            try:
                facts = json.loads(fj.read_text())
            except ValueError:
                facts = {}
        # §12.4: stage facts carry the canonical census shape the
        # checkers read (same canonicalization as signoff metrics).
        cbt = {}
        for mtype, stats in (facts.get("cells_by_type") or {}).items():
            canon = canonical_cell_type(mtype)
            cur = cbt.setdefault(canon, {"count": 0, "area": 0.0})
            if isinstance(stats, dict):
                cur["count"] += stats.get("count", 0)
                cur["area"] += stats.get("area_um2", 0.0)
            else:
                cur["count"] += stats or 0
        facts["cells_by_type"] = cbt
        facts["netlist_sha256"] = netlist_sha
        facts["artifact_sha256"] = artifact_sha
        facts["def_path"] = str(def_p)
        facts["netlist_path"] = str(net_p)
        return facts
