// Authenticated command/response wrapper. The policy engine never sees an
// unverified privileged request as authenticated. Inputs are captured at start.
module siliconsign_core #(
    parameter integer CLK_HZ = 50000000,
    parameter integer NV = 64,
    parameter integer NI = 256,
    parameter [255:0] ADMIN_KEY = 256'b0,
    parameter [255:0] CONTROLLER_KEY = 256'b0,
    parameter [255:0] RESPONSE_KEY = 256'b0
) (
    input wire clk, input wire rst_n, input wire start,
    input wire [2047:0] command, input wire [255:0] command_mac,
    output reg busy, output reg done,
    output reg [2047:0] response, output reg [255:0] response_mac
);
    localparam KEYS_VALID = (ADMIN_KEY != 0) && (CONTROLLER_KEY != 0) &&
        (RESPONSE_KEY != 0) && (ADMIN_KEY != CONTROLLER_KEY) &&
        (ADMIN_KEY != RESPONSE_KEY) && (CONTROLLER_KEY != RESPONSE_KEY);
    localparam [3:0] IDLE=0, CLASSIFY=1, AUTH_ISSUE=2, AUTH_WAIT=3,
        POLICY_SEND=4, POLICY_WAIT=5, SIGN_ISSUE=6, SIGN_WAIT=7, DISABLED=8;
    reg [3:0] state;
    reg [2047:0] command_hold;
    reg [255:0] mac_hold, hash_key;
    reg authenticated;
    wire hash_done, hash_busy;
    wire [255:0] hash_digest;
    wire [2047:0] hash_message = (state == SIGN_ISSUE || state == SIGN_WAIT)
        ? response : command_hold;
    wire hash_start = state == AUTH_ISSUE || state == SIGN_ISSUE;
    wire policy_ready, policy_response_valid;
    wire [2047:0] policy_response;
    wire [31:0] opcode = command_hold[64 +: 32];

    hmac_sha256 authentication (
        .clk(clk), .rst_n(rst_n), .start(hash_start), .key(hash_key),
        .message(hash_message), .busy(hash_busy), .done(hash_done), .digest(hash_digest)
    );
    policy_engine #(.CLK_HZ(CLK_HZ), .NV(NV), .NI(NI)) policy (
        .clk(clk), .rst_n(rst_n),
        .cmd_valid(state == POLICY_SEND && policy_ready),
        .cmd(command_hold), .auth_ok(authenticated), .cmd_ready(policy_ready),
        .resp_valid(policy_response_valid), .resp(policy_response)
    );

    always @(posedge clk) begin
        if (!rst_n) begin
            state <= IDLE; busy <= 0; done <= 0;
            response <= 0; response_mac <= 0; command_hold <= 0;
            mac_hold <= 0; hash_key <= 0; authenticated <= 0;
        end else begin
            done <= 0;
            case (state)
                IDLE: if (start) begin
                    busy <= 1; command_hold <= command; mac_hold <= command_mac;
                    authenticated <= 0; response <= 0; response_mac <= 0;
                    if (KEYS_VALID) state <= CLASSIFY;
                    else state <= DISABLED;
                end
                DISABLED: begin
                    // Deliberately no MAC is released with missing/reused keys.
                    response <= command_hold;
                    response[0 +: 32] <= 32'h53494c52;
                    response[56*32 +: 32] <= 32'd2;
                    response[57*32 +: 224] <= 0;
                    response_mac <= 0; busy <= 0; done <= 1; state <= IDLE;
                end
                CLASSIFY: begin
                    case (opcode)
                        1,2,3,7,8,10: begin hash_key <= ADMIN_KEY; state <= AUTH_ISSUE; end
                        4,5,11: begin hash_key <= CONTROLLER_KEY; state <= AUTH_ISSUE; end
                        default: begin authenticated <= 0; state <= POLICY_SEND; end
                    endcase
                end
                AUTH_ISSUE: state <= AUTH_WAIT;
                AUTH_WAIT: if (hash_done) begin
                    // Fixed-width equality, not bytewise early-exit comparison.
                    authenticated <= (hash_digest == mac_hold);
                    hash_key <= 0;
                    state <= POLICY_SEND;
                end
                POLICY_SEND: if (policy_ready) state <= POLICY_WAIT;
                POLICY_WAIT: if (policy_response_valid) begin
                    response <= policy_response;
                    hash_key <= RESPONSE_KEY;
                    state <= SIGN_ISSUE;
                end
                SIGN_ISSUE: state <= SIGN_WAIT;
                SIGN_WAIT: if (hash_done) begin
                    response_mac <= hash_digest;
                    hash_key <= 0; mac_hold <= 0; command_hold <= 0;
                    authenticated <= 0; busy <= 0; done <= 1; state <= IDLE;
                end
                default: begin
                    // Corrupted state never becomes an authorization.
                    busy <= 1; done <= 0; response_mac <= 0;
                    authenticated <= 0; hash_key <= 0;
                end
            endcase
        end
    end
endmodule
