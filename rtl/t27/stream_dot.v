// Wiring adapter: ready-capable native storage -> native join -> framed dot
// pipeline (issue #15). Storage, sequencing, join arbitration, decoding,
// arithmetic and state are native t27 functions/state; this file only wires
// ports and binds elaboration-time parameters.
`timescale 1ns/1ps
`default_nettype none
module ternary_stream_dot_t27 #(
    parameter integer DENSE5 = 1,
    parameter integer TRIT_COUNT = 320,
    parameter integer ACC_WIDTH = 32,
    parameter integer CODE_WIDTH = (DENSE5 != 0) ? 8 : 10,
    parameter integer WORDS = (TRIT_COUNT + 4) / 5,
    parameter integer ADDR_WIDTH = (WORDS > 1) ? $clog2(WORDS) : 1
) (
    input wire clk, rst, start,
    output wire busy,
    input wire load_en,
    input wire [ADDR_WIDTH-1:0] load_addr,
    input wire [CODE_WIDTH-1:0] load_code,
    output wire load_ready,
    input wire act_valid,
    output wire act_ready,
    input wire [39:0] act_data,
    output wire beat,
    output wire out_valid,
    input wire out_ready,
    output wire signed [ACC_WIDTH-1:0] out_result,
    output wire out_error
);
    localparam integer LAST_LANES = TRIT_COUNT - 5 * (WORDS - 1);
    localparam [7:0] LAST_MASK = (1 << LAST_LANES) - 1;
    generate if (TRIT_COUNT < 1 || TRIT_COUNT > 320 || WORDS != (TRIT_COUNT + 4) / 5 ||
                 CODE_WIDTH != ((DENSE5 != 0) ? 8 : 10)) begin : invalid_config
        initial $fatal(1, "ternary_stream_dot_t27 requires TRIT_COUNT=1..320, WORDS=ceil(TRIT_COUNT/5) and the codec's code width");
    end else begin : supported_config
        wire word_valid, word_last, in_ready;
        wire [CODE_WIDTH-1:0] code;
        wire [15:0] joined;
        ternary_stream_storage_t27 #(.CODE_WIDTH(CODE_WIDTH), .WORDS(WORDS), .ADDR_WIDTH(ADDR_WIDTH)) storage (
            .clk(clk), .rst(rst), .start(start), .busy(busy),
            .load_en(load_en), .load_addr(load_addr), .load_code(load_code), .out_ready(joined[1]),
            .load_ready(load_ready), .out_valid(word_valid), .out_last(word_last), .out_code(code)
        );
        TrinityStreamJoinT27 joiner (
            .clk(1'b0), .rst_n(1'b1), .en(1'b1), .ready(),
            .word_valid(word_valid), .word_last(word_last), .act_valid(act_valid),
            .dot_ready(in_ready), .tail_mask(LAST_MASK), .result(joined)
        );
        trinity_dot_stream_t27 #(.DENSE5(DENSE5), .ACC_WIDTH(ACC_WIDTH)) dot (
            .clk(clk), .rst(rst), .in_valid(joined[3]), .in_ready(in_ready),
            .in_code({{(10-CODE_WIDTH){1'b0}}, code}), .in_activations(act_data),
            .in_mask(joined[9:5]), .in_last(joined[4]),
            .out_valid(out_valid), .out_ready(out_ready), .out_result(out_result), .out_error(out_error)
        );
        assign act_ready = joined[2];
        assign beat = joined[0];
    end endgenerate
endmodule
`default_nettype wire
