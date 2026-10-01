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
wire scalar_start, scalar_done, scalar_fault, scalar_negative;
wire [31:0] scalar_mode;
wire signed [31:0] scalar_aux;
wire [63:0] scalar_x,scalar_y,scalar_r0,scalar_r1,scalar_d0,scalar_d1,scalar_value;
TrinityGf16WideNormT27 kernel (
    .scalar_start(scalar_start),.scalar_mode(scalar_mode),.scalar_x(scalar_x),.scalar_y(scalar_y),
    .scalar_r0(scalar_r0),.scalar_r1(scalar_r1),.scalar_d0(scalar_d0),.scalar_d1(scalar_d1),
    .scalar_negative(scalar_negative),.scalar_aux(scalar_aux),
    .scalar_done(scalar_done),.scalar_fault(scalar_fault),.scalar_value(scalar_value),

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
TrinityGf16ScalarT27 scalar (
    .clk(clk),.rst_n(rst_n),.en(en),.ready(),
    .start(scalar_start),.mode(scalar_mode),.x(scalar_x),.y(scalar_y),
    .r0(scalar_r0),.r1(scalar_r1),.d0(scalar_d0),.d1(scalar_d1),
    .negative(scalar_negative),.aux(scalar_aux),.done(scalar_done),.fault(scalar_fault),.value(scalar_value)
);
endmodule
