# Produce the Linux FPGA-manager bitstream plus a polling-only UIO overlay.
# Called after implementation by build_kv260.tcl. Does not deploy to a board.
proc siliconsign_package_overlay {out_dir} {
    set bit_file [file join $out_dir siliconsign.bit]
    if {![file isfile $bit_file]} { error "Missing implemented bitstream" }
    set bif [file join $out_dir siliconsign.bif]
    set fh [open $bif w]
    puts $fh "all:\n\{\n  \[destination_device=pl\] $bit_file\n\}"
    close $fh
    # Bootgen, shipped with the same AMD installation, handles ZynqMP word order.
    if {[catch {exec bootgen -image $bif -arch zynqmp -process_bitstream bin -w} result]} {
        error "Bootgen did not package the FPGA-manager binary: $result"
    }
    if {![file isfile "${bit_file}.bin"]} { error "Bootgen output siliconsign.bit.bin missing" }
    siliconsign_write_overlay $out_dir
}

# This can be called alone to repair Linux PS-PL configuration without changing
# or re-packaging the already implemented private bitstream.
proc siliconsign_write_overlay {out_dir} {
    file mkdir $out_dir
    set fh [open [file join $out_dir siliconsign.dts] w]
    puts $fh {/dts-v1/;
/plugin/;
/ {
    fragment@0 {
        target = <&fpga_full>;
        __overlay__ {
            firmware-name = "siliconsign.bit.bin";
        };
    };
    fragment@1 {
        target = <&amba>;
        __overlay__ {
            #address-cells = <2>;
            #size-cells = <2>;
            siliconsign_clocking0 {
                compatible = "xlnx,fclk";
                status = "okay";
                #clock-cells = <0>;
                clocks = <&zynqmp_clk 71>;
                clock-output-names = "siliconsign_fabric_clk";
                assigned-clocks = <&zynqmp_clk 71>;
                assigned-clock-rates = <50000000>;
            };
            siliconsign_afi {
                compatible = "xlnx,afi-fpga";
                config-afi = <14 0>;
                resets = <&zynqmp_reset 116>, <&zynqmp_reset 117>,
                         <&zynqmp_reset 118>, <&zynqmp_reset 119>;
                status = "okay";
            };
            siliconsign@a0000000 {
                compatible = "generic-uio";
                reg = <0x0 0xa0000000 0x0 0x1000>;
                clock-names = "s_axi_aclk";
                clocks = <&zynqmp_clk 71>;
                assigned-clocks = <&zynqmp_clk 71>;
                assigned-clock-rates = <50000000>;
                status = "okay";
            };
        };
    };
};}
    close $fh
    # Clock71 is PL0_REF, per AMD's own DT generator:
    # https://github.com/Xilinx/system-device-tree-xlnx/blob/xlnx_rel_v2025.2/device_tree/data/device_tree.tcl
    # (set clk_num [expr {71 + $index}], xlnx,fclk and assigned-clock-rates).
    # AFI index14 combines the two FPD PS-master widths. AMD's get_max_afi_val
    # helper in common_proc.tcl encodes 32/64/128 as 0/1/2, shifted by 8 for
    # MAXIGP0 and by 10 for MAXIGP1. Zero explicitly selects our active GP0's
    # 32-bit interface and 32 bits for GP1, which this design disables. GP2 and
    # all PS-slave ports are unused and their AFI registers are left untouched.
    # https://github.com/Xilinx/system-device-tree-xlnx/blob/xlnx_rel_v2025.2/device_tree/data/common_proc.tcl
    # The AFI driver programs the widths, then pulses its resets through the
    # firmware reset API. Use the exact AMD generator's full reset list: firmware
    # IDs116..119 map to EMIO92..95 respectively, and pl_resetn0 uses EMIO95.
    # The Linux symbol PS_PL0 for116 does not establish the Vivado signal mapping.
    # https://github.com/Xilinx/embeddedsw/blob/xlnx_rel_v2024.1/lib/sw_apps/zynqmp_pmufw/src/pm_defs.h
    # https://adaptivesupport.amd.com/s/article/68962
    # A reset on fpga_full alone is not consumed by the AMD 6.6 region driver.
    # https://github.com/Xilinx/linux-xlnx/blob/xlnx_rebase_v6.6_LTS/drivers/fpga/xilinx-afi.c
    # Base DT must export fpga_full, amba, zynqmp_clk and zynqmp_reset symbols.
    # Before any MMIO, require clock/AFI drivers bound and the clock enabled.
    # Generic UIO may require of_id=generic-uio.
    if {[auto_execok dtc] ne ""} {
        exec dtc -@ -I dts -O dtb -o [file join $out_dir siliconsign.dtbo] [file join $out_dir siliconsign.dts]
    } else {
        puts "dtc unavailable: siliconsign.dts generated; compile on the Kria with dtc -@."
    }
}
