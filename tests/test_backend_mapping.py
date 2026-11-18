"""Backend metric mapping: canned measure.json → canonical metrics,
lyrdb counting, LEC log classification, NDR sanitizer."""

from __future__ import annotations

import io
import json
from pathlib import Path

from guardci.backends.kepler_formal import classify_log
from guardci.backends.klayout import count_lyrdb_violations, lyrdb_stats
from guardci.backends.openroad import canonical_cell_type, map_metrics, sanitize_def_ndr

FIXTURES = Path(__file__).parent / "fixtures"


def test_map_metrics_from_reference():
    raw = json.loads((FIXTURES / "measure_raw.json").read_text())
    m = map_metrics(raw, period=0.435)
    assert m["clock_period"] == 0.435
    assert m["setup_wns"] == raw["setup_wns"]
    assert m["setup_tns"] == raw["setup_tns"]
    assert m["setup_margin_ratio"] == raw["setup_wns"] / 0.435
    assert m["hold_wns"] == raw["hold_wns"]
    assert m["electrical_violations"] == 0
    assert m["cells_by_type"]["tapcell"]["count"] == 46
    assert m["cells_by_type"]["filler"]["count"] == 256
    assert m["cells_by_type"]["core"]["count"] == 686
    assert "CORE_SPACER" not in m["cells_by_type"]
    assert m["pdn_wires_by_layer"]["metal1"] == 24
    assert m["ir_drop"]["VDD"]["nominal"] == 1.1
    assert abs(m["ir_drop"]["VDD"]["drop"] -
               (1.1 - 1.093844)) < 1e-9
    assert abs(m["ir_drop"]["VSS"]["drop"] - 0.003889) < 1e-9
    assert m["cell_area"] == raw["cell_area_um2"]
    assert m["instances"] == 732
    assert m["placement_legal"] is True
    assert m["timing_error"] is None


def test_canonical_cell_types():
    assert canonical_cell_type("CORE_WELLTAP") == "tapcell"
    assert canonical_cell_type("ENDCAP_PRE") == "endcap"
    assert canonical_cell_type("CORE_ANTENNACELL") == "antenna"
    assert canonical_cell_type("CORE_SPACER") == "filler"
    assert canonical_cell_type("CORE") == "core"
    assert canonical_cell_type("BLOCK") == "BLOCK"


def test_map_metrics_timing_error():
    m = map_metrics({}, None,
                    timing_error="main clock not defined by "
                                 "implementation constraints")
    assert m["clock_period"] is None
    assert m["timing_error"].startswith("main clock")


LYRDB = b"""<?xml version="1.0"?>
<report-database>
 <items>
  <item>
   <category>"via2.5"</category>
   <values><value>x</value></values>
  </item>
  <item>
   <category>met1.1</category>
   <values><value>y</value><value>z</value></values>
  </item>
 </items>
</report-database>
"""


def test_lyrdb_counting(tmp_path):
    p = tmp_path / "d.lyrdb"
    p.write_bytes(LYRDB)
    assert count_lyrdb_violations(p) == 3
    items, cats = lyrdb_stats(p)
    assert items == 2
    assert cats == {"via2.5": 1, "met1.1": 1}


def test_lec_log_classification():
    assert classify_log(b"Circuits are IDENTICAL\n")[0] == "identical"
    assert classify_log(b"Found difference at x\n")[0] == "different"
    assert classify_log(b"garbage")[0] == "error"


DEF_NDR = """\
VERSION 5.8 ;
NONDEFAULTRULES 1 ;
- ndr_a + RULEWIDTH 0.1 ;
END NONDEFAULTRULES
NETS 2 ;
- n1 ( PIN a ) + NONDEFAULTRULE ndr_a ;
- n2 ( PIN b ) + NONDEFAULTRULE ghost_ndr ;
END NETS
SPECIALNETS 1 ;
- VDD + NONDEFAULTRULE ghost_ndr ;
END SPECIALNETS
END DESIGN
"""


def test_sanitize_def_ndr():
    out = io.StringIO()
    res = sanitize_def_ndr(io.StringIO(DEF_NDR), out)
    text = out.getvalue()
    assert res["names"] == ["ghost_ndr"]
    assert res["nets"] == 2
    assert "ndr_a" in text
    assert "ghost_ndr" not in text
