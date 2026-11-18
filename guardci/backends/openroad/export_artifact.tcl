# §12.3 library_closure step 1: export a stage artifact (ODB) as DEF +
# functional Verilog (without physical-only cells — same rule as
# write_netlist.tcl) under the golden Liberty files, and list every
# instance master.
#
# Run as: openroad -no_init -no_splash -exit export_artifact.tcl
# Environment:
#   GUARDCI_ODB      the stage artifact (.odb)
#   GUARDCI_LIBS     golden Liberty files (space separated)
#   GUARDCI_DEF_OUT  exported DEF path
#   GUARDCI_OUT      exported functional Verilog path
#   GUARDCI_MASTERS  output JSON path {"masters": [...]} or
#                    {"unreadable": 1, "error": "..."}

proc guardci_env {name} {
  if {![info exists ::env($name)] || $::env($name) eq ""} {
    error "missing environment variable $name"
  }
  return $::env($name)
}

# JSON escaping helpers — never interpolate raw values into JSON.
proc json_str {s} {
  return "\"[string map {\\ \\\\ \" \\\" \n \\n \r \\r \t \\t} $s]\""
}
proc json_str_list {items} {
  set parts {}
  foreach i $items { lappend parts [json_str $i] }
  return "\[[join $parts {, }]\]"
}

set masters_file [guardci_env GUARDCI_MASTERS]
if {[catch {read_db [guardci_env GUARDCI_ODB]} err]} {
  set f [open $masters_file w]
  puts $f "{\"unreadable\": 1, \"error\": [json_str $err]}"
  close $f
  exit 0
}
foreach lib [split [guardci_env GUARDCI_LIBS]] { read_liberty $lib }

set masters {}
foreach inst [[ord::get_db_block] getInsts] {
  lappend masters [[$inst getMaster] getName]
}
set masters [lsort -unique $masters]
set f [open $masters_file w]
puts $f "{\"masters\": [json_str_list $masters]}"
close $f

write_def [guardci_env GUARDCI_DEF_OUT]

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
