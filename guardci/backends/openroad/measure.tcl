# Independent PPA measurement of a routed DEF with original PDK inputs.
#
# Run as: openroad -no_init -no_splash -exit measure.tcl
# Inputs are environment variables (paths are absolute):
#   GUARDCI_TECH_LEF      original technology LEF
#   GUARDCI_SC_LEFS       original standard-cell LEFs (space separated)
#   GUARDCI_LIBS          original Liberty files (space separated)
#   GUARDCI_DEF           routed DEF to measure
#   GUARDCI_SDC_TEMPLATE  measurement SDC; may read the Tcl variable ::guardci_period
#   GUARDCI_PERIOD        clock period for this measurement (library time units)
#   GUARDCI_RCX_RULES     original OpenRCX rules file
#   GUARDCI_OUT           output JSON path
#   GUARDCI_NETLIST_OUT   optional: Verilog netlist without physical-only cells,
#                     for equivalence checking
#
# Slacks and periods are reported in Liberty time units (time_unit), power in
# watts, areas in square microns.
# The DEF is read against the original LEFs, so cell geometry and area come
# from the original library. Parasitics are extracted with the original RCX
# rules. Power is vectorless with explicitly fixed input activity.
#
# Geometry results (area, netlist) are computed before extraction; placement
# and antenna checks run last. If extraction fails (for example a layout whose
# wires short when read with the original LEF), timing and power are reported
# as null together with the error; the other results are still written.

proc guardci_env {name} {
  if {![info exists ::env($name)] || $::env($name) eq ""} {
    error "missing environment variable $name"
  }
  return $::env($name)
}

# Values stored through guardci_raw are preformatted JSON and written as-is.
proc guardci_raw {json} { return "\x01$json" }

proc guardci_json_value {value} {
  if {[string index $value 0] eq "\x01"} {
    return [string range $value 1 end]
  }
  if {$value eq "null"} { return "null" }
  if {[string is double -strict $value]} {
    if {$value eq "inf" || $value eq "-inf" || $value eq "nan"} {
      return "\"$value\""
    }
    return $value
  }
  return "\"[string map {\\ \\\\ \" \\\" \n \\n \t \\t} $value]\""
}

proc guardci_str {value} {
  return "\"[string map {\\ \\\\ \" \\\" \n \\n \t \\t} $value]\""
}

proc guardci_num {value} {
  if {$value eq "" || $value eq "null"} { return "null" }
  if {[string is double -strict $value] && $value ni {inf -inf nan}} {
    return $value
  }
  return [guardci_str $value]
}

proc guardci_obj {pairs} {
  set parts {}
  foreach {k v} $pairs { lappend parts "[guardci_str $k]: $v" }
  return "\{[join $parts {, }]\}"
}

proc guardci_list {items} { return "\[[join $items {, }]\]" }

proc guardci_bool {value} {
  return [expr {$value eq "1" || $value eq "true" || $value eq "yes"}]
}

proc guardci_write {path results} {
  set handle [open $path w]
  puts $handle "\{"
  set keys [dict keys $results]
  set last [lindex $keys end]
  foreach key $keys {
    set sep [expr {$key eq $last ? "" : ","}]
    puts $handle "  \"$key\": [guardci_json_value [dict get $results $key]]$sep"
  }
  puts $handle "\}"
  close $handle
}

set ::guardci_period ""
if {[info exists ::env(GUARDCI_PERIOD)] && $::env(GUARDCI_PERIOD) ne ""} {
  set ::guardci_period $::env(GUARDCI_PERIOD)
}
set out_path [guardci_env GUARDCI_OUT]

set results [dict create]
dict set results period $::guardci_period

# Constraint reset so re-sourcing the golden template fully replaces
# clock definitions, latencies and IO delays (§11.3).
proc guardci_reset_sdc {} {
  catch {unset_input_delay [all_inputs]}
  catch {unset_output_delay [all_outputs]}
  catch {unset_clock_uncertainty -all}
  catch {unset_clock_transition -all}
  catch {unset_clock_groups -all}
  catch {unset_disable_timing -all}
  catch {unset_timing_derate}
  foreach c [all_clocks] {
    catch {delete_generated_clock $c}
    catch {delete_clock $c}
  }
}

proc guardci_source_sdc {p} {
  guardci_reset_sdc
  set ::guardci_period $p
  source $::env(GUARDCI_SDC_TEMPLATE)
  set_propagated_clock [all_clocks]
}

proc guardci_sta_at {p} {
  guardci_source_sdc $p
  return [list [sta::time_sta_ui [sta::worst_slack_cmd max]] \
               [sta::time_sta_ui [sta::total_negative_slack_cmd max]]]
}

