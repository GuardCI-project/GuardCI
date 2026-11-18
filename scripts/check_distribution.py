"""Check an installed distribution without relying on a source checkout."""
import sysconfig
from pathlib import Path

import guardci
from guardci.contract.loader import BUILTIN_DIR, load

package = Path(guardci.__file__).resolve().parent
site = Path(sysconfig.get_paths()["purelib"]).resolve()
assert package.is_relative_to(site), f"Expected an installed package, got {package}"
packs = sorted(BUILTIN_DIR.glob("*.yaml"))
assert len(packs) == 5, packs
for path in packs:
    assert load(path.stem).clauses, path
for relative in (
    "backends/cells_extra.v",
    "flows/orfs/variables.yaml",
    "backends/openroad/measure.tcl",
    "backends/openroad/facts.tcl",
    "backends/openroad/write_netlist.tcl",
    "backends/openroad/export_artifact.tcl",
    "backends/openroad/cts_estimate.tcl",
):
    assert (package / relative).is_file(), relative
print("PASS: installed package; all five built-in contracts and runtime assets present")
