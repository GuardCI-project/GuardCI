# §11.4 "Predicted open": time the post-CTS database under golden
# constraints at the node's acceptance period with estimated parasitics.
# Inputs (env):
#   GUARDCI_CTS_ODB       the build's 4_cts.odb (mounted read-only)
#   GUARDCI_LIBS          golden Liberty files (space separated)
#   GUARDCI_SDC_TEMPLATE  golden SDC template; reads ::guardci_period
#   GUARDCI_PERIOD        acceptance period (library time units)
#   GUARDCI_SET_RC_TCL    golden platform's set_rc_tcl script
#   GUARDCI_OUT           output JSON path

proc guardci_env {name} {
  if {![info exists ::env($name)] || $::env($name) eq ""} {
    error "missing environment variable $name"
  }
  return $::env($name)
}

foreach lib [split [guardci_env GUARDCI_LIBS]] { read_liberty $lib }
read_db [guardci_env GUARDCI_CTS_ODB]

set ::guardci_period [guardci_env GUARDCI_PERIOD]
source [guardci_env GUARDCI_SDC_TEMPLATE]
set_propagated_clock [all_clocks]
set rc_tcl [guardci_env GUARDCI_SET_RC_TCL]
if {$rc_tcl ne ""} { source $rc_tcl }
estimate_parasitics -placement

proc num_or_null {x} {
  if {$x eq "" || $x eq "INFINITY" || $x eq "-INFINITY"} { return "null" }
  return $x
}

set ws [sta::time_sta_ui [sta::worst_slack_cmd max]]
set ts [sta::time_sta_ui [sta::total_negative_slack_cmd max]]
set wh [sta::time_sta_ui [sta::worst_slack_cmd min]]
set f [open [guardci_env GUARDCI_OUT] w]
puts $f "{"
puts $f "  \"est_setup_wns\": [num_or_null $ws],"
puts $f "  \"est_setup_tns\": [num_or_null $ts],"
puts $f "  \"est_hold_wns\": [num_or_null $wh]"
puts $f "}"
close $f
