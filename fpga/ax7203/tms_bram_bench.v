// Wiring adapter of the AX7203 block-RAM trit packing bench. Every state machine
// and every rule is executable t27: the tick and reset generators, the UART
// transmitter and the line emitter of the trace player (t27/rtl/fpga_*.t27), the
// bench sequencer (t27/rtl/fpga_bram_bench.t27), the codec
// (t27/rtl/bram_trit_codec.t27) and the engines, which
// tools/generate-bram-bench.py specializes from t27/rtl/bram_trit_engine.t27.
// This file only instantiates and connects them and the two Xilinx clock
// primitives. Synthesize with `read_verilog -nomem2reg` and the 1K x 36 block RAM
// library (fpga/ax7203/Makefile, target bram-synth).
//
// Engines, one per 36-bit word layout, all storing the same trit stream:
//   format 0: b2   (18 trits per word, two bits per trit)
//   format 1: d5   (20 trits per word, four dense5 bytes, bits 35:32 unused)
//   format 2: d5d2 (22 trits per word, four dense5 bytes and a dense2 nibble in bits 35:32)
// ONLY = -1 builds all three (slots 0, 1, 2 = formats 0, 1, 2); ONLY = 0, 1 or 2
// builds that format alone in slot 0, which keeps the placement small enough for
// nextpnr-xilinx to route (all three need 150 RAMB36E1 and ~9k LUTs).
// Report lines: see t27/rtl/fpga_bram_bench.t27. Clocking as in the trace player
// (CLOCK_MODE 1: 25 MHz from the tick generator's divide-by-eight bit on a second
// BUFG; 0: 200 MHz with a one-in-eight tick enable).
// LEDs: [0] heartbeat, [1] run active, [2] run complete, [3] any bad word or invalid group.
`timescale 1ns/1ps
`default_nettype none
module tms_bram_bench_ax7203 #(
    parameter integer CLOCK_MODE = 1,
    parameter integer TICK_DIV = 8,         // fixed by the tick generator (three-bit counter)
    parameter integer BAUD_DIV = 217,       // ticks per UART bit (25 M / 115200 = 217.01)
    parameter integer ONLY = -1,            // -1: all three layouts; 0, 1, 2: one layout
    parameter [31:0] BUILD_ID = 32'h0
) (
    input  wire       clk200_p,
    input  wire       clk200_n,
    input  wire       rst_n,
    output wire [3:0] led,
    output wire       uart_tx,
    input  wire       uart_rx
);
    // ---- clocks ----
    wire clk200_raw, clk200, clk, tick, tick_raw, div_bit;
    IBUFDS clk_ibufds (.I(clk200_p), .IB(clk200_n), .O(clk200_raw));
    BUFG clk200_bufg (.I(clk200_raw), .O(clk200));
    TrinityFpgaTickT27 tickgen (
        .clk(clk200), .rst_n(1'b1), .en(1'b1), .ready(), .hold(1'b0),
        .count(), .tick(tick_raw), .div_bit(div_bit)
    );
    generate if (CLOCK_MODE != 0) begin : divided_clock
        BUFG clk_bufg (.I(div_bit), .O(clk));
        assign tick = 1'b1;
    end else begin : tick_enable
        assign clk = clk200;
        assign tick = tick_raw;
    end endgenerate

    // ---- reset and heartbeat ----
    wire rst, heartbeat;
    TrinityFpgaResetT27 resetgen (
        .clk(clk), .rst_n(1'b1), .en(tick), .ready(), .button_n(rst_n),
        .stage1(), .stage2(), .hold(), .blink(), .reset(rst), .heartbeat(heartbeat)
    );

    // ---- UART transmitter and line emitter ----
    wire        tx_start, tx_busy, line_idle, line_go;
    wire [31:0] tx_byte, line_tag, line_a;
    wire [63:0] line_b;
    TrinityFpgaUartTxT27 uart (
        .clk(clk), .rst_n(!rst), .en(tick), .ready(),
        .start(tx_start), .data(tx_byte), .baud_div(BAUD_DIV),
        .busy(tx_busy), .shift(), .count(), .bits(), .tx(uart_tx)
    );
    TrinityFpgaLineEmitterT27 emitter (
        .clk(clk), .rst_n(!rst), .en(tick), .ready(),
        .go(line_go), .tag(line_tag), .a(line_a), .b(line_b), .tx_busy(tx_busy),
        .line_busy(), .pos(), .tag_q(), .a_q(), .b_q(), .tx_start(tx_start), .tx_byte(tx_byte), .idle(line_idle)
    );

    // ---- engine slots: encoder, block-RAM store, decoder ----
    // Slot i reports as engine i; an empty slot is done with zero results.
    wire        start;
    wire        s0_done;
    wire [31:0] s0_words, s0_fmt, s0_k, s0_wt, s0_rt, s0_bad, s0_inv, s0_pos, s0_neg;
    wire signed [63:0] s0_dot;
    wire [63:0] s0_chk;
    wire        s1_done;
    wire [31:0] s1_words, s1_fmt, s1_k, s1_wt, s1_rt, s1_bad, s1_inv, s1_pos, s1_neg;
    wire signed [63:0] s1_dot;
    wire [63:0] s1_chk;
    wire        s2_done;
    wire [31:0] s2_words, s2_fmt, s2_k, s2_wt, s2_rt, s2_bad, s2_inv, s2_pos, s2_neg;
    wire signed [63:0] s2_dot;
    wire [63:0] s2_chk;
    wire [31:0] engines = (ONLY < 0) ? 32'd3 : 32'd1;

    // Slot 0 holds format F0 (all three: b2; one layout: that layout); slots 1 and 2
    // hold d5 and d5d2 when all three are built, and are empty otherwise.
    localparam integer F0 = (ONLY < 0) ? 0 : ONLY;
    generate
        if (F0 == 0) begin : slot0_b2
            wire [63:0] lanes, word, rdata, dec;
            TrinityBramTritCodecT27 enc (.clk(clk), .rst_n(1'b1), .en(1'b1), .ready(), .format(32'd0), .op(32'd0), .x(lanes), .result(word));
            TrinityBramTritCodecT27 dec_c (.clk(clk), .rst_n(1'b1), .en(1'b1), .ready(), .format(32'd0), .op(32'd1), .x(rdata), .result(dec));
            TrinityBramTritEngineB2T27 eng (
                .clk(clk), .rst_n(!rst), .en(tick), .ready(),
                .start(start), .enc_word(word), .dec_out(dec), .rdata(rdata), .enc_lanes(lanes),
                .done(s0_done), .words_out(s0_words), .format_out(s0_fmt), .lanes_out(s0_k),
                .write_ticks(s0_wt), .read_ticks(s0_rt), .bad_words(s0_bad), .invalid_groups(s0_inv),
                .pos(s0_pos), .neg(s0_neg), .dot(s0_dot), .chk(s0_chk)
            );
        end else if (F0 == 1) begin : slot0_d5
            wire [63:0] lanes, word, rdata, dec;
            TrinityBramTritCodecT27 enc (.clk(clk), .rst_n(1'b1), .en(1'b1), .ready(), .format(32'd1), .op(32'd0), .x(lanes), .result(word));
            TrinityBramTritCodecT27 dec_c (.clk(clk), .rst_n(1'b1), .en(1'b1), .ready(), .format(32'd1), .op(32'd1), .x(rdata), .result(dec));
            TrinityBramTritEngineD5T27 eng (
                .clk(clk), .rst_n(!rst), .en(tick), .ready(),
                .start(start), .enc_word(word), .dec_out(dec), .rdata(rdata), .enc_lanes(lanes),
                .done(s0_done), .words_out(s0_words), .format_out(s0_fmt), .lanes_out(s0_k),
                .write_ticks(s0_wt), .read_ticks(s0_rt), .bad_words(s0_bad), .invalid_groups(s0_inv),
                .pos(s0_pos), .neg(s0_neg), .dot(s0_dot), .chk(s0_chk)
            );
        end else begin : slot0_d5d2
            wire [63:0] lanes, word, rdata, dec;
            TrinityBramTritCodecT27 enc (.clk(clk), .rst_n(1'b1), .en(1'b1), .ready(), .format(32'd2), .op(32'd0), .x(lanes), .result(word));
            TrinityBramTritCodecT27 dec_c (.clk(clk), .rst_n(1'b1), .en(1'b1), .ready(), .format(32'd2), .op(32'd1), .x(rdata), .result(dec));
            TrinityBramTritEngineD5D2T27 eng (
                .clk(clk), .rst_n(!rst), .en(tick), .ready(),
                .start(start), .enc_word(word), .dec_out(dec), .rdata(rdata), .enc_lanes(lanes),
                .done(s0_done), .words_out(s0_words), .format_out(s0_fmt), .lanes_out(s0_k),
                .write_ticks(s0_wt), .read_ticks(s0_rt), .bad_words(s0_bad), .invalid_groups(s0_inv),
                .pos(s0_pos), .neg(s0_neg), .dot(s0_dot), .chk(s0_chk)
            );
        end
        if (ONLY < 0) begin : slot1_d5
            wire [63:0] lanes, word, rdata, dec;
            TrinityBramTritCodecT27 enc (.clk(clk), .rst_n(1'b1), .en(1'b1), .ready(), .format(32'd1), .op(32'd0), .x(lanes), .result(word));
            TrinityBramTritCodecT27 dec_c (.clk(clk), .rst_n(1'b1), .en(1'b1), .ready(), .format(32'd1), .op(32'd1), .x(rdata), .result(dec));
            TrinityBramTritEngineD5T27 eng (
                .clk(clk), .rst_n(!rst), .en(tick), .ready(),
                .start(start), .enc_word(word), .dec_out(dec), .rdata(rdata), .enc_lanes(lanes),
                .done(s1_done), .words_out(s1_words), .format_out(s1_fmt), .lanes_out(s1_k),
                .write_ticks(s1_wt), .read_ticks(s1_rt), .bad_words(s1_bad), .invalid_groups(s1_inv),
                .pos(s1_pos), .neg(s1_neg), .dot(s1_dot), .chk(s1_chk)
            );
        end else begin : slot1_empty
            assign s1_done = 1'b1;
            assign s1_words = 32'd0; assign s1_fmt = 32'd0; assign s1_k = 32'd0; assign s1_wt = 32'd0;
            assign s1_rt = 32'd0; assign s1_bad = 32'd0; assign s1_inv = 32'd0; assign s1_pos = 32'd0;
            assign s1_neg = 32'd0; assign s1_dot = 64'sd0; assign s1_chk = 64'd0;
        end
        if (ONLY < 0) begin : slot2_d5d2
            wire [63:0] lanes, word, rdata, dec;
            TrinityBramTritCodecT27 enc (.clk(clk), .rst_n(1'b1), .en(1'b1), .ready(), .format(32'd2), .op(32'd0), .x(lanes), .result(word));
            TrinityBramTritCodecT27 dec_c (.clk(clk), .rst_n(1'b1), .en(1'b1), .ready(), .format(32'd2), .op(32'd1), .x(rdata), .result(dec));
            TrinityBramTritEngineD5D2T27 eng (
                .clk(clk), .rst_n(!rst), .en(tick), .ready(),
                .start(start), .enc_word(word), .dec_out(dec), .rdata(rdata), .enc_lanes(lanes),
                .done(s2_done), .words_out(s2_words), .format_out(s2_fmt), .lanes_out(s2_k),
                .write_ticks(s2_wt), .read_ticks(s2_rt), .bad_words(s2_bad), .invalid_groups(s2_inv),
                .pos(s2_pos), .neg(s2_neg), .dot(s2_dot), .chk(s2_chk)
            );
        end else begin : slot2_empty
            assign s2_done = 1'b1;
            assign s2_words = 32'd0; assign s2_fmt = 32'd0; assign s2_k = 32'd0; assign s2_wt = 32'd0;
            assign s2_rt = 32'd0; assign s2_bad = 32'd0; assign s2_inv = 32'd0; assign s2_pos = 32'd0;
            assign s2_neg = 32'd0; assign s2_dot = 64'sd0; assign s2_chk = 64'd0;
        end
    endgenerate

    // ---- bench sequencer ----
    wire run_active, run_done, any_bad;
    TrinityFpgaBramBenchT27 bench (
        .clk(clk), .rst_n(!rst), .en(tick), .ready(),
        .uart_rx(uart_rx), .line_idle(line_idle), .engines(engines),
        .d0(s0_done), .words0(s0_words), .fmt0(s0_fmt), .lanes0(s0_k), .wt0(s0_wt), .rt0(s0_rt),
        .bad0(s0_bad), .inv0(s0_inv), .pos0(s0_pos), .neg0(s0_neg), .dot0(s0_dot), .chk0(s0_chk),
        .d1(s1_done), .words1(s1_words), .fmt1(s1_fmt), .lanes1(s1_k), .wt1(s1_wt), .rt1(s1_rt),
        .bad1(s1_bad), .inv1(s1_inv), .pos1(s1_pos), .neg1(s1_neg), .dot1(s1_dot), .chk1(s1_chk),
        .d2(s2_done), .words2(s2_words), .fmt2(s2_fmt), .lanes2(s2_k), .wt2(s2_wt), .rt2(s2_rt),
        .bad2(s2_bad), .inv2(s2_inv), .pos2(s2_pos), .neg2(s2_neg), .dot2(s2_dot), .chk2(s2_chk),
        .engines_start(start), .line_go(line_go), .line_tag(line_tag), .line_a(line_a), .line_b(line_b),
        .run_done(run_done), .any_bad(any_bad), .run_active(run_active)
    );

    assign led = {any_bad, run_done, run_active, heartbeat};
endmodule
`default_nettype wire
