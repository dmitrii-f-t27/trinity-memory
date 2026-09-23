// Out-of-context wrapper of the block-RAM trit codec alone
// (t27/rtl/bram_trit_codec.t27) for `make bram-ooc`: FORMAT 0 = b2, 1 = d5,
// 2 = d5d2; OP 0 = encoder (lanes -> word), 1 = decoder (word -> lanes and the
// count of invalid groups). Both parameters are tied, as in tms_bram_bench.v.
`timescale 1ns/1ps
`default_nettype none
module tms_bram_codec_ooc #(
    parameter integer FORMAT = 0,
    parameter integer OP = 0
) (
    input  wire [63:0] x,
    output wire [63:0] y
);
    TrinityBramTritCodecT27 codec (
        .clk(1'b0), .rst_n(1'b1), .en(1'b1), .ready(),
        .format(FORMAT), .op(OP), .x(x), .result(y)
    );
endmodule
`default_nettype wire
