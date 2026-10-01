// Issue #111. Wiring only; arithmetic/control are generated from .t27.
`timescale 1ns/1ps
`default_nettype none
module trinity_gf16_ffn_t27 (
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
    wire math_start, math_done, math_fault;
    wire [31:0] math_mode;
    wire [63:0] a0,a1,a2,b0,b1,q0,q1,q2,r0,r1;
    wire norm_start, norm_in_valid, norm_in_ready, norm_out_ready, norm_out_valid, norm_done, norm_fault;
    wire [31:0] norm_length, norm_gate, norm_up, norm_weight, norm_value, norm_product, norm_index;
    TrinityFfnWideT27 math (
        .clk(clk),.rst_n(rst_n),.en(1'b1),.ready(),
        .start(math_start),.mode(math_mode),.a0(a0),.a1(a1),.a2(a2),.b0(b0),.b1(b1),
        .done(math_done),.fault(math_fault),.q0(q0),.q1(q1),.q2(q2),.r0(r0),.r1(r1)
    );
    gf16_wide_norm norm (
        .clk(clk),.rst_n(rst_n),.en(1'b1),.start(norm_start),.row_length(norm_length),
        .in_valid(norm_in_valid),.in_ready(norm_in_ready),.gate_in(norm_gate),.up_in(norm_up),.weight_in(norm_weight),
        .out_valid(norm_out_valid),.out_ready(norm_out_ready),.out_value(norm_value),.out_unit(),
        .out_product(norm_product),.index(norm_index),.done(norm_done),.fault(norm_fault),
        .busy(),.error_code(),.overflow_count(),.cycles()
    );
    wire scalar_start, scalar_done, scalar_fault, scalar_negative;
wire [31:0] scalar_mode;
wire signed [31:0] scalar_aux;
wire [63:0] scalar_x,scalar_y,scalar_r0,scalar_r1,scalar_d0,scalar_d1,scalar_value;
TrinityGf16FfnT27 core (
    .scalar_start(scalar_start),.scalar_mode(scalar_mode),.scalar_x(scalar_x),.scalar_y(scalar_y),
    .scalar_r0(scalar_r0),.scalar_r1(scalar_r1),.scalar_d0(scalar_d0),.scalar_d1(scalar_d1),
    .scalar_negative(scalar_negative),.scalar_aux(scalar_aux),
    .scalar_done(scalar_done),.scalar_fault(scalar_fault),.scalar_value(scalar_value),

        .clk(clk),.rst_n(rst_n),.en(1'b1),.ready(),
        .calib(calib),.stall(stall),.ack(ack),.rdata_lo(rdata_lo),.rdata_hi(rdata_hi),
        .hidden(hidden),.inner(inner),.output_rows(output_rows),
        .math_start(math_start),.math_mode(math_mode),.a0(a0),.a1(a1),.a2(a2),.b0(b0),.b1(b1),
        .math_done(math_done),.math_fault(math_fault),.q0(q0),.q1(q1),.q2(q2),.r0(r0),.r1(r1),
        .norm_start(norm_start),.norm_length(norm_length),.norm_in_valid(norm_in_valid),
        .norm_in_ready(norm_in_ready),.norm_gate(norm_gate),.norm_up(norm_up),.norm_weight(norm_weight),
        .norm_out_ready(norm_out_ready),.norm_out_valid(norm_out_valid),.norm_value(norm_value),
        .norm_product(norm_product),.norm_index(norm_index),.norm_done(norm_done),.norm_fault(norm_fault),
        .line_idle(line_idle),.s_go(s_go),.s_tag(s_tag),.s_a(s_a),.s_b(s_b),
        .line_go(line_go),.line_tag(line_tag),.line_a(line_a),.line_b(line_b),.s_idle(s_idle),
        .wb_cyc(wb_cyc),.wb_stb(wb_stb),.wb_addr(wb_addr),.state(state)
    );
TrinityGf16ScalarT27 scalar (
    .clk(clk),.rst_n(rst_n),.en(1'b1),.ready(),
    .start(scalar_start),.mode(scalar_mode),.x(scalar_x),.y(scalar_y),
    .r0(scalar_r0),.r1(scalar_r1),.d0(scalar_d0),.d1(scalar_d1),
    .negative(scalar_negative),.aux(scalar_aux),.done(scalar_done),.fault(scalar_fault),.value(scalar_value)
);
endmodule
`default_nettype wire
