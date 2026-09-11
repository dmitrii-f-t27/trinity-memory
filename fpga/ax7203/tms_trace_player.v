// On-device trace player for the AX7203 measurement track (issue #10).
//
// Replays every dot_trace and storage_trace vector of
// conformance/memory_stream_compute.json on the generated cores, cycle by
// cycle, and reports what the device observed over the UART so the host
// (tools/fpga-capture.py) can compare it with the reference independently.
//
// Two passes per run:
//   stepped   each trace cycle is applied with `en` high for exactly one clock,
//             the outputs are captured with the same stimulus still applied
//             (the sampling rule of tests/tb_spec_*_trace.v), compared with the
//             expected ROM word and written as a `C` line;
//   free-run  the same vectors are replayed without the UART pauses, two
//             harness clocks per trace cycle (one with `en` high, one holding
//             the stimulus while the outputs are sampled, because in_ready and
//             load_ready are combinational in the inputs); only the on-device
//             mismatch count per vector is reported (`F` lines).
//
// Line format (19 bytes): tag, 8 hex digits (a), 9 hex digits (b), LF.
//   H a=BUILD_ID          b={4'h1, total cycles[15:0], vector count[15:0]}
//   V a=vector index      b=cycles in the vector
//   C a=cycle index       b=observed word (packing of the expected word)
//   E a=vector index      b=device mismatches in the stepped pass
//   K a=counter index     b=counter value (six per vector, see the manifest)
//   F a=vector index      b=device mismatches in the free-run pass
//   D a=total stepped mismatches b=total free-run mismatches
// A run starts after configuration and again whenever a byte arrives on uart_rx.
// LEDs: [0] heartbeat, [1] run active, [2] run complete, [3] any mismatch.
`timescale 1ns/1ps
`default_nettype none
module tms_trace_player_ax7203 #(
    parameter integer CLK_DIV = 4,          // 200 MHz LVDS input divided to the harness clock (power of two)
    parameter integer BAUD_DIV = 434,       // harness clocks per UART bit (50 MHz / 115200)
    parameter [31:0] BUILD_ID = 32'h0
) (
    input  wire       clk200_p,
    input  wire       clk200_n,
    input  wire       rst_n,
    output wire [3:0] led,
    output wire       uart_tx,
    input  wire       uart_rx
);
    // ---- clocking: IBUFDS -> BUFG -> divider register -> BUFG ----
    localparam integer DIV_BITS = (CLK_DIV >= 2) ? $clog2(CLK_DIV) : 1;
    wire clk200_raw, clk200, clk;
    IBUFDS clk_ibufds (.I(clk200_p), .IB(clk200_n), .O(clk200_raw));
    BUFG clk200_bufg (.I(clk200_raw), .O(clk200));
    reg [DIV_BITS-1:0] div = {DIV_BITS{1'b0}};
    always @(posedge clk200) div <= div + 1'b1;
    BUFG clk_bufg (.I(div[DIV_BITS-1]), .O(clk));

    // ---- reset: power-on counter plus the synchronized active-low button ----
    reg [1:0] rst_sync = 2'b00;
    reg [7:0] por = 8'd0;
    always @(posedge clk) begin
        rst_sync <= {rst_sync[0], rst_n};
        if (!por[7]) por <= por + 1'b1;
    end
    wire rst = !por[7] || !rst_sync[1];

    // ---- UART receive trigger: any start bit restarts the run ----
    reg [2:0] rx_sync = 3'b111;
    always @(posedge clk) rx_sync <= {rx_sync[1:0], uart_rx};
    wire rx_falling = rx_sync[2] && !rx_sync[1];

    // ---- UART transmit and the 19-byte line emitter ----
    reg        tx_start = 1'b0;
    reg [7:0]  tx_byte = 8'd0;
    wire       tx_busy;
    tms_uart_tx #(.BAUD_DIV(BAUD_DIV)) uart (
        .clk(clk), .rst(rst), .data(tx_byte), .start(tx_start), .busy(tx_busy), .tx(uart_tx)
    );
    reg        line_go = 1'b0;
    reg [7:0]  line_tag = 8'd0;
    reg [31:0] line_a = 32'd0;
    reg [35:0] line_b = 36'd0;
    reg        line_busy = 1'b0;
    reg [4:0]  line_pos = 5'd0;
    function [7:0] hex;
        input [3:0] nibble;
        hex = (nibble < 4'd10) ? (8'h30 + {4'd0, nibble}) : (8'h57 + {4'd0, nibble});
    endfunction
    function [7:0] line_char;
        input [4:0]  pos;
        input [7:0]  tag;
        input [31:0] a;
        input [35:0] b;
        begin
            if (pos == 5'd0)
                line_char = tag;
            else if (pos <= 5'd8)
                line_char = hex(a[(5'd8 - pos) * 4 +: 4]);
            else if (pos <= 5'd17)
                line_char = hex(b[(5'd17 - pos) * 4 +: 4]);
            else
                line_char = 8'h0a;
        end
    endfunction
    wire [7:0] line_byte = line_char(line_pos, line_tag, line_a, line_b);
    always @(posedge clk) begin
        tx_start <= 1'b0;
        if (rst) begin
            line_busy <= 1'b0;
            line_pos <= 5'd0;
        end else if (!line_busy) begin
            if (line_go) begin
                line_busy <= 1'b1;
                line_pos <= 5'd0;
            end
        end else if (!tx_busy && !tx_start) begin
            tx_byte <= line_byte;
            tx_start <= 1'b1;
            if (line_pos == 5'd18)
                line_busy <= 1'b0;
            else
                line_pos <= line_pos + 1'b1;
        end
    end
    wire line_idle = !line_busy && !tx_busy && !tx_start;

    // ---- trace bank: ROMs, vector table and the cores ----
    reg  [9:0]  vector = 10'd0;
    reg  [9:0]  addr = 10'd0;
    reg  [63:0] stim_q = 64'd0;
    reg         dut_en = 1'b0;
    reg         dut_rst_n = 1'b0;
    wire [63:0] rom_stim;
    wire [35:0] rom_exp;
    wire [9:0]  vbase, vcycles, vector_count, cycle_count;
    wire        vkind;
    wire [3:0]  vdut;
    wire [35:0] observed;
    tms_trace_bank bank (
        .clk(clk), .dut_rst_n(dut_rst_n), .step(dut_en), .vector(vector), .addr(addr),
        .stim_q(stim_q), .stimulus(rom_stim), .expected(rom_exp), .vector_base(vbase),
        .vector_cycles(vcycles), .vector_kind(vkind), .vector_dut(vdut),
        .vector_count(vector_count), .cycle_count(cycle_count), .observed(observed)
    );

    // ---- sequencer ----
    localparam [4:0] S_IDLE = 5'd0, S_LINE_START = 5'd1, S_LINE_WAIT = 5'd2,
                     S_VEC_BEGIN = 5'd3, S_VEC_RESET = 5'd4,
                     S_STEP_APPLY = 5'd5, S_STEP_ENABLE = 5'd6, S_STEP_WAIT = 5'd7,
                     S_STEP_CAPTURE = 5'd8, S_STEP_COMPARE = 5'd9, S_STEP_NEXT = 5'd10,
                     S_VEC_END = 5'd11, S_VEC_K = 5'd12, S_VEC_K_NEXT = 5'd13,
                     S_FREE_BEGIN = 5'd14, S_FREE_VEC = 5'd15, S_FREE_RESET = 5'd16,
                     S_FREE_LOAD = 5'd17, S_FREE_CONSUME = 5'd18, S_FREE_DRAIN = 5'd19,
                     S_FREE_REPORT = 5'd20, S_FREE_NEXT = 5'd21, S_DONE = 5'd22;
    reg [4:0]  state = S_IDLE;
    reg [4:0]  after_line = S_IDLE;
    reg [9:0]  cyc = 10'd0;
    reg [35:0] obs_q = 36'd0;
    reg [35:0] exp_q = 36'd0;
    reg [31:0] mism = 32'd0;
    reg [31:0] mism_total = 32'd0;
    reg [31:0] free_total = 32'd0;
    reg [31:0] counter [0:5];
    reg [2:0]  kidx = 3'd0;
    reg [2:0]  wait_cnt = 3'd0;
    reg        auto_started = 1'b0;
    reg        run_done = 1'b0;
    reg        mism_seen = 1'b0;
    reg [23:0] heartbeat = 24'd0;
    // outputs as the core sees them at the consuming edge (stimulus applied,
    // state not yet advanced): the handshake values the counters are defined on
    reg [35:0] pre_q = 36'd0;
    reg        free_first = 1'b0;
    reg        trigger_pending = 1'b0;
    integer k;
    function [3:0] popcount5;
        input [4:0] bits;
        popcount5 = {3'd0, bits[0]} + {3'd0, bits[1]} + {3'd0, bits[2]} + {3'd0, bits[3]} + {3'd0, bits[4]};
    endfunction

    always @(posedge clk) begin
        heartbeat <= heartbeat + 1'b1;
        line_go <= 1'b0;
        if (rx_falling)
            trigger_pending <= 1'b1;
        if (rst) begin
            state <= S_IDLE;
            dut_en <= 1'b0;
            dut_rst_n <= 1'b0;
            auto_started <= 1'b0;
            trigger_pending <= 1'b0;
            run_done <= 1'b0;
            mism_seen <= 1'b0;
        end else begin
            case (state)
                S_IDLE: begin
                    dut_en <= 1'b0;
                    if (trigger_pending || !auto_started) begin
                        auto_started <= 1'b1;
                        trigger_pending <= 1'b0;
                        run_done <= 1'b0;
                        mism_seen <= 1'b0;
                        vector <= 10'd0;
                        mism_total <= 32'd0;
                        free_total <= 32'd0;
                        line_tag <= "H"; line_a <= BUILD_ID;
                        line_b <= {4'h1, 6'd0, cycle_count, 6'd0, vector_count};
                        line_go <= 1'b1; after_line <= S_VEC_BEGIN; state <= S_LINE_START;
                    end
                end
                S_LINE_START: state <= S_LINE_WAIT;
                S_LINE_WAIT: if (line_idle) state <= after_line;
                S_VEC_BEGIN: begin
                    if (vector == vector_count) begin
                        state <= S_FREE_BEGIN;
                    end else begin
                        dut_rst_n <= 1'b0;
                        dut_en <= 1'b0;
                        wait_cnt <= 3'd0;
                        cyc <= 10'd0;
                        addr <= vbase;
                        mism <= 32'd0;
                        for (k = 0; k < 6; k = k + 1) counter[k] <= 32'd0;
                        state <= S_VEC_RESET;
                    end
                end
                S_VEC_RESET: begin
                    wait_cnt <= wait_cnt + 1'b1;
                    if (wait_cnt == 3'd3) begin
                        dut_rst_n <= 1'b1;
                        line_tag <= "V"; line_a <= {22'd0, vector}; line_b <= {26'd0, vcycles};
                        line_go <= 1'b1; after_line <= S_STEP_APPLY; state <= S_LINE_START;
                    end
                end
                S_STEP_APPLY: begin
                    stim_q <= rom_stim;
                    exp_q <= rom_exp;
                    state <= S_STEP_ENABLE;
                end
                S_STEP_ENABLE: begin
                    dut_en <= 1'b1;
                    state <= S_STEP_WAIT;
                end
                S_STEP_WAIT: begin
                    // this edge is the consuming edge: `observed` still holds the
                    // outputs the core evaluates its handshakes on
                    pre_q <= observed;
                    dut_en <= 1'b0;
                    state <= S_STEP_CAPTURE;
                end
                S_STEP_CAPTURE: begin
                    obs_q <= observed;
                    state <= S_STEP_COMPARE;
                end
                S_STEP_COMPARE: begin
                    if (obs_q != exp_q) begin
                        mism <= mism + 1'b1;
                        mism_seen <= 1'b1;
                    end
                    if (!vkind) begin
                        counter[0] <= counter[0] + {31'd0, stim_q[57] && pre_q[34]};
                        counter[1] <= counter[1] + {31'd0, stim_q[58] && pre_q[33]};
                        counter[2] <= counter[2] + {31'd0, stim_q[57] && !pre_q[34]};
                        counter[3] <= counter[3] + {31'd0, pre_q[33] && !stim_q[58]};
                        counter[4] <= counter[4] + {31'd0, stim_q[58] && pre_q[33] && pre_q[32]};
                        counter[5] <= counter[5] + {31'd0, stim_q[56]};
                    end else begin
                        counter[0] <= counter[0] + {31'd0, stim_q[2] && pre_q[19]};
                        counter[1] <= counter[1] + {31'd0, obs_q[17]};
                        counter[2] <= counter[2] + (obs_q[17] ? {28'd0, popcount5(obs_q[14:10])} : 32'd0);
                        counter[3] <= counter[3] + {31'd0, obs_q[17] && !obs_q[15]};
                        counter[4] <= counter[4] + {31'd0, stim_q[1] && !pre_q[18] && !stim_q[0]};
                        counter[5] <= counter[5] + {31'd0, stim_q[0]};
                    end
                    line_tag <= "C"; line_a <= {22'd0, cyc}; line_b <= obs_q;
                    line_go <= 1'b1; after_line <= S_STEP_NEXT; state <= S_LINE_START;
                end
                S_STEP_NEXT: begin
                    cyc <= cyc + 1'b1;
                    addr <= addr + 1'b1;
                    if (cyc + 10'd1 == vcycles)
                        state <= S_VEC_END;
                    else
                        state <= S_STEP_APPLY;
                end
                S_VEC_END: begin
                    mism_total <= mism_total + mism;
                    kidx <= 3'd0;
                    line_tag <= "E"; line_a <= {22'd0, vector}; line_b <= {4'd0, mism};
                    line_go <= 1'b1; after_line <= S_VEC_K; state <= S_LINE_START;
                end
                S_VEC_K: begin
                    line_tag <= "K"; line_a <= {29'd0, kidx}; line_b <= {4'd0, counter[kidx]};
                    line_go <= 1'b1; after_line <= S_VEC_K_NEXT; state <= S_LINE_START;
                end
                S_VEC_K_NEXT: begin
                    if (kidx == 3'd5) begin
                        vector <= vector + 1'b1;
                        state <= S_VEC_BEGIN;
                    end else begin
                        kidx <= kidx + 1'b1;
                        state <= S_VEC_K;
                    end
                end
                S_FREE_BEGIN: begin
                    vector <= 10'd0;
                    state <= S_FREE_VEC;
                end
                S_FREE_VEC: begin
                    if (vector == vector_count) begin
                        line_tag <= "D"; line_a <= mism_total; line_b <= {4'd0, free_total};
                        line_go <= 1'b1; after_line <= S_DONE; state <= S_LINE_START;
                    end else begin
                        dut_rst_n <= 1'b0;
                        dut_en <= 1'b0;
                        wait_cnt <= 3'd0;
                        cyc <= 10'd0;
                        addr <= vbase;
                        mism <= 32'd0;
                        free_first <= 1'b1;
                        state <= S_FREE_RESET;
                    end
                end
                S_FREE_RESET: begin
                    wait_cnt <= wait_cnt + 1'b1;
                    if (wait_cnt == 3'd3) begin
                        dut_rst_n <= 1'b1;
                        state <= S_FREE_LOAD;
                    end
                end
                S_FREE_LOAD: begin
                    // `observed` still shows the previous trace cycle with its stimulus applied
                    if (!free_first && observed != exp_q) begin
                        mism <= mism + 1'b1;
                        mism_seen <= 1'b1;
                    end
                    free_first <= 1'b0;
                    stim_q <= rom_stim;
                    exp_q <= rom_exp;
                    dut_en <= 1'b1;
                    addr <= addr + 1'b1;
                    state <= S_FREE_CONSUME;
                end
                S_FREE_CONSUME: begin
                    dut_en <= 1'b0;
                    cyc <= cyc + 1'b1;
                    if (cyc + 10'd1 == vcycles)
                        state <= S_FREE_DRAIN;
                    else
                        state <= S_FREE_LOAD;
                end
                S_FREE_DRAIN: begin
                    if (observed != exp_q) begin
                        mism <= mism + 1'b1;
                        mism_seen <= 1'b1;
                    end
                    state <= S_FREE_REPORT;
                end
                S_FREE_REPORT: begin
                    free_total <= free_total + mism;
                    line_tag <= "F"; line_a <= {22'd0, vector}; line_b <= {4'd0, mism};
                    line_go <= 1'b1; after_line <= S_FREE_NEXT; state <= S_LINE_START;
                end
                S_FREE_NEXT: begin
                    vector <= vector + 1'b1;
                    state <= S_FREE_VEC;
                end
                S_DONE: begin
                    run_done <= 1'b1;
                    state <= S_IDLE;
                end
                default: state <= S_IDLE;
            endcase
        end
    end
    assign led = {mism_seen, run_done, state != S_IDLE, heartbeat[23]};
endmodule
`default_nettype wire
