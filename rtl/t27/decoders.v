// Wiring-only adapters to native .t27 decoder functions. Do not add algorithms.
`timescale 1ns/1ps
`default_nettype none
module ternary_dense5_decoder_t27 (
    input wire [7:0] code, output wire [9:0] trits, output wire valid
);
    wire [15:0] result;
    TrinityDense5DecoderT27 core (
        .clk(1'b0), .rst_n(1'b1), .en(1'b1), .code(code), .ready(), .result(result)
    );
    assign trits = result[9:0];
    assign valid = result[10];
endmodule

module ternary_baseline5_decoder_t27 (
    input wire [9:0] code, output wire [9:0] trits, output wire valid
);
    wire [15:0] result;
    TrinityBaseline5DecoderT27 core (
        .clk(1'b0), .rst_n(1'b1), .en(1'b1), .code({6'd0, code}), .ready(), .result(result)
    );
    assign trits = result[9:0];
    assign valid = result[10];
endmodule

module ternary_sparse41_decoder_t27 (
    input wire [3:0] code, output wire [7:0] trits, output wire valid
);
    wire [15:0] result;
    TrinitySparse41DecoderT27 core (
        .clk(1'b0), .rst_n(1'b1), .en(1'b1), .code({4'd0, code}), .ready(), .result(result)
    );
    assign trits = result[7:0];
    assign valid = result[8];
endmodule
`default_nettype wire
