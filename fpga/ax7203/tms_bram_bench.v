// Wiring adapter of the AX7203 block-RAM trit packing bench. Every state machine
// and every rule is executable t27: the tick and reset generators, the UART
// transmitter and the line emitter of the trace player (t27/rtl/fpga_*.t27), the
// bench sequencer (t27/rtl/fpga_bram_bench.t27), the codec
// (t27/rtl/bram_trit_codec.t27) and the three engines, which
// tools/generate-bram-bench.py specializes from t27/rtl/bram_trit_engine.t27.
// This file only instantiates and connects them and the two Xilinx clock
// primitives. Synthesize with `read_verilog -nomem2reg` so the engines' arrays
// become block RAM (fpga/ax7203/Makefile, target bram-synth).
//
// Engines, one per 36-bit word layout, all storing the same trit stream:
//   0: b2  (18 trits per word, two bits per trit)
//   1: d5  (20 trits per word, four dense5 bytes, bits 35:32 unused)
//   2: d5d2 (22 trits per word, four dense5 bytes and a dense2 nibble in bits 35:32)
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

    // ---- engines: encoder, block-RAM store, decoder ----
    wire        start;
    wire [63:0] lanes_b2, word_b2, rdata_b2, dec_b2;
    wire [63:0] lanes_d5, word_d5, rdata_d5, dec_d5;
    wire [63:0] lanes_d5d2, word_d5d2, rdata_d5d2, dec_d5d2;
    wire        done_b2, done_d5, done_d5d2;
    wire [31:0] words_b2, fmt_b2, k_b2, wt_b2, rt_b2, bad_b2, inv_b2, pos_b2, neg_b2;
    wire [31:0] words_d5, fmt_d5, k_d5, wt_d5, rt_d5, bad_d5, inv_d5, pos_d5, neg_d5;
    wire [31:0] words_d5d2, fmt_d5d2, k_d5d2, wt_d5d2, rt_d5d2, bad_d5d2, inv_d5d2, pos_d5d2, neg_d5d2;
    wire signed [63:0] dot_b2, dot_d5, dot_d5d2;
    wire [63:0] chk_b2, chk_d5, chk_d5d2;

    TrinityBramTritCodecT27 enc_b2 (.clk(clk), .rst_n(1'b1), .en(1'b1), .ready(), .format(32'd0), .op(32'd0), .x(lanes_b2), .result(word_b2));
    TrinityBramTritCodecT27 dec_b2c (.clk(clk), .rst_n(1'b1), .en(1'b1), .ready(), .format(32'd0), .op(32'd1), .x(rdata_b2), .result(dec_b2));
    TrinityBramTritEngineB2T27 eng_b2 (
        .clk(clk), .rst_n(!rst), .en(tick), .ready(),
        .start(start), .enc_word(word_b2), .dec_out(dec_b2),
        .rdata(rdata_b2), .enc_lanes(lanes_b2), .done(done_b2),
        .words_out(words_b2), .format_out(fmt_b2), .lanes_out(k_b2),
        .write_ticks(wt_b2), .read_ticks(rt_b2), .bad_words(bad_b2), .invalid_groups(inv_b2),
        .pos(pos_b2), .neg(neg_b2), .dot(dot_b2), .chk(chk_b2)
    );

    TrinityBramTritCodecT27 enc_d5 (.clk(clk), .rst_n(1'b1), .en(1'b1), .ready(), .format(32'd1), .op(32'd0), .x(lanes_d5), .result(word_d5));
    TrinityBramTritCodecT27 dec_d5c (.clk(clk), .rst_n(1'b1), .en(1'b1), .ready(), .format(32'd1), .op(32'd1), .x(rdata_d5), .result(dec_d5));
    TrinityBramTritEngineD5T27 eng_d5 (
        .clk(clk), .rst_n(!rst), .en(tick), .ready(),
        .start(start), .enc_word(word_d5), .dec_out(dec_d5),
        .rdata(rdata_d5), .enc_lanes(lanes_d5), .done(done_d5),
        .words_out(words_d5), .format_out(fmt_d5), .lanes_out(k_d5),
        .write_ticks(wt_d5), .read_ticks(rt_d5), .bad_words(bad_d5), .invalid_groups(inv_d5),
        .pos(pos_d5), .neg(neg_d5), .dot(dot_d5), .chk(chk_d5)
    );

    TrinityBramTritCodecT27 enc_d5d2 (.clk(clk), .rst_n(1'b1), .en(1'b1), .ready(), .format(32'd2), .op(32'd0), .x(lanes_d5d2), .result(word_d5d2));
    TrinityBramTritCodecT27 dec_d5d2c (.clk(clk), .rst_n(1'b1), .en(1'b1), .ready(), .format(32'd2), .op(32'd1), .x(rdata_d5d2), .result(dec_d5d2));
    TrinityBramTritEngineD5D2T27 eng_d5d2 (
        .clk(clk), .rst_n(!rst), .en(tick), .ready(),
        .start(start), .enc_word(word_d5d2), .dec_out(dec_d5d2),
        .rdata(rdata_d5d2), .enc_lanes(lanes_d5d2), .done(done_d5d2),
        .words_out(words_d5d2), .format_out(fmt_d5d2), .lanes_out(k_d5d2),
        .write_ticks(wt_d5d2), .read_ticks(rt_d5d2), .bad_words(bad_d5d2), .invalid_groups(inv_d5d2),
        .pos(pos_d5d2), .neg(neg_d5d2), .dot(dot_d5d2), .chk(chk_d5d2)
    );

    // ---- bench sequencer ----
    wire run_active, run_done, any_bad;
    TrinityFpgaBramBenchT27 bench (
        .clk(clk), .rst_n(!rst), .en(tick), .ready(),
        .uart_rx(uart_rx), .line_idle(line_idle),
        .d0(done_b2), .words0(words_b2), .fmt0(fmt_b2), .lanes0(k_b2), .wt0(wt_b2), .rt0(rt_b2),
        .bad0(bad_b2), .inv0(inv_b2), .pos0(pos_b2), .neg0(neg_b2), .dot0(dot_b2), .chk0(chk_b2),
        .d1(done_d5), .words1(words_d5), .fmt1(fmt_d5), .lanes1(k_d5), .wt1(wt_d5), .rt1(rt_d5),
        .bad1(bad_d5), .inv1(inv_d5), .pos1(pos_d5), .neg1(neg_d5), .dot1(dot_d5), .chk1(chk_d5),
        .d2(done_d5d2), .words2(words_d5d2), .fmt2(fmt_d5d2), .lanes2(k_d5d2), .wt2(wt_d5d2), .rt2(rt_d5d2),
        .bad2(bad_d5d2), .inv2(inv_d5d2), .pos2(pos_d5d2), .neg2(neg_d5d2), .dot2(dot_d5d2), .chk2(chk_d5d2),
        .engines_start(start), .line_go(line_go), .line_tag(line_tag), .line_a(line_a), .line_b(line_b),
        .run_done(run_done), .any_bad(any_bad), .run_active(run_active)
    );

    assign led = {any_bad, run_done, run_active, heartbeat};
endmodule
`default_nettype wire
