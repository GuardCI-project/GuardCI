"""KLayout DRC backend (DESIGN.md §9.5): run the golden platform DRC
deck on the CI-produced stream. Port of phb measure.drc's klayout part
plus _count_lyrdb_violations/_lyrdb_stats (ORFS counts ``<value>``
occurrences in the lyrdb)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

from ..artifacts.model import ArtifactKind
from ..executors.base import InfraError, infra_rc
from .base import Backend, MeasureRequest, register
from .toolenv import CONTAINER_OUT, TOOL_ENV


def count_lyrdb_violations(path) -> int:
    """ORFS's own DRC count: occurrences of <value> in the lyrdb."""
    with open(path, "rb") as fh:
        return fh.read().count(b"<value>")


def lyrdb_stats(path):
    """(items, categories) — <item> count and per-item <category> name
    counts (port of phb _lyrdb_stats)."""
    with open(path, "rb") as fh:
        data = fh.read()
    items = len(re.findall(rb"<item>", data))
    categories = {}
    for block in re.findall(rb"<item>(.*?)</item>", data, re.DOTALL):
        m = re.search(rb"<category>\s*\"?([^\"<]+?)\"?\s*</category>",
                      block)
        if m:
            name = m.group(1).decode("utf-8", "replace").strip().strip('"')
            categories[name] = categories.get(name, 0) + 1
        else:
            categories["(none)"] = categories.get("(none)", 0) + 1
    return items, categories


@register
class KLayoutBackend(Backend):
    memo_params: ClassVar[set] = set()
    provides = "drc"
    tool = "klayout"
    version = "m2a.1"

    def run(self, req: MeasureRequest) -> dict:
        librefs = req.golden.get("librefs") or req.golden
        deck = librefs.get("klayout_drc_file")
        if not deck:
            return {"drc_count": None, "drc_unsupported": True,
                    "drc_categories": {}}
        outdir = Path(req.outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        gds = Path(req.artifacts.get(ArtifactKind.STREAM))
        lyrdb_c = f"{CONTAINER_OUT}/drc.lyrdb"
        rr = req.executor.run(
            ["/usr/bin/klayout", "-zz",
             "-rd", "in_gds=/in/stream.gds",
             "-rd", f"report_file={lyrdb_c}",
             "-r", deck],
            mounts={librefs["host_root"]: ("/golden/flow", "ro"),
                    str(gds): ("/in/stream.gds", "ro"),
                    str(outdir): (CONTAINER_OUT, "rw")},
            workdir="/", env=TOOL_ENV, slot=req.slot,
            timeout=req.timeout, log=outdir / "drc.log")
        if infra_rc(rr.rc):
            raise InfraError(f"klayout rc={rr.rc}")
        lyrdb = outdir / "drc.lyrdb"
        if not lyrdb.exists():
            raise InfraError(
                f"klayout produced no lyrdb (rc={rr.rc}, "
                f"log {outdir}/drc.log)")
        items, categories = lyrdb_stats(lyrdb)
        return {"drc_count": count_lyrdb_violations(lyrdb),
                "drc_categories": categories,
                "drc_items": items,
                "drc_unsupported": False}
