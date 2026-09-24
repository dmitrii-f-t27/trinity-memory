// Self-checking testbench: the t27-generated E4M3 decoder and encoder against the tables written by
// kits/processorci/model/make_vectors.py from the independent reference model. Run it from
// kits/processorci/vectors so that $readmemh finds the tables. Prints "ALL PASS" or "FAIL <n>".
`timescale 1ns/1ps
`default_nettype none
`include "e4m3_edge_count.vh"
module tb_fp8_e4m3;
    reg  [7:0]  dcode;
    wire [31:0] dres;
    reg  [31:0] ebits;
    reg  [7:0]  esat;
    wire [7:0]  eres;
    TrinityFp8E4m3DecodeT27 dec (.clk(1'b0), .rst_n(1'b1), .en(1'b1), .code(dcode), .ready(), .result(dres));
    TrinityFp8E4m3EncodeT27 enc (.clk(1'b0), .rst_n(1'b1), .en(1'b1), .bits(ebits), .saturate(esat), .ready(), .result(eres));

    reg [31:0] dec_exp [0:255];
    reg [7:0]  bf_sat  [0:65535];
    reg [7:0]  bf_nan  [0:65535];
    reg [31:0] edge_in [0:`E4M3_EDGE_N-1];
    reg [7:0]  edge_sat[0:`E4M3_EDGE_N-1];
    reg [7:0]  edge_nan[0:`E4M3_EDGE_N-1];
    reg [31:0] rnd_in  [0:65535];
    reg [7:0]  rnd_sat [0:65535];
    reg [7:0]  rnd_nan [0:65535];
    integer i, n, bad, total;

    task check_encode;
        input [31:0] x;
        input [7:0] want_sat;
        input [7:0] want_nan;
        begin
            ebits = x; esat = 8'd1; #1;
            if (eres !== want_sat) begin bad = bad + 1;
                if (bad <= 5) $display("  MISMATCH sat x=%08x got=%02x want=%02x", x, eres, want_sat); end
            esat = 8'd0; #1;
            if (eres !== want_nan) begin bad = bad + 1;
                if (bad <= 5) $display("  MISMATCH nan x=%08x got=%02x want=%02x", x, eres, want_nan); end
        end
    endtask

    initial begin
        $readmemh("e4m3_decode.hex", dec_exp);
        $readmemh("e4m3_bf16_sat.hex", bf_sat);
        $readmemh("e4m3_bf16_nan.hex", bf_nan);
        $readmemh("e4m3_edge_in.hex", edge_in);
        $readmemh("e4m3_edge_sat.hex", edge_sat);
        $readmemh("e4m3_edge_nan.hex", edge_nan);
        $readmemh("e4m3_rand_in.hex", rnd_in);
        $readmemh("e4m3_rand_sat.hex", rnd_sat);
        $readmemh("e4m3_rand_nan.hex", rnd_nan);
        total = 0;

        bad = 0;
        for (i = 0; i < 256; i = i + 1) begin
            dcode = i[7:0]; #1;
            if (dres !== dec_exp[i]) begin bad = bad + 1;
                if (bad <= 5) $display("  MISMATCH decode code=%02x got=%08x want=%08x", i, dres, dec_exp[i]); end
        end
        $display("decode, every code:              %0d mismatches of 256", bad); total = total + bad;

        bad = 0;
        for (i = 0; i < 65536; i = i + 1) check_encode({i[15:0], 16'h0000}, bf_sat[i], bf_nan[i]);
        $display("encode, every BF16 input x 2:    %0d mismatches of 131072", bad); total = total + bad;

        bad = 0; n = `E4M3_EDGE_N;
        for (i = 0; i < `E4M3_EDGE_N; i = i + 1) check_encode(edge_in[i], edge_sat[i], edge_nan[i]);
        $display("encode, rounding edges x 2:      %0d mismatches of %0d", bad, 2 * n); total = total + bad;

        bad = 0;
        for (i = 0; i < 65536; i = i + 1) check_encode(rnd_in[i], rnd_sat[i], rnd_nan[i]);
        $display("encode, seeded binary32 x 2:     %0d mismatches of 131072", bad); total = total + bad;

        if (total == 0) $display("ALL PASS"); else $display("FAIL %0d", total);
        $finish;
    end
endmodule
