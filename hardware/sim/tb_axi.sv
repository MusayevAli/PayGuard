`timescale 1ns/1ps
// Full RTL integration test, including the real HMAC and policy engines.
module tb_axi;
    reg clk=0;
    always #10 clk=~clk;
    reg rst_n=0;
    reg [11:0] awaddr=0, araddr=0;
    reg [31:0] wdata=0;
    reg [3:0] wstrb=0;
    reg awvalid=0,wvalid=0,bready=0,arvalid=0,rready=0;
    wire awready,wready,bvalid,arready,rvalid;
    wire [1:0] bresp,rresp;
    wire [31:0] rdata;
    localparam [255:0] ADMIN=256'h000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f;
    localparam [255:0] CONTROLLER=256'h202122232425262728292a2b2c2d2e2f303132333435363738393a3b3c3d3e3f;
    localparam [255:0] RESPONSE=256'h404142434445464748494a4b4c4d4e4f505152535455565758595a5b5c5d5e5f;
    // Python hmac.new(bytes(range(0x40,0x60)), struct.pack('<64I', *response_words),
    // hashlib.sha256).digest(). No value is obtained from the DUT.
    localparam [255:0] STATUS_MAC=256'hfcef305527ad72783f46c2a700f2c22f5bf0417c32f36e40cb3c085ad3a300be;
    siliconsign_axi #(.ADMIN_KEY(ADMIN),
        .CONTROLLER_KEY(CONTROLLER),.RESPONSE_KEY(RESPONSE)) dut (
        .s_axi_aclk(clk),.s_axi_aresetn(rst_n),
        .s_axi_awaddr(awaddr),.s_axi_awprot(3'b0),.s_axi_awvalid(awvalid),.s_axi_awready(awready),
        .s_axi_wdata(wdata),.s_axi_wstrb(wstrb),.s_axi_wvalid(wvalid),.s_axi_wready(wready),
        .s_axi_bresp(bresp),.s_axi_bvalid(bvalid),.s_axi_bready(bready),
        .s_axi_araddr(araddr),.s_axi_arprot(3'b0),.s_axi_arvalid(arvalid),.s_axi_arready(arready),
        .s_axi_rdata(rdata),.s_axi_rresp(rresp),.s_axi_rvalid(rvalid),.s_axi_rready(rready)
    );
    integer checks=0,i,tries;
    reg [31:0] got;
    reg [31:0] expected_words [0:63];
    reg [255:0] tag_readback;

    task automatic send_aw(input [11:0] address);
        integer n;
        begin
            @(negedge clk); awaddr=address; awvalid=1;
            n=0;
            @(posedge clk);
            while (!awready && n<1000) begin @(posedge clk); n=n+1; end
            if(!awready) $fatal(1,"AW handshake timeout");
            @(negedge clk); awvalid=0;
        end
    endtask
    task automatic send_w(input [31:0] data,input [3:0] strobe);
        integer n;
        begin
            @(negedge clk); wdata=data; wstrb=strobe; wvalid=1;
            n=0;
            @(posedge clk);
            while(!wready && n<1000) begin @(posedge clk); n=n+1; end
            if(!wready) $fatal(1,"W handshake timeout");
            @(negedge clk); wvalid=0;
        end
    endtask
    // order: 0 together, 1 AW first, 2 W first. The delayed channel must not
    // produce an early write response. B is intentionally held under stall.
    task automatic write_reg(input [11:0] address,input [31:0] data,
                             input [3:0] strobe,input integer order,
                             input integer stall,input [1:0] want_response);
        integer n,k;
        begin
            bready=0;
            if(order==1) begin
                send_aw(address);
                repeat(3) begin @(negedge clk); if(bvalid) $fatal(1,"B before W"); end
                send_w(data,strobe);
            end else if(order==2) begin
                send_w(data,strobe);
                repeat(3) begin @(negedge clk); if(bvalid) $fatal(1,"B before AW"); end
                send_aw(address);
            end else begin
                fork send_aw(address); send_w(data,strobe); join
            end
            n=0;
            while(!bvalid && n<1000) begin @(negedge clk); n=n+1; end
            if(!bvalid) $fatal(1,"B response timeout at %h",address);
            if(bresp!==want_response) $fatal(1,"BRESP at %h: %h expected %h",address,bresp,want_response);
            for(k=0;k<stall;k=k+1) begin
                @(negedge clk);
                if(!bvalid || bresp!==want_response) $fatal(1,"B changed under backpressure");
                if(awready || wready) $fatal(1,"accepted new write while B outstanding");
            end
            bready=1;
            @(negedge clk); bready=0;
            if(bvalid) $fatal(1,"B not released after handshake");
            checks=checks+1;
        end
    endtask
    task automatic read_reg(input [11:0] address,input integer stall,
                            input [1:0] want_response,output [31:0] data);
        integer n,k;
        reg [31:0] held;
        begin
            @(negedge clk); araddr=address; arvalid=1; rready=0;
            n=0;
            @(posedge clk);
            while(!arready && n<1000) begin @(posedge clk); n=n+1; end
            if(!arready) $fatal(1,"AR handshake timeout");
            @(negedge clk); arvalid=0;
            n=0;
            while(!rvalid && n<1000) begin @(negedge clk); n=n+1; end
            if(!rvalid) $fatal(1,"R response timeout");
            if(rresp!==want_response) $fatal(1,"RRESP at %h: %h expected %h",address,rresp,want_response);
            held=rdata;
            araddr=12'hfff;
            for(k=0;k<stall;k=k+1) begin
                @(negedge clk);
                if(!rvalid || rresp!==want_response || rdata!==held)
                    $fatal(1,"R changed under backpressure");
                if(arready) $fatal(1,"accepted new read while R outstanding");
            end
            data=held; rready=1;
            @(negedge clk); rready=0;
            if(rvalid) $fatal(1,"R not released after handshake");
            checks=checks+1;
        end
    endtask
    task automatic expect_word(input [11:0] address,input [31:0] expected);
        reg [31:0] value;
        begin
            read_reg(address,2,0,value);
            if(value!==expected) $fatal(1,"read %h: %h expected %h",address,value,expected);
        end
    endtask

    initial begin
        repeat(4) @(negedge clk); rst_n=1;
        expect_word(12'h008,32'h53494c31);
        expect_word(12'h004,32'h4); // valid distinct keys, idle, no completion
        expect_word(12'h00c,32'h01000040); // default 64 vendors / 256 obligations
        if(dut.core.NV != 64 || dut.core.NI != 256 ||
           dut.core.policy.NV != 64 || dut.core.policy.NI != 256 ||
           dut.core.CLK_HZ != 50000000 || dut.core.policy.CLK_HZ != 50000000)
            $fatal(1,"capacity must propagate through the authenticated core");
        write_reg(12'h00c,32'hffffffff,4'hf,0,2,2); // read-only capabilities
        expect_word(12'h00c,32'h01000040);
        expect_word(12'h010,32'd50000000); // compiled seconds divider
        write_reg(12'h010,32'd100000000,4'hf,0,2,2); // immutable divider
        expect_word(12'h010,32'd50000000);

        write_reg(12'h110,32'h11223344,4'hf,1,4,0);
        expect_word(12'h110,32'h11223344);
        write_reg(12'h110,32'haabbccdd,4'h5,2,4,0);
        expect_word(12'h110,32'h11bb33dd);
        write_reg(12'h110,32'hffffffff,4'h0,0,0,0);
        expect_word(12'h110,32'h11bb33dd);

        // A held read must remain stable even if another channel changes the
        // underlying register before that read is acknowledged.
        @(negedge clk); araddr=12'h110; arvalid=1; rready=0;
        @(posedge clk); if(!arready) $fatal(1,"expected free read channel");
        @(negedge clk); arvalid=0;
        if(!rvalid || rdata!==32'h11bb33dd) $fatal(1,"initial held read mismatch");
        write_reg(12'h110,32'hcafef00d,4'hf,0,0,0);
        if(!rvalid || rdata!==32'h11bb33dd) $fatal(1,"held read followed live register");
        rready=1; @(negedge clk); rready=0;
        expect_word(12'h110,32'hcafef00d);

        write_reg(12'h111,32'h0,4'hf,0,2,2); // unaligned
        expect_word(12'h110,32'hcafef00d);
        write_reg(12'h500,32'h0,4'hf,0,0,2); // unmapped
        write_reg(12'h300,32'h0,4'hf,0,0,2); // read-only response
        write_reg(12'h000,32'h1,4'h1,0,0,2); // partial doorbell forbidden
        write_reg(12'h000,32'h2,4'hf,0,0,2); // no completion to acknowledge
        read_reg(12'h111,3,2,got);
        read_reg(12'h500,0,2,got);

        for(i=0;i<64;i=i+1) expected_words[i]=0;
        expected_words[0]=32'h53494c43; expected_words[1]=1;
        expected_words[2]=9; expected_words[3]=32'h10203040;
        expected_words[4]=32'h11223344; expected_words[5]=32'h55667788;
        expected_words[6]=32'h99aabbcc; expected_words[7]=32'hddeeff00;
        expected_words[10]=840;
        for(i=0;i<64;i=i+1)
            write_reg(12'h100+i*4,expected_words[i],4'hf,i%3,0,0);
        for(i=0;i<8;i=i+1) write_reg(12'h200+i*4,32'h0,4'hf,0,0,0);

        write_reg(12'h000,32'd1,4'hf,0,2,0);
        read_reg(12'h004,0,0,got);
        if(got!==32'h5) $fatal(1,"request not busy");
        // Every mutation path, including a second start and premature ACK,
        // must fail while the accepted request is running.
        write_reg(12'h10c,32'hffffffff,4'hf,0,0,2);
        write_reg(12'h200,32'hffffffff,4'hf,0,0,2);
        write_reg(12'h000,32'd1,4'hf,1,0,2);
        write_reg(12'h000,32'd2,4'hf,2,0,2);
        expect_word(12'h10c,32'h10203040);
        expect_word(12'h200,32'h0);
        tries=0;
        got=0;
        while(!got[1] && tries<1000) begin
            read_reg(12'h004,0,0,got); tries=tries+1;
        end
        if(got!==32'h6) $fatal(1,"completion timeout/status=%h",got);
        expected_words[0]=32'h53494c52;
        for(i=0;i<64;i=i+1) expect_word(12'h300+i*4,expected_words[i]);
        tag_readback=0;
        for(i=0;i<8;i=i+1) begin
            read_reg(12'h400+i*4,3,0,got);
            tag_readback[255-i*32 -:32]={got[7:0],got[15:8],got[23:16],got[31:24]};
        end
        if(tag_readback!==STATUS_MAC) $fatal(1,"response MAC mismatch: %h",tag_readback);
        $display("PASS AXI signed response matches independent Python HMAC");

        // Completed results cannot be replaced until explicit ACK; read
        // backpressure and rejected writes cannot alter the saved response.
        write_reg(12'h10c,32'hffffffff,4'hf,0,0,2);
        write_reg(12'h200,32'hffffffff,4'hf,0,0,2);
        write_reg(12'h000,32'd1,4'hf,0,0,2);
        expect_word(12'h30c,32'h10203040);
        write_reg(12'h000,32'd2,4'hf,0,3,0);
        expect_word(12'h004,32'h4);
        write_reg(12'h10c,32'h0badf00d,4'hf,2,2,0);
        expect_word(12'h10c,32'h0badf00d);
        write_reg(12'h000,32'd2,4'hf,0,0,2); // ACK cannot be reused

        $display("PASS: %0d AXI transactions; AW/W ordering, strobes, R/B backpressure, busy and pending protection",checks);
        $finish;
    end
    initial begin
        #1000000;
        $fatal(1,"AXI global watchdog timeout");
    end
endmodule
