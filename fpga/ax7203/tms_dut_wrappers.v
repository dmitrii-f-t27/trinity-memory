// Harness wrappers for the FPGA trace player. They replicate the wiring of
// rtl/t27/dot_stream.v and rtl/t27/streams.v around the generated cores
// (build/t27/specs/rtl/*.v) with two differences needed on the device:
//   - `en` is exposed, so the player steps a core one clock at a time while the
//     UART serializes the observation (a core with en=0 holds its state);
//   - `rst_n` (the cores' asynchronous reset) is exposed, so each vector starts
//     from the reset state exactly as a fresh simulation instance does.
// Stimulus and observation words use the packing of tests/tb_spec_dot_trace.v
// and tests/tb_spec_storage_trace.v. Arithmetic and state live in .t27 only.
`timescale 1ns/1ps
`default_nettype none
module tms_dot_dut #(
    parameter integer DENSE5 = 1,
    parameter integer ACC_WIDTH = 32
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        en,
    input  wire [63:0] stim,      // [9:0] code, [49:10] activations, [54:50] mask, [55] last, [56] reset, [57] in_valid, [58] out_ready
    output wire [35:0] observed   // [31:0] result (sign-extended), [32] out_error, [33] out_valid, [34] in_ready
);
    localparam signed [63:0] MIN_VALUE = -(64'sd1 << (ACC_WIDTH - 1));
    localparam signed [63:0] MAX_VALUE = (64'sd1 << (ACC_WIDTH - 1)) - 1;
    wire signed [31:0] result32;
    wire in_ready, out_valid, out_error;
    TrinityDotStreamT27 core (
        .clk(clk), .rst_n(rst_n), .en(en), .ready(),
        .reset(stim[56]), .in_valid(stim[57]), .in_code({6'd0, stim[9:0]}),
        .in_activations({24'd0, stim[49:10]}), .in_mask({3'd0, stim[54:50]}),
        .in_last(stim[55]), .out_ready(stim[58]), .dense_mode(DENSE5 != 0),
        .min_value(MIN_VALUE), .max_value(MAX_VALUE),
        .in_ready(in_ready), .out_valid(out_valid), .out_result(result32), .out_error(out_error)
    );
    // Same narrowing as rtl/t27/dot_stream.v, then the sign extension the trace testbench applies.
    wire signed [ACC_WIDTH-1:0] narrow = result32[ACC_WIDTH-1:0];
    wire signed [31:0] widened;
    generate if (ACC_WIDTH < 32) begin : extend
        assign widened = {{(32 - ACC_WIDTH){narrow[ACC_WIDTH-1]}}, narrow};
    end else begin : full
        assign widened = narrow;
    end endgenerate
    assign observed = {1'b0, in_ready, out_valid, out_error, widened};
endmodule

module tms_storage_dut #(
    parameter integer DENSE5 = 1,
    parameter integer TRIT_COUNT = 12
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        en,
    input  wire [63:0] stim,      // [0] rst, [1] start, [2] load_en, [8:3] load_addr, [18:9] load_code
    output wire [35:0] observed   // [9:0] trits, [14:10] lane mask, [15] code_valid, [16] last, [17] valid, [18] busy, [19] load_ready
);
    localparam integer CODE_WIDTH = (DENSE5 != 0) ? 8 : 10;
    localparam integer WORDS = (TRIT_COUNT + 4) / 5;
    localparam integer ADDR_WIDTH = (WORDS > 1) ? $clog2(WORDS) : 1;
    localparam integer LAST_LANES = TRIT_COUNT - 5 * (WORDS - 1);
    localparam [7:0] LAST_MASK = (1 << LAST_LANES) - 1;
    wire [ADDR_WIDTH-1:0] load_addr = stim[3 +: ADDR_WIDTH];
    wire [CODE_WIDTH-1:0] load_code = stim[9 +: CODE_WIDTH];
    wire [15:0] native_code;
    wire [15:0] visible;
    wire busy, load_ready, out_valid, out_last;
    TrinityStreamStorageT27 core (
        .clk(clk), .rst_n(rst_n), .en(en), .ready(), .reset(stim[0]),
        .start(stim[1]), .load_en(stim[2]), .load_addr({{(32-ADDR_WIDTH){1'b0}}, load_addr}),
        .load_code({{(16-CODE_WIDTH){1'b0}}, load_code}), .word_count(WORDS),
        .busy(busy), .load_ready(load_ready), .out_valid(out_valid),
        .out_last(out_last), .out_code(native_code)
    );
    wire [CODE_WIDTH-1:0] code = native_code[CODE_WIDTH-1:0];
    TrinityStreamViewT27 view (
        .clk(1'b0), .rst_n(1'b1), .en(1'b1), .ready(),
        .code({{(16-CODE_WIDTH){1'b0}}, code}), .valid(out_valid), .last(out_last),
        .tail_mask(LAST_MASK), .dense_mode(DENSE5 != 0), .result(visible)
    );
    assign observed = {16'd0, load_ready, busy, out_valid, out_last, visible[15:0]};
endmodule
`default_nettype wire
