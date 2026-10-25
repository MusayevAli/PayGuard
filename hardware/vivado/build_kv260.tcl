# Run on the EXISTING starline-builder (do not create another VM):
# source /home/ali_official2808/AMD/2026.1/Vivado/settings64.sh
# vivado -mode batch -source vivado/build_kv260.tcl \
#   -tclargs /absolute/private/keys.json /absolute/new/build-directory
# The keys JSON contains admin/controller/response, each 64 hexadecimal digits.
# Existing output directories with an .xpr are refused: no Starline project overwritten.

set script_dir [file dirname [file normalize [info script]]]
set source_root [file dirname $script_dir]
if {[llength $argv] != 2} { error "Expected keys.json and NEW output directory" }
set keys_file [file normalize [lindex $argv 0]]
set out_dir [file normalize [lindex $argv 1]]
if {![file isfile $keys_file]} { error "Key file does not exist" }
if {[llength [glob -nocomplain -directory $out_dir *.xpr]] != 0} {
    error "Refusing to overwrite an existing Vivado project. Choose a new output directory."
}
set fh [open $keys_file r]; set key_json [read $fh]; close $fh
array set keys {}
foreach role {admin controller response} {
    set expression [format {"%s"\s*:\s*"([0-9a-fA-F]{64})"} $role]
    if {![regexp $expression $key_json unused key]} { error "Invalid or missing $role key" }
    set key [string tolower $key]
    if {$key eq [string repeat 0 64]} { error "Zero keys are forbidden" }
    set keys($role) $key
}
if {$keys(admin) eq $keys(controller) || $keys(admin) eq $keys(response) ||
    $keys(controller) eq $keys(response)} { error "Role keys must differ" }
file mkdir $out_dir
file attributes $out_dir -permissions 0700
set generated [file join $out_dir .generated]
file mkdir $generated
set key_header [file join $generated siliconsign_keys.vh]
set fh [open $key_header w]
foreach role {admin controller response} {
    puts $fh "`define SILICONSIGN_[string toupper $role]_KEY 256'h$keys($role)"
}
close $fh
file attributes $key_header -permissions 0600
unset key_json key

set part xck26-sfvc784-2LV-c
# Reuse the existing VM's board definitions without copying or modifying them.
if {[info exists ::env(SILICONSIGN_BOARD_REPO)]} {
    set board_repo [file normalize $::env(SILICONSIGN_BOARD_REPO)]
    if {![file isdirectory $board_repo]} { error "SILICONSIGN_BOARD_REPO is not a directory" }
    set_param board.repoPaths [concat [get_param board.repoPaths] [list $board_repo]]
}
create_project siliconsign $out_dir -part $part
set_property target_language Verilog [current_project]
set_property simulator_language Mixed [current_project]
set boards [get_board_parts -quiet *kv260_som*]
if {[llength $boards] == 0} { set boards [get_board_parts -quiet *kv260*] }
if {[llength $boards] == 0} {
    error "KV260 board preset is missing. Install the existing AMD KV260 board files; do not substitute a KR260 preset."
}
set compatible_boards {}
foreach candidate $boards {
    if {[string equal -nocase [get_property PART_NAME $candidate] $part]} {
        lappend compatible_boards $candidate
    }
}
if {[llength $compatible_boards] == 0} { error "No discovered KV260 board preset matches $part" }
set_property board_part [lindex [lsort -dictionary $compatible_boards] end] [current_project]
set rtl_files [list sha256_core.sv hmac_sha256.sv policy_engine.sv siliconsign_core.sv siliconsign_axi.sv]
foreach source $rtl_files { add_files -norecurse [file join $source_root rtl $source] }
add_files -norecurse $key_header
set_property file_type {Verilog Header} [get_files $key_header]
set_property include_dirs [list $generated] [get_filesets sources_1]
set_property verilog_define {SILICONSIGN_GENERATED_KEYS} [get_filesets sources_1]
update_compile_order -fileset sources_1

proc newest_ip {name} {
    set matches [get_ipdefs -all -quiet "xilinx.com:ip:${name}:*"]
    if {[llength $matches] == 0} { error "Required AMD IP $name is unavailable" }
    return [lindex [lsort -dictionary $matches] end]
}
create_bd_design siliconsign_bd
set ps [create_bd_cell -type ip -vlnv [newest_ip zynq_ultra_ps_e] ps]
apply_bd_automation -rule xilinx.com:bd_rule:zynq_ultra_ps_e \
    -config {apply_board_preset "1"} $ps
