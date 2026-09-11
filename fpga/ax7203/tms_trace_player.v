// Wiring adapter of the AX7203 trace player. Every state machine and every rule
// is executable t27 in t27/rtl/fpga_*.t27 (tick generator, reset generator,
// UART transmitter, line emitter, sequencer, Edge argmax) and in the generated
// ROM core build/fpga/fpga_trace_rom.t27; the cores under test are the
// generated stream modules. This file only instantiates and connects them and
// the two Xilinx clock primitives.
//
// Report line format (20 bytes): tag, 8 hex digits (a), 10 hex digits (b), LF.
//   H a=0                 b={format 2, total cycles[15:0], vector count[15:0]}
//   V a=vector index      b=cycles      C a=cycle index  b=observed word
//   E a=vector index      b=device mismatches (stepped)   K a=counter index b=value
//   F a=vector index      b=device mismatches (free-run)
//   W a=0 b=frames        R a=frame b=result (two's complement)   T a=index b=total
//   X a=fixture           b={latency ticks[..3], label[2:0] (7 = ambiguous)}
//   A a=fixture*4+row     b=accumulator (two's complement)
//   D a=total stepped mismatches b=total free-run mismatches
// Clocking, two variants of one design selected by CLOCK_MODE (both from the
// 200 MHz LVDS oscillator through IBUFDS and BUFG, no PLL): 1 runs everything
// on a second BUFG fed by the t27 tick generator's divide-by-eight bit (25 MHz,
// every register enabled every clock); 0 runs everything on the 200 MHz clock
// with every register enabled by the generator's tick, one clock in eight.
// LEDs: [0] heartbeat, [1] run active, [2] run complete, [3] any mismatch.
`timescale 1ns/1ps
`default_nettype none
module tms_trace_player_ax7203 #(
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

    // ---- sequencer ----
    wire [31:0] vector, addr, wl_load_addr, edge_load_row, edge_load_addr, edge_ra, edge_fb;
    wire [63:0] stim_q, wl_act_data;
    wire        dut_en, dut_reset, run_active, run_done, mism_seen;
    wire        wl_reset, wl_start, wl_load_en, wl_act_valid;
    wire        edge_reset, edge_load_en, edge_start, edge_act_valid, edge_out_ready;
    wire [63:0] stimulus;
    wire [39:0] expected, observed, edge_beat_data;
    wire [31:0] entry;
    wire [9:0]  vector_count, cycle_count;
    wire [7:0]  wl_code, edge_code, workload_frames;
    wire [2:0]  edge_fixtures;
    wire        wl_busy, wl_act_ready, wl_beat, wl_out_valid;
    wire signed [31:0] wl_result, edge_acc0, edge_acc1, edge_acc2;
    wire        edge_act_ready, edge_beat, edge_done, edge_ambiguous;
    wire [1:0]  edge_label;
    TrinityFpgaTracePlayerT27 player (
        .clk(clk), .rst_n(!rst), .en(tick), .ready(),
        .uart_rx(uart_rx), .line_idle(line_idle),
        .stimulus(stimulus), .expected({24'd0, expected}), .entry(entry),
        .vector_count({22'd0, vector_count}), .cycle_count({22'd0, cycle_count}),
        .observed({24'd0, observed}), .workload_frames({24'd0, workload_frames}),
        .wl_busy(wl_busy), .wl_act_ready(wl_act_ready), .wl_beat(wl_beat), .wl_out_valid(wl_out_valid), .wl_result(wl_result),
        .edge_fixtures({29'd0, edge_fixtures}), .edge_act_ready(edge_act_ready), .edge_beat(edge_beat), .edge_done(edge_done),
        .edge_acc0(edge_acc0), .edge_acc1(edge_acc1), .edge_acc2(edge_acc2),
        .edge_label(edge_ambiguous ? 32'd7 : {30'd0, edge_label}),
        .state(), .after_line(), .vector(vector), .addr(addr), .cyc(), .stim_q(stim_q), .exp_q(), .obs_q(), .pre_q(),
        .dut_en(dut_en), .dut_reset(dut_reset), .line_go(line_go), .line_tag(line_tag), .line_a(line_a), .line_b(line_b),
        .mism(), .mism_total(), .free_total(), .kidx(), .wait_cnt(), .auto_started(), .trigger_pending(),
        .run_done(run_done), .mism_seen(mism_seen), .free_first(), .rx1(), .rx2(), .rx3(),
        .wl_reset(wl_reset), .wl_start(wl_start), .wl_load_en(wl_load_en), .wl_load_addr(wl_load_addr),
        .wl_act_valid(wl_act_valid), .wl_beat_index(), .wl_frame(), .wl_ticks(), .wl_beats(), .wl_stalls(), .wl_done(), .wl_load_ticks(),
        .edge_reset(edge_reset), .edge_load_en(edge_load_en), .edge_load_row(edge_load_row), .edge_load_addr(edge_load_addr),
        .edge_start(edge_start), .edge_act_valid(edge_act_valid), .edge_beat_index(), .edge_fixture(), .edge_out_ready(edge_out_ready),
        .edge_ticks(), .edge_acc0_q(), .edge_acc1_q(), .edge_acc2_q(), .edge_label_q(),
        .run_active(run_active), .wl_act_data(wl_act_data), .wl_out_ready(), .edge_ra(edge_ra), .edge_fb(edge_fb),
        .vbase(), .vcycles(), .vkind()
    );

    // ---- tables and cores under test ----
    tms_trace_bank bank (
        .clk(clk), .dut_rst_n(!dut_reset), .step(dut_en && tick), .vector(vector[9:0]), .addr(addr[9:0]),
        .stim_q(stim_q), .stimulus(stimulus), .expected(expected), .entry(entry),
        .vector_count(vector_count), .cycle_count(cycle_count), .observed(observed),
        .wl_addr(wl_load_addr[5:0]), .wl_code(wl_code), .edge_ra(edge_ra[3:0]), .edge_code(edge_code),
        .edge_fb(edge_fb[4:0]), .edge_beat(edge_beat_data), .workload_frames(workload_frames), .edge_fixtures(edge_fixtures)
    );

    // ---- throughput workload: the joined path with 64 stored words ----
    tms_join_path #(.DENSE5(1), .TRIT_COUNT(320), .ACC_WIDTH(32)) workload (
        .clk(clk), .rst_n(!wl_reset), .en(tick), .rst(1'b0), .start(wl_start),
        .load_en(wl_load_en), .load_addr(wl_load_addr[5:0]), .load_code({2'd0, wl_code}),
        .act_valid(wl_act_valid), .act_data(wl_act_data[39:0]), .out_ready(1'b1),
        .busy(wl_busy), .load_ready(), .act_ready(wl_act_ready), .beat(wl_beat),
        .out_valid(wl_out_valid), .out_error(), .out_result(wl_result)
    );

    // ---- Edge Demo: three template rows in lockstep, t27 argmax ----
    tms_edge_datapath edge_demo (
        .clk(clk), .rst_n(!edge_reset), .en(tick), .rst(1'b0),
        .load_en(edge_load_en), .load_row(edge_load_row[1:0]), .load_addr(edge_load_addr[1:0]), .load_code(edge_code),
        .start(edge_start), .act_valid(edge_act_valid), .act_data(edge_beat_data), .out_ready(edge_out_ready),
        .busy(), .act_ready(edge_act_ready), .beat(edge_beat), .done(edge_done),
        .acc0(edge_acc0), .acc1(edge_acc1), .acc2(edge_acc2), .label(edge_label), .ambiguous(edge_ambiguous)
    );

    assign led = {mism_seen, run_done, run_active, heartbeat};
endmodule
`default_nettype wire
