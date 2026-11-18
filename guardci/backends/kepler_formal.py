"""kepler-formal LEC backend (DESIGN.md §9.5): structural equivalence of
the gold (baseline synthesis) and revised netlists, like ORFS's
LEC_CHECK. Port of phb measure.lec."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from ..artifacts.model import ArtifactKind
from ..executors.base import InfraError, infra_rc
from .base import Backend, MeasureRequest, register
from .toolenv import CONTAINER_GOLDEN, CONTAINER_OUT, TOOL_ENV


def classify_log(log_bytes: bytes) -> tuple[str, int]:
    differences = log_bytes.count(b"Found difference")
    if b"Circuits are IDENTICAL" in log_bytes and not differences:
        return "identical", differences
    if differences:
        return "different", differences
    if b"LEC boundary mismatch" in log_bytes:
        return "boundary_mismatch", differences
    return "error", differences


@register
class KeplerFormalBackend(Backend):
    memo_params: ClassVar[set] = {"timeout"}
    provides = "lec"
    tool = "kepler_formal"
    version = "m2a.2"

    def run(self, req: MeasureRequest) -> dict:
        librefs = req.golden.get("librefs") or req.golden
        gold = req.golden.get("gold_netlist")
        revised = req.artifacts.get(ArtifactKind.NETLIST)
        if gold is None or revised is None:
            return {"lec_result": "error", "lec_differences": None,
                    "error": "gold or revised netlist missing"}
        outdir = Path(req.outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        libs = "".join(f"  - {lib}\n" for lib in librefs.get("libs", []))
        yml = ("format: verilog\n"
               "input_paths:\n  - /in/lec-0.v\n  - /in/lec-1.v\n"
               f"liberty_files:\n{libs}"
               f"log_file: {CONTAINER_OUT}/lec.log\n")
        (outdir / "lec.yml").write_text(yml)
        timeout = int((req.params or {}).get("timeout",
                                             req.timeout or 3600))
        mounts = {librefs["host_root"]: (CONTAINER_GOLDEN, "ro"),
                  str(gold): ("/in/lec-0.v", "ro"),
                  str(revised): ("/in/lec-1.v", "ro"),
                  str(outdir): (CONTAINER_OUT, "rw")}
        try:
            rr = req.executor.run(
                [TOOL_ENV["KEPLER_FORMAL_EXE"], "--config",
                 f"{CONTAINER_OUT}/lec.yml"],
                mounts=mounts, workdir="/", env=TOOL_ENV, slot=req.slot,
                timeout=timeout, log=outdir / "lec-run.log")
        except Exception as exc:
            raise InfraError(f"kepler-formal: {exc}") from exc
        if rr.timed_out:
            return {"lec_result": "timeout",
                    "lec_differences": None,
                    "lec_timeout_s": timeout,
                    "error": f"timeout after {timeout}s"}
        if infra_rc(rr.rc):
            raise InfraError(f"kepler-formal rc={rr.rc}")
        log = outdir / "lec.log"
        log_bytes = log.read_bytes() if log.exists() else b""
        result, differences = classify_log(log_bytes)
        run_log = outdir / "lec-run.log"
        run_bytes = run_log.read_bytes() if run_log.exists() else b""
        if result == "error" and b"LEC boundary mismatch" in run_bytes:
            result = "boundary_mismatch"
        res = {"lec_result": result, "lec_differences": differences}
        if result in ("error", "boundary_mismatch"):
            res["error"] = run_bytes[-400:].decode(errors="replace")
        return res
