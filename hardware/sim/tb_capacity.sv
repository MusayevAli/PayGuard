`timescale 1ns/1ps
// Exercise the production table boundaries through real command acceptance.
// Slots are immutable, including paid slots: exhausting this session must fail
// closed instead of aliasing an old authorization or reusing its identity.
module tb_capacity;
    reg clk=0; always #10 clk=~clk;
    reg rst_n=0, cmd_valid=0, auth_ok=0;
    reg [2047:0] cmd=0;
    wire cmd_ready, resp_valid;
    wire [2047:0] resp;
    integer checks=0, k;
    reg [31:0] admin_seq=0, controller_seq=0, payment_seq=0;
    localparam [127:0] SESSION_ID=128'h0123456789abcdeffedcba9876543210;
    policy_engine dut(.*);

    task automatic make(input integer op, input integer vendor, input integer invoice);
        begin
            cmd=0;
            cmd[0 +:32]=32'h53494c43; cmd[32 +:32]=1;
            cmd[64 +:32]=op; cmd[128 +:128]=SESSION_ID;
            if(op==1 || op==2) begin
                admin_seq=admin_seq+1; cmd[96 +:32]=admin_seq;
            end else if(op==4 || op==5) begin
                controller_seq=controller_seq+1; cmd[96 +:32]=controller_seq;
            end else if(op==6) begin
                payment_seq=payment_seq+1; cmd[96 +:32]=payment_seq;
            end
            cmd[256 +:32]=vendor; cmd[288 +:32]=invoice;
            cmd[320 +:32]=978; cmd[384 +:64]=100; cmd[448 +:32]=1;
            cmd[512 +:256]=256'ha000+vendor;
            cmd[768 +:256]=256'h1000+invoice;
            cmd[1472 +:256]=256'heeeeee;
            auth_ok=(op==1 || op==2 || op==4 || op==5);
        end
    endtask

    task automatic send(input integer expected, input [383:0] label);
        integer waited;
        begin
            @(negedge clk);
            if(!cmd_ready) $fatal(1,"request accepted only while ready");
            cmd_valid=1;
            @(posedge clk); #1;
            @(negedge clk); cmd_valid=0;
            waited=0;
            while(!resp_valid && waited<1024) begin
                @(posedge clk); #1; waited=waited+1;
            end
            if(!resp_valid || resp[1792 +:32] !== expected)
                $fatal(1,"%0s: expected=%0d actual=%0d",label,expected,resp[1792 +:32]);
            checks=checks+1;
            @(negedge clk); cmd_valid=0;
            @(posedge clk); #1;
        end
    endtask

    initial begin
        if(dut.NV != 64 || dut.NI != 256 || dut.CLK_HZ != 50000000)
            $fatal(1,"production capacity or real-time clock default changed");
        repeat(3) @(negedge clk); rst_n=1;
        // Fresh payload RAM is unknown in simulation. Validity guards must
        // still produce exact, deterministic rejections and zero counters.
        make(9,63,255); send(0,"status before payload initialization");
        if(resp[1824 +:192] !== 0) $fatal(1,"uninitialized payload leaked into status");
        make(6,63,255); send(3,"uninitialized device rejects payment");
        make(1,0,0);
        cmd[1024 +:64]=1700000000; cmd[1152 +:64]=100000;
        cmd[1216 +:64]=100000; cmd[1280 +:32]=60; cmd[1312 +:32]=1000;
        send(0,"initialize production capacity");
        make(6,63,255); send(8,"uninitialized vendor payload is inaccessible");

        for(k=0;k<64;k=k+1) begin
            make(2,k,0); cmd[1088 +:64]=100000; send(0,"all 64 vendor slots usable");
        end
        make(2,64,0); cmd[1088 +:64]=100000; send(6,"vendor 64 rejected");
        make(6,63,255); send(10,"uninitialized obligation payload is inaccessible");

        // First occupy the last entry. A duplicate must be found there even
        // when submitted to a different free slot and vendor.
        make(4,63,255); send(0,"vendor 63 and invoice 255 accepted");
        make(4,0,0); cmd[768 +:256]=256'h10ff;
        send(26,"duplicate scan includes invoice 255");
        for(k=0;k<255;k=k+1) begin
            make(4,k%64,k); send(0,"all 256 obligation slots usable");
        end
        make(4,0,256); send(6,"obligation 256 rejected after full table");
        make(4,64,0); send(6,"commit cannot alias vendor 64");

        make(6,63,255); cmd[512 +:256]=256'ha000;
        send(9,"high-slot account substitution rejected");
        make(6,63,255); send(0,"high-slot payment accepted");
        make(6,63,255); send(17,"high-slot payment cannot replay");
        make(4,63,255); cmd[768 +:256]=256'h2000;
        send(7,"paid slot cannot be recycled");
        make(6,0,0); send(0,"low slot is independent");
        make(6,63,127); send(0,"middle slot is independent");
        make(5,61,253); cmd[480 +:32]=120; send(0,"leave pending approval before reset");
        make(6,62,254); send(0,"adjacent high slot is independent");
        make(6,0,256); send(6,"pay cannot alias invoice 256");
        make(6,64,0); send(6,"pay cannot alias vendor 64");
        make(9,63,0); send(0,"read counters after boundary rejections");
        if(resp[1920 +:64] !== 400 || resp[1824 +:32] !== 4 || resp[1984 +:32] !== 2)
            $fatal(1,"boundary rejections altered spend or authorization counters");

        // Reset invalidates all slots without clearing their payload RAM.
        // A new session must not inherit an account, invoice or old approval.
        @(negedge clk); rst_n=0;
        repeat(3) @(negedge clk); rst_n=1;
        admin_seq=0; controller_seq=0; payment_seq=0;
        if(dut.invoice_account[255] !== 256'ha03f || dut.invoice_amount[255] !== 100 ||
           dut.invoice_approval_deadline[253] !== 1700000120 || dut.invoice_approved[253] !== 0)
            $fatal(1,"test must retain stale payload RAM across reset");
        make(9,63,255); send(0,"reset hides retained payload");
        if(resp[1824 +:192] !== 0) $fatal(1,"stale payload leaked into reset status");
        make(1,0,0);
        cmd[1024 +:64]=1700000000; cmd[1152 +:64]=100000;
        cmd[1216 +:64]=100000; cmd[1280 +:32]=60; cmd[1312 +:32]=1000;
        send(0,"initialize after reset");
        make(6,63,255); send(8,"old vendor validity cleared");
        make(2,63,0); cmd[1088 +:64]=100000; cmd[512 +:256]=256'hb03f;
        send(0,"reprovision new account after reset");
        make(6,63,255); send(10,"old obligation validity cleared");
        make(4,63,253); send(9,"old account payload is not authorization");
        make(4,63,253); cmd[512 +:256]=256'hb03f; cmd[352 +:32]=1;
        send(0,"commit overwrites retained payload");
        make(6,63,253); cmd[512 +:256]=256'hb03f; cmd[352 +:32]=1;
        send(16,"old approval cannot survive reset");
        make(5,63,253); cmd[512 +:256]=256'hb03f; cmd[352 +:32]=1; cmd[480 +:32]=120;
        send(0,"new exact approval required");
        make(6,63,253); cmd[512 +:256]=256'hb03f; cmd[352 +:32]=1;
        send(0,"new session uses only newly published payload");
        $display("PASS production capacity: %0d checks; 64 vendors / 256 immutable obligations",checks);
        $finish;
    end
    initial begin #10000000; $fatal(1,"capacity test timeout"); end
endmodule
