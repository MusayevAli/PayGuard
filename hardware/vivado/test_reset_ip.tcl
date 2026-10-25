# Standalone behavioral simulation of the ACTUAL generated AMD reset IP.
# Usage: vivado -mode batch -source hardware/vivado/test_reset_ip.tcl \
#          -tclargs /absolute/path/to/generated/reset.xci /new/test/output
# No private image/keys are opened. The XCI is imported (copied), not modified.
if {$argc != 2} { error "Expected generated reset XCI path and new simulation output directory" }
set reset_xci [file normalize [lindex $argv 0]]
set out_dir [file normalize [lindex $argv 1]]
set script_dir [file dirname [file normalize [info script]]]
set bench [file normalize [file join $script_dir .. sim tb_proc_reset.sv]]
if {![file isfile $reset_xci] || [file extension $reset_xci] ne ".xci"} {
    error "Actual generated reset XCI is missing"
}
if {![file isfile $bench]} { error "Missing tb_proc_reset.sv" }
if {[file exists $out_dir]} { error "Use a new output directory to avoid stale simulation results" }
create_project reset_ip_check $out_dir -part xck26-sfvc784-2LV-c
set_property target_language Verilog [current_project]
set_property target_simulator XSim [current_project]
import_ip $reset_xci
set reset_ip [get_ips]
if {[llength $reset_ip] != 1} { error "Expected exactly the one imported reset IP" }
set reset_vlnv [get_property IPDEF $reset_ip]
if {![string match "xilinx.com:ip:proc_sys_reset:*" $reset_vlnv]} {
    error "The imported XCI is not AMD proc_sys_reset"
}
foreach {property expected} {
    CONFIG.C_EXT_RESET_HIGH 0
    CONFIG.C_AUX_RESET_HIGH 0
    CONFIG.C_NUM_BUS_RST 1
    CONFIG.C_NUM_PERP_RST 1
    CONFIG.C_NUM_INTERCONNECT_ARESETN 1
    CONFIG.C_NUM_PERP_ARESETN 1
} {
    set actual [get_property $property $reset_ip]
    if {$actual ne "$expected"} {
        error "Generated reset IP mismatch: $property=$actual, expected $expected"
    }
}
set dut_name [get_property NAME $reset_ip]
if {![regexp {^[A-Za-z_][A-Za-z0-9_]*$} $dut_name]} { error "Unexpected reset module name" }
generate_target simulation $reset_ip
export_ip_user_files -of_objects $reset_ip -no_script -sync -force
add_files -fileset sim_1 -norecurse $bench
set_property top tb_proc_reset [get_filesets sim_1]
set_property verilog_define [list "PAYGUARD_RESET_DUT=$dut_name"] [get_filesets sim_1]
set_property xsim.simulate.runtime all [get_filesets sim_1]
update_compile_order -fileset sim_1
launch_simulation -simset sim_1 -mode behavioral
close_sim
set log_file [file join $out_dir reset_ip_check.sim sim_1 behav xsim simulate.log]
if {![file isfile $log_file]} {
    set log_file [file join $out_dir reset_ip_check.sim sim_1 behav xsim xsim.log]
}
if {![file isfile $log_file]} { error "No simulator result log was produced" }
set fh [open $log_file r]
set simulation_log [read $fh]
close $fh
if {[string first "PAYGUARD_RESET_IP_FAIL" $simulation_log] >= 0 ||
    [string first "PAYGUARD_RESET_IP_PASS checks=11 clock_hz=50000000" $simulation_log] < 0} {
    error "Actual AMD reset-IP simulation did not produce its required pass result; inspect $log_file"
}
set fh [open [file join $out_dir reset-ip-verification.txt] w]
puts $fh "PASS: 11 actual AMD reset-IP checks at 50 MHz"
puts $fh "Imported XCI: $reset_xci"
puts $fh "IP: $reset_vlnv"
puts $fh "External/auxiliary polarity: active low; debug polarity: active high"
puts $fh "Verified startup, permanent auxiliary-low negative control, auxiliary/external/debug/clock-lock assertion and recovery."
close $fh
puts "PAYGUARD_RESET_IP_VERIFIED: $log_file"
