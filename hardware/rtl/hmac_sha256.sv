`timescale 1ns/1ps
// SiliconSign HMAC-SHA256, fixed 256-byte messages and 32-byte keys.
// message[i*8 +: 8] is message byte i (matching little-endian MMIO words).
// key[255:248] is key byte 0; digest[255:248] is digest byte 0.
// start is accepted only while !busy; all inputs are captured. done is a
// one-clock pulse. The 8 compression blocks run sequentially through one
// SHA-256 round datapath. No keys are exposed on the module interface.
module hmac_sha256 (
    input  wire          clk,
    input  wire          rst_n,
    input  wire          start,
    input  wire [255:0]  key,
    input  wire [2047:0] message,
    output reg           busy,
    output reg           done,
    output reg  [255:0]  digest
);
    localparam [255:0] IV = 256'h6a09e667bb67ae853c6ef372a54ff53a510e527f9b05688c1f83d9ab5be0cd19;
    localparam [1:0] IDLE=2'd0, ISSUE=2'd1, WAIT=2'd2;
    reg [1:0] state;
    reg [2:0] block_number;
    reg [255:0] key_latched;
    reg [2047:0] message_latched;
    reg [255:0] chaining_state;
    reg [255:0] inner_digest;
    reg [511:0] sha_block;
    wire sha_start = (state == ISSUE);
    wire sha_busy, sha_done;
    wire [255:0] sha_digest;
    integer i;

    // SHA blocks are big-endian byte vectors even though the input message
    // is a little byte-position vector. Explicit byte selection avoids an
    // implicit whole-frame endian reversal.
    always @* begin
        sha_block = 512'b0;
        case (block_number)
            3'd0: begin
                for (i=0; i<64; i=i+1) begin
                    if (i<32) sha_block[511-i*8 -: 8] = key_latched[255-i*8 -: 8] ^ 8'h36;
                    else      sha_block[511-i*8 -: 8] = 8'h36;
                end
            end
            3'd1, 3'd2, 3'd3, 3'd4: begin
                for (i=0; i<64; i=i+1)
                    sha_block[511-i*8 -: 8] = message_latched[((block_number-1)*64+i)*8 +: 8];
            end
            3'd5: begin
                sha_block[511:504] = 8'h80;
                // Inner hash length: 64-byte ipad + 256-byte message.
                sha_block[63:0] = 64'd2560;
            end
            3'd6: begin
                for (i=0; i<64; i=i+1) begin
                    if (i<32) sha_block[511-i*8 -: 8] = key_latched[255-i*8 -: 8] ^ 8'h5c;
                    else      sha_block[511-i*8 -: 8] = 8'h5c;
                end
            end
            3'd7: begin
                sha_block[511:256] = inner_digest;
                sha_block[255:248] = 8'h80;
                // Outer hash length: 64-byte opad + 32-byte inner digest.
                sha_block[63:0] = 64'd768;
            end
            default: sha_block = 512'b0;
        endcase
    end

    sha256_core compression (
        .clk(clk), .rst_n(rst_n), .start(sha_start),
        .block(sha_block), .state_in(chaining_state),
        .busy(sha_busy), .done(sha_done), .digest(sha_digest)
    );

    always @(posedge clk) begin
        if (!rst_n) begin
            state <= IDLE;
            busy <= 1'b0;
            done <= 1'b0;
            digest <= 256'b0;
            key_latched <= 256'b0;
            message_latched <= 2048'b0;
            chaining_state <= 256'b0;
            inner_digest <= 256'b0;
            block_number <= 3'b0;
        end else begin
            done <= 1'b0;
            case (state)
                IDLE: begin
                    if (start) begin
                        key_latched <= key;
                        message_latched <= message;
                        chaining_state <= IV;
                        inner_digest <= 256'b0;
                        block_number <= 3'd0;
                        busy <= 1'b1;
                        state <= ISSUE;
                    end
                end
                ISSUE: state <= WAIT;
                WAIT: begin
                    if (sha_done) begin
                        if (block_number == 3'd7) begin
                            digest <= sha_digest;
                            done <= 1'b1;
                            busy <= 1'b0;
                            key_latched <= 256'b0;
                            message_latched <= 2048'b0;
                            chaining_state <= 256'b0;
                            inner_digest <= 256'b0;
                            state <= IDLE;
                        end else begin
                            if (block_number == 3'd5) begin
                                inner_digest <= sha_digest;
                                chaining_state <= IV;
                            end else chaining_state <= sha_digest;
                            block_number <= block_number + 1'b1;
                            state <= ISSUE;
                        end
                    end
                end
                default: begin
                    // Fail closed on illegal state. Reset is required for
                    // integration-level recovery; no completion is emitted.
                    busy <= 1'b1;
                    key_latched <= 256'b0;
                    message_latched <= 2048'b0;
                    chaining_state <= 256'b0;
                    inner_digest <= 256'b0;
                end
            endcase
        end
    end
endmodule
