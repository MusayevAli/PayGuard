`timescale 1ns/1ps
// Independent payment-policy state machine. All monetary arithmetic uses integer cents.
// auth_ok is supplied ONLY by the role-aware HMAC-verifying wrapper, never by MMIO.
module policy_engine #(
    parameter integer CLK_HZ = 50000000,
    parameter integer NV = 64,
    parameter integer NI = 256
) (
    input wire clk, input wire rst_n,
    input wire cmd_valid, input wire [2047:0] cmd, input wire auth_ok,
    output wire cmd_ready,
    output reg resp_valid, output reg [2047:0] resp
);
    localparam [31:0] OK=0, BAD_FRAME=1, AUTH=2, LOCKED=3, SESSION=4,
        SEQUENCE=5, RANGE=6, EXISTS=7, VENDOR=8, ACCOUNT=9, INVOICE=10,
        AMOUNT=11, CURRENCY=12, COOLDOWN=13, DAILY_CAP=14, VENDOR_CAP=15,
        APPROVAL=16, PAID=17, CAPACITY=18, TIME_ERR=19, FLAGS=20,
        DISABLED=21, VERSION_ERR=22, ZERO=23, COUNT=24, DAY_ROLLOVER=25,
        DUPLICATE=26, OVERFLOW=27;
    localparam [31:0] OP_INIT=1, OP_ADD_VENDOR=2, OP_CHANGE_BANK=3,
        OP_COMMIT=4, OP_APPROVE=5, OP_PAY=6, OP_DISABLE=7,
        OP_ADVANCE_DAY=8, OP_STATUS=9, OP_LOCK=10, OP_REBIND=11;
    localparam integer DIV_WIDTH = (CLK_HZ <= 1) ? 1 : $clog2(CLK_HZ);
    localparam integer HASH_ADDR_WIDTH = (NI <= 1) ? 1 : $clog2(NI);
    localparam [1:0] REQUEST_IDLE=0, HASH_LOAD=1, HASH_SCAN=2, REQUEST_CHECK=3;
    reg [1:0] request_state;
    reg [2047:0] command_hold;
    reg auth_hold, duplicate_hash;
    reg [HASH_ADDR_WIDTH-1:0] scan_index;
    reg [255:0] hash_read_data;

    reg initialized, locked_latched;
    reg [127:0] session_id;
    reg [31:0] configured_currency, last_admin_seq, last_controller_seq, last_pay_seq;
    reg [31:0] authorization_counter, min_hold, max_count;
    reg [63:0] now_seconds, current_day, budget_day, global_limit, threshold, global_spent;
    reg [31:0] seconds_of_day;
    reg [DIV_WIDTH-1:0] tick_divider;

    reg vendor_used [0:NV-1];
    reg vendor_enabled [0:NV-1];
    reg [255:0] vendor_account [0:NV-1];
    reg [31:0] vendor_version [0:NV-1];
    reg [63:0] vendor_limit [0:NV-1];
    reg [63:0] vendor_spent [0:NV-1];
    reg [63:0] vendor_hold_until [0:NV-1];
    reg [31:0] vendor_count [0:NV-1];

    reg invoice_used [0:NI-1];
    reg invoice_paid [0:NI-1];
    reg invoice_approved [0:NI-1];
    reg [31:0] invoice_vendor [0:NI-1];
    reg [31:0] invoice_version [0:NI-1];
    reg [31:0] invoice_currency [0:NI-1];
    reg [31:0] invoice_flags [0:NI-1];
    reg [255:0] invoice_account [0:NI-1];
    (* ram_style = "block" *) reg [255:0] invoice_hash [0:NI-1];
    reg [63:0] invoice_amount [0:NI-1];
    reg [63:0] invoice_approval_deadline [0:NI-1];

    // Payload memories intentionally have no bulk reset, allowing distributed
    // RAM inference at production capacity. Reset clears the validity flags;
    // ADD_VENDOR / COMMIT write every payload field before publishing a slot.
    // No payload can affect acceptance until its slot is valid. Counters read
    // by STATUS remain resettable. Invoice identities use a synchronous block RAM
    // port and a bounded sequential scan, avoiding a device-wide parallel CAM.

    wire [31:0] op = command_hold[64 +:32];
    wire [31:0] seq = command_hold[96 +:32];
    wire [127:0] req_session = command_hold[128 +:128];
    wire [31:0] vi = command_hold[256 +:32];
    wire [31:0] ii = command_hold[288 +:32];
    wire [31:0] req_currency = command_hold[320 +:32];
    wire [31:0] req_flags = command_hold[352 +:32];
    wire [63:0] amount = command_hold[384 +:64];
    wire [31:0] req_version = command_hold[448 +:32];
    wire [31:0] ttl = command_hold[480 +:32];
    wire [255:0] account = command_hold[512 +:256];
    wire [255:0] identity = command_hold[768 +:256];
    wire [63:0] req_time = command_hold[1024 +:64];
    wire [63:0] req_vendor_limit = command_hold[1088 +:64];
    wire [63:0] req_global_limit = command_hold[1152 +:64];
    wire [63:0] req_threshold = command_hold[1216 +:64];
    wire [31:0] req_hold = command_hold[1280 +:32];
    wire [31:0] req_count_limit = command_hold[1312 +:32];
    wire [63:0] req_global_spent = command_hold[1344 +:64];
    wire [63:0] req_vendor_spent = command_hold[1408 +:64];
    wire [255:0] evidence = command_hold[1472 +:256];
    wire [31:0] req_vendor_count = command_hold[1728 +:32];
    wire admin_op = op==OP_INIT || op==OP_ADD_VENDOR || op==OP_CHANGE_BANK ||
                         op==OP_DISABLE || op==OP_ADVANCE_DAY || op==OP_LOCK;
    wire controller_op = op==OP_COMMIT || op==OP_APPROVE || op==OP_REBIND;
    wire tick = tick_divider == CLK_HZ-1;
    wire clock_overflow = initialized && tick && now_seconds == 64'hffffffffffffffff;
    assign cmd_ready = request_state == REQUEST_IDLE && !clock_overflow;

    reg [31:0] status;
    reg [63:0] visible_now, visible_spent;
    reg [31:0] visible_count, visible_auth;
    reg visible_initialized;
    reg [64:0] global_sum, vendor_sum, future_time;
    integer i;

    wire hash_read_enable = request_state == HASH_LOAD ||
        (request_state == HASH_SCAN && scan_index < NI-1);
    wire [HASH_ADDR_WIDTH-1:0] hash_read_address = request_state == HASH_LOAD ?
        (op == OP_COMMIT ? {HASH_ADDR_WIDTH{1'b0}} : ii[HASH_ADDR_WIDTH-1:0]) :
        scan_index + 1'b1;
    // No bulk reset: validity flags mask stale contents. Both addresses are
    // bounded before any accepted mutation; read results are consumed only in
    // a later state, after this synchronous read completes.
    always @(posedge clk) begin
        if (rst_n && hash_read_enable) hash_read_data <= invoice_hash[hash_read_address];
        if (rst_n && request_state == REQUEST_CHECK && !clock_overflow &&
            status == OK && op == OP_COMMIT)
            invoice_hash[ii[HASH_ADDR_WIDTH-1:0]] <= identity;
    end

    // Validate the captured command after its selected hash / duplicate scan is
    // available. Every accepted request produces at most one state mutation.
    always @* begin
        status = OK;
        global_sum = {1'b0,global_spent} + {1'b0,amount};
        vendor_sum = 65'd0;
        future_time = 65'd0;
        if (vi < NV) vendor_sum = {1'b0,vendor_spent[vi]} + {1'b0,amount};
        if (op == OP_CHANGE_BANK) future_time = {1'b0,now_seconds} + {33'd0,min_hold};
        if (op == OP_APPROVE) future_time = {1'b0,now_seconds} + {33'd0,ttl};

        if (command_hold[0 +:32] != 32'h53494c43 || command_hold[32 +:32] != 1 ||
            command_hold[1760 +:288] != 0 || op < OP_INIT || op > OP_REBIND)
            status = BAD_FRAME;
        else if (req_flags & 32'hfffffffe) status = FLAGS;
        else if ((admin_op || controller_op) && !auth_hold) status = AUTH;
        else if (op == OP_STATUS) status = OK;
        else if (op == OP_INIT) begin
            if (initialized || locked_latched) status = LOCKED;
            else if (seq <= last_admin_seq) status = SEQUENCE;
            else if (req_session == 0 || req_time == 0 || req_global_limit == 0 ||
                     req_threshold == 0 || req_hold == 0 || req_count_limit == 0)
                status = ZERO;
            else if (req_currency == 0 || req_currency > 999) status = CURRENCY;
            else if (req_count_limit > 65535) status = RANGE;
            else if (req_global_spent > req_global_limit) status = DAILY_CAP;
        end else if (!initialized || locked_latched) status = LOCKED;
        else if (req_session != session_id) status = SESSION;
        else if ((admin_op && seq <= last_admin_seq) ||
                 (controller_op && seq <= last_controller_seq) ||
                 (op == OP_PAY && seq <= last_pay_seq)) status = SEQUENCE;
        else begin
            case (op)
                OP_ADD_VENDOR: begin
                    if (vi >= NV) status = RANGE;
                    else if (vendor_used[vi]) status = EXISTS;
                    else if (account == 0 || evidence == 0 || req_vendor_limit == 0)
                        status = ZERO;
                    else if (req_version != 1) status = VERSION_ERR;
                    else if (req_currency != configured_currency) status = CURRENCY;
                    else if (req_vendor_spent > req_vendor_limit) status = VENDOR_CAP;
                    else if (req_vendor_count > max_count) status = COUNT;
                end
                OP_CHANGE_BANK: begin
                    if (vi >= NV) status = RANGE;
                    else if (!vendor_used[vi]) status = VENDOR;
                    else if (!vendor_enabled[vi]) status = DISABLED;
                    else if (account == 0 || evidence == 0) status = ZERO;
                    else if (vendor_version[vi] == 32'hffffffff) status = OVERFLOW;
                    else if (req_version != vendor_version[vi]+1) status = VERSION_ERR;
                    else if (account == vendor_account[vi]) status = ACCOUNT;
                    else if (req_currency != configured_currency) status = CURRENCY;
                    else if (future_time[64]) status = OVERFLOW;
                end
                OP_COMMIT: begin
                    if (vi >= NV || ii >= NI) status = RANGE;
                    else if (!vendor_used[vi]) status = VENDOR;
                    else if (!vendor_enabled[vi]) status = DISABLED;
                    else if (invoice_used[ii]) status = EXISTS;
                    else if (duplicate_hash) status = DUPLICATE;
                    else if (account != vendor_account[vi]) status = ACCOUNT;
                    else if (req_version != vendor_version[vi]) status = VERSION_ERR;
                    else if (req_currency != configured_currency) status = CURRENCY;
                    else if (amount == 0) status = AMOUNT;
                    else if (identity == 0 || evidence == 0) status = ZERO;
                end
                OP_APPROVE, OP_PAY: begin
                    if (vi >= NV || ii >= NI) status = RANGE;
                    else if (!vendor_used[vi]) status = VENDOR;
                    else if (!vendor_enabled[vi]) status = DISABLED;
                    else if (!invoice_used[ii]) status = INVOICE;
                    else if (invoice_paid[ii]) status = PAID;
                    else if (invoice_vendor[ii] != vi || hash_read_data != identity)
                        status = INVOICE;
                    else if (account != vendor_account[vi] || account != invoice_account[ii])
                        status = ACCOUNT;
                    else if (req_version != vendor_version[vi] ||
                             req_version != invoice_version[ii]) status = VERSION_ERR;
                    else if (req_currency != configured_currency ||
                             req_currency != invoice_currency[ii]) status = CURRENCY;
                    else if (amount == 0 || amount != invoice_amount[ii]) status = AMOUNT;
                    else if (req_flags != invoice_flags[ii]) status = FLAGS;
                    else if (op == OP_APPROVE) begin
                        if (ttl == 0 || ttl > 300) status = RANGE;
                        else if (future_time[64]) status = OVERFLOW;
                    end else begin
                        if (current_day != budget_day) status = DAY_ROLLOVER;
                        else if (now_seconds < vendor_hold_until[vi]) status = COOLDOWN;
                        else if (global_sum[64] || vendor_sum[64]) status = OVERFLOW;
                        else if (global_sum[63:0] > global_limit) status = DAILY_CAP;
                        else if (vendor_sum[63:0] > vendor_limit[vi]) status = VENDOR_CAP;
                        else if (vendor_count[vi] >= max_count) status = COUNT;
                        else if ((invoice_flags[ii][0] || vendor_sum[63:0] > threshold) &&
                                 (!invoice_approved[ii] || now_seconds >= invoice_approval_deadline[ii]))
                            status = APPROVAL;
                        else if (authorization_counter == 32'hffffffff) status = OVERFLOW;
                    end
                end
                OP_REBIND: begin
                    if (vi >= NV || ii >= NI) status = RANGE;
                    else if (!vendor_used[vi]) status = VENDOR;
                    else if (!vendor_enabled[vi]) status = DISABLED;
                    else if (!invoice_used[ii]) status = INVOICE;
                    else if (invoice_paid[ii]) status = PAID;
                    else if (invoice_vendor[ii] != vi || hash_read_data != identity)
                        status = INVOICE;
                    else if (account != vendor_account[vi]) status = ACCOUNT;
                    else if (req_version != vendor_version[vi]) status = VERSION_ERR;
                    else if (req_currency != configured_currency ||
                             req_currency != invoice_currency[ii]) status = CURRENCY;
                    else if (amount == 0 || amount != invoice_amount[ii]) status = AMOUNT;
                    else if (req_flags != invoice_flags[ii]) status = FLAGS;
                    else if (evidence == 0) status = ZERO;
                    else if (account == invoice_account[ii] &&
                             req_version == invoice_version[ii]) status = ACCOUNT;
                end
                OP_DISABLE: begin
                    if (vi >= NV) status = RANGE;
                    else if (!vendor_used[vi]) status = VENDOR;
                    else if (!vendor_enabled[vi]) status = DISABLED;
                end
                OP_ADVANCE_DAY: begin
                    if (req_time > now_seconds || req_time/86400 <= budget_day)
                        status = TIME_ERR;
                end
                OP_LOCK: status = OK;
                default: status = BAD_FRAME;
            endcase
        end
    end

    always @* begin
        visible_now = now_seconds;
        visible_spent = global_spent;
        visible_count = 0;
        if (vi < NV) visible_count = vendor_count[vi];
        visible_auth = authorization_counter;
        visible_initialized = initialized;
        if (status == OK) begin
            if (op == OP_INIT) begin
                visible_now = req_time;
                visible_spent = req_global_spent;
                visible_initialized = 1;
            end
            if (op == OP_ADD_VENDOR) visible_count = req_vendor_count;
            if (op == OP_PAY) begin
                visible_spent = global_sum[63:0];
                visible_count = vendor_count[vi] + 1;
                visible_auth = authorization_counter + 1;
            end
            if (op == OP_ADVANCE_DAY) begin
                visible_spent = 0;
                visible_count = 0;
            end
            if (op == OP_LOCK) visible_initialized = 0;
        end
    end

    always @(posedge clk) begin
        if (!rst_n) begin
            initialized <= 0; locked_latched <= 0; session_id <= 0;
            configured_currency <= 840; last_admin_seq <= 0;
            last_controller_seq <= 0; last_pay_seq <= 0; authorization_counter <= 0;
            min_hold <= 0; max_count <= 0; now_seconds <= 0; current_day <= 0;
            budget_day <= 0; seconds_of_day <= 0; global_limit <= 0;
            threshold <= 0; global_spent <= 0; tick_divider <= 0;
            resp_valid <= 0; resp <= 0;
            request_state <= REQUEST_IDLE; command_hold <= 0; auth_hold <= 0;
            scan_index <= 0; duplicate_hash <= 0;
            for (i=0; i<NV; i=i+1) begin
                vendor_used[i] <= 0; vendor_enabled[i] <= 0;
                vendor_spent[i] <= 0; vendor_count[i] <= 0;
            end
            for (i=0; i<NI; i=i+1) begin
                invoice_used[i] <= 0; invoice_paid[i] <= 0; invoice_approved[i] <= 0;
            end
        end else begin
            resp_valid <= 0;
            case (request_state)
                REQUEST_IDLE: if (cmd_valid && cmd_ready) begin
                    command_hold <= cmd; auth_hold <= auth_ok;
                    duplicate_hash <= 0; scan_index <= 0;
                    request_state <= HASH_LOAD;
                end
                HASH_LOAD: request_state <= op == OP_COMMIT ? HASH_SCAN : REQUEST_CHECK;
                HASH_SCAN: begin
                    if (invoice_used[scan_index] && hash_read_data == identity)
                        duplicate_hash <= 1;
                    if (scan_index == NI-1) request_state <= REQUEST_CHECK;
                    else scan_index <= scan_index+1'b1;
                end
                REQUEST_CHECK: if (!clock_overflow) begin
                    request_state <= REQUEST_IDLE; auth_hold <= 0;
                end
                default: request_state <= REQUEST_IDLE;
            endcase
            if (initialized) begin
                if (tick) begin
                    tick_divider <= 0;
                    if (!clock_overflow) begin
                        now_seconds <= now_seconds+1;
                        if (seconds_of_day == 86399) begin
                            seconds_of_day <= 0;
                            current_day <= current_day+1;
                        end else seconds_of_day <= seconds_of_day+1;
                    end
                end else tick_divider <= tick_divider+1;
            end
            if (clock_overflow) begin
                initialized <= 0;
                locked_latched <= 1;
            end else if (request_state == REQUEST_CHECK) begin
                resp <= command_hold;
                resp[0 +:32] <= 32'h53494c52;
                resp[1792 +:32] <= status;
                resp[1824 +:32] <= visible_auth;
                resp[1856 +:64] <= visible_now;
                resp[1920 +:64] <= visible_spent;
                resp[1984 +:32] <= visible_count;
                resp[2016 +:32] <= {31'd0,visible_initialized};
                resp_valid <= 1;
                if (status == OK) begin
                    if (admin_op) last_admin_seq <= seq;
                    if (controller_op) last_controller_seq <= seq;
                    if (op == OP_PAY) last_pay_seq <= seq;
                    case (op)
                        OP_INIT: begin
                            initialized <= 1; session_id <= req_session;
                            configured_currency <= req_currency;
                            now_seconds <= req_time; current_day <= req_time/86400;
                            budget_day <= req_time/86400; seconds_of_day <= req_time%86400;
                            tick_divider <= 0; global_limit <= req_global_limit;
                            threshold <= req_threshold; min_hold <= req_hold;
                            max_count <= req_count_limit; global_spent <= req_global_spent;
                        end
                        OP_ADD_VENDOR: begin
                            vendor_used[vi] <= 1; vendor_enabled[vi] <= 1;
                            vendor_account[vi] <= account; vendor_version[vi] <= 1;
                            vendor_limit[vi] <= req_vendor_limit;
                            vendor_spent[vi] <= req_vendor_spent;
                            vendor_count[vi] <= req_vendor_count;
                            vendor_hold_until[vi] <= now_seconds;
                        end
                        OP_CHANGE_BANK: begin
                            vendor_account[vi] <= account; vendor_version[vi] <= req_version;
                            vendor_hold_until[vi] <= future_time[63:0];
                        end
                        OP_COMMIT: begin
                            invoice_used[ii] <= 1; invoice_paid[ii] <= 0;
                            invoice_approved[ii] <= 0; invoice_vendor[ii] <= vi;
                            invoice_version[ii] <= req_version; invoice_currency[ii] <= req_currency;
                            invoice_flags[ii] <= req_flags; invoice_account[ii] <= account;
                            invoice_amount[ii] <= amount;
                            invoice_approval_deadline[ii] <= 0;
                        end
                        OP_APPROVE: begin
                            invoice_approved[ii] <= 1;
                            invoice_approval_deadline[ii] <= future_time[63:0];
                        end
                        OP_PAY: begin
                            invoice_paid[ii] <= 1; invoice_approved[ii] <= 0;
                            global_spent <= global_sum[63:0];
                            vendor_spent[vi] <= vendor_sum[63:0];
                            vendor_count[vi] <= vendor_count[vi]+1;
                            authorization_counter <= authorization_counter+1;
                        end
                        OP_REBIND: begin
                            invoice_account[ii] <= account;
                            invoice_version[ii] <= req_version;
                            invoice_approved[ii] <= 0;
                            invoice_approval_deadline[ii] <= 0;
                        end
                        OP_DISABLE: vendor_enabled[vi] <= 0;
                        OP_ADVANCE_DAY: begin
                            budget_day <= req_time/86400; global_spent <= 0;
                            for (i=0; i<NV; i=i+1) begin
                                vendor_spent[i] <= 0; vendor_count[i] <= 0;
                            end
                        end
                        OP_LOCK: begin initialized <= 0; locked_latched <= 1; end
                        default: begin end
                    endcase
                end
            end
        end
    end
endmodule
