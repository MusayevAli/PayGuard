`ifdef SILICONSIGN_GENERATED_KEYS
`include "siliconsign_keys.vh"
`else
`define SILICONSIGN_ADMIN_KEY 256'b0
`define SILICONSIGN_CONTROLLER_KEY 256'b0
`define SILICONSIGN_RESPONSE_KEY 256'b0
`endif
// 4KiB AXI4-Lite register aperture. Independent AW/W buffering and held R/B
// responses support arbitrary legal channel ordering and backpressure.
// No key or reset-policy register is exposed to the requester.
module siliconsign_axi #(
    parameter integer CLK_HZ=50000000,
    parameter integer NV=64,
    parameter integer NI=256,
    parameter [255:0] ADMIN_KEY=`SILICONSIGN_ADMIN_KEY,
    parameter [255:0] CONTROLLER_KEY=`SILICONSIGN_CONTROLLER_KEY,
    parameter [255:0] RESPONSE_KEY=`SILICONSIGN_RESPONSE_KEY
) (
    (* X_INTERFACE_INFO="xilinx.com:signal:clock:1.0 s_axi_aclk CLK",
       X_INTERFACE_PARAMETER="ASSOCIATED_BUSIF S_AXI, ASSOCIATED_RESET s_axi_aresetn" *)
    input wire s_axi_aclk,
    (* X_INTERFACE_INFO="xilinx.com:signal:reset:1.0 s_axi_aresetn RST",
       X_INTERFACE_PARAMETER="POLARITY ACTIVE_LOW" *) input wire s_axi_aresetn,
    (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI AWADDR",
       X_INTERFACE_PARAMETER="PROTOCOL AXI4LITE, DATA_WIDTH 32, ADDR_WIDTH 12, READ_WRITE_MODE READ_WRITE, HAS_BURST 0, HAS_LOCK 0, HAS_CACHE 0, HAS_QOS 0, HAS_REGION 0, NUM_READ_OUTSTANDING 1, NUM_WRITE_OUTSTANDING 1" *)
    input wire [11:0] s_axi_awaddr,
    (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI AWPROT" *) input wire [2:0] s_axi_awprot,
    (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI AWVALID" *) input wire s_axi_awvalid,
    (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI AWREADY" *) output wire s_axi_awready,
    (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI WDATA" *) input wire [31:0] s_axi_wdata,
    (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI WSTRB" *) input wire [3:0] s_axi_wstrb,
    (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI WVALID" *) input wire s_axi_wvalid,
    (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI WREADY" *) output wire s_axi_wready,
    (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI BRESP" *) output reg [1:0] s_axi_bresp,
    (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI BVALID" *) output reg s_axi_bvalid,
    (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI BREADY" *) input wire s_axi_bready,
    (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI ARADDR" *) input wire [11:0] s_axi_araddr,
    (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI ARPROT" *) input wire [2:0] s_axi_arprot,
    (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI ARVALID" *) input wire s_axi_arvalid,
    (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI ARREADY" *) output wire s_axi_arready,
    (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI RDATA" *) output reg [31:0] s_axi_rdata,
    (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI RRESP" *) output reg [1:0] s_axi_rresp,
    (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI RVALID" *) output reg s_axi_rvalid,
    (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI RREADY" *) input wire s_axi_rready
);
    localparam KEYS_VALID = (ADMIN_KEY != 0) && (CONTROLLER_KEY != 0) &&
        (RESPONSE_KEY != 0) && (ADMIN_KEY != CONTROLLER_KEY) &&
        (ADMIN_KEY != RESPONSE_KEY) && (CONTROLLER_KEY != RESPONSE_KEY);
    // The host must discover actual synthesized capacity before provisioning.
    // Old images have no capability register and must not be treated as 64/256.
    localparam [31:0] CAPABILITIES = {16'(NI),16'(NV)};
    reg [2047:0] command_buffer, response_buffer;
    reg [255:0] tag_buffer, response_tag_buffer;
    reg request_active, completion_pending, core_start;
    wire core_busy, core_done;
    wire [2047:0] core_response;
    wire [255:0] core_mac, command_mac;
    reg aw_pending, w_pending;
    reg [11:0] aw_address;
    reg [31:0] w_data;
    reg [3:0] w_strobe;
    integer b;
    genvar j;
    generate for(j=0;j<32;j=j+1) begin: mac_endian
        assign command_mac[255-j*8 -:8] = tag_buffer[j*8 +:8];
    end endgenerate
    assign s_axi_awready = !aw_pending && !s_axi_bvalid;
    assign s_axi_wready = !w_pending && !s_axi_bvalid;
    assign s_axi_arready = !s_axi_rvalid;
    wire registers_locked = request_active || completion_pending || core_busy;

    siliconsign_core #(.CLK_HZ(CLK_HZ),.NV(NV),.NI(NI),
        .ADMIN_KEY(ADMIN_KEY),.CONTROLLER_KEY(CONTROLLER_KEY),.RESPONSE_KEY(RESPONSE_KEY)) core (
        .clk(s_axi_aclk),.rst_n(s_axi_aresetn),.start(core_start),
        .command(command_buffer),.command_mac(command_mac),.busy(core_busy),
        .done(core_done),.response(core_response),.response_mac(core_mac)
    );

    always @(posedge s_axi_aclk) begin
        if(!s_axi_aresetn) begin
            command_buffer<=0; tag_buffer<=0; response_buffer<=0; response_tag_buffer<=0;
            request_active<=0; completion_pending<=0; core_start<=0;
            aw_pending<=0; w_pending<=0; aw_address<=0; w_data<=0; w_strobe<=0;
            s_axi_bvalid<=0; s_axi_bresp<=0;
            s_axi_rvalid<=0; s_axi_rresp<=0; s_axi_rdata<=0;
        end else begin
            core_start<=0;
            if(core_done) begin
                response_buffer<=core_response;
                for(b=0;b<32;b=b+1) response_tag_buffer[b*8 +:8]<=core_mac[255-b*8 -:8];
                request_active<=0; completion_pending<=1;
            end
            if(s_axi_bvalid && s_axi_bready) s_axi_bvalid<=0;
            if(s_axi_awvalid && s_axi_awready) begin
                aw_address<=s_axi_awaddr; aw_pending<=1;
            end
            if(s_axi_wvalid && s_axi_wready) begin
                w_data<=s_axi_wdata; w_strobe<=s_axi_wstrb; w_pending<=1;
            end
            if(aw_pending && w_pending && !s_axi_bvalid) begin
                aw_pending<=0; w_pending<=0;
                s_axi_bvalid<=1; s_axi_bresp<=2'b00;
                if(aw_address[1:0] != 0) s_axi_bresp<=2'b10;
                else if(aw_address == 12'h000) begin
                    if(w_strobe != 4'hf) s_axi_bresp<=2'b10;
                    else if(w_data == 32'd1 && !registers_locked) begin
                        core_start<=1; request_active<=1;
                        response_buffer<=0; response_tag_buffer<=0;
                    end else if(w_data == 32'd2 && !request_active && !core_busy && completion_pending)
                        completion_pending<=0;
                    else s_axi_bresp<=2'b10;
                end else if(aw_address >= 12'h100 && aw_address <= 12'h1fc) begin
                    if(registers_locked) s_axi_bresp<=2'b10;
                    else for(b=0;b<4;b=b+1) if(w_strobe[b])
                        command_buffer[(aw_address-12'h100)*8+b*8 +:8]<=w_data[b*8 +:8];
                end else if(aw_address >= 12'h200 && aw_address <= 12'h21c) begin
                    if(registers_locked) s_axi_bresp<=2'b10;
                    else for(b=0;b<4;b=b+1) if(w_strobe[b])
                        tag_buffer[(aw_address-12'h200)*8+b*8 +:8]<=w_data[b*8 +:8];
                end else s_axi_bresp<=2'b10;
            end

            if(s_axi_rvalid && s_axi_rready) s_axi_rvalid<=0;
            if(s_axi_arvalid && s_axi_arready) begin
                s_axi_rvalid<=1; s_axi_rresp<=2'b00; s_axi_rdata<=0;
                if(s_axi_araddr[1:0] != 0) s_axi_rresp<=2'b10;
                else if(s_axi_araddr == 12'h000) s_axi_rdata<=0;
                else if(s_axi_araddr == 12'h004)
                    s_axi_rdata<={29'b0,1'(KEYS_VALID),completion_pending,(request_active||core_busy)};
                else if(s_axi_araddr == 12'h008) s_axi_rdata<=32'h53494c31;
                else if(s_axi_araddr == 12'h00c) s_axi_rdata<=CAPABILITIES;
                else if(s_axi_araddr == 12'h010) s_axi_rdata<=CLK_HZ;
                else if(s_axi_araddr >= 12'h100 && s_axi_araddr <= 12'h1fc)
                    s_axi_rdata<=command_buffer[(s_axi_araddr-12'h100)*8 +:32];
                else if(s_axi_araddr >= 12'h200 && s_axi_araddr <= 12'h21c)
                    s_axi_rdata<=tag_buffer[(s_axi_araddr-12'h200)*8 +:32];
                else if(s_axi_araddr >= 12'h300 && s_axi_araddr <= 12'h3fc)
                    s_axi_rdata<=response_buffer[(s_axi_araddr-12'h300)*8 +:32];
                else if(s_axi_araddr >= 12'h400 && s_axi_araddr <= 12'h41c)
                    s_axi_rdata<=response_tag_buffer[(s_axi_araddr-12'h400)*8 +:32];
                else s_axi_rresp<=2'b10;
            end
        end
    end
endmodule
