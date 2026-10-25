`timescale 1ns/1ps
// Compile against the copied, generated AMD proc_sys_reset XCI, not an RTL model.
`ifndef PAYGUARD_RESET_DUT
`define PAYGUARD_RESET_DUT siliconsign_bd_reset_0
`endif

module tb_proc_reset;
    reg clk = 0;
    always #10 clk = ~clk; // Production 50 MHz fabric clock.
    reg ext_n = 0;
    reg aux_n = 1;
    reg debug_reset = 0;
    reg locked = 1;
    wire mb_reset, bus_reset, peripheral_reset;
    wire interconnect_n, peripheral_n;
    wire old_aux_peripheral_n;
    integer checks = 0;

    `PAYGUARD_RESET_DUT dut (
        .slowest_sync_clk(clk), .ext_reset_in(ext_n), .aux_reset_in(aux_n),
        .mb_debug_sys_rst(debug_reset), .dcm_locked(locked),
        .mb_reset(mb_reset), .bus_struct_reset(bus_reset),
        .peripheral_reset(peripheral_reset),
        .interconnect_aresetn(interconnect_n), .peripheral_aresetn(peripheral_n)
    );

    // Reproduce the r2 integration bug using the same actual AMD IP/configuration.
    `PAYGUARD_RESET_DUT old_aux_tied_low (
        .slowest_sync_clk(clk), .ext_reset_in(ext_n), .aux_reset_in(1'b0),
        .mb_debug_sys_rst(debug_reset), .dcm_locked(locked),
        .mb_reset(), .bus_struct_reset(), .peripheral_reset(),
        .interconnect_aresetn(), .peripheral_aresetn(old_aux_peripheral_n)
    );

    task automatic expect_reset_state(input bit released, input string phase);
        integer cycle;
        bit reached;
        begin
            reached = 0;
            for (cycle = 0; cycle < 256 && !reached; cycle = cycle + 1) begin
                @(negedge clk);
                if (peripheral_n === released && interconnect_n === released &&
                    mb_reset === !released && bus_reset === !released &&
                    peripheral_reset === !released) begin
                    reached = 1;
                end
            end
            if (!reached)
                $fatal(1, "PAYGUARD_RESET_IP_FAIL %s: periph_n=%b interconnect_n=%b mb=%b bus=%b periph=%b",
                       phase, peripheral_n, interconnect_n, mb_reset, bus_reset, peripheral_reset);
            checks = checks + 1;
            $display("RESET_CHECK %0d %s cycles=%0d", checks, phase, cycle);
        end
    endtask

    initial begin
        // Wait beyond initial FPGA/global reset and the IP's power-on sequence.
        repeat (100) @(negedge clk);
        expect_reset_state(0, "external reset holds startup");
        ext_n = 1;
        expect_reset_state(1, "external released and auxiliary high releases outputs");
        repeat (256) begin
            @(negedge clk);
            if (old_aux_peripheral_n !== 1'b0)
                $fatal(1, "PAYGUARD_RESET_IP_FAIL historical auxiliary-low control unexpectedly released");
            if (peripheral_n !== 1'b1)
                $fatal(1, "PAYGUARD_RESET_IP_FAIL corrected inactive auxiliary input did not remain released");
        end
        checks = checks + 1;
        $display("RESET_CHECK %0d historical auxiliary-low fault reproduced for 256 clocks", checks);

        aux_n = 0;
        expect_reset_state(0, "auxiliary low asserts all resets");
        aux_n = 1;
        expect_reset_state(1, "auxiliary high recovers");

        ext_n = 0;
        expect_reset_state(0, "external low asserts all resets");
        ext_n = 1;
        expect_reset_state(1, "external high recovers");

        debug_reset = 1;
        expect_reset_state(0, "debug high asserts all resets");
        debug_reset = 0;
        expect_reset_state(1, "debug low recovers");

        locked = 0;
        expect_reset_state(0, "clock unlock asserts all resets");
        locked = 1;
        expect_reset_state(1, "clock lock recovers");
        $display("PAYGUARD_RESET_IP_PASS checks=%0d clock_hz=50000000", checks);
        $finish;
    end

    initial begin
        #1000000;
        $fatal(1, "PAYGUARD_RESET_IP_FAIL watchdog");
    end
endmodule