read_lef [guardci_env GUARDCI_TECH_LEF]
foreach lef [guardci_env GUARDCI_SC_LEFS] { read_lef $lef }
foreach lib [guardci_env GUARDCI_LIBS] { read_liberty $lib }
read_def [guardci_env GUARDCI_DEF]
dict set results time_unit "[sta::unit_scale_abbreviation time][sta::unit_suffix time]"

# --- geometry: independent of parasitics --------------------------------
set block [ord::get_db_block]
set dbu [expr {double([$block getDbUnitsPerMicron])}]
set cell_area 0.0
set spacer_area 0.0
set instances 0
set spacers 0
set unplaced 0
set type_counts [dict create]
set type_areas [dict create]
foreach inst [$block getInsts] {
  set master [$inst getMaster]
  set mtype [$master getType]
  set area [expr {[$master getWidth] * [$master getHeight] / ($dbu * $dbu)}]
  dict incr type_counts $mtype
  dict set type_areas $mtype [expr {$area +
    ([dict exists $type_areas $mtype] ? [dict get $type_areas $mtype] : 0.0)}]
  if {$mtype eq "CORE_SPACER"} {
    set spacer_area [expr {$spacer_area + $area}]
    incr spacers
    continue
  }
  set cell_area [expr {$cell_area + $area}]
  incr instances
  if {![$inst isPlaced]} { incr unplaced }
}
set die [$block getDieArea]
dict set results cell_area_um2 $cell_area
dict set results instances $instances
dict set results spacer_area_um2 $spacer_area
dict set results spacer_instances $spacers
dict set results unplaced_instances $unplaced
dict set results die_area_um2 [expr {[$die dx] * [$die dy] / ($dbu * $dbu)}]
set core [$block getCoreArea]
dict set results core_area_um2 [expr {[$core dx] * [$core dy] / ($dbu * $dbu)}]

set cbt_parts {}
foreach mtype [lsort [dict keys $type_counts]] {
  lappend cbt_parts "[guardci_str $mtype]: [guardci_obj [list \
    count [dict get $type_counts $mtype] \
    area_um2 [dict get $type_areas $mtype]]]"
}
dict set results cells_by_type [guardci_raw "\{[join $cbt_parts {, }]\}"]

