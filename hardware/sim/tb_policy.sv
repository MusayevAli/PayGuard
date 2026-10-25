`timescale 1ns/1ps
module tb_policy;
    reg clk=0; always #5 clk=~clk;
    reg rst_n=0, cmd_valid=0, auth_ok=0;
    reg [2047:0] cmd=0;
    wire cmd_ready, resp_valid;
    wire [2047:0] resp;
    integer checks=0;
    reg [31:0] asq=0, csq=0, psq=0;
    localparam [127:0] SESSION_ID=128'h112233445566778899aabbccddeeff00;
    localparam [255:0] ACCOUNT_A=256'haaaa, ACCOUNT_B=256'hbbbb;
    policy_engine #(.CLK_HZ(1000),.NV(4),.NI(16)) dut(.*);

    task automatic reset;
        begin
            @(negedge clk); rst_n=0; cmd_valid=0; cmd=0; auth_ok=0;
            repeat(3) @(posedge clk);
            @(negedge clk); rst_n=1;
            asq=0; csq=0; psq=0;
        end
    endtask

    task automatic make(input integer op);
        begin
            cmd=0;
            cmd[0 +:32]=32'h53494c43; cmd[32 +:32]=1;
            cmd[64 +:32]=op; cmd[128 +:128]=SESSION_ID;
            if (op==1 || op==2 || op==3 || op==7 || op==8 || op==10) begin
                asq=asq+1; cmd[96 +:32]=asq;
            end else if(op==4 || op==5 || op==11) begin csq=csq+1; cmd[96 +:32]=csq; end
            else if(op==6) begin psq=psq+1; cmd[96 +:32]=psq; end
            cmd[320 +:32]=840; cmd[384 +:64]=600; cmd[448 +:32]=1;
            cmd[512 +:256]=ACCOUNT_A; cmd[768 +:256]=256'h1000;
            cmd[1472 +:256]=256'heeeee;
            auth_ok=(op!=6 && op!=9);
        end
    endtask

    task automatic send(input integer expected, input [255:0] label);
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
            if(!resp_valid || resp[1792 +:32] !== expected) begin
                $display("FAIL %0s expected=%0d actual=%0d valid=%0b",label,expected,resp[1792 +:32],resp_valid);
                $fatal(1);
            end
            if(resp[0 +:32]!==32'h53494c52 || resp[32 +:32]!==1) $fatal(1,"response framing");
            checks=checks+1;
            @(negedge clk); cmd_valid=0;
            @(posedge clk); #1;
            if(resp_valid) $fatal(1,"response valid must pulse");
        end
    endtask

    task automatic initialize(input [63:0] t, input [63:0] cap, input [63:0] thr, input [31:0] cnt);
        begin
            make(1); cmd[1024 +:64]=t; cmd[1152 +:64]=cap;
            cmd[1216 +:64]=thr; cmd[1280 +:32]=3; cmd[1312 +:32]=cnt;
            send(0,"initialize");
        end
    endtask

    task automatic add_vendor(input [63:0] cap);
        begin make(2); cmd[1088 +:64]=cap; send(0,"add vendor"); end
    endtask

    task automatic invoice(input integer slot, input [63:0] cents, input [31:0] flags);
        begin
            make(4); cmd[288 +:32]=slot; cmd[768 +:256]=256'h1000+slot;
            cmd[384 +:64]=cents; cmd[352 +:32]=flags; send(0,"commit invoice");
        end
    endtask

    task automatic payment(input integer slot, input [63:0] cents, input [31:0] flags);
        begin
            make(6); cmd[288 +:32]=slot; cmd[768 +:256]=256'h1000+slot;
            cmd[384 +:64]=cents; cmd[352 +:32]=flags;
        end
    endtask

    initial begin
        reset();
        make(9); send(0,"status before init");
        make(6); send(3,"pay fail closed");
        make(1); auth_ok=0; send(2,"init bad auth");
        initialize(1700000000,5000,1000,10);
        make(1); send(3,"init cannot overwrite state");
        make(2); cmd[1088 +:64]=4000; auth_ok=0; send(2,"vendor bad auth");
        add_vendor(4000);
        make(2); send(7,"cannot overwrite vendor");
        make(2); cmd[256 +:32]=4; send(6,"vendor out of range");
        make(4); auth_ok=0; send(2,"invoice bad auth");
        invoice(0,600,0);
        payment(0,600,0); cmd[512 +:256]=ACCOUNT_B; send(9,"bank substitution");
        payment(0,601,0); send(11,"amount substitution");
        payment(0,600,1); send(20,"unusual flag substitution");
        payment(0,600,0); cmd[320 +:32]=978; send(12,"currency substitution");
        payment(0,600,0); cmd[448 +:32]=2; send(22,"version substitution");
        payment(0,600,0); cmd[768 +:256]=256'hdeed; send(10,"invoice substitution");
        payment(0,600,0); cmd[128 +:128]=128'hbad; send(4,"session substitution");
        payment(0,600,0); cmd[352 +:32]=2; send(20,"unknown flags");
        payment(0,600,0); cmd[2047]=1; send(1,"reserved data");
        payment(0,600,0); send(0,"valid first payment");
        if(resp[1920 +:64]!==600 || resp[1824 +:32]!==1 || resp[1984 +:32]!==1)
            $fatal(1,"payment counters");
        send(5,"same-sequence replay");
        payment(0,600,0); send(17,"invoice cannot pay twice");
        make(4); cmd[288 +:32]=1; send(26,"duplicate identity new slot");
        invoice(1,600,0);
        payment(1,600,0); send(16,"split threshold cumulative");
        make(5); cmd[288 +:32]=1; cmd[768 +:256]=256'h1001; cmd[384 +:64]=601;
        cmd[480 +:32]=1; send(11,"approval exact amount");
        make(5); cmd[288 +:32]=1; cmd[768 +:256]=256'h1001;
        cmd[480 +:32]=1; send(0,"approve exact invoice");
        repeat(1001) @(posedge clk);
        payment(1,600,0); send(16,"approval expired");
        make(5); cmd[288 +:32]=1; cmd[768 +:256]=256'h1001;
        cmd[480 +:32]=300; send(0,"approve renewed exact invoice");
        payment(1,600,0); send(0,"approved unusual payment");
        if(resp[1920 +:64]!==1200 || resp[1824 +:32]!==2) $fatal(1,"cumulative spending");
        invoice(2,100,1);
        make(3); cmd[512 +:256]=ACCOUNT_B; cmd[448 +:32]=2;
        send(0,"authenticated bank change");
        payment(2,100,1); send(9,"old invoice invalidated");
        make(11); cmd[288 +:32]=2; cmd[768 +:256]=256'h1002;
        cmd[384 +:64]=100; cmd[352 +:32]=1; cmd[512 +:256]=ACCOUNT_B;
        cmd[448 +:32]=2; auth_ok=0; send(2,"rebind requires controller auth");
        make(11); cmd[288 +:32]=2; cmd[768 +:256]=256'h1002;
        cmd[384 +:64]=101; cmd[352 +:32]=1; cmd[512 +:256]=ACCOUNT_B;
        cmd[448 +:32]=2; send(11,"rebind cannot change amount");
        make(11); cmd[288 +:32]=2; cmd[768 +:256]=256'h1002;
        cmd[384 +:64]=100; cmd[352 +:32]=1; cmd[512 +:256]=ACCOUNT_B;
        cmd[448 +:32]=2; send(0,"rebind existing unpaid obligation");
        payment(2,100,1); send(9,"old account still rejected");
        payment(2,100,1); cmd[512 +:256]=ACCOUNT_B; cmd[448 +:32]=2;
        send(13,"rebound invoice cannot skip hold");
        make(4); cmd[288 +:32]=3; cmd[768 +:256]=256'h1003;
        cmd[384 +:64]=100; cmd[512 +:256]=ACCOUNT_B; cmd[448 +:32]=2;
        send(0,"new-bank invoice commit");
        make(5); cmd[288 +:32]=3; cmd[768 +:256]=256'h1003;
        cmd[384 +:64]=100; cmd[512 +:256]=ACCOUNT_B; cmd[448 +:32]=2;
        cmd[480 +:32]=300; send(0,"new-bank exception approve");
        payment(3,100,0); cmd[512 +:256]=ACCOUNT_B; cmd[448 +:32]=2;
        send(13,"approval cannot skip hold");
        repeat(3001) @(posedge clk);
        payment(3,100,0); cmd[512 +:256]=ACCOUNT_B; cmd[448 +:32]=2;
        send(0,"bank hold elapsed");
        payment(2,100,1); cmd[512 +:256]=ACCOUNT_B; cmd[448 +:32]=2;
        send(16,"rebound invoice needs fresh approval");
        make(5); cmd[288 +:32]=2; cmd[768 +:256]=256'h1002;
        cmd[384 +:64]=100; cmd[352 +:32]=1; cmd[512 +:256]=ACCOUNT_B;
        cmd[448 +:32]=2; cmd[480 +:32]=300; send(0,"approve rebound invoice");
        payment(2,100,1); cmd[512 +:256]=ACCOUNT_B; cmd[448 +:32]=2;
        send(0,"rebound obligation pays once");
        make(11); cmd[288 +:32]=2; cmd[768 +:256]=256'h1002;
        cmd[384 +:64]=100; cmd[352 +:32]=1; cmd[512 +:256]=ACCOUNT_B;
        cmd[448 +:32]=2; send(17,"cannot rebind paid obligation");
        make(8); cmd[1024 +:64]=1700086400; send(19,"cannot advance clock");
        make(7); send(0,"disable vendor");
        make(3); cmd[512 +:256]=ACCOUNT_A; cmd[448 +:32]=3; send(21,"disable permanent");
        make(2); send(7,"cannot re-add disabled slot");
        make(10); auth_ok=0; send(2,"unauthenticated lock");
        make(10); send(0,"authenticated lock");
        payment(3,100,0); send(3,"locked payments");
        make(1); send(3,"lock cannot be reinitialized");

        reset(); initialize(86399,1000,1000,1); add_vendor(900);
        invoice(0,600,0); payment(0,600,0); send(0,"day zero payment");
        invoice(1,301,0); payment(1,301,0); send(15,"vendor cap");
        invoice(2,401,0); payment(2,401,0); send(14,"global cap");
        invoice(3,100,0); payment(3,100,0); send(24,"velocity count");
        repeat(1001) @(posedge clk);
        payment(3,100,0); send(25,"day boundary requires rollover");
        make(8); cmd[1024 +:64]=86400; send(0,"authenticated daily rollover");
        payment(3,100,0); send(0,"new day budget usable");
        make(8); cmd[1024 +:64]=86400; send(19,"same day cannot reset cap");
        payment(0,600,0); send(17,"rollover preserves paid flag");

        reset(); initialize(1700000000,64'hffffffffffffffff,64'hffffffffffffffff,100);
        make(2); cmd[1088 +:64]=64'hffffffffffffffff;
        cmd[1408 +:64]=64'hfffffffffffffffe; send(0,"restore high vendor spend");
        invoice(0,4,0); payment(0,4,0); send(27,"vendor addition overflow");
        make(4); cmd[288 +:32]=1; cmd[768 +:256]=256'h1001;
        cmd[384 +:64]=0; send(11,"zero amount invoice");
        make(4); cmd[288 +:32]=16; send(6,"invoice out of bounds");

        reset();
        make(1); cmd[1024 +:64]=1700000000; cmd[1152 +:64]=100;
        cmd[1216 +:64]=100; cmd[1280 +:32]=3; cmd[1312 +:32]=10;
        cmd[1344 +:64]=101; send(14,"invalid restore global spent");
        initialize(1700000000,64'hffffffffffffffff,64'hffffffffffffffff,100);
        // A separately restored global total cannot wrap to a small accepted amount.
        make(10); send(0,"lock before reset lifecycle");
        reset();
        make(1); cmd[1024 +:64]=1700000000;
        cmd[1152 +:64]=64'hffffffffffffffff;
        cmd[1216 +:64]=64'hffffffffffffffff;
        cmd[1280 +:32]=3; cmd[1312 +:32]=100;
        cmd[1344 +:64]=64'hfffffffffffffffe; send(0,"restore global maximum");
        add_vendor(1000); invoice(0,4,0); payment(0,4,0); send(27,"global addition overflow");
        make(9); send(0,"status after failed overflow");
        if(resp[1920 +:64]!==64'hfffffffffffffffe || resp[1824 +:32]!==0)
            $fatal(1,"rejection mutated counters");

        reset(); initialize(1700000000,1000,1000,100); add_vendor(1000);
        invoice(0,100,1); payment(0,100,1); send(16,"explicit unusual requires approval");
        make(11); cmd[384 +:64]=100; cmd[352 +:32]=1;
        send(9,"no-op rebind rejected");
        make(5); cmd[384 +:64]=100; cmd[352 +:32]=1;
        cmd[480 +:32]=301; send(6,"approval ttl bounded");
        make(5); cmd[384 +:64]=100; cmd[352 +:32]=1;
        cmd[480 +:32]=2; send(0,"unusual approval accepted");
        payment(0,100,1); send(0,"explicit unusual pays once");
        payment(0,100,1); send(17,"exception cannot replay");
        make(2); cmd[256 +:32]=1; cmd[1088 +:64]=1000;
        cmd[1408 +:64]=1001; send(15,"restore vendor cap invalid");
        make(2); cmd[256 +:32]=1; cmd[1088 +:64]=1000;
        cmd[1728 +:32]=101; send(24,"restore velocity invalid");
        make(2); cmd[256 +:32]=1; cmd[1088 +:64]=1000; send(0,"second vendor");
        make(4); cmd[256 +:32]=1; cmd[288 +:32]=1; send(26,"cross-vendor invoice duplicate");
        make(3); cmd[448 +:32]=2; send(9,"same-account bank change rejected");
        make(3); cmd[448 +:32]=3; cmd[512 +:256]=ACCOUNT_B;
        send(22,"bank version cannot skip");

        reset(); initialize(64'hffffffffffffffff,1000,1000,100);
        repeat(1001) @(posedge clk);
        make(9); send(0,"status after clock saturation");
        if(resp[1856 +:64]!==64'hffffffffffffffff || resp[2016 +:32]!==0)
            $fatal(1,"clock must saturate and lock");
        make(1); send(3,"clock saturation locks reinit");
        $display("PASS policy_engine: %0d adversarial and lifecycle checks",checks);
        $finish;
    end
    initial begin #1000000; $fatal(1,"test timeout"); end
endmodule
