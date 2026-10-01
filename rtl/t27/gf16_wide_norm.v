// Wiring only. Arithmetic and state machines are generated from .t27 sources.
module gf16_wide_norm (
    input wire clk, rst_n, en, start,
    input wire [31:0] row_length,
    input wire in_valid,
    input wire [31:0] gate_in, up_in, weight_in,
    output wire in_ready,
    input wire out_ready,
    output wire out_valid,
    output wire [31:0] out_value, out_unit, out_product, index,
    output wire busy, done, fault,
    output wire [31:0] error_code, overflow_count,
    output wire [63:0] cycles
);
wire math_start, math_done, math_fault;
wire [31:0] math_mode;
wire [63:0] a0,a1,a2,b0,b1,q0,q1,q2,r0,r1;
TrinityGf16WideNormT27 kernel (
    .clk(clk),.rst_n(rst_n),.en(en),.ready(),
    .start(start),.row_length(row_length),.in_valid(in_valid),
    .gate_in(gate_in),.up_in(up_in),.weight_in(weight_in),.in_ready(in_ready),
    .out_ready(out_ready),.out_valid(out_valid),.out_value(out_value),
    .out_unit(out_unit),.out_product(out_product),.index(index),
    .busy(busy),.done(done),.fault(fault),.error_code(error_code),
    .overflow_count(overflow_count),.cycles(cycles),
    .math_start(math_start),.math_mode(math_mode),.a0(a0),.a1(a1),.a2(a2),.b0(b0),.b1(b1),
    .math_done(math_done),.math_fault(math_fault),.q0(q0),.q1(q1),.q2(q2),.r0(r0),.r1(r1)
);
TrinityFfnWideT27 arithmetic (
    .clk(clk),.rst_n(rst_n),.en(en),.ready(),
    .start(math_start),.mode(math_mode),.a0(a0),.a1(a1),.a2(a2),.b0(b0),.b1(b1),
    .q0(q0),.q1(q1),.q2(q2),.r0(r0),.r1(r1),.done(math_done),.fault(math_fault),.busy()
);
endmodule
