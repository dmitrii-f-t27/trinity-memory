// Actual payload memory width is 8 bits per five trits.
`timescale 1ns/1ps
`default_nettype none
module ternary_dense5_stream #(
    parameter integer TRIT_COUNT = 320,
    parameter integer WORDS = (TRIT_COUNT + 4) / 5,
    parameter integer ADDR_WIDTH = (WORDS > 1) ? $clog2(WORDS) : 1
) (
    input wire clk,
    input wire rst,
    input wire start,
    output wire busy,
    input wire load_en,
    input wire [ADDR_WIDTH-1:0] load_addr,
    input wire [7:0] load_code,
    output wire load_ready,
    output wire out_valid,
    output wire out_last,
    output wire out_code_valid,
    output wire [4:0] out_lane_mask,
    output wire [9:0] out_trits
);
    localparam integer LAST_LANES = TRIT_COUNT - 5 * (WORDS - 1);
    localparam [4:0] LAST_MASK = (1 << LAST_LANES) - 1;
    wire [7:0] code;
    wire [9:0] decoded;
    wire code_valid;

    ternary_stream_storage #(
        .CODE_WIDTH(8), .WORDS(WORDS), .ADDR_WIDTH(ADDR_WIDTH)
    ) storage (
        .clk(clk), .rst(rst), .start(start), .busy(busy),
        .load_en(load_en), .load_addr(load_addr), .load_code(load_code),
        .load_ready(load_ready), .out_valid(out_valid), .out_last(out_last), .out_code(code)
    );
    ternary_dense5_decoder decoder (.code(code), .trits(decoded), .valid(code_valid));
    assign out_code_valid = out_valid && code_valid;
    assign out_lane_mask = out_valid ? (out_last ? LAST_MASK : 5'b11111) : 5'd0;
    genvar lane;
    generate
        for (lane = 0; lane < 5; lane = lane + 1) begin : mask_output
            assign out_trits[lane*2 +: 2] =
                (out_code_valid && out_lane_mask[lane]) ? decoded[lane*2 +: 2] : 2'b00;
        end
    endgenerate
endmodule
`default_nettype wire
