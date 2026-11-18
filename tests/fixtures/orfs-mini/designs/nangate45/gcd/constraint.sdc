# golden signoff constraints for gcd
set clk_name  clk
set clk_port_name clk
set clk_period 1.0

create_clock [get_ports $clk_port] -name $clk_name -period $clk_period
set_clock_uncertainty 0.05 [get_clocks $clk_name]

set_input_delay  0.2 -clock $clk_name [all_inputs]
set_output_delay 0.2 -clock $clk_name [all_outputs]

set_max_transition 0.5 [current_design]
set_max_capacitance 0.1 [current_design]