set_property -dict [list CONFIG.PSU__USE__M_AXI_GP0 {1} \
    CONFIG.PSU__USE__M_AXI_GP1 {0} CONFIG.PSU__USE__M_AXI_GP2 {0} \
    CONFIG.PSU__MAXIGP0__DATA_WIDTH {32} CONFIG.PSU__FPGA_PL0_ENABLE {1} \
    CONFIG.PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ {50}] $ps
set gate [create_bd_cell -type module -reference siliconsign_axi payment_gate]
# Module-reference OOC runs must not depend solely on a sources_1 preprocessor
# define: missing defines would silently select the all-zero, disabled defaults.
# Set the three role generics explicitly in this private project, and compare
# their readback numerically without ever printing secret values.
set_property -dict [list \
    CONFIG.ADMIN_KEY "256'h$keys(admin)" \
    CONFIG.CONTROLLER_KEY "256'h$keys(controller)" \
    CONFIG.RESPONSE_KEY "256'h$keys(response)"] $gate
proc key_parameter_value {value} {
    set value [string tolower [string map {_ ""} [string trim $value]]]
    set radix 10
    set digits $value
    if {[regexp {^([0-9]+)?'s?([hbd])([0-9a-f]+)$} $value unused width format digits]} {
        if {$width ne "" && $width != 256} { error "Role-key parameter width is not 256 bits" }
        set radix [dict get [dict create h 16 b 2 d 10] $format]
    } elseif {[regexp {^0x([0-9a-f]+)$} $value unused digits]} {
        set radix 16
    } elseif {![regexp {^[0-9]+$} $digits]} {
        error "Role-key parameter did not resolve to a numeric value"
    }
    set number 0
    foreach character [split $digits ""] {
        set digit [string first $character 0123456789abcdef]
        if {$digit < 0 || $digit >= $radix} { error "Invalid role-key parameter encoding" }
        set number [expr {$number * $radix + $digit}]
    }
    return $number
}
foreach role {admin controller response} {
    set parameter [get_property "CONFIG.[string toupper $role]_KEY" $gate]
    if {[key_parameter_value $parameter] != [expr "0x$keys($role)"]} {
        error "Role-key parameter propagation failed for $role; refusing a disabled payment gate"
    }
}
unset parameter
array unset keys
# Set production capacity explicitly as well as in the RTL defaults. This new
# project is separate from the previous 16/64 image and all Starline projects.
set_property -dict [list CONFIG.NV {64} CONFIG.NI {256} CONFIG.CLK_HZ {50000000}] $gate
if {[get_property CONFIG.NV $gate] != 64 || [get_property CONFIG.NI $gate] != 256 ||
    [get_property CONFIG.CLK_HZ $gate] != 50000000} {
    error "Payment-gate production capacity or real-time clock divider does not match the required build"
}
set sc [create_bd_cell -type ip -vlnv [newest_ip smartconnect] interconnect]
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {1}] $sc
set reset [create_bd_cell -type ip -vlnv [newest_ip proc_sys_reset] reset]
# Vivado 2026.1 derives C_EXT_RESET_HIGH from the connected reset signal's
# ACTIVE_LOW metadata; setting this derived/read-only property is rejected.
set one [create_bd_cell -type ip -vlnv [newest_ip xlconstant] one]
set_property CONFIG.CONST_VAL {1} $one
set zero [create_bd_cell -type ip -vlnv [newest_ip xlconstant] zero]
set_property CONFIG.CONST_VAL {0} $zero
connect_bd_intf_net [get_bd_intf_pins ps/M_AXI_HPM0_FPD] [get_bd_intf_pins interconnect/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins interconnect/M00_AXI] [get_bd_intf_pins payment_gate/S_AXI]
connect_bd_net [get_bd_pins ps/pl_clk0] [get_bd_pins ps/maxihpm0_fpd_aclk] \
    [get_bd_pins interconnect/aclk] [get_bd_pins payment_gate/s_axi_aclk] \
    [get_bd_pins reset/slowest_sync_clk]
connect_bd_net [get_bd_pins ps/pl_resetn0] [get_bd_pins reset/ext_reset_in]
# Vivado 2026.1 resolves this auxiliary input as ACTIVE_LOW. Keeping it at
# zero holds both SmartConnect and the payment gate permanently in reset.
# Its inactive level is one; mb_debug_sys_rst is always active-high.
connect_bd_net [get_bd_pins one/dout] [get_bd_pins reset/dcm_locked] [get_bd_pins reset/aux_reset_in]
connect_bd_net [get_bd_pins zero/dout] [get_bd_pins reset/mb_debug_sys_rst]
connect_bd_net [get_bd_pins reset/peripheral_aresetn] [get_bd_pins interconnect/aresetn] \
    [get_bd_pins payment_gate/s_axi_aresetn]
assign_bd_address
set address_segments [get_bd_addr_segs -of_objects [get_bd_addr_spaces ps/Data] -filter {NAME =~ "*payment_gate*"}]
if {[llength $address_segments] != 1} { error "Expected exactly one payment-gate address segment" }
set_property offset 0xA0000000 $address_segments
set_property range 4K $address_segments
validate_bd_design
source [file join $script_dir reset_contract.tcl]
siliconsign_assert_reset_wiring $reset $one $zero
save_bd_design
set bd_file [get_files [file join $out_dir siliconsign.srcs sources_1 bd siliconsign_bd siliconsign_bd.bd]]
generate_target all $bd_file
set wrapper [make_wrapper -files $bd_file -top]
add_files -norecurse $wrapper
set_property top siliconsign_bd_wrapper [get_filesets sources_1]
update_compile_order -fileset sources_1

set jobs 4
if {[info exists ::env(SILICONSIGN_BUILD_JOBS)]} {
    set jobs $::env(SILICONSIGN_BUILD_JOBS)
    if {![string is integer -strict $jobs] || $jobs < 1 || $jobs > 16} { error "Invalid build jobs" }
}
launch_runs synth_1 -jobs $jobs
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} { error "Synthesis failed" }
open_run synth_1
report_utilization -hierarchical -file [file join $out_dir synthesis_hierarchy.rpt]
# The bounded duplicate scan uses synchronous block RAM instead of a parallel
# 65,536-flip-flop CAM. Require retained hash storage, not merely a surviving HMAC
# datapath; explicit generics/readback above also prevent a disabled-key image.
set hash_rams [get_cells -hierarchical -quiet -filter {NAME =~ *invoice_hash* && (REF_NAME =~ RAMB18* || REF_NAME =~ RAMB36*)}]
set hash_storage_bits 0
foreach cell $hash_rams {
    if {[string match RAMB36* [get_property REF_NAME $cell]]} {
        incr hash_storage_bits 36864
    } else {
        incr hash_storage_bits 18432
    }
}
if {$hash_storage_bits < 65536} {
    error "Synthesis did not retain sufficient block RAM for the 256 x 256-bit invoice identity table; refusing this image"
}
puts "Verified production capacity and retained block-RAM invoice-identity storage after synthesis."
launch_runs impl_1 -to_step write_bitstream -jobs $jobs
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} { error "Implementation failed" }
open_run impl_1
set timing_text [report_timing_summary -delay_type min_max -check_timing_verbose -return_string]
set report_file [open [file join $out_dir timing_summary.rpt] w]
puts $report_file $timing_text
close $report_file
report_pulse_width -file [file join $out_dir pulse_width.rpt]
report_utilization -file [file join $out_dir utilization.rpt]
report_drc -file [file join $out_dir drc.rpt]
foreach delay_type {max min} {
    set paths [get_timing_paths -quiet -delay_type $delay_type -max_paths 1 -nworst 1]
    if {[llength $paths] == 0 || [get_property SLACK [lindex $paths 0]] < 0} {
        error "Timing not verified / negative $delay_type (setup/hold) slack. Do not deploy this build."
    }
}
# Vivado's all-constraints-met verdict includes setup, hold AND pulse-width
# checks in the design timing summary. Missing verdict also fails closed.
if {![string match {*All user specified timing constraints are met.*} $timing_text]} {
    error "Timing summary does not confirm all setup/hold/pulse-width constraints are met. Do not deploy."
}
write_bitstream -force [file join $out_dir siliconsign.bit]
write_hw_platform -fixed -include_bit -force -file [file join $out_dir siliconsign.xsa]
source [file join $script_dir package_overlay.tcl]
siliconsign_package_overlay $out_dir
set capacity_file [open [file join $out_dir capabilities.json] w]
puts $capacity_file {{"vendor_slots":64,"obligation_slots":256,"register":"0x00c","register_value":"0x01000040","clock_register":"0x010","clock_divider_hz":50000000,"part":"xck26-sfvc784-2LV-c"}}
close $capacity_file
puts "SILICONSIGN_BUILD_COMPLETE: $out_dir"
puts "Target clock 50 MHz with 50,000,000-cycle seconds; verify live Kria PS clock before loading. No board was flashed."
