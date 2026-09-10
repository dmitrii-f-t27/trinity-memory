# Out-of-context synthesis of native t27 storage, with no board/pin assumption.
# First build the pinned compiler (T27_ROOT/T27C); this script specializes the
# executable .t27 source to GROUPS=ceil(TRIT_COUNT/5) and reads only its output.
# vivado -mode batch -source rtl/synth_vivado.tcl \
#   -tclargs <exact-part> <period-ns> <positive-trit-count> <output-directory>
if {$argc != 4} { error "Expected: exact-part period-ns positive-trit-count output-directory" }
set part [lindex $argv 0]
set period [lindex $argv 1]
set trit_count [lindex $argv 2]
set output_dir [file normalize [lindex $argv 3]]
if {![string is double -strict $period] || ![expr {$period > 0 && abs(double($period)) < Inf}]} {
    error "Clock period must be a positive finite number of nanoseconds"
}
if {![string is integer -strict $trit_count] || $trit_count <= 0 || $trit_count > 2147483640} {
    error "Trit count must be an integer in 1..2147483640"
}
set groups [expr {($trit_count + 4) / 5}]
set rtl_dir [file dirname [file normalize [info script]]]
set project_root [file dirname $rtl_dir]
set native_dir [file join $output_dir native-storage]
file mkdir $output_dir
# Python is a build-tool adapter here, not a hardware implementation.
exec python3 [file join $project_root tools generate-t27-storage.py] \
    --trits $trit_count --output $native_dir
set sources [list [file join $native_dir stream_storage.v] \
    [file join $native_dir stream_view.v] [file join $native_dir streams.v]]
foreach source $sources { if {![file isfile $source]} { error "Missing generated native RTL: $source" } }
set manifest [open [file join $output_dir settings.txt] w]
puts $manifest "Vivado: [version -short]"
puts $manifest "Part: $part"
puts $manifest "Target period ns: $period"
puts $manifest "Logical trits: $trit_count"
puts $manifest "GROUPS: $groups"
puts $manifest "Native source provenance: [file join $native_dir storage-specialization.json]"
puts $manifest "Native element width: 16 bits; logical code widths: dense5=8, baseline5=10."
puts $manifest "Results are synthesis estimates, not placed/routed or measured hardware."
close $manifest
foreach top {ternary_dense5_stream_t27 ternary_baseline5_stream_t27} {
    create_project -in_memory -part $part
    foreach source $sources { read_verilog $source }
    synth_design -mode out_of_context -top $top -part $part \
        -generic [list TRIT_COUNT=$trit_count WORDS=$groups]
    create_clock -name clk -period $period [get_ports clk]
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
