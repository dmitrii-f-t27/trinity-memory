# Generic out-of-context synthesis. No board, device, or clock is assumed.
# vivado -mode batch -source rtl/synth_vivado.tcl \
#   -tclargs <exact-part> <period-ns> <positive-trit-count> <output-directory>
if {$argc != 4} {
    error "Expected: exact-part period-ns positive-trit-count output-directory"
}
set part [lindex $argv 0]
set period [lindex $argv 1]
set trit_count [lindex $argv 2]
set output_dir [file normalize [lindex $argv 3]]
if {![string is double -strict $period] || $period <= 0} {
    error "Clock period must be a positive number of nanoseconds"
}
if {![string is integer -strict $trit_count] || $trit_count <= 0} {
    error "Trit count must be a positive integer"
}
set rtl_dir [file dirname [file normalize [info script]]]
file mkdir $output_dir
set manifest [open [file join $output_dir settings.txt] w]
puts $manifest "Vivado: [version -short]"
puts $manifest "Part: $part"
puts $manifest "Target period ns: $period"
puts $manifest "Logical trits: $trit_count"
puts $manifest "Results are synthesis estimates, not placed/routed or measured hardware."
close $manifest

foreach top {ternary_dense5_stream ternary_baseline5_stream} {
    create_project -in_memory -part $part
    read_verilog [glob [file join $rtl_dir *.v]]
    read_verilog [glob [file join $rtl_dir generated *.v]]
    synth_design -mode out_of_context -top $top -part $part \
        -generic TRIT_COUNT=$trit_count
    create_clock -name clk -period $period [get_ports clk]
    # Identical virtual interface budget for both variants; no pin assignment.
    set interface_delay [expr {$period * 0.25}]
    set_input_delay -clock clk $interface_delay \
        [get_ports -filter {DIRECTION == IN && NAME != clk}]
    set_output_delay -clock clk $interface_delay [get_ports -filter {DIRECTION == OUT}]
    report_utilization -hierarchical -file [file join $output_dir ${top}_utilization.rpt]
    report_timing_summary -report_unconstrained \
        -file [file join $output_dir ${top}_synthesis_timing.rpt]
    write_checkpoint -force [file join $output_dir ${top}_synth.dcp]
    close_project
}
