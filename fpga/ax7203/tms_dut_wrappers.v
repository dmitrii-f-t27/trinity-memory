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
    output wire [39:0] observed   // [31:0] result (sign-extended), [32] out_error, [33] out_valid, [34] in_ready
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
    assign observed = {5'd0, in_ready, out_valid, out_error, widened};
endmodule

module tms_storage_dut #(
    parameter integer DENSE5 = 1,
    parameter integer TRIT_COUNT = 12
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        en,
    input  wire [63:0] stim,      // [0] rst, [1] start, [2] load_en, [8:3] load_addr, [18:9] load_code, [19] out_stall
    output wire [39:0] observed   // [9:0] trits, [14:10] lane mask, [15] code_valid, [16] last, [17] valid, [18] busy, [19] load_ready
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
        .out_ready(!stim[19]),
        .busy(busy), .load_ready(load_ready), .out_valid(out_valid),
        .out_last(out_last), .out_code(native_code)
    );
    wire [CODE_WIDTH-1:0] code = native_code[CODE_WIDTH-1:0];
    TrinityStreamViewT27 view (
        .clk(1'b0), .rst_n(1'b1), .en(1'b1), .ready(),
        .code({{(16-CODE_WIDTH){1'b0}}, code}), .valid(out_valid), .last(out_last),
        .tail_mask(LAST_MASK), .dense_mode(DENSE5 != 0), .result(visible)
    );
    assign observed = {20'd0, load_ready, busy, out_valid, out_last, visible[15:0]};
endmodule
`default_nettype wire

// Joined path (rtl/t27/stream_dot.v wiring) with the cores' en and rst_n exposed:
// ready-capable storage -> combinational join -> framed dot pipeline.
`default_nettype none
module tms_join_path #(
    parameter integer DENSE5 = 1,
    parameter integer TRIT_COUNT = 12,
    parameter integer ACC_WIDTH = 32
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        en,
    input  wire        rst,
    input  wire        start,
    input  wire        load_en,
    input  wire [5:0]  load_addr,
    input  wire [9:0]  load_code,
    input  wire        act_valid,
    input  wire [39:0] act_data,
    input  wire        out_ready,
    output wire        busy,
    output wire        load_ready,
    output wire        act_ready,
    output wire        beat,
    output wire        out_valid,
    output wire        out_error,
    output wire signed [31:0] out_result   // sign-extended from ACC_WIDTH
);
    localparam integer CODE_WIDTH = (DENSE5 != 0) ? 8 : 10;
    localparam integer WORDS = (TRIT_COUNT + 4) / 5;
    localparam integer ADDR_WIDTH = (WORDS > 1) ? $clog2(WORDS) : 1;
    localparam integer LAST_LANES = TRIT_COUNT - 5 * (WORDS - 1);
    localparam [7:0] LAST_MASK = (1 << LAST_LANES) - 1;
    localparam signed [63:0] MIN_VALUE = -(64'sd1 << (ACC_WIDTH - 1));
    localparam signed [63:0] MAX_VALUE = (64'sd1 << (ACC_WIDTH - 1)) - 1;
    wire [ADDR_WIDTH-1:0] addr = load_addr[ADDR_WIDTH-1:0];
    wire [CODE_WIDTH-1:0] code_in = load_code[CODE_WIDTH-1:0];
    wire [15:0] native_code;
    wire word_valid, word_last, in_ready;
    wire [15:0] joined;
    TrinityStreamStorageT27 storage (
        .clk(clk), .rst_n(rst_n), .en(en), .ready(), .reset(rst),
        .start(start), .load_en(load_en), .load_addr({{(32-ADDR_WIDTH){1'b0}}, addr}),
        .load_code({{(16-CODE_WIDTH){1'b0}}, code_in}), .word_count(WORDS), .out_ready(joined[1]),
        .busy(busy), .load_ready(load_ready), .out_valid(word_valid), .out_last(word_last), .out_code(native_code)
    );
    TrinityStreamJoinT27 joiner (
        .clk(1'b0), .rst_n(1'b1), .en(1'b1), .ready(),
        .word_valid(word_valid), .word_last(word_last), .act_valid(act_valid),
        .dot_ready(in_ready), .tail_mask(LAST_MASK), .result(joined)
    );
    wire signed [31:0] result32;
    TrinityDotStreamT27 dot (
        .clk(clk), .rst_n(rst_n), .en(en), .ready(),
        .reset(rst), .in_valid(joined[3]), .in_code({6'd0, native_code[9:0]}),
        .in_activations({24'd0, act_data}), .in_mask({3'd0, joined[9:5]}),
        .in_last(joined[4]), .out_ready(out_ready), .dense_mode(DENSE5 != 0),
        .min_value(MIN_VALUE), .max_value(MAX_VALUE),
        .in_ready(in_ready), .out_valid(out_valid), .out_result(result32), .out_error(out_error)
    );
    wire signed [ACC_WIDTH-1:0] narrow = result32[ACC_WIDTH-1:0];
    generate if (ACC_WIDTH < 32) begin : extend
        assign out_result = {{(32 - ACC_WIDTH){narrow[ACC_WIDTH-1]}}, narrow};
    end else begin : full
        assign out_result = narrow;
    end endgenerate
    assign act_ready = joined[2];
    assign beat = joined[0];
endmodule

// Trace wrapper of the joined path. Stimulus and observation words use the packing of
// tests/tb_spec_join_trace.v: stim [0] rst, [1] start, [2] load_en, [8:3] load_addr,
// [18:9] load_code, [19] act_valid, [59:20] act_data, [60] out_ready; observed [31:0]
// result, [32] out_error, [33] out_valid, [34] act_ready, [35] load_ready, [36] busy, [37] beat.
module tms_join_dut #(
    parameter integer DENSE5 = 1,
    parameter integer TRIT_COUNT = 12,
    parameter integer ACC_WIDTH = 32
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        en,
    input  wire [63:0] stim,
    output wire [39:0] observed
);
    wire busy, load_ready, act_ready, beat, out_valid, out_error;
    wire signed [31:0] out_result;
    tms_join_path #(.DENSE5(DENSE5), .TRIT_COUNT(TRIT_COUNT), .ACC_WIDTH(ACC_WIDTH)) path (
        .clk(clk), .rst_n(rst_n), .en(en), .rst(stim[0]), .start(stim[1]),
        .load_en(stim[2]), .load_addr(stim[8:3]), .load_code(stim[18:9]),
        .act_valid(stim[19]), .act_data(stim[59:20]), .out_ready(stim[60]),
        .busy(busy), .load_ready(load_ready), .act_ready(act_ready), .beat(beat),
        .out_valid(out_valid), .out_error(out_error), .out_result(out_result)
    );
    assign observed = {2'd0, beat, busy, load_ready, act_ready, out_valid, out_error, out_result};
endmodule

// Edge Demo datapath (wiring): the three template rows of specs/memory/edge_demo.t27
// each in their own joined path (dense5, 12 trits = 3 words), fed the same activation
// beats in lockstep; the label comes from the t27 argmax core. Templates are loaded
// through load_row/load_addr/load_code.
module tms_edge_datapath (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        en,
    input  wire        rst,
    input  wire        load_en,
    input  wire [1:0]  load_row,
    input  wire [1:0]  load_addr,
    input  wire [7:0]  load_code,
    input  wire        start,
    input  wire        act_valid,
    input  wire [39:0] act_data,
    input  wire        out_ready,
    output wire        busy,
    output wire        act_ready,
    output wire        beat,
    output wire        done,          // all three accumulators valid
    output wire signed [31:0] acc0,
    output wire signed [31:0] acc1,
    output wire signed [31:0] acc2,
    output wire [1:0]  label,         // 0..2, or 3 when ambiguous
    output wire        ambiguous
);
    wire [2:0] busy_v, ready_v, beat_v, valid_v;
    wire signed [31:0] acc [0:2];
    wire all_ready = &ready_v;
    genvar r;
    generate for (r = 0; r < 3; r = r + 1) begin : row
        tms_join_path #(.DENSE5(1), .TRIT_COUNT(12), .ACC_WIDTH(32)) path (
            .clk(clk), .rst_n(rst_n), .en(en), .rst(rst), .start(start),
            .load_en(load_en && (load_row == r)), .load_addr({4'd0, load_addr}), .load_code({2'd0, load_code}),
            .act_valid(act_valid && all_ready), .act_data(act_data), .out_ready(out_ready),
            .busy(busy_v[r]), .load_ready(), .act_ready(ready_v[r]), .beat(beat_v[r]),
            .out_valid(valid_v[r]), .out_error(), .out_result(acc[r])
        );
    end endgenerate
    assign busy = |busy_v;
    assign act_ready = all_ready;
    assign beat = beat_v[0];
    assign done = &valid_v;
    assign acc0 = acc[0];
    assign acc1 = acc[1];
    assign acc2 = acc[2];
    // The label rule is executable t27 (t27/rtl/fpga_edge_argmax.t27): strict maximum, ties ambiguous (7).
    wire [15:0] verdict;
    TrinityFpgaEdgeArgmaxT27 argmax (
        .clk(1'b0), .rst_n(1'b1), .en(1'b1), .ready(),
        .acc0(acc[0]), .acc1(acc[1]), .acc2(acc[2]), .result(verdict)
    );
    assign ambiguous = verdict == 16'd7;
    assign label = ambiguous ? 2'd3 : verdict[1:0];
endmodule
`default_nettype wire
