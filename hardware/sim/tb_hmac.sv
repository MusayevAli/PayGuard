`timescale 1ns/1ps
module tb_hmac;
    reg clk=0;
    always #5 clk=~clk;
    reg rst_n=0, start=0;
    reg [255:0] key=0;
    reg [2047:0] message=0;
    wire busy, done;
    wire [255:0] digest;
    hmac_sha256 dut(.clk(clk),.rst_n(rst_n),.start(start),.key(key),
                    .message(message),.busy(busy),.done(done),.digest(digest));
    integer i, cycles, tests=0;
    reg [255:0] expected;

    // Expectations generated independently with Python's
    // hmac.new(key_bytes,message_bytes,hashlib.sha256).hexdigest().
    task run_case;
        input integer which;
        input integer disturb_inputs;
        begin
            @(negedge clk);
            case (which)
                0: begin
                    key=0; message=0;
                    expected=256'h1c76c43cda8bf8d46db15f121876ca2286656194a6f5eba5237ecc342c9582aa;
                end
                1: begin
                    for(i=0;i<32;i=i+1) key[255-i*8 -:8]=i;
                    for(i=0;i<256;i=i+1) message[i*8 +:8]=i;
                    expected=256'h42bf6fcd2ae46e32b29ce8bcbfd1f43896ea948aa9df2744eb4a47d28a8386c8;
                end
                2: begin
                    for(i=0;i<32;i=i+1) key[255-i*8 -:8]=(i*7+3)&255;
                    for(i=0;i<256;i=i+1) message[i*8 +:8]=(i*17+5)&255;
                    expected=256'hbb1b80b853c9e606d62dd81cdbbca722c500ee93c5bfdce292a0bd79e3b21da1;
                end
                3: begin
                    key={256{1'b1}}; message={2048{1'b1}};
                    expected=256'hd2e8a0e36c8239db24e41f802d9495f4c6e811eab1093c0d97b5727c8825a0c3;
                end
            endcase
            start=1;
            @(negedge clk); start=0;
            if (!busy) $fatal(1,"start not accepted");
            cycles=0;
            while (!done && cycles<1000) begin
                // Request data and key must be latched. A spurious start
                // while busy must not replace the accepted operation.
                if (disturb_inputs && cycles==4) begin
                    key=~key; message=~message; start=1;
                end
                if (cycles==5) start=0;
                @(negedge clk);
                cycles=cycles+1;
            end
            if (!done) $fatal(1,"HMAC timeout");
            if (busy) $fatal(1,"busy remains set at completion");
            if (digest!==expected)
                $fatal(1,"vector %0d: got %h expected %h",which,digest,expected);
            if (dut.key_latched!==256'b0 || dut.message_latched!==2048'b0)
                $fatal(1,"input snapshot not cleared after completion");
            @(negedge clk);
            if (done) $fatal(1,"done is not a one-cycle pulse");
            if (digest!==expected) $fatal(1,"digest changed while idle");
            tests=tests+1;
            $display("PASS HMAC vector %0d (%0d cycles)",which,cycles);
        end
    endtask

    initial begin
        repeat(3) @(negedge clk);
        rst_n=1;
        run_case(0,0);
        run_case(1,1);
        run_case(2,0);
        run_case(3,0);
        // Reset during a hash must abort and erase transient state.
        @(negedge clk); key={256{1'b1}}; message={2048{1'b1}}; start=1;
        @(negedge clk); start=0;
        repeat(20) @(negedge clk);
        rst_n=0;
        @(negedge clk);
        if (busy || done || digest!==256'b0 || dut.key_latched!==256'b0)
            $fatal(1,"reset did not clear crypto state");
        rst_n=1;
        run_case(1,0);
        $display("PASS: %0d HMAC vectors, input snapshot, busy-start, pulse, reset/recovery",tests);
        $finish;
    end
    initial begin
        #100000;
        $fatal(1,"global watchdog timeout");
    end
endmodule
