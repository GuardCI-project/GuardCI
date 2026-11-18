"""Tiered equivalence (DESIGN §11.5): the ``equivalence`` composite
backend orchestrates three tools over a netlist pair —

  tier 1  ``kepler_formal``   one-to-one LEC (``identical`` → proven)
  tier 2  ``yosys_equiv``     sequential induction, then a bounded
                              (BMC) check from reset
  tier 3  ``cosim``           iverilog co-simulation; a defined-output
                              mismatch refutes, enough compared cycles
                              bounds

A tier that times out or errors is recorded and the next one runs. A
BMC counterexample is only a hint — it refutes only if co-simulation
reproduces a defined-output mismatch. Without a declared reset the
co-simulation tier cannot run and the status degrades to ``unknown``.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import ClassVar

from ..artifacts.model import ArtifactKind, ArtifactSet
from ..executors.base import InfraError, infra_rc
from .base import Backend, MeasureRequest, register
from .toolenv import CONTAINER_GOLDEN, CONTAINER_OUT, TOOL_ENV

YOSYS = TOOL_ENV["YOSYS_EXE"]
IVERILOG = "iverilog"
VVP = "vvp"

EQUIV_PARAMS: ClassVar[tuple] = (
    "timeout", "induction_seconds", "bmc_depth", "sim_cycles",
    "sim_seconds", "min_cycles", "seed")
EQUIV_DEFAULTS = {
    "timeout": 1800, "induction_seconds": 120, "bmc_depth": 20,
    "sim_cycles": 200000, "sim_seconds": 600, "min_cycles": 10000,
    "seed": 1}


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _pair_artifacts(b: Path) -> ArtifactSet:
    arts = ArtifactSet()
    arts.put(ArtifactKind.NETLIST, Path(b))
    return arts


def _pair_req(req: MeasureRequest, a: Path, b: Path,
              outdir: Path, params: dict, timeout: int) -> MeasureRequest:
    golden = dict(req.golden or {})
    golden["gold_netlist"] = str(a)
    outdir.mkdir(parents=True, exist_ok=True)
    return MeasureRequest(artifacts=_pair_artifacts(b), golden=golden,
                          params=params, outdir=outdir,
                          executor=req.executor, slot=req.slot,
                          timeout=timeout)


def gen_cells(req: MeasureRequest, outdir: Path) -> tuple[Path, Path]:
    """Functional cell models from the golden liberty
    (``read_liberty`` → ``write_verilog``), cached in the golden dir so
    every compare re-uses them; plus the shipped ``cells_extra.v``
    (cells whose Liberty statetables tools cannot derive, e.g.
    CLKGATE). Returns (cells.v, cells_extra.v)."""
    librefs = req.golden.get("librefs") or {}
    libs = librefs.get("libs") or []
    gold = req.golden.get("gold_netlist")
    key = _hash_text("".join(libs))
    gdir = Path(gold).parent if gold else outdir
    cells = gdir / f"cells-{key[:16]}.v"
    extra = Path(__file__).with_name("cells_extra.v")
    if not cells.exists():
        cdir = outdir / ".cells"
        cdir.mkdir(parents=True, exist_ok=True)
        script = ("".join(f"read_liberty -ignore_miss_func {lib}\n"
                  for lib in libs)
                  + f"write_verilog {CONTAINER_OUT}/cells.v\n")
        (cdir / "gen.ys").write_text(script)
        mounts = {str(librefs["host_root"]): (CONTAINER_GOLDEN, "ro"),
                  str(cdir): (CONTAINER_OUT, "rw")}
        rr = req.executor.run(
            [YOSYS, f"{CONTAINER_OUT}/gen.ys"],
            mounts=mounts, workdir="/", env=TOOL_ENV, slot=req.slot,
            timeout=600, log=cdir / "gen.log")
        out = cdir / "cells.v"
        if infra_rc(rr.rc) or not out.exists():
            raise InfraError(f"cell-model generation failed rc={rr.rc}")
        tmp = cells.with_suffix(".v.tmp")
        tmp.write_bytes(out.read_bytes())
        tmp.rename(cells)
    return cells, extra


def _hash_text(*parts) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode())
        h.update(b"\0")
    return h.hexdigest()


def _rename_module(src: Path, dest: Path, new_name: str) -> Path:
    """Copy a flat netlist with its (single, detected) top module
    renamed. A netlist with more than one ``module`` is hierarchical:
    renaming by position would silently rename the wrong unit, so it
    is refused."""
    text = Path(src).read_text()
    mods = re.findall(r"(?m)^module\s+(\w+)", text)
    if len(mods) != 1:
        raise InfraError("hierarchical netlist")
    text2, n = re.subn(r"(?m)^module\s+" + re.escape(mods[0]) + r"\b",
                       f"module {new_name}", text, count=1)
    if n != 1:
        raise InfraError(f"cannot rename top module in {src}")
    Path(dest).write_text(text2)
    return Path(dest)


def _module_count(text: str) -> int:
    return len(re.findall(r"(?m)^module\s+\w+", text))


def _prepare_revised(req: MeasureRequest, src: Path, cells: Path,
                     extra: Path, outdir: Path) -> Path:
    """Revised netlist for tier-2/3: flat, top renamed to ``revised``.
    A netlist with more than one module is flattened with yosys
    (cell models as ``-lib`` + ``hierarchy -top`` + ``flatten``)
    before renaming."""
    if _module_count(Path(src).read_text()) <= 1:
        return _rename_module(src, outdir / "revised_mod.v", "revised")
    top, _ = _top_ports(req, Path(src), outdir / ".top_rev")
    workdir = outdir / ".flat"
    workdir.mkdir(parents=True, exist_ok=True)
    ys = ("read_verilog -lib /in/cells.v /in/cells_extra.v\n"
          "read_verilog /in/net.v\n"
          f"hierarchy -top {top}\n"
          "flatten\n"
          "opt_clean -purge\n"
          f"write_verilog -noattr {CONTAINER_OUT}/flat.v\n")
    (workdir / "flat.ys").write_text(ys)
    mounts = {str(cells): ("/in/cells.v", "ro"),
              str(extra): ("/in/cells_extra.v", "ro"),
              str(src): ("/in/net.v", "ro"),
              str(workdir): (CONTAINER_OUT, "rw")}
    rr = req.executor.run(
        [YOSYS, f"{CONTAINER_OUT}/flat.ys"],
        mounts=mounts, workdir="/", env=TOOL_ENV, slot=req.slot,
        timeout=600, log=workdir / "flat.log")
    flat = workdir / "flat.v"
    if infra_rc(rr.rc) or not flat.exists():
        raise InfraError(f"flatten failed rc={rr.rc}")
    return _rename_module(flat, outdir / "revised_mod.v", "revised")


def _top_ports(req: MeasureRequest, netlist: Path,
               workdir: Path) -> tuple[str, dict]:
    """(top module, {port: {direction, bits}}) via yosys
    ``hierarchy -auto-top`` + ``write_json``."""
    workdir.mkdir(parents=True, exist_ok=True)
    ys = (f"read_verilog /in/net.v\n"
          f"hierarchy -auto-top\n"
          f"write_json {CONTAINER_OUT}/top.json\n")
    (workdir / "top.ys").write_text(ys)
    mounts = {str(netlist): ("/in/net.v", "ro"),
              str(workdir): (CONTAINER_OUT, "rw")}
    rr = req.executor.run(
        [YOSYS, f"{CONTAINER_OUT}/top.ys"],
        mounts=mounts, workdir="/", env=TOOL_ENV, slot=req.slot,
        timeout=300, log=workdir / "top.log")
    js = workdir / "top.json"
    if infra_rc(rr.rc) or not js.exists():
        raise InfraError(f"top detection failed rc={rr.rc}")
    data = json.loads(js.read_text())
    top = None
    for name, mod in (data.get("modules") or {}).items():
        attrs = mod.get("attributes") or {}
        val = attrs.get("top")
        if val is not None and int(val, 2):
            top = name
            ports = {}
            for p, info in (mod.get("ports") or {}).items():
                ports[p] = {"direction": info.get("direction"),
                            "bits": len(info.get("bits") or [])}
            return top, ports
    raise InfraError("no top module found")


_TB_TEMPLATE = """`timescale 1ns/1ps
module tb;
  reg {clock}=0, {reset}={reset_active};
  {inputs_decl}
  {outputs_decl}
  integer cycle = 0;
  integer diffs = 0;
  integer compared = 0;
  integer xcycles = 0;
  integer stimf;
  integer rng_state = 32'h{seed:08x};
  {gold_inst}
  {rev_inst}
  always #5 {clock} = ~{clock};
  initial begin
    stimf = $fopen("/out/stimulus.txt", "w");
    repeat ({reset_cycles}) @(negedge {clock});
    {reset} = {reset_inactive};
    forever begin
      @(negedge {clock});
      cycle = cycle + 1;
      $fdisplay(stimf, "cycle %0d {stim_fmt}", cycle{stim_args});
      if ((|({gold_outs} ^ {rev_outs})) === 1'b1) begin
        diffs = diffs + 1;
        if (diffs == 1) begin
          $fdisplay(stimf, "MISMATCH cycle=%0d", cycle);
          {dump_mismatch}
          $fclose(stimf);
          $display("RESULT mismatch=1 cycle=%0d compared=%0d xcycles=%0d",
                   cycle, compared, xcycles);
          $finish;
        end
      end else if ((^{gold_outs} === 1'bx) || (^{rev_outs} === 1'bx)) begin
        xcycles = xcycles + 1;
      end else begin
        compared = compared + 1;
      end
      if (cycle % 1000 == 0) begin
        $display("PROGRESS cycle=%0d compared=%0d xcycles=%0d",
                 cycle, compared, xcycles);
        $fflush;
      end
      if (cycle == {sim_cycles}) begin
        $fclose(stimf);
        $display("RESULT mismatch=0 cycle=%0d compared=%0d xcycles=%0d",
                 cycle, compared, xcycles);
        $finish;
      end
      {drive_inputs}
    end
  end
endmodule
"""


def _generate_tb(top: str, ports: dict, reset: dict, clock: str,
                 sim_cycles: int, seed: int = 1) -> str:
    reset_port = reset["port"]
    ins = [p for p, i in ports.items()
           if i["direction"] == "input"
           and p not in (clock, reset_port)]
    outs = [p for p, i in ports.items() if i["direction"] == "output"]
    inputs_decl = "\n  ".join(
        f"reg [{max(i['bits'],1)-1}:0] {p}=0;" if i["bits"] > 1
        else f"reg {p}=0;"
        for p, i in ports.items() if p in ins)
    outputs_decl = "\n  ".join(
        f"wire [{max(ports[p]['bits'],1)-1}:0] g_{p}; "
        f"wire [{max(ports[p]['bits'],1)-1}:0] r_{p};"
        for p in outs)

    def conns(prefix):
        return ",\n    ".join(
            (f".{p}({prefix}_{p})" if p in outs else f".{p}({p})")
            for p in ports)

    gold_outs = "{" + ",".join(f"g_{p}" for p in outs) + "}"
    rev_outs = "{" + ",".join(f"r_{p}" for p in outs) + "}"
    stim_fmt = " ".join(f"{p}=%h" for p in ins)
    stim_args = "".join(f", {p}" for p in ins)
    dump = "\n          ".join(
        f'$fdisplay(stimf, "  {p} gold=%b revised=%b", g_{p}, r_{p});'
        for p in outs)
    drive_lines = []
    for p in ins:
        words = (max(ports[p]["bits"], 1) + 31) // 32
        draws = ", ".join("$random(rng_state)" for _ in range(words))
        drive_lines.append(f"{p} = {{{draws}}};")
    drive = "\n      ".join(drive_lines)
    return _TB_TEMPLATE.format(
        clock=clock, reset=reset_port,
        reset_active=reset["active"],
        reset_inactive=1 - int(reset["active"]),
        reset_cycles=reset.get("cycles", 2),
        inputs_decl=inputs_decl, outputs_decl=outputs_decl,
        gold_inst=f"{top} gold    ({conns('g')});",
        rev_inst=f"revised rev     ({conns('r')});",
        gold_outs=gold_outs, rev_outs=rev_outs,
        stim_fmt=stim_fmt, stim_args=stim_args,
        dump_mismatch=dump, drive_inputs=drive,
        sim_cycles=sim_cycles, seed=seed & 0xFFFFFFFF)


@register
class YosysEquivBackend(Backend):
    """Yosys equivalence engine (tier 2): sequential induction
    (``equiv_make`` + ``equiv_simple -seq`` + ``equiv_induct``) or a
    bounded check from reset (``miter`` + ``sat -seq``)."""
    tool = "yosys_equiv"
    provides = ""
    version = "v2.3"

    def run(self, req: MeasureRequest) -> dict:
        a = req.golden.get("gold_netlist")
        b = req.artifacts.get(ArtifactKind.NETLIST)
        if a is None or b is None:
            return {"yosys_result": "error",
                    "error": "netlist pair missing"}
        outdir = Path(req.outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        cells, extra = gen_cells(req, outdir)
        rev = _prepare_revised(req, Path(b), cells, extra, outdir)
        top = req.params.get("top")
        if not top:
            top, _ = _top_ports(req, Path(a), outdir / ".top")
        mode = req.params.get("mode", "induction")
        if mode == "bmc":
            return self._bmc(req, a, b, rev, cells, extra, top, outdir)
        return self._induct(req, a, b, rev, cells, extra, top, outdir)

    def _common_mounts(self, req, cells, extra, a, b, outdir):
        librefs = req.golden.get("librefs") or {}
        mounts = {str(cells): ("/in/cells.v", "ro"),
                  str(extra): ("/in/cells_extra.v", "ro"),
                  str(a): ("/in/gold.v", "ro"),
                  str(b): ("/in/revised_mod.v", "ro"),
                  str(outdir): (CONTAINER_OUT, "rw")}
        if librefs.get("host_root"):
            mounts[str(librefs["host_root"])] = (CONTAINER_GOLDEN, "ro")
        return mounts

    def _induct(self, req, a, b, rev, cells, extra, top, outdir):
        seq = int(req.params.get("induct_seq", 5))
        ys = (f"read_verilog /in/cells.v /in/cells_extra.v\n"
              f"read_verilog /in/gold.v\n"
              f"rename {top} gold\n"
              f"read_verilog /in/revised_mod.v\n"
              f"equiv_make gold revised equiv\n"
              f"flatten equiv\n"
              f"equiv_simple -seq {seq}\n"
              f"equiv_induct\n"
              f"equiv_status -assert\n")
        (outdir / "equiv.ys").write_text(ys)
        timeout = int(req.params.get("induction_seconds", 120))
        rr = req.executor.run(
            [YOSYS, f"{CONTAINER_OUT}/equiv.ys"],
            mounts=self._common_mounts(req, cells, extra, a, rev, outdir),
            workdir="/", env=TOOL_ENV, slot=req.slot, timeout=timeout,
            log=outdir / "equiv.log")
        log = (outdir / "equiv.log").read_text(errors="replace") \
            if (outdir / "equiv.log").exists() else ""
        if rr.timed_out:
            return {"yosys_result": "timeout", "yosys_seconds": timeout}
        if rr.rc == 0 and "ERROR" not in log and \
                "Equivalence successfully proven" in log:
            return {"yosys_result": "proven"}
        if "unproven" in log.lower():
            return {"yosys_result": "not_proven"}
        if "ERROR" in log or rr.rc != 0:
            return {"yosys_result": "error", "error": log[-400:]}
        return {"yosys_result": "not_proven"}

    def _bmc(self, req, a, b, rev, cells, extra, top, outdir):
        reset = (req.golden.get("reset") or {})
        depth = int(req.params.get("bmc_depth", 20))
        sets = []
        if reset.get("port"):
            rp = f"in_{reset['port']}"
            active = int(reset.get("active", 1))
            rcy = int(reset.get("cycles", 2))
            for k in range(1, depth + 1):
                sets += ["-set-at", str(k), rp,
                         str(active if k <= rcy else 1 - active)]
        ys = (f"read_verilog /in/cells.v /in/cells_extra.v\n"
              f"read_verilog /in/gold.v\n"
              f"rename {top} gold\n"
              f"read_verilog /in/revised_mod.v\n"
              f"hierarchy -check\n"
              f"miter -equiv -make_outputs -make_assert gold revised miter\n"
              f"flatten miter\n"
              f"proc\n"
              f"async2sync\n"
              f"dffunmap\n"
              f"opt_clean -purge\n"
              f"sat -seq {depth} -verify -prove-asserts "
              f"{' '.join(sets)} miter\n")
        (outdir / "miter.ys").write_text(ys)
        timeout = int(req.params.get("bmc_seconds",
                                     req.params.get("induction_seconds",
                                                    120)))
        rr = req.executor.run(
            [YOSYS, f"{CONTAINER_OUT}/miter.ys"],
            mounts=self._common_mounts(req, cells, extra, a, rev, outdir),
            workdir="/", env=TOOL_ENV, slot=req.slot, timeout=timeout,
            log=outdir / "miter.log")
        log = (outdir / "miter.log").read_text(errors="replace") \
            if (outdir / "miter.log").exists() else ""
        if rr.timed_out:
            return {"bmc_result": "timeout", "bmc_depth": depth}
        if "FAIL" in log:
            return {"bmc_result": "counterexample", "bmc_depth": depth}
        if "SUCCESS" in log:
            return {"bmc_result": "no_counterexample",
                    "bmc_depth": depth}
        return {"bmc_result": "error", "bmc_depth": depth,
                "error": log[-400:]}


@register
class CosimBackend(Backend):
    """iverilog co-simulation (tier 3): reset sequence, then random
    stimulus on all non-clock/non-reset inputs; primary outputs are
    compared bitwise every cycle. Defined-bit differences refute even
    alongside X/Z; only fully defined equal cycles count. A mismatch
    keeps the stimulus log and the differing outputs as replayable
    evidence."""
    tool = "cosim"
    provides = ""
    version = "v2.4"

    def run(self, req: MeasureRequest) -> dict:
        a = req.golden.get("gold_netlist")
        b = req.artifacts.get(ArtifactKind.NETLIST)
        reset = req.golden.get("reset") or {}
        if a is None or b is None:
            return {"sim_result": "error",
                    "error": "netlist pair missing"}
        if not reset.get("port"):
            return {"sim_result": "skipped",
                    "error": "no reset declared"}
        outdir = Path(req.outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        top, ports = _top_ports(req, Path(a), outdir / ".top")
        top = req.params.get("top") or top
        clock = req.golden.get("clock_port")
        if not clock or clock not in ports:
            clock = next((p for p, i in ports.items()
                          if i["direction"] == "input"
                          and p != reset["port"]), None)
        cells, extra = gen_cells(req, outdir)
        rev = _prepare_revised(req, Path(b), cells, extra, outdir)
        sim_cycles = int(req.params.get("sim_cycles", 200000))
        seed = int(req.params.get("seed", 1))
        tb = _generate_tb(top, ports, reset, clock, sim_cycles, seed)
        (outdir / "tb.v").write_text(tb)
        timeout = int(req.params.get("sim_seconds", 600))
        mounts = {str(cells): ("/in/cells.v", "ro"),
                  str(extra): ("/in/cells_extra.v", "ro"),
                  str(a): ("/in/gold.v", "ro"),
                  str(rev): ("/in/revised_mod.v", "ro"),
                  str(outdir / "tb.v"): ("/in/tb.v", "ro"),
                  str(outdir): (CONTAINER_OUT, "rw")}
        rr = req.executor.run(
            ["bash", "-lc",
             (f"{IVERILOG} -o {CONTAINER_OUT}/sim.vvp -s tb "
              f"/in/cells.v /in/cells_extra.v /in/gold.v "
              f"/in/revised_mod.v /in/tb.v && "
              f"{VVP} {CONTAINER_OUT}/sim.vvp")],
            mounts=mounts, workdir="/", env=TOOL_ENV, slot=req.slot,
            timeout=timeout, log=outdir / "sim.log")
        log = (outdir / "sim.log").read_text(errors="replace") \
            if (outdir / "sim.log").exists() else ""
        m = re.search(
            r"RESULT mismatch=(\d+) cycle=(\d+) compared=(\d+) "
            r"xcycles=(\d+)", log)
        cex = outdir / "counterexample.json"
        if m and int(m.group(1)):
            stim = outdir / "stimulus.txt"
            cex.write_text(json.dumps({
                "cycle": int(m.group(2)), "top": top,
                "clock": clock, "reset": reset,
                "stimulus": str(stim),
                "stimulus_log": (stim.read_text(errors="replace")
                                 [-2000:] if stim.exists() else "")},
                indent=1))
        if m is None and rr.timed_out:
            prog = re.findall(
                r"PROGRESS cycle=(\d+) compared=(\d+) xcycles=(\d+)",
                log)
            if prog:
                cyc, cmpd, xcy = prog[-1]
                res = {"sim_result": "match", "sim_partial": True,
                       "sim_cycles": int(cyc),
                       "sim_compared": int(cmpd),
                       "sim_x_cycles": int(xcy),
                       "sim_seconds": timeout}
            else:
                res = {"sim_result": "timeout", "sim_seconds": timeout}
        elif m is None or infra_rc(rr.rc):
            res = {"sim_result": "error", "error": log[-400:]}
        else:
            res = {"sim_result": ("mismatch" if int(m.group(1))
                                  else "match"),
                   "sim_cycles": int(m.group(2)),
                   "sim_compared": int(m.group(3)),
                   "sim_x_cycles": int(m.group(4))}
        res["seed"] = int(req.params.get("seed", 1))
        if cex.exists():
            res["counterexample"] = str(cex)
        return res


class EquivalenceBackend(Backend):
    """Composite ``lec`` measurement (§11.5): one-to-one LEC →
    sequential induction → co-simulation, per the tier rules; a BMC
    counterexample is only a hint."""
    provides = "lec"
    tool = "equivalence"
    version = "v2.4"
    memo_params: ClassVar[set] = set(EQUIV_PARAMS)

    def __init__(self, tiers: dict | None = None):
        self._tiers = tiers

    def _tier(self, name: str):
        if self._tiers is not None:
            return self._tiers[name]
        from . import base as b
        reg = b.registry()
        tool = {"one_to_one": "kepler_formal",
                "induction": "yosys_equiv",
                "bmc": "yosys_equiv",
                "cosim": "cosim"}[name]
        return reg[tool]()

    def run(self, req: MeasureRequest) -> dict:
        a = req.golden.get("gold_netlist")
        b = req.artifacts.get(ArtifactKind.NETLIST)
        return self.compare(Path(a), Path(b), req.params or {}, req)

    def compare(self, a: Path, b: Path, params: dict,
                req: MeasureRequest, tiers=None) -> dict:
        P = {**EQUIV_DEFAULTS,
             **{k: v for k, v in (params or {}).items()
                if k in EQUIV_DEFAULTS or k == "timeout"}}
        want = tiers or ("one_to_one", "induction", "bmc", "cosim")
        records: list[dict] = []
        metrics: dict = {"seed": P["seed"], "bmc_depth": P["bmc_depth"]}
        t0 = time.monotonic()
        gold_top = None
        if any(t in want for t in ("induction", "bmc", "cosim")):
            gold_top, _ = _top_ports(req, Path(a),
                                     Path(req.outdir) / ".top")

        def rec(tier, tool, result, seconds, detail=""):
            records.append({"tier": tier, "tool": tool,
                            "result": result, "seconds": seconds,
                            "detail": detail})

        def sub(name, outname, timeout, extra=None):
            p = dict(params or {})
            p.update(extra or {})
            subreq = _pair_req(req, a, b, Path(req.outdir) / outname,
                               p, timeout)
            tb = time.monotonic()
            backend = self._tier(name)
            try:
                mm = backend.run(subreq)
            except InfraError as exc:
                return {"result": "error", "error": str(exc)}, \
                    time.monotonic() - tb
            except Exception as exc:  # noqa: BLE001
                return {"result": "error", "error": str(exc)}, \
                    time.monotonic() - tb
            return mm, time.monotonic() - tb

        status = "unknown"
        if "one_to_one" in want:
            m, sec = sub("one_to_one", "tier1_lec", P["timeout"])
            r = m.get("lec_result", "error")
            metrics["lec_result"] = r
            metrics["lec_differences"] = m.get("lec_differences")
            metrics["lec_timeout_s"] = m.get("lec_timeout_s")
            rec("one_to_one", "kepler_formal", r, round(sec, 3),
                m.get("error") or "")
            if r == "identical":
                status = "proven"
        if status != "proven" and "induction" in want:
            m, sec = sub("induction", "tier2_induct",
                         P["induction_seconds"],
                         {"mode": "induction",
                          "induction_seconds": P["induction_seconds"],
                          "top": gold_top})
            r = m.get("yosys_result", "error")
            rec("induction", "yosys_equiv", r, round(sec, 3),
                m.get("error") or "")
            if r == "proven":
                status = "proven_sequential"
        if status not in ("proven", "proven_sequential") \
                and "bmc" in want:
            m, sec = sub("bmc", "tier2_bmc",
                         P["induction_seconds"],
                         {"mode": "bmc", "bmc_depth": P["bmc_depth"],
                          "top": gold_top})
            r = m.get("bmc_result", "error")
            metrics["bmc_result"] = r
            rec("bmc", "yosys_equiv", r, round(sec, 3),
                m.get("error") or "")
            # a counterexample is only a hint (§11.5): it must be
            # reproduced by co-simulation to refute
        if status not in ("proven", "proven_sequential") \
                and "cosim" in want:
            if not (req.golden.get("reset") or {}).get("port"):
                rec("cosim", "cosim", "skipped", 0.0,
                    "no reset declared")
            else:
                m, sec = sub("cosim", "tier3_sim", P["sim_seconds"],
                             {"sim_cycles": P["sim_cycles"],
                              "sim_seconds": P["sim_seconds"],
                              "seed": P["seed"],
                              "top": gold_top})
                r = m.get("sim_result", "error")
                metrics["sim_cycles"] = m.get("sim_cycles")
                metrics["sim_compared"] = m.get("sim_compared")
                metrics["sim_x_cycles"] = m.get("sim_x_cycles")
                if m.get("counterexample"):
                    metrics["counterexample"] = m["counterexample"]
                rec("cosim", "cosim", r, round(sec, 3),
                    m.get("error") or "")
                if r == "mismatch":
                    status = "refuted"
                elif r == "match" and \
                        (m.get("sim_compared") or 0) >= P["min_cycles"]:
                    status = "bounded"
        metrics["equivalence_status"] = status
        metrics["equivalence_tiers"] = records
        metrics["equivalence_seconds"] = round(
            time.monotonic() - t0, 3)
        return metrics


register(EquivalenceBackend)
