// Out-of-context wrapper of one block-RAM trit engine with its encoder and
// decoder (the same instances as in tms_bram_bench.v), so yosys can report the
// cells of each word layout separately: FORMAT 0 = b2, 1 = d5, 2 = d5d2. Every
// result the bench reports is an output, so nothing the bench keeps is trimmed.
`timescale 1ns/1ps
`default_nettype none
module tms_bram_ooc #(
    parameter integer FORMAT = 0
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        start,
    output wire        done,
    output wire [31:0] write_ticks,
    output wire [31:0] read_ticks,
    output wire [31:0] bad_words,
    output wire [31:0] invalid_groups,
    output wire [31:0] pos,
    output wire [31:0] neg,
    output wire [63:0] dot,
    output wire [63:0] chk
);
    wire [63:0] lanes, word, rdata, dec;
    TrinityBramTritCodecT27 enc (.clk(clk), .rst_n(1'b1), .en(1'b1), .ready(), .format(FORMAT), .op(32'd0), .x(lanes), .result(word));
    TrinityBramTritCodecT27 dec_c (.clk(clk), .rst_n(1'b1), .en(1'b1), .ready(), .format(FORMAT), .op(32'd1), .x(rdata), .result(dec));
    generate if (FORMAT == 0) begin : b2
        TrinityBramTritEngineB2T27 eng (.clk(clk), .rst_n(rst_n), .en(1'b1), .ready(), .start(start), .enc_word(word), .dec_out(dec),
            .rdata(rdata), .enc_lanes(lanes), .done(done), .write_ticks(write_ticks), .read_ticks(read_ticks),
            .bad_words(bad_words), .invalid_groups(invalid_groups), .pos(pos), .neg(neg), .dot(dot), .chk(chk));
    end else if (FORMAT == 1) begin : d5
        TrinityBramTritEngineD5T27 eng (.clk(clk), .rst_n(rst_n), .en(1'b1), .ready(), .start(start), .enc_word(word), .dec_out(dec),
            .rdata(rdata), .enc_lanes(lanes), .done(done), .write_ticks(write_ticks), .read_ticks(read_ticks),
            .bad_words(bad_words), .invalid_groups(invalid_groups), .pos(pos), .neg(neg), .dot(dot), .chk(chk));
    end else if (FORMAT == 3) begin : d5p
        // d5p (read-only store only): the codec decodes format 3 as d5 bytes; the store adds the pair's parity byte.
        TrinityBramTritEngineD5PT27 eng (.clk(clk), .rst_n(rst_n), .en(1'b1), .ready(), .start(start), .enc_word(word), .dec_out(dec),
            .rdata(rdata), .enc_lanes(lanes), .done(done), .write_ticks(write_ticks), .read_ticks(read_ticks),
            .bad_words(bad_words), .invalid_groups(invalid_groups), .pos(pos), .neg(neg), .dot(dot), .chk(chk));
    end else begin : d5d2
        TrinityBramTritEngineD5D2T27 eng (.clk(clk), .rst_n(rst_n), .en(1'b1), .ready(), .start(start), .enc_word(word), .dec_out(dec),
            .rdata(rdata), .enc_lanes(lanes), .done(done), .write_ticks(write_ticks), .read_ticks(read_ticks),
            .bad_words(bad_words), .invalid_groups(invalid_groups), .pos(pos), .neg(neg), .dot(dot), .chk(chk));
    end endgenerate
endmodule
`default_nettype wire
