`timescale 1ns/1ps
// SiliconSign SHA-256 compression function.
// One 512-bit block, 64 rounds, one round per clock. This module does not pad
// messages: block[511:504] is the first byte; state_in/digest contain H0..H7
// in most-significant-word-first order. start is a one-clock pulse accepted
// only while !busy. Inputs are captured on acceptance. done pulses for one
// clock and digest remains valid until the next completed compression.
module sha256_core (
    input  wire         clk,
    input  wire         rst_n,
    input  wire         start,
    input  wire [511:0] block,
    input  wire [255:0] state_in,
    output reg          busy,
    output reg          done,
    output reg  [255:0] digest
);
    reg [31:0] w [0:15];
    reg [31:0] a, b, c, d, e, f, g, h;
    reg [255:0] initial_state;
    reg [5:0] round;
    integer i;

    function automatic [31:0] rotr;
        input [31:0] x;
        input integer n;
        begin rotr = (x >> n) | (x << (32-n)); end
    endfunction
    function automatic [31:0] sigma0;
        input [31:0] x;
        begin sigma0 = rotr(x,7) ^ rotr(x,18) ^ (x >> 3); end
    endfunction
    function automatic [31:0] sigma1;
        input [31:0] x;
        begin sigma1 = rotr(x,17) ^ rotr(x,19) ^ (x >> 10); end
    endfunction
    function automatic [31:0] sum0;
        input [31:0] x;
        begin sum0 = rotr(x,2) ^ rotr(x,13) ^ rotr(x,22); end
    endfunction
    function automatic [31:0] sum1;
        input [31:0] x;
        begin sum1 = rotr(x,6) ^ rotr(x,11) ^ rotr(x,25); end
    endfunction
    function automatic [31:0] round_k;
        input [5:0] n;
        begin
            case (n)
                0: round_k=32'h428a2f98;  1: round_k=32'h71374491;
                2: round_k=32'hb5c0fbcf;  3: round_k=32'he9b5dba5;
                4: round_k=32'h3956c25b;  5: round_k=32'h59f111f1;
                6: round_k=32'h923f82a4;  7: round_k=32'hab1c5ed5;
                8: round_k=32'hd807aa98;  9: round_k=32'h12835b01;
               10: round_k=32'h243185be; 11: round_k=32'h550c7dc3;
               12: round_k=32'h72be5d74; 13: round_k=32'h80deb1fe;
               14: round_k=32'h9bdc06a7; 15: round_k=32'hc19bf174;
               16: round_k=32'he49b69c1; 17: round_k=32'hefbe4786;
               18: round_k=32'h0fc19dc6; 19: round_k=32'h240ca1cc;
               20: round_k=32'h2de92c6f; 21: round_k=32'h4a7484aa;
               22: round_k=32'h5cb0a9dc; 23: round_k=32'h76f988da;
               24: round_k=32'h983e5152; 25: round_k=32'ha831c66d;
               26: round_k=32'hb00327c8; 27: round_k=32'hbf597fc7;
               28: round_k=32'hc6e00bf3; 29: round_k=32'hd5a79147;
               30: round_k=32'h06ca6351; 31: round_k=32'h14292967;
               32: round_k=32'h27b70a85; 33: round_k=32'h2e1b2138;
               34: round_k=32'h4d2c6dfc; 35: round_k=32'h53380d13;
               36: round_k=32'h650a7354; 37: round_k=32'h766a0abb;
               38: round_k=32'h81c2c92e; 39: round_k=32'h92722c85;
               40: round_k=32'ha2bfe8a1; 41: round_k=32'ha81a664b;
               42: round_k=32'hc24b8b70; 43: round_k=32'hc76c51a3;
               44: round_k=32'hd192e819; 45: round_k=32'hd6990624;
               46: round_k=32'hf40e3585; 47: round_k=32'h106aa070;
               48: round_k=32'h19a4c116; 49: round_k=32'h1e376c08;
               50: round_k=32'h2748774c; 51: round_k=32'h34b0bcb5;
               52: round_k=32'h391c0cb3; 53: round_k=32'h4ed8aa4a;
               54: round_k=32'h5b9cca4f; 55: round_k=32'h682e6ff3;
               56: round_k=32'h748f82ee; 57: round_k=32'h78a5636f;
               58: round_k=32'h84c87814; 59: round_k=32'h8cc70208;
               60: round_k=32'h90befffa; 61: round_k=32'ha4506ceb;
               62: round_k=32'hbef9a3f7; 63: round_k=32'hc67178f2;
               default: round_k=32'h00000000;
            endcase
        end
    endfunction

    wire [31:0] t1 = h + sum1(e) + ((e & f) ^ (~e & g))
                    + round_k(round) + w[0];
    wire [31:0] t2 = sum0(a) + ((a & b) ^ (a & c) ^ (b & c));
    wire [31:0] next_w = sigma1(w[14]) + w[9] + sigma0(w[1]) + w[0];

    always @(posedge clk) begin
        if (!rst_n) begin
            busy <= 1'b0;
            done <= 1'b0;
            digest <= 256'b0;
            initial_state <= 256'b0;
            round <= 6'b0;
            a<=0; b<=0; c<=0; d<=0; e<=0; f<=0; g<=0; h<=0;
            for (i=0; i<16; i=i+1) w[i] <= 32'b0;
        end else begin
            done <= 1'b0;
            if (!busy) begin
                if (start) begin
                    initial_state <= state_in;
                    {a,b,c,d,e,f,g,h} <= state_in;
                    for (i=0; i<16; i=i+1)
                        w[i] <= block[511-i*32 -: 32];
                    round <= 6'b0;
                    busy <= 1'b1;
                end
            end else begin
                a <= t1+t2;
                b <= a; c <= b; d <= c;
                e <= d+t1;
                f <= e; g <= f; h <= g;
                for (i=0; i<15; i=i+1) w[i] <= w[i+1];
                w[15] <= next_w;
                if (round == 6'd63) begin
                    digest <= {
                        initial_state[255:224] + t1 + t2,
                        initial_state[223:192] + a,
                        initial_state[191:160] + b,
                        initial_state[159:128] + c,
                        initial_state[127:96] + d + t1,
                        initial_state[95:64] + e,
                        initial_state[63:32] + f,
                        initial_state[31:0] + g
                    };
                    busy <= 1'b0;
                    done <= 1'b1;
                    // Avoid retaining the key-derived SHA working state.
                    initial_state <= 256'b0;
                    a<=0; b<=0; c<=0; d<=0; e<=0; f<=0; g<=0; h<=0;
                    for (i=0; i<16; i=i+1) w[i] <= 32'b0;
                end else round <= round + 1'b1;
            end
        end
    end
endmodule
