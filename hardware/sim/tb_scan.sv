`timescale 1ns/1ps
// Sequential hash-table integration: capture, busy input isolation, exactly one
// response/mutation, last-slot collision, and reset while a scan is in progress.
module tb_scan;
    reg clk=0; always #10 clk=~clk;
    reg rst_n=0, cmd_valid=0, auth_ok=0;
    reg [2047:0] cmd=0;
    wire cmd_ready, resp_valid;
    wire [2047:0] resp;
    reg [127:0] session_id=128'h100;
    reg [63:0] initial_epoch=1700000000;
    reg [255:0] prior_hash;
    reg [31:0] admin_seq=0, controller_seq=0, payment_seq=0;
    integer checks=0, waited;
    policy_engine dut(.*);

    task automatic make(input integer op, input integer slot);
        begin
            cmd=0; cmd[0 +:32]=32'h53494c43; cmd[32 +:32]=1;
            cmd[64 +:32]=op; cmd[128 +:128]=session_id;
            if(op==1 || op==2) begin
                admin_seq=admin_seq+1; cmd[96 +:32]=admin_seq;
            end else if(op==4) begin
                controller_seq=controller_seq+1; cmd[96 +:32]=controller_seq;
            end else if(op==6) begin
                payment_seq=payment_seq+1; cmd[96 +:32]=payment_seq;
            end
            cmd[256 +:32]=63; cmd[288 +:32]=slot; cmd[320 +:32]=978;
            cmd[384 +:64]=100; cmd[448 +:32]=1; cmd[512 +:256]=256'haaaa;
            cmd[768 +:256]=256'h1000+slot; cmd[1472 +:256]=256'heeee;
            auth_ok=(op==1 || op==2 || op==4);
        end
    endtask
    task automatic start_request;
        begin
            @(negedge clk);
            if(!cmd_ready) $fatal(1,"expected idle policy");
            cmd_valid=1;
            @(posedge clk); #1;
            if(cmd_ready || resp_valid) $fatal(1,"accepted request must become busy before response");
            @(negedge clk); cmd_valid=0;
        end
    endtask
    task automatic finish_request(input integer expected);
        begin
            waited=0;
            while(!resp_valid && waited<1024) begin @(posedge clk); #1; waited=waited+1; end
            if(!resp_valid || resp[1792 +:32] !== expected)
                $fatal(1,"scan response expected=%0d actual=%0d",expected,resp[1792 +:32]);
            checks=checks+1;
            @(posedge clk); #1;
            if(resp_valid || !cmd_ready) $fatal(1,"response must pulse and return idle");
        end
    endtask
    task automatic send(input integer expected);
        begin start_request(); finish_request(expected); end
    endtask
    task automatic setup_session;
        begin
            make(1,0); cmd[1024 +:64]=initial_epoch; cmd[1152 +:64]=1000;
            cmd[1216 +:64]=1000; cmd[1280 +:32]=60; cmd[1312 +:32]=10;
            send(0);
            make(2,0); cmd[1088 +:64]=1000; send(0);
        end
    endtask
    task automatic new_session(input [63:0] epoch);
        begin
            @(negedge clk); rst_n=0; cmd_valid=0; cmd=0; auth_ok=0;
            repeat(3) @(negedge clk); rst_n=1;
            session_id=session_id+1; admin_seq=0; controller_seq=0; payment_seq=0;
            initial_epoch=epoch;
            setup_session();
        end
    endtask
    task automatic wait_for_check;
        begin
            waited=0;
            while(dut.request_state != dut.REQUEST_CHECK && waited<1024) begin
                @(negedge clk); waited=waited+1;
            end
            if(dut.request_state != dut.REQUEST_CHECK || resp_valid)
                $fatal(1,"expected final validation before any mutation");
        end
    endtask
    task automatic disturbed_commit(input integer expected);
        reg [2047:0] accepted;
        begin
            accepted=cmd;
            start_request();
            cmd=~accepted; auth_ok=0;
            repeat(20) begin
                @(posedge clk); #1;
                if(cmd_ready || resp_valid) $fatal(1,"256-entry scan ended prematurely");
            end
            // A second valid pulse while ready is low must not replace or queue
            // another request, nor change the captured authentication result.
            @(negedge clk); cmd_valid=1;
            @(posedge clk); #1;
            if(cmd_ready || resp_valid) $fatal(1,"busy request was accepted");
            @(negedge clk); cmd_valid=0;
            finish_request(expected);
            if(resp[32 +:1760] !== accepted[32 +:1760])
                $fatal(1,"response must bind original captured command");
            repeat(270) begin
                @(posedge clk); #1;
                if(resp_valid || !cmd_ready) $fatal(1,"busy valid pulse queued an extra response");
            end
            checks=checks+1;
        end
    endtask

    initial begin
        repeat(3) @(negedge clk); rst_n=1;
        setup_session();
        make(4,255); send(0);
        make(4,0); disturbed_commit(0);
        make(4,1); cmd[768 +:256]=256'h10ff; disturbed_commit(26);
        make(6,0); send(0);
        if(resp[1824 +:32] !== 1 || resp[1920 +:64] !== 100)
            $fatal(1,"captured request must mutate only once");
        make(6,0); send(17);

        make(4,1); start_request();
        repeat(20) @(negedge clk);
        if(cmd_ready || resp_valid || dut.invoice_used[1])
            $fatal(1,"mutation occurred before completed scan");
        rst_n=0; cmd=0; auth_ok=0; cmd_valid=0;
        repeat(3) @(negedge clk); rst_n=1;
        repeat(270) begin
            @(posedge clk); #1;
            if(resp_valid || !cmd_ready || dut.invoice_used[1])
                $fatal(1,"reset did not abort the in-flight scan");
        end
        if(dut.invoice_hash[255] !== 256'h10ff)
            $fatal(1,"reset test must retain stale hash RAM");
        checks=checks+1;
        session_id=128'h200; admin_seq=0; controller_seq=0; payment_seq=0;
        setup_session();
        make(4,0); cmd[768 +:256]=256'h10ff; send(0);
        make(4,1); cmd[768 +:256]=256'h10ff; send(26);
        make(6,0); cmd[768 +:256]=256'h10ff; send(0);
        if(resp[1824 +:32] !== 1 || resp[1920 +:64] !== 100)
            $fatal(1,"reset or stale RAM corrupted fresh session counters");

        // Saturating the real clock while a scan is pending must finish denied.
        // Deposit only the divider boundary; the timer itself still performs
        // its normal overflow transition, as in the dedicated clock bench.
        new_session(64'hffffffffffffffff);
        prior_hash=dut.invoice_hash[0];
        make(4,0); start_request(); repeat(8) @(negedge clk);
        dut.tick_divider=49999999;
        finish_request(3);
        if(dut.invoice_used[0] || dut.invoice_hash[0] !== prior_hash ||
           resp[2016 +:32] !== 0 || resp[1824 +:32] !== 0)
            $fatal(1,"clock overflow during scan mutated an obligation");
        checks=checks+1;

        // Overflow on the exact mutation edge must suppress both the RAM write
        // and validity publication, then return one LOCKED response.
        new_session(64'hffffffffffffffff);
        prior_hash=dut.invoice_hash[0];
        make(4,0); start_request(); wait_for_check();
        dut.tick_divider=49999999;
        @(posedge clk); #1;
        if(resp_valid || cmd_ready || dut.invoice_used[0] || dut.invoice_hash[0] !== prior_hash)
            $fatal(1,"overflow edge did not defer validation without mutation");
        finish_request(3);
        checks=checks+1;

        // Reset on that same final edge must abort completely, without a late
        // response or a half-published hash/validity pair.
        new_session(1700000000);
        prior_hash=dut.invoice_hash[0];
        make(4,0); start_request(); wait_for_check(); rst_n=0;
        repeat(3) @(negedge clk); rst_n=1;
        repeat(10) begin
            @(posedge clk); #1;
            if(resp_valid || !cmd_ready || dut.invoice_used[0] || dut.invoice_hash[0] !== prior_hash)
                $fatal(1,"reset on final check edge did not abort atomically");
        end
        checks=checks+1;
        $display("PASS sequential hash scan: %0d capture, busy, reset, duplicate and mutation checks",checks);
        $finish;
    end
    initial begin #1000000; $fatal(1,"scan test timeout"); end
endmodule
