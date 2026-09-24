// Pre-silicon check of the AX7203 DDR3 pattern test (issue #61): the whole top
// fpga/ax7203/ddr3/tms_ddr3_ax7203.v with PATTERN_TEST 1, UberDDR3's ddr3_top
// replaced by the behavioural Wishbone memory of tests/sim_ddr3_top_model.v
// (faults by plusargs) and the PLL by a pass-through stub. The UART is decoded
// into lines, written with the simulation time to the file named by +capture=,
// and tests/test_ddr3_pattern.py hands them to tools/fpga-ddr3-capture.py's
// decoder. The run ends after a Z line (all rounds done) or a T line (watchdog).
`timescale 1ns/1ps
`default_nettype none
// Simulation stand-in for the PLL: every output is the input clock, LOCKED after 16 cycles.
module PLLE2_ADV #(
    parameter BANDWIDTH = "OPTIMIZED", parameter COMPENSATION = "INTERNAL", parameter STARTUP_WAIT = "FALSE",
    parameter real CLKIN1_PERIOD = 5.0, parameter integer DIVCLK_DIVIDE = 1, parameter integer CLKFBOUT_MULT = 5,
    parameter real CLKFBOUT_PHASE = 0.0,
    parameter integer CLKOUT0_DIVIDE = 1, parameter real CLKOUT0_PHASE = 0.0, parameter real CLKOUT0_DUTY_CYCLE = 0.5,
    parameter integer CLKOUT1_DIVIDE = 1, parameter real CLKOUT1_PHASE = 0.0, parameter real CLKOUT1_DUTY_CYCLE = 0.5,
    parameter integer CLKOUT2_DIVIDE = 1, parameter real CLKOUT2_PHASE = 0.0, parameter real CLKOUT2_DUTY_CYCLE = 0.5,
    parameter integer CLKOUT3_DIVIDE = 1, parameter real CLKOUT3_PHASE = 0.0, parameter real CLKOUT3_DUTY_CYCLE = 0.5
) (
    input wire CLKIN1, CLKIN2, CLKINSEL, CLKFBIN, RST, PWRDWN, DCLK, DEN, DWE,
    input wire [6:0] DADDR, input wire [15:0] DI,
    output wire CLKFBOUT, CLKOUT0, CLKOUT1, CLKOUT2, CLKOUT3, CLKOUT4, CLKOUT5,
    output reg LOCKED, output wire [15:0] DO, output wire DRDY
);
    integer n = 0;
    initial LOCKED = 1'b0;
    always @(posedge CLKIN1) begin
        n = n + 1;
        if (n == 16) LOCKED <= 1'b1;
    end
    assign {CLKFBOUT, CLKOUT0, CLKOUT1, CLKOUT2, CLKOUT3} = {5{CLKIN1}};
    assign {CLKOUT4, CLKOUT5, DRDY} = 3'b000;
    assign DO = 16'd0;
endmodule

module tb_ddr3_pattern;
    parameter integer LANES = 2;
    parameter integer BURSTS = 256;
    parameter integer ROUNDS = 1;
    parameter integer HOLD = 0;
    parameter integer WATCHDOG = 16777216;
    parameter integer UART_DIV = 4;
    localparam real BIT_NS = 5.0 * UART_DIV;
    reg clk200 = 1'b0;
    always #2.5 clk200 = !clk200;
    reg rst_n = 1'b0;
    wire [3:0] led;
    wire uart_tx;
    wire ck_p, ck_n, reset_n, cke, cs_n, ras_n, cas_n, we_n, odt;
    wire [14:0] addr;
    wire [2:0] ba;
    wire [8*LANES-1:0] dq;
    wire [LANES-1:0] dqs_p, dqs_n, dm;
    tms_ddr3_ax7203 #(.BYTE_LANES(LANES), .REPORT_PERIOD(20000), .BUILD_ID(32'h5eedc0de), .PATTERN_TEST(1),
                      .PATTERN_BURSTS(BURSTS), .PATTERN_SEED(32'h61), .PATTERN_HOLD(HOLD), .PATTERN_ROUNDS(ROUNDS),
                      .PATTERN_WATCHDOG(WATCHDOG), .UART_DIV(UART_DIV)) dut (
        .clk200_p(clk200), .clk200_n(!clk200), .rst_n(rst_n), .led(led), .uart_tx(uart_tx),
        .ddr3_ck_p(ck_p), .ddr3_ck_n(ck_n), .ddr3_reset_n(reset_n), .ddr3_cke(cke), .ddr3_cs_n(cs_n),
        .ddr3_ras_n(ras_n), .ddr3_cas_n(cas_n), .ddr3_we_n(we_n), .ddr3_odt(odt), .ddr3_addr(addr), .ddr3_ba(ba),
        .ddr3_dq(dq), .ddr3_dqs_p(dqs_p), .ddr3_dqs_n(dqs_n), .ddr3_dm(dm)
    );
    reg [4095:0] path;
    integer fd, lines;
    reg [7:0] byte_r, first;
    reg at_line_start;
    event finish_later;
    always @(finish_later) begin
        #(BIT_NS * 20 * 30);
        $fclose(fd);
        $display("TB_PASS lines=%0d leds=%b", lines, led);
        $finish;
    end
    initial begin
        if (!$value$plusargs("capture=%s", path)) $fatal(1, "missing plusarg capture=");
        fd = $fopen(path, "w");
        lines = 0; at_line_start = 1'b1; first = 8'd0;
        #200 rst_n = 1'b1;
    end
    initial begin
        #2_000_000_000;
        $fatal(1, "timeout waiting for the pattern test");
    end
    // Each line as "<simulation time in ns at its first byte>\t<text>".
    always begin : receiver
        integer k;
        @(negedge uart_tx);
        #(BIT_NS * 1.5);
        byte_r = 8'd0;
        for (k = 0; k < 8; k = k + 1) begin
            byte_r[k] = uart_tx;
            #(BIT_NS);
        end
        if (uart_tx !== 1'b1) $fatal(1, "framing error: stop bit low");
        if (at_line_start) begin
            first = byte_r;
            at_line_start = 1'b0;
            $fwrite(fd, "%0d\t", $time);
        end
        $fwrite(fd, "%c", byte_r);
        if (byte_r == 8'h0a) begin
            lines = lines + 1;
            at_line_start = 1'b1;
            if (first == "Z" || first == "T") -> finish_later;
        end
    end
endmodule
`default_nettype wire
