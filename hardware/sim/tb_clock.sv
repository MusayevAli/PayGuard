`timescale 1ns/1ps
// One complete real production second is simulated at a 20 ns clock period.
// Later seconds jump only the divider's idle interior to focus on 59/60-second
// hold boundaries; they are not a claim that 60 wall-clock seconds were simulated.
module tb_clock;
    reg clk=0; always #10 clk=~clk;
    reg rst_n=0, cmd_valid=0, auth_ok=0;
    reg [2047:0] cmd=0;
    wire cmd_ready, resp_valid;
    wire [2047:0] resp;
    integer cycle_count=0, init_cycle, checks=0, second;
    reg [31:0] admin_seq=0, controller_seq=0, payment_seq=0;
    localparam [63:0] EPOCH=1700000000;
    localparam [127:0] SESSION_ID=128'hfeed1234;
    policy_engine #(.NV(1),.NI(1)) dut(.*);
    always @(posedge clk) cycle_count=cycle_count+1;

    task automatic make(input integer op);
        begin
            cmd=0; cmd[0 +:32]=32'h53494c43; cmd[32 +:32]=1;
            cmd[64 +:32]=op; cmd[128 +:128]=SESSION_ID;
            if(op==1 || op==2 || op==3) begin
                admin_seq=admin_seq+1; cmd[96 +:32]=admin_seq;
            end else if(op==4) begin
                controller_seq=controller_seq+1; cmd[96 +:32]=controller_seq;
            end else if(op==6) begin
                payment_seq=payment_seq+1; cmd[96 +:32]=payment_seq;
            end
            cmd[320 +:32]=978; cmd[384 +:64]=100;
            cmd[448 +:32]=2; cmd[512 +:256]=256'hbbbb;
            cmd[768 +:256]=256'h1234; cmd[1472 +:256]=256'heeee;
            auth_ok=(op!=6 && op!=9);
        end
    endtask
    task automatic send(input integer status);
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
            if(!resp_valid || resp[1792 +:32] !== status)
                $fatal(1,"clock test op=%0d expected=%0d actual=%0d",cmd[64 +:32],status,resp[1792 +:32]);
            if(cmd[64 +:32]==1 && status==0) init_cycle=cycle_count;
            checks=checks+1;
            @(negedge clk); cmd_valid=0;
            @(posedge clk); #1;
        end
    endtask

    initial begin
        if(dut.CLK_HZ != 50000000) $fatal(1,"production clock divider must be 50 MHz");
        repeat(3) @(negedge clk); rst_n=1;
        make(1); cmd[1024 +:64]=EPOCH; cmd[1152 +:64]=1000;
        cmd[1216 +:64]=1000; cmd[1280 +:32]=60; cmd[1312 +:32]=10;
        send(0);
        make(2); cmd[448 +:32]=1; cmd[512 +:256]=256'haaaa;
        cmd[1088 +:64]=1000; send(0);
        make(3); send(0);
        if(dut.vendor_hold_until[0] !== EPOCH+60) $fatal(1,"bank change must require 60 policy seconds");
        make(4); send(0);
        make(6); send(13);

        // Count every edge for the first full second, rather than shortening
        // the divider parameter or asserting only a constant configuration.
        while(cycle_count < init_cycle+49999999) begin @(posedge clk); #1; end
        if(dut.now_seconds !== EPOCH || dut.tick_divider !== 49999999)
            $fatal(1,"production second advanced before 50,000,000 cycles");
        @(posedge clk); #1;
        if(cycle_count !== init_cycle+50000000 || dut.now_seconds !== EPOCH+1 || dut.tick_divider !== 0)
            $fatal(1,"production second did not advance on cycle 50,000,000");
        checks=checks+1;
        $display("PASS production second: 50,000,000 cycles at 20 ns = 1 second");
        make(6); send(13);

        for(second=2;second<=60;second=second+1) begin
            // Simulation-only deposit skips already-tested interior cycles.
            @(negedge clk); dut.tick_divider=49999998;
            @(posedge clk); #1;
            if(dut.now_seconds !== EPOCH+second-1 || dut.tick_divider !== 49999999)
                $fatal(1,"second boundary advanced early");
            @(posedge clk); #1;
            if(dut.now_seconds !== EPOCH+second || dut.tick_divider !== 0)
                $fatal(1,"second boundary failed to advance");
            checks=checks+1;
            if(second==59) begin make(6); send(13); end
        end
        make(6); send(0);
        if(resp[1856 +:64] !== EPOCH+60 || resp[1920 +:64] !== 100)
            $fatal(1,"60-second hold or approved amount changed");
        $display("PASS production timer and 60-second hold: %0d checks",checks);
        $finish;
    end
    initial begin #1100000000; $fatal(1,"clock test timeout"); end
endmodule
