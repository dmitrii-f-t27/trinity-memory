// Wiring adapter of the AX7203 UART loader (issue #63, part 1: block RAM). Every
// state machine and rule is executable t27: the tick and reset generators, the
// UART transmitter and the line emitter of the benches (t27/rtl/fpga_*.t27), the
// receiver (t27/rtl/fpga_uart_rx.t27), the loader with its receive FIFO, frame
// parser, CRC-32, staging buffer, commit, read-back and responses
// (t27/rtl/fpga_uart_loader.t27) and the block-RAM store behind its write and
// read ports (t27/rtl/fpga_loader_store.t27). This file only instantiates and
// connects them and the two Xilinx clock primitives. Synthesize with
// `read_verilog -nomem2reg` (fpga/ax7203/Makefile, target loader-synth).
//
// Protocol: docs/uart-loader.md. Clocking as in the block-RAM bench
// (fpga/ax7203/tms_bram_bench.v): CLOCK_MODE 1 is 25 MHz from the tick
// generator's divide-by-eight bit on a second BUFG, 2 and 3 are 50 and 100 MHz.
// BAUD_DIV is clocks per UART bit after reset (217 for 115200 at 25 MHz); the
// host can change it with a 'B' frame and the loader returns to BAUD_DIV unless
// a good frame arrives within PROBATION_CLOCKS. TIMEOUT_CLOCKS is the inter-byte
// timeout that drops a partial frame (1 250 000 = 50 ms at 25 MHz).
// The transmitter is shared: the line emitter sends the response lines, the
// loader itself sends the bytes of a read-back frame, only while the emitter is
// idle (the loader never asks for a line while it sends a frame), so the two
// start signals are never high together.
// LEDs: [0] heartbeat, [1] a baud rate other than BAUD_DIV is in use,
// [2] a chunk has been committed since reset, [3] a nak has been sent since reset.
`timescale 1ns/1ps
`default_nettype none
module tms_uart_loader_ax7203 #(
    parameter integer CLOCK_MODE = 1,
    parameter integer BAUD_DIV = 217,              // clocks per UART bit (25 M / 115200 = 217.01)
    parameter integer TIMEOUT_CLOCKS = 1250000,    // inter-byte timeout inside a frame
    parameter integer PROBATION_CLOCKS = 75000000, // a new baud rate must be confirmed within this
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
    wire [31:0] tick_count;
    IBUFDS clk_ibufds (.I(clk200_p), .IB(clk200_n), .O(clk200_raw));
    BUFG clk200_bufg (.I(clk200_raw), .O(clk200));
    TrinityFpgaTickT27 tickgen (
        .clk(clk200), .rst_n(1'b1), .en(1'b1), .ready(), .hold(1'b0),
        .count(tick_count), .tick(tick_raw), .div_bit(div_bit)
    );
    generate if (CLOCK_MODE == 2 || CLOCK_MODE == 3) begin : fast_clock
        BUFG clk_bufg (.I(tick_count[3 - CLOCK_MODE]), .O(clk));
        assign tick = 1'b1;
    end else if (CLOCK_MODE != 0) begin : divided_clock
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

    // ---- receiver ----
    wire [31:0] div_now;
    wire        rx_valid, rx_ferr;
    wire [31:0] rx_data, rx_bytes, rx_ferrs, rx_false;
    TrinityFpgaUartRxT27 receiver (
        .clk(clk), .rst_n(!rst), .en(tick), .ready(),
        .uart_rx(uart_rx), .baud_div(div_now),
        .valid(rx_valid), .data(rx_data), .ferr(rx_ferr),
        .bytes(rx_bytes), .framing_errors(rx_ferrs), .false_starts(rx_false)
    );

    // ---- transmitter, line emitter ----
    wire        em_tx_start, ld_tx_start, tx_busy, line_idle, line_go;
    wire [31:0] em_tx_byte, ld_tx_byte, line_tag, line_a;
    wire [63:0] line_b;
    TrinityFpgaUartTxT27 transmitter (
        .clk(clk), .rst_n(!rst), .en(tick), .ready(),
        .start(em_tx_start | ld_tx_start), .data(ld_tx_start ? ld_tx_byte : em_tx_byte), .baud_div(div_now),
        .busy(tx_busy), .shift(), .count(), .bits(), .tx(uart_tx)
    );
    TrinityFpgaLineEmitterT27 emitter (
        .clk(clk), .rst_n(!rst), .en(tick), .ready(),
        .go(line_go), .tag(line_tag), .a(line_a), .b(line_b), .tx_busy(tx_busy),
        .line_busy(), .pos(), .tag_q(), .a_q(), .b_q(), .tx_start(em_tx_start), .tx_byte(em_tx_byte), .idle(line_idle)
    );

    // ---- store: the write and read ports of the loader ----
    wire        wr_valid, wr_last, wr_ready, wr_idle, rd_req, rd_ready, rd_valid;
    wire [31:0] wr_addr, wr_data, rd_addr, store_log2;
    wire [7:0]  rd_byte;
    TrinityFpgaLoaderStoreT27 store (
        .clk(clk), .rst_n(!rst), .en(tick), .ready(),
        .wr_valid(wr_valid), .wr_addr(wr_addr), .wr_data(wr_data), .rd_req(rd_req), .rd_addr(rd_addr),
        .rdata(rd_byte), .rd_valid(rd_valid), .writes(),
        .wr_ready(wr_ready), .wr_idle(wr_idle), .rd_ready(rd_ready), .size_log2(store_log2)
    );

    // ---- loader ----
    wire committed_any, any_nak, baud_sel;
    TrinityFpgaUartLoaderT27 loader (
        .clk(clk), .rst_n(!rst), .en(tick), .ready(),
        .rx_valid(rx_valid), .rx_data(rx_data), .rx_ferr(rx_ferr),
        .rx_bytes(rx_bytes), .rx_ferrs(rx_ferrs), .rx_false(rx_false),
        .line_idle(line_idle), .tx_busy(tx_busy), .wr_ready(wr_ready), .wr_idle(wr_idle),
        .rd_ready(rd_ready), .rd_valid(rd_valid), .rd_data({24'd0, rd_byte}),
        .store_log2(store_log2), .default_div(BAUD_DIV), .timeout_clocks(TIMEOUT_CLOCKS),
        .probation_clocks(PROBATION_CLOCKS), .build_id(BUILD_ID),
        .line_go(line_go), .line_tag(line_tag), .line_a(line_a), .line_b(line_b),
        .tx_start(ld_tx_start), .tx_byte(ld_tx_byte),
        .wr_valid(wr_valid), .wr_addr(wr_addr), .wr_data(wr_data), .wr_last(wr_last),
        .rd_req(rd_req), .rd_addr(rd_addr),
        .div_now(div_now), .baud_sel(baud_sel), .committed_any(committed_any), .any_nak(any_nak)
    );

    assign led = {any_nak, committed_any, baud_sel, heartbeat};
endmodule
`default_nettype wire
