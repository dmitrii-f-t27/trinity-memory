// Wiring/configuration adapter. Arithmetic and state transitions live in .t27.
`timescale 1ns/1ps
`default_nettype none
module trinity_dot_stream_t27 #(
    parameter integer DENSE5 = 1,
    parameter integer ACC_WIDTH = 32
) (
    input wire clk,
    input wire rst,
    input wire in_valid,
    output wire in_ready,
    input wire [9:0] in_code,
    input wire [39:0] in_activations,
    input wire [4:0] in_mask,
    input wire in_last,
    output wire out_valid,
    input wire out_ready,
    output wire signed [ACC_WIDTH-1:0] out_result,
    output wire out_error
);
    // These are elaboration-time configuration values, not a second datapath.
    localparam signed [63:0] MIN_VALUE = -(64'sd1 << (ACC_WIDTH - 1));
    localparam signed [63:0] MAX_VALUE = (64'sd1 << (ACC_WIDTH - 1)) - 1;
    wire signed [31:0] result32;
    generate if (ACC_WIDTH < 2 || ACC_WIDTH > 32) begin : invalid_width
        initial $fatal(1, "trinity_dot_stream_t27 supports ACC_WIDTH=2..32 only");
    end else begin : supported_width
    TrinityDotStreamT27 core (
        .clk(clk), .rst_n(1'b1), .en(1'b1), .ready(),
        .reset(rst), .in_valid(in_valid), .in_code({6'd0, in_code}),
        .in_activations({24'd0, in_activations}), .in_mask({3'd0, in_mask}),
        .in_last(in_last), .out_ready(out_ready), .dense_mode(DENSE5 != 0),
        .min_value(MIN_VALUE), .max_value(MAX_VALUE),
        .in_ready(in_ready), .out_valid(out_valid), .out_result(result32), .out_error(out_error)
    );
    assign out_result = result32[ACC_WIDTH-1:0];
    end endgenerate
endmodule
`default_nettype wire
