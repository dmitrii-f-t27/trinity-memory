// Runs trinity_arith_harness the way the ProcessorCI controller does: a word memory answering Wishbone
// requests one cycle later, a descriptor written before reset is released, the run ending when the
// harness puts END_ADDR on the bus. Checks every result block against vectors/harness_expected.hex
// (count and CRC-32 per test, computed by the reference models). Run from kits/processorci/vectors.
`timescale 1ns/1ps
`default_nettype none
module tb_harness;
    localparam [31:0] RESULT_ADDR = 32'h0000_0100;
    localparam [31:0] END_ADDR    = 32'h0000_1FFC;
    reg clk = 1'b0, rst = 1'b1;
    always #5 clk = ~clk;

    wire        cyc, stb, we;
    wire [3:0]  wstrb;
    wire [31:0] addr, wdata;
    reg  [31:0] rdata = 32'd0;
    reg         ack = 1'b0;
    reg  [31:0] mem [0:2047];
    reg  [31:0] expected [0:15];
    integer t, i, cycles, bad, ended;

    trinity_arith_harness dut (.clk(clk), .rst(rst), .cyc(cyc), .stb(stb), .we(we), .wstrb(wstrb),
                               .addr(addr), .wdata(wdata), .rdata(rdata), .ack(ack));

    always @(posedge clk) begin
        if (cyc && stb && !ack) begin
            ack <= 1'b1;
            if (we) mem[addr[12:2]] <= wdata;
            rdata <= mem[addr[12:2]];
        end else begin
            ack <= 1'b0;
        end
    end

    initial begin
        $readmemh("harness_expected.hex", expected);
        bad = 0;
        for (t = 1; t <= 8; t = t + 1) begin
            for (i = 0; i < 2048; i = i + 1) mem[i] = 32'd0;
            mem[0] = t; mem[1] = 32'd65536; mem[2] = 32'h27272727;
            rst = 1'b1; repeat (4) @(posedge clk); rst = 1'b0;
            cycles = 0; ended = 0;
            while (!ended && cycles < 400000) begin
                @(posedge clk); cycles = cycles + 1;
                if (cyc && stb && addr == END_ADDR) ended = 1;
            end
            repeat (3) @(posedge clk);
            if (!ended || mem[RESULT_ADDR/4] !== 32'h54524931 || mem[RESULT_ADDR/4 + 1] !== t
                || mem[RESULT_ADDR/4 + 2] !== expected[2*(t-1)] || mem[RESULT_ADDR/4 + 3] !== expected[2*(t-1)+1]
                || mem[RESULT_ADDR/4 + 4] !== 32'h0000600D) begin
                bad = bad + 1;
                $display("FAIL test %0d: ended=%0d folded=%0d crc=%08x status=%08x (want %0d / %08x)", t, ended,
                         mem[RESULT_ADDR/4 + 2], mem[RESULT_ADDR/4 + 3], mem[RESULT_ADDR/4 + 4],
                         expected[2*(t-1)], expected[2*(t-1)+1]);
            end else begin
                $display("PASS test %0d: %0d outputs, CRC-32 %08x, %0d cycles", t, mem[RESULT_ADDR/4 + 2],
                         mem[RESULT_ADDR/4 + 3], cycles);
            end
        end
        // An unknown test id must report 0xBAD1 and still end the run.
        for (i = 0; i < 2048; i = i + 1) mem[i] = 32'd0;
        mem[0] = 32'd99;
        rst = 1'b1; repeat (4) @(posedge clk); rst = 1'b0;
        cycles = 0; ended = 0;
        while (!ended && cycles < 1000) begin
            @(posedge clk); cycles = cycles + 1;
            if (cyc && stb && addr == END_ADDR) ended = 1;
        end
        repeat (3) @(posedge clk);
        if (!ended || mem[RESULT_ADDR/4 + 4] !== 32'h0000BAD1) begin
            bad = bad + 1; $display("FAIL unknown test id: ended=%0d status=%08x", ended, mem[RESULT_ADDR/4 + 4]);
        end else $display("PASS unknown test id reports 0xBAD1");
        if (bad == 0) $display("ALL PASS"); else $display("FAIL %0d", bad);
        $finish;
    end
endmodule
`default_nettype wire