# Sequential cells via liberty register-clock pins; clock-gate cells via the
# STA instance property, falling back to master-name matching.
set instances_sequential 0
set clock_gate_cells 0
set icg_property_fails 0
set icg_name_match 0
set seq_master [dict create]
foreach inst [$block getInsts] {
  set mname [[$inst getMaster] getName]
  if {![dict exists $seq_master $mname]} {
    set isseq 0
    foreach lp [get_lib_pins -quiet */$mname/*] {
      if {![catch {get_property -quiet $lp is_register_clock} rc]
          && [guardci_bool $rc]} {
        set isseq 1
        break
      }
    }
    dict set seq_master $mname $isseq
  }
  if {[dict get $seq_master $mname]} { incr instances_sequential }
  if {[catch {get_property -object_type instance [$inst getName] \
        is_clock_gate} cg]} {
    incr icg_property_fails
    if {[string match -nocase CLKGATE* $mname]
        || [string match -nocase dlclkp* $mname]
        || [string match -nocase sdlclkp* $mname]} {
      incr clock_gate_cells
      incr icg_name_match
    }
  } elseif {[guardci_bool $cg]} {
    incr clock_gate_cells
  }
}
dict set results instances_sequential $instances_sequential
dict set results clock_gate_cells $clock_gate_cells
if {$icg_property_fails == 0} {
  dict set results clock_gate_cells_method "instance:is_clock_gate"
} elseif {$icg_name_match == $clock_gate_cells} {
  dict set results clock_gate_cells_method "master_name_match"
} else {
  dict set results clock_gate_cells_method "mixed"
}

set pdn_layers [dict create]
set pdn_nets [dict create]
set special_net_names {}
foreach net [$block getNets] {
  if {![$net isSpecial]} { continue }
  lappend special_net_names [guardci_str [$net getName]]
  foreach sw [$net getSWires] {
    foreach shape [$sw getWires] {
      if {[$shape isVia]} {
        set lname "via"
        if {![catch {set lname "via:[[$shape getTechVia] getName]"}]} {
        } else {
          catch {set lname "via:[[$shape getBlockVia] getName]"}
        }
      } elseif {[catch {set lname [[$shape getTechLayer] getName]}]} {
        set lname "unknown"
      }
      dict incr pdn_layers $lname
      dict incr pdn_nets [$net getName]
    }
  }
}
dict set results special_nets [guardci_raw [guardci_list $special_net_names]]
set pdn_parts {}
foreach lname [lsort [dict keys $pdn_layers]] {
  lappend pdn_parts "[guardci_str $lname]: [dict get $pdn_layers $lname]"
}
dict set results pdn_wires_by_layer [guardci_raw "\{[join $pdn_parts {, }]\}"]
set pdn_net_parts {}
foreach nname [lsort [dict keys $pdn_nets]] {
  lappend pdn_net_parts "[guardci_str $nname]: [dict get $pdn_nets $nname]"
}
dict set results pdn_wires_by_net [guardci_raw "\{[join $pdn_net_parts {, }]\}"]

# Placement and antenna checks run last; results are written after every
# stage so a crash in a later stage keeps the earlier results.
dict set results check_placement not_completed
dict set results antenna_violations null
if {[info exists ::env(GUARDCI_NETLIST_OUT)] && $::env(GUARDCI_NETLIST_OUT) ne ""} {
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
  write_verilog -remove_cells $physical_only $::env(GUARDCI_NETLIST_OUT)
  dict set results netlist $::env(GUARDCI_NETLIST_OUT)
}
guardci_write $out_path $results

# --- timing and power: require constraints and extracted parasitics -----
set timing_keys {clocks setup_wns setup_tns hold_wns hold_tns
                 power_internal power_switching power_leakage power_total
                 max_slew_violations max_cap_violations max_fanout_violations
                 setup_wns_over_period unconstrained_endpoints}
if {[catch {
  # required period: GUARDCI_PERIOD when given (the owner's bound),
  # else the search result
  set required_period $::guardci_period
  set golden_period ""
  if {[info exists ::env(GUARDCI_GOLDEN_PERIOD)]
      && $::env(GUARDCI_GOLDEN_PERIOD) ne ""} {
    set golden_period [expr {double($::env(GUARDCI_GOLDEN_PERIOD))}]
  }
  set start_p $golden_period
  if {$required_period ne ""} { set start_p $required_period }
  if {$start_p eq ""} { error "no required or golden period" }
  set resolution [expr {0.001 * ($golden_period ne ""
                                   ? $golden_period : $start_p)}]

  guardci_source_sdc $start_p
  # fanoutViolationCount dereferences check_fanouts_ which is only built
  # lazily by another fanout query; a cold call segfaults this build.
  catch {sta::max_fanout_min_slack_limit}
  define_process_corner -ext_model_index 0 X
  extract_parasitics -ext_model_file [guardci_env GUARDCI_RCX_RULES]
  set spef_path "${out_path}.spef"
  write_spef $spef_path
  read_spef $spef_path

  # --- min_period search (port of phb reference.search_reference_period;
  # §11.3): smallest period where the golden constraints close,
  # wns >= 0 and tns = 0, to a resolution of 0.1 % of the golden period
  set trial_rows {}
  set min_period ""
  set min_err ""
  set prev_wns ""
  set p $start_p
  for {set i 0} {$i < 16} {incr i} {
    lassign [guardci_sta_at $p] w tns
    lappend trial_rows [list $p $w $tns 0]
    if {![string is double -strict $w]} {
      set min_err "non-numeric setup wns"
      break
    }
    if {$w >= 0 && $w <= [expr {max(0.01 * $p, $resolution)}]} break
    if {$prev_wns ne "" \
        && [expr {abs($w - $prev_wns)}] <= [expr {1e-6 * $p}] \
        && [expr {abs($w)}] > $resolution} {
      set min_err "search stagnated"
      break
    }
    set prev_wns $w
    set p [expr {$p - $w}]
  }
  if {$min_err eq ""} {
    set ok {}
    foreach row $trial_rows {
      lassign $row tp tw tns ff
      if {[string is double -strict $tw] && $tw >= 0} { lappend ok $tp }
    }
    if {![llength $ok]} {
      set min_err "no achievable period"
    } else {
      set p [expr {ceil([lindex [lsort -real $ok] 0] / $resolution
                        - 1e-9) * $resolution}]
      for {set i 0} {$i < 5} {incr i} {
        lassign [guardci_sta_at $p] w tns
        lappend trial_rows [list $p $w $tns 1]
        if {[string is double -strict $w] && $w >= 0 \
            && [string is double -strict $tns] && $tns == 0} {
          set min_period $p
          break
        }
        set p [expr {$p + $resolution}]
      }
      if {$min_period eq ""} { set min_err "search did not converge" }
    }
  }
  dict set results min_period \
    [expr {$min_period eq "" ? "null" : $min_period}]
  if {$min_err ne ""} { dict set results min_period_error $min_err }
  set trial_parts {}
  foreach row $trial_rows {
    lassign $row tp tw tns ff
    lappend trial_parts [guardci_obj [list \
      period $tp setup_wns [guardci_num $tw] \
      setup_tns [guardci_num $tns]]]
  }
  dict set results min_period_trials \
    [guardci_raw [guardci_list $trial_parts]]

  # --- period-dependent metrics at the required period (§11.3;
  # without a requirement, at min_period)
  set measure_p $required_period
  if {$measure_p eq ""} { set measure_p $min_period }
  if {$measure_p eq ""} { error "no period to measure at" }
  dict set results period $measure_p
  guardci_source_sdc $measure_p

  set clocks {}
  foreach clk [all_clocks] {
    lappend clocks "[get_property $clk name]=[get_property $clk period]"
  }
  dict set results clocks [join $clocks ","]
  dict set results setup_wns [sta::time_sta_ui [sta::worst_slack_cmd max]]
  dict set results setup_tns [sta::time_sta_ui [sta::total_negative_slack_cmd max]]
  dict set results hold_wns [sta::time_sta_ui [sta::worst_slack_cmd min]]
  dict set results hold_tns [sta::time_sta_ui [sta::total_negative_slack_cmd min]]

  set_power_activity -input -activity 0.1 -duty 0.5
  set power [sta::design_power [sta::cmd_scene]]
  dict set results power_internal [lindex $power 0]
  dict set results power_switching [lindex $power 1]
  dict set results power_leakage [lindex $power 2]
  dict set results power_total [lindex $power 3]

  dict set results max_slew_violations [sta::max_slew_violation_count]
  dict set results max_cap_violations [sta::max_capacitance_violation_count]
  dict set results max_fanout_violations [sta::max_fanout_violation_count]
  if {[catch {expr {[dict get $results setup_wns] / $::guardci_period}} sop]} {
    dict set results setup_wns_over_period null
  } else {
    dict set results setup_wns_over_period $sop
  }
  if {[catch {
    set n_endpoints 0
    foreach err [sta::check_timing_cmd 0 0 0 0 1 0 0] {
      set objs [lrange $err 1 end]
      if {[llength $objs]} {
        incr n_endpoints [llength $objs]
      } elseif {[regexp {[0-9]+} [lindex $err 0] cnt]} {
        incr n_endpoints $cnt
      }
    }
    dict set results unconstrained_endpoints $n_endpoints
    dict set results unconstrained_endpoints_method check_timing_cmd
  } ue_msg]} {
    dict set results unconstrained_endpoints null
    dict set results unconstrained_endpoints_method \
      "unavailable: $ue_msg"
  }
} message]} {
  foreach key $timing_keys {
    if {![dict exists $results $key]} { dict set results $key null }
  }
  dict set results timing_error $message
}
guardci_write $out_path $results

# --- checks ----------------------------------------------------------------
if {[catch {check_placement} message]} {
  dict set results check_placement "fail: $message"
} else {
  dict set results check_placement pass
}
if {[catch {check_antennas} count]} {
  dict set results antenna_violations null
  dict set results antenna_error $count
} else {
  dict set results antenna_violations $count
}
guardci_write $out_path $results

# --- IR drop (optional; original platform net voltages) ---------------------
# GUARDCI_PWR_NETS_VOLTAGES / GUARDCI_GND_NETS_VOLTAGES have the ORFS format
# "NET voltage [NET voltage ...]"; empty means the platform skips this.
# Per-instance node voltages come from analyze_power_grid -voltage_file;
# min is the worst case for a power net, max for a ground net.
dict set results ir_drop_worst_v null
if {[catch {
  set ir {}
  foreach env_name {GUARDCI_PWR_NETS_VOLTAGES GUARDCI_GND_NETS_VOLTAGES} {
    if {![info exists ::env($env_name)] || $::env($env_name) eq ""} {
      continue
    }
    foreach {net voltage} $::env($env_name) {
      if {[catch {
        set_pdnsim_net_voltage -net $net -voltage $voltage
        set vfile "${out_path}.irdrop-${net}.csv"
        analyze_power_grid -net $net -voltage_file $vfile \
          -error_file "${out_path}.irdrop-${net}.rpt"
        set vmin ""
        set vmax ""
        set fh [open $vfile r]
        set data [read $fh]
        close $fh
        foreach line [split $data "\n"] {
          set v [lindex [split $line ","] end]
          if {![string is double -strict $v]} { continue }
          if {$vmin eq "" || $v < $vmin} { set vmin $v }
          if {$vmax eq "" || $v > $vmax} { set vmax $v }
        }
        lappend ir [guardci_obj [list net [guardci_str $net] \
          voltage [guardci_num $voltage] min [guardci_num $vmin] \
          max [guardci_num $vmax]]]
      } ir_one]} {
        lappend ir [guardci_obj [list net [guardci_str $net] \
          error [guardci_str $ir_one]]]
      }
    }
  }
  if {[llength $ir]} {
    dict set results ir_drop_worst_v [guardci_raw [guardci_list $ir]]
  }
} ir_msg]} {
  dict set results ir_drop_error $ir_msg
}
guardci_write $out_path $results
