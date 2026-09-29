// Issue #92. Wiring only; arithmetic and sequencing are generated from .t27.
`timescale 1ns/1ps
`default_nettype none
module trinity_ffn_t27 (
    input wire clk, rst_n, calib, stall, ack,
    input wire [63:0] rdata_lo, rdata_hi,
    input wire [31:0] hidden, inner, output_rows,
    input wire line_idle, s_go,
    input wire [31:0] s_tag, s_a,
    input wire [63:0] s_b,
    output wire wb_cyc, wb_stb, line_go, s_idle,
    output wire [31:0] wb_addr, line_tag, line_a, state,
    output wire [63:0] line_b
);
    wire start, done, fault;
    wire [31:0] mode;
    wire [63:0] a0,a1,a2,b0,b1,q0,q1,q2,r0,r1;
    TrinityFfnWideT27 math (
        .clk(clk), .rst_n(rst_n), .en(1'b1), .ready(),
        .start(start), .mode(mode), .a0(a0), .a1(a1), .a2(a2), .b0(b0), .b1(b1),
        .done(done), .fault(fault), .q0(q0), .q1(q1), .q2(q2), .r0(r0), .r1(r1)
    );
    TrinityFpgaFfnT27 core (
        .clk(clk), .rst_n(rst_n), .en(1'b1), .ready(),
        .calib(calib), .stall(stall), .ack(ack), .rdata_lo(rdata_lo), .rdata_hi(rdata_hi),
        .hidden(hidden), .inner(inner), .output_rows(output_rows),
        .math_start(start), .math_mode(mode), .a0(a0), .a1(a1), .a2(a2), .b0(b0), .b1(b1),
        .math_done(done), .math_fault(fault), .q0(q0), .q1(q1), .q2(q2), .r0(r0), .r1(r1),
        .line_idle(line_idle), .s_go(s_go), .s_tag(s_tag), .s_a(s_a), .s_b(s_b),
        .line_go(line_go), .line_tag(line_tag), .line_a(line_a), .line_b(line_b), .s_idle(s_idle),
        .wb_cyc(wb_cyc), .wb_stb(wb_stb), .wb_addr(wb_addr), .state(state)
    );
endmodule
`default_nettype wire
