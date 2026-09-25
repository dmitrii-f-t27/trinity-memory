// Out-of-context harness of the device matvec (t27/rtl/fpga_ddr3_matvec.t27, issue #64) for
// synthesis and place-and-route estimates only (tools/fpga-matvec-ooc.py); never loaded on the
// board. Wiring only: every input of the module comes from one serial input pin through a shift
// register, so nothing is a constant that yosys could fold, and the line the module would send
// (go, tag, a, b) plus done, busy and in_ready are XOR-reduced into one registered output pin, so nothing
// the module computes is trimmed. The clock is the board's 200 MHz differential input through
// IBUFDS and BUFG; the XDC constrains it to the DDR3 controller clock's period (12 ns, 83.33 MHz).
`timescale 1ns/1ps
`default_nettype none
module tms_matvec_ooc (
    input  wire clk200_p,
    input  wire clk200_n,
    input  wire rst_n,
    input  wire din,
    output reg  dout
);
    wire clk_in, clk;
    IBUFDS ibuf (.I(clk200_p), .IB(clk200_n), .O(clk_in));
    BUFG bufg (.I(clk_in), .O(clk));
    // start, cfg_rows, cfg_cols, cfg_fmt, cfg_seq, in_valid, in_lo, in_hi, act_we, act_entry,
    // act_bank, act_data, line_idle, abort: 1 + 4 * 32 + 1 + 128 + 1 + 2 * 32 + 64 + 1 + 1 = 389 bits.
    reg [388:0] sr = 389'd0;
    always @(posedge clk) sr <= {sr[387:0], din};
    wire line_go, done, busy, in_ready;
    wire [31:0] line_tag, line_a;
    wire [63:0] line_b;
    TrinityFpgaDdr3MatvecT27 mv (
        .clk(clk), .rst_n(rst_n), .en(1'b1),
        .start(sr[0]), .cfg_rows(sr[32:1]), .cfg_cols(sr[64:33]), .cfg_fmt(sr[96:65]), .cfg_seq(sr[128:97]),
        .in_valid(sr[129]), .in_lo(sr[193:130]), .in_hi(sr[257:194]),
        .act_we(sr[258]), .act_entry(sr[290:259]), .act_bank(sr[322:291]), .act_data(sr[386:323]),
        .line_idle(sr[387]), .abort(sr[388]),
        .line_go(line_go), .line_tag(line_tag), .line_a(line_a), .line_b(line_b), .done(done), .busy(busy),
        .in_ready(in_ready));
    always @(posedge clk) dout <= ^{line_go, line_tag, line_a, line_b, done, busy, in_ready};
endmodule
`default_nettype wire
