"""Resources must be inside the importable package, not the working directory."""
from pathlib import Path

import guardci
from guardci.contract.loader import BUILTIN_DIR, load


def test_builtin_contracts_outside_checkout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    package = Path(guardci.__file__).resolve().parent
    assert BUILTIN_DIR.is_relative_to(package)
    for name in ("orfs-default", "orfs-rebuild", "phb-pa80",
                 "phb-pa80-rebuild", "phb-pa80-replay"):
        assert load(name).clauses


def test_runtime_assets_present():
    package = Path(guardci.__file__).resolve().parent
    assert (package / "backends/cells_extra.v").is_file()
    assert (package / "flows/orfs/variables.yaml").is_file()
    scripts = package / "backends/openroad"
    assert {p.name for p in scripts.glob("*.tcl")} >= {
        "measure.tcl", "facts.tcl", "write_netlist.tcl",
        "export_artifact.tcl", "cts_estimate.tcl",
    }
