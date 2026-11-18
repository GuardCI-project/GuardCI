# Write a structural Verilog netlist from an ODB, without physical-only
# cells (same rule as measure.tcl), under the golden liberty files.
#
# Run as: openroad -no_init -no_splash -exit write_netlist.tcl
# Environment: GUARDCI_ODB, GUARDCI_LIBS, GUARDCI_OUT.

proc guardci_env {name} {
  if {![info exists ::env($name)] || $::env($name) eq ""} {
    error "missing environment variable $name"
  }
  return $::env($name)
}

read_db [guardci_env GUARDCI_ODB]
foreach lib [guardci_env GUARDCI_LIBS] { read_liberty $lib }

set physical_only {}
foreach lib [[ord::get_db] getLibs] {
  foreach master [$lib getMasters] {
    set type [$master getType]
    if {$type in {CORE_SPACER CORE_WELLTAP CORE_ANTENNACELL}
        || [string match ENDCAP* $type]
        || [llength [get_lib_cells -quiet */[$master getName]]] == 0} {
      lappend physical_only [$master getName]
    }
  }
}
write_verilog -remove_cells $physical_only [guardci_env GUARDCI_OUT]
