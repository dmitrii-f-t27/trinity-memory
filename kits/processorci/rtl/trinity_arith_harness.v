// Test harness for ProcessorCI (LSC-Unicamp): takes a RISC-V core's slot in processorci_top and speaks the
// same 32-bit Wishbone master bus. It reads a three-word descriptor, drives the t27-generated arithmetic
// modules over a whole input space from on-chip counters, folds every output into a CRC-32, writes a
// five-word result block and only then touches END_ADDR, which ends the run in the ProcessorCI controller.
//
// This file is test infrastructure written by hand. The arithmetic lives only in the generated modules
// (rtl/generated/*.v, from t27/rtl/*.t27); nothing here decodes, encodes or rounds.
//
// Descriptor (written by the host before the run):
//   DESC_ADDR + 0 : test id
//   DESC_ADDR + 4 : input count  (tests 4 and 5 only)
//   DESC_ADDR + 8 : xorshift32 seed (tests 4 and 5 only)
// Tests:
//   1  FP8 E4M3 decode, every code (256), output 4 bytes little-endian
//   2  FP8 E4M3 encode, every BF16 pattern as binary32 (65536), saturating, 1 byte
//   3  same inputs, non-saturating (NaN on overflow), 1 byte
//   4  FP8 E4M3 encode, `count` xorshift32 binary32 inputs from `seed`, saturating, 1 byte
//   5  same inputs, non-saturating, 1 byte
//   6  ternary dense5 decoder, every byte (256), 2 bytes
//   7  ternary baseline5 decoder, every 10-bit code (1024), 2 bytes
//   8  ternary sparse 4:1 decoder, every byte (256), 2 bytes
// Result block at RESULT_ADDR: 'TRI1' magic, test id, outputs folded, CRC-32 (zlib), status
// (0x600D done, 0xBAD1 unknown test id).
`timescale 1ns/1ps
`default_nettype none
module trinity_arith_harness #(
    parameter [31:0] DESC_ADDR   = 32'h0000_0000,
    parameter [31:0] RESULT_ADDR = 32'h0000_0100,
    parameter [31:0] END_ADDR    = 32'h0000_1FFC
) (
    input  wire        clk,
    input  wire        rst,        // active high, as rst_core in processorci_top
    output reg         cyc,
    output reg         stb,
    output reg         we,
    output reg  [3:0]  wstrb,
    output reg  [31:0] addr,
    output reg  [31:0] wdata,
    input  wire [31:0] rdata,
    input  wire        ack
);
    localparam S_RD_ID = 3'd0, S_RD_CNT = 3'd1, S_RD_SEED = 3'd2, S_SETUP = 3'd3,
               S_RUN = 3'd4, S_WR = 3'd5, S_END = 3'd6, S_IDLE = 3'd7;

    reg [2:0]  state;
    reg [2:0]  wr_i;
    reg [7:0]  test;
    reg [31:0] count, seed, total, idx, rng, crc, folded;
    reg [15:0] status;
    reg [31:0] out_q;
    reg [2:0]  nbytes_q;
    reg        valid_q;

    function [31:0] xorshift32;
        input [31:0] s;
        reg [31:0] x;
        begin
            x = s ^ (s << 13);
            x = x ^ (x >> 17);
            xorshift32 = x ^ (x << 5);
        end
    endfunction

    function [31:0] crc32_byte;
        input [31:0] c;
        input [7:0] b;
        integer k;
        reg [31:0] x;
        begin
            x = c ^ {24'd0, b};
            for (k = 0; k < 8; k = k + 1)
                x = x[0] ? ((x >> 1) ^ 32'hEDB88320) : (x >> 1);
            crc32_byte = x;
        end
    endfunction

    function [31:0] crc32_word;
        input [31:0] c;
        input [31:0] w;
        input [2:0] n;
        reg [31:0] x;
        begin
            x = crc32_byte(c, w[7:0]);
            if (n >= 3'd2) x = crc32_byte(x, w[15:8]);
            if (n >= 3'd4) begin
                x = crc32_byte(x, w[23:16]);
                x = crc32_byte(x, w[31:24]);
            end
            crc32_word = x;
        end
    endfunction

    // Inputs to the generated modules, derived from the registered counters.
    wire        random_test = (test == 8'd4) || (test == 8'd5);
    wire [31:0] enc_bits    = random_test ? rng : {idx[15:0], 16'h0000};
    wire [7:0]  enc_sat     = ((test == 8'd2) || (test == 8'd4)) ? 8'd1 : 8'd0;
    wire [31:0] dec_out;
    wire [7:0]  enc_out;
    wire [15:0] d5_out, b5_out, s41_out;

    TrinityFp8E4m3DecodeT27    u_dec (.clk(clk), .rst_n(~rst), .en(1'b1), .code(idx[7:0]), .ready(), .result(dec_out));
    TrinityFp8E4m3EncodeT27    u_enc (.clk(clk), .rst_n(~rst), .en(1'b1), .bits(enc_bits), .saturate(enc_sat), .ready(), .result(enc_out));
    TrinityDense5DecoderT27    u_d5  (.clk(clk), .rst_n(~rst), .en(1'b1), .code(idx[7:0]), .ready(), .result(d5_out));
    TrinityBaseline5DecoderT27 u_b5  (.clk(clk), .rst_n(~rst), .en(1'b1), .code({6'd0, idx[9:0]}), .ready(), .result(b5_out));
    TrinitySparse41DecoderT27  u_s41 (.clk(clk), .rst_n(~rst), .en(1'b1), .code(idx[7:0]), .ready(), .result(s41_out));

    reg [31:0] out_now;
    reg [2:0]  nbytes_now;
    always @(*) begin
        case (test)
            8'd1:                   begin out_now = dec_out;          nbytes_now = 3'd4; end
            8'd2, 8'd3, 8'd4, 8'd5: begin out_now = {24'd0, enc_out}; nbytes_now = 3'd1; end
            8'd6:                   begin out_now = {16'd0, d5_out};  nbytes_now = 3'd2; end
            8'd7:                   begin out_now = {16'd0, b5_out};  nbytes_now = 3'd2; end
            8'd8:                   begin out_now = {16'd0, s41_out}; nbytes_now = 3'd2; end
            default:                begin out_now = 32'd0;            nbytes_now = 3'd1; end
        endcase
    end

    reg [31:0] result_word;
    always @(*) begin
        case (wr_i)
            3'd0: result_word = 32'h5452_4931;           // 'TRI1'
            3'd1: result_word = {24'd0, test};
            3'd2: result_word = folded;
            3'd3: result_word = ~crc;
            default: result_word = {16'd0, status};
        endcase
    end

    always @(posedge clk) begin
        if (rst) begin
            state <= S_RD_ID; wr_i <= 3'd0;
            cyc <= 1'b0; stb <= 1'b0; we <= 1'b0; wstrb <= 4'h0; addr <= 32'd0; wdata <= 32'd0;
            test <= 8'd0; count <= 32'd0; seed <= 32'd0; total <= 32'd0; idx <= 32'd0; rng <= 32'd0;
            crc <= 32'hFFFF_FFFF; folded <= 32'd0; status <= 16'd0;
            out_q <= 32'd0; nbytes_q <= 3'd1; valid_q <= 1'b0;
        end else begin
            case (state)
                S_RD_ID, S_RD_CNT, S_RD_SEED: begin
                    if (!cyc) begin
                        cyc <= 1'b1; stb <= 1'b1; we <= 1'b0; wstrb <= 4'h0;
                        addr <= DESC_ADDR + {28'd0, state[1:0], 2'b00};
                    end else if (ack) begin
                        cyc <= 1'b0; stb <= 1'b0;
                        if (state == S_RD_ID)  begin test  <= rdata[7:0]; state <= S_RD_CNT;  end
                        if (state == S_RD_CNT) begin count <= rdata;      state <= S_RD_SEED; end
                        if (state == S_RD_SEED) begin seed <= rdata;      state <= S_SETUP;   end
                    end
                end
                S_SETUP: begin
                    idx <= 32'd0; rng <= xorshift32(seed); crc <= 32'hFFFF_FFFF; folded <= 32'd0; valid_q <= 1'b0;
                    case (test)
                        8'd1, 8'd6, 8'd8: begin total <= 32'd256;   status <= 16'h600D; end
                        8'd2, 8'd3:       begin total <= 32'd65536; status <= 16'h600D; end
                        8'd4, 8'd5:       begin total <= count;     status <= 16'h600D; end
                        8'd7:             begin total <= 32'd1024;  status <= 16'h600D; end
                        default:          begin total <= 32'd0;     status <= 16'hBAD1; end
                    endcase
                    state <= S_RUN;
                end
                S_RUN: begin
                    // Stage 2: fold the output captured last cycle.
                    if (valid_q) begin
                        crc <= crc32_word(crc, out_q, nbytes_q);
                        folded <= folded + 32'd1;
                    end
                    // Stage 1: capture the generated modules' output for the current input.
                    if (idx != total) begin
                        out_q <= out_now; nbytes_q <= nbytes_now; valid_q <= 1'b1;
                        idx <= idx + 32'd1; rng <= xorshift32(rng);
                    end else begin
                        valid_q <= 1'b0;
                        if (!valid_q) begin state <= S_WR; wr_i <= 3'd0; end
                    end
                end
                S_WR: begin
                    if (!cyc) begin
                        cyc <= 1'b1; stb <= 1'b1; we <= 1'b1; wstrb <= 4'hF;
                        addr <= RESULT_ADDR + {27'd0, wr_i, 2'b00}; wdata <= result_word;
                    end else if (ack) begin
                        cyc <= 1'b0; stb <= 1'b0; we <= 1'b0; wstrb <= 4'h0;
                        if (wr_i == 3'd4) state <= S_END; else wr_i <= wr_i + 3'd1;
                    end
                end
                S_END: begin
                    if (!cyc) begin
                        cyc <= 1'b1; stb <= 1'b1; we <= 1'b0; wstrb <= 4'h0; addr <= END_ADDR;
                    end else if (ack) begin
                        cyc <= 1'b0; stb <= 1'b0; state <= S_IDLE;
                    end
                end
                default: begin
                    cyc <= 1'b0; stb <= 1'b0; we <= 1'b0;
                end
            endcase
        end
    end
endmodule
`default_nettype wire
