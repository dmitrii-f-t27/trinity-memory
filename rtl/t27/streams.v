// Wiring/configuration adapters only. Storage, sequencing, decode and masking
// are native t27 functions/state. Physical native RAM capacity is 64 groups.
`timescale 1ns/1ps
`default_nettype none
module ternary_stream_storage_t27 #(
    parameter integer CODE_WIDTH = 8,
    parameter integer WORDS = 64,
    parameter integer ADDR_WIDTH = (WORDS > 1) ? $clog2(WORDS) : 1
) (
    input wire clk, rst, start,
    output wire busy,
    input wire load_en,
    input wire [ADDR_WIDTH-1:0] load_addr,
    input wire [CODE_WIDTH-1:0] load_code,
    input wire out_ready,
    output wire load_ready, out_valid, out_last,
    output wire [CODE_WIDTH-1:0] out_code
);
    generate if ((CODE_WIDTH != 8 && CODE_WIDTH != 10) || WORDS < 1 || WORDS > 64 ||
                 ADDR_WIDTH < 1 || ADDR_WIDTH > 32 || (64'd1 << ADDR_WIDTH) < WORDS) begin : invalid_config
        initial $fatal(1, "ternary_stream_storage_t27 requires CODE_WIDTH=8/10, WORDS=1..64 and sufficient ADDR_WIDTH=1..32");
    end else begin : supported_config
        wire [15:0] native_code;
        TrinityStreamStorageT27 core (
            .clk(clk), .rst_n(1'b1), .en(1'b1), .ready(), .reset(rst),
            .start(start), .load_en(load_en), .load_addr({{(32-ADDR_WIDTH){1'b0}}, load_addr}),
            .load_code({{(16-CODE_WIDTH){1'b0}}, load_code}), .word_count(WORDS),
            .out_ready(out_ready),
            .busy(busy), .load_ready(load_ready), .out_valid(out_valid),
            .out_last(out_last), .out_code(native_code)
        );
        assign out_code = native_code[CODE_WIDTH-1:0];
    end endgenerate
endmodule

module ternary_stream_adapter_t27 #(
    parameter integer DENSE5 = 1,
    parameter integer CODE_WIDTH = 8,
    parameter integer TRIT_COUNT = 320,
    parameter integer WORDS = (TRIT_COUNT + 4) / 5,
    parameter integer ADDR_WIDTH = (WORDS > 1) ? $clog2(WORDS) : 1
) (
    input wire clk, rst, start,
    output wire busy,
    input wire load_en,
    input wire [ADDR_WIDTH-1:0] load_addr,
    input wire [CODE_WIDTH-1:0] load_code,
    input wire out_ready,
    output wire load_ready, out_valid, out_last, out_code_valid,
    output wire [4:0] out_lane_mask,
    output wire [9:0] out_trits
);
    localparam integer LAST_LANES = TRIT_COUNT - 5 * (WORDS - 1);
    localparam [7:0] LAST_MASK = (1 << LAST_LANES) - 1;
    generate if (TRIT_COUNT < 1 || TRIT_COUNT > 320 || WORDS != (TRIT_COUNT + 4) / 5) begin : invalid_count
        initial $fatal(1, "native t27 streams require TRIT_COUNT=1..320 and WORDS=ceil(TRIT_COUNT/5)");
    end else begin : supported_count
        wire [CODE_WIDTH-1:0] code;
        wire [15:0] visible;
        ternary_stream_storage_t27 #(.CODE_WIDTH(CODE_WIDTH), .WORDS(WORDS), .ADDR_WIDTH(ADDR_WIDTH)) storage (
            .clk(clk), .rst(rst), .start(start), .busy(busy),
            .load_en(load_en), .load_addr(load_addr), .load_code(load_code), .out_ready(out_ready),
            .load_ready(load_ready), .out_valid(out_valid), .out_last(out_last), .out_code(code)
        );
        TrinityStreamViewT27 view (
            .clk(1'b0), .rst_n(1'b1), .en(1'b1), .ready(),
            .code({{(16-CODE_WIDTH){1'b0}}, code}), .valid(out_valid), .last(out_last),
            .tail_mask(LAST_MASK), .dense_mode(DENSE5 != 0), .result(visible)
        );
        assign out_trits = visible[9:0];
        assign out_lane_mask = visible[14:10];
        assign out_code_valid = visible[15];
    end endgenerate
endmodule

module ternary_dense5_stream_t27 #(
    parameter integer TRIT_COUNT = 320,
    parameter integer WORDS = (TRIT_COUNT + 4) / 5,
    parameter integer ADDR_WIDTH = (WORDS > 1) ? $clog2(WORDS) : 1
) (
    input wire clk, rst, start,
    output wire busy,
    input wire load_en,
    input wire [ADDR_WIDTH-1:0] load_addr,
    input wire [7:0] load_code,
    output wire load_ready, out_valid, out_last, out_code_valid,
    output wire [4:0] out_lane_mask,
    output wire [9:0] out_trits
);
    ternary_stream_adapter_t27 #(.DENSE5(1), .CODE_WIDTH(8), .TRIT_COUNT(TRIT_COUNT),
        .WORDS(WORDS), .ADDR_WIDTH(ADDR_WIDTH)) adapter (
        .clk(clk), .rst(rst), .start(start), .busy(busy), .load_en(load_en),
        .load_addr(load_addr), .load_code(load_code), .out_ready(1'b1), .load_ready(load_ready),
        .out_valid(out_valid), .out_last(out_last), .out_code_valid(out_code_valid),
        .out_lane_mask(out_lane_mask), .out_trits(out_trits)
    );
endmodule

module ternary_baseline5_stream_t27 #(
    parameter integer TRIT_COUNT = 320,
    parameter integer WORDS = (TRIT_COUNT + 4) / 5,
    parameter integer ADDR_WIDTH = (WORDS > 1) ? $clog2(WORDS) : 1
) (
    input wire clk, rst, start,
    output wire busy,
    input wire load_en,
    input wire [ADDR_WIDTH-1:0] load_addr,
    input wire [9:0] load_code,
    output wire load_ready, out_valid, out_last, out_code_valid,
    output wire [4:0] out_lane_mask,
    output wire [9:0] out_trits
);
    ternary_stream_adapter_t27 #(.DENSE5(0), .CODE_WIDTH(10), .TRIT_COUNT(TRIT_COUNT),
        .WORDS(WORDS), .ADDR_WIDTH(ADDR_WIDTH)) adapter (
        .clk(clk), .rst(rst), .start(start), .busy(busy), .load_en(load_en),
        .load_addr(load_addr), .load_code(load_code), .out_ready(1'b1), .load_ready(load_ready),
        .out_valid(out_valid), .out_last(out_last), .out_code_valid(out_code_valid),
        .out_lane_mask(out_lane_mask), .out_trits(out_trits)
    );
endmodule
`default_nettype wire
