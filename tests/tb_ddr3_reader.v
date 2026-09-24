// Pre-silicon check of the AX7203 DDR3 read path (issue #62): the whole top
// fpga/ax7203/ddr3/tms_ddr3_ax7203.v read with `define DDR3_READER, UberDDR3's ddr3_top
// replaced by the behavioural Wishbone memory of tests/sim_ddr3_top_model.v (random
// stalls and ack latency, faults by plusargs; our code, not UberDDR3's) and the PLL by
// a pass-through stub. The UART is decoded into lines, written with the simulation
// time to the file named by +capture=, and tests/test_ddr3_reader.py hands them to
// tools/fpga-ddr3-capture.py's decoder and compares them with tools/ddr3_read_model.py.
//
// Independently of the reader's own counters, the bench watches the Wishbone port at
// every controller clock and prints one line per phase (TB_PHASE): the clocks from
// the first request presented to the last ack of the phase, the requests taken, the
// acks, the clocks with a request presented and o_wb_stall high, the clocks with a
// request outstanding and no ack, the most requests outstanding. A phase is the
// requests of one direction (write = fill, read) between two changes of i_wb_we;
// it ends when its acks equal its requests and no request is presented.
// At the end it writes the model's first +dump_words= stored bursts to +dump=
// (one 128-bit word in hex per line), so the test can check the byte order in memory.
// The run ends after a z line (all runs done) or a t line (watchdog).
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

module tb_ddr3_reader;
    parameter integer TRITS = 1237;
    parameter integer SEED = 98;
    parameter integer CAP = 64;
    parameter integer RUNS = 4;
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
    wire [15:0] dq;
    wire [1:0] dqs_p, dqs_n, dm;
    tms_ddr3_ax7203 #(.BYTE_LANES(2), .REPORT_PERIOD(20000), .BUILD_ID(32'h5eedc0de), .UART_DIV(UART_DIV),
                      .READER_TRITS(TRITS), .READER_SEED(SEED), .READER_CAP(CAP), .READER_RUNS(RUNS),
                      .READER_WATCHDOG(WATCHDOG)) dut (
        .clk200_p(clk200), .clk200_n(!clk200), .rst_n(rst_n), .led(led), .uart_tx(uart_tx),
        .ddr3_ck_p(ck_p), .ddr3_ck_n(ck_n), .ddr3_reset_n(reset_n), .ddr3_cke(cke), .ddr3_cs_n(cs_n),
        .ddr3_ras_n(ras_n), .ddr3_cas_n(cas_n), .ddr3_we_n(we_n), .ddr3_odt(odt), .ddr3_addr(addr), .ddr3_ba(ba),
        .ddr3_dq(dq), .ddr3_dqs_p(dqs_p), .ddr3_dqs_n(dqs_n), .ddr3_dm(dm)
    );

    // ---- Wishbone monitor, per phase ----
    wire mclk = dut.clk_ctrl;
    reg  in_phase = 1'b0, ph_we = 1'b0;
    integer ph_n = 0;
    reg [63:0] ph_clocks, ph_taken, ph_acks, ph_cmd, ph_wait, ph_outst, ph_max;
    always @(posedge mclk) begin
        if (dut.ddr3.o_calib_complete && dut.wb_cyc) begin
            if (!in_phase && dut.wb_stb) begin
                in_phase = 1'b1;
                ph_we = dut.wb_we;
                ph_clocks = 0; ph_taken = 0; ph_acks = 0; ph_cmd = 0; ph_wait = 0; ph_outst = 0; ph_max = 0;
            end
            if (in_phase) begin
                if (dut.wb_stb && dut.wb_we != ph_we) $fatal(1, "monitor: a request of the other direction inside a phase");
                ph_clocks = ph_clocks + 1;
                if (ph_outst > ph_max) ph_max = ph_outst;
                if (dut.wb_stb && dut.wb_stall) ph_cmd = ph_cmd + 1;
                if (ph_outst != 0 && !dut.wb_ack) ph_wait = ph_wait + 1;
                if (dut.wb_ack) begin ph_acks = ph_acks + 1; ph_outst = ph_outst - 1; end
                if (dut.wb_stb && !dut.wb_stall) begin ph_taken = ph_taken + 1; ph_outst = ph_outst + 1; end
                // The phase is over at the ack that leaves no request outstanding while none is
                // presented (the reader keeps stb high while it has requests left and room under its cap).
                if (ph_outst == 0 && dut.wb_ack && !dut.wb_stb) begin
                    $display("TB_PHASE n=%0d we=%0d clocks=%0d taken=%0d acks=%0d cmd_stalls=%0d wait_stalls=%0d max_outstanding=%0d",
                             ph_n, ph_we, ph_clocks, ph_taken, ph_acks, ph_cmd, ph_wait, ph_max);
                    ph_n = ph_n + 1;
                    in_phase = 1'b0;
                end
            end
        end
    end

    reg [4095:0] path, dump_path;
    integer fd, lines, dump_words, k;
    reg [7:0] byte_r, first;
    reg at_line_start;
    event finish_later, finish_now;
    always @(finish_later) begin
        #(BIT_NS * 20 * 30);
        -> finish_now;
    end
    always @(finish_now) begin
        $fclose(fd);
        if ($value$plusargs("dump=%s", dump_path)) begin
            if (!$value$plusargs("dump_words=%d", dump_words)) dump_words = 0;
            fd = $fopen(dump_path, "w");
            for (k = 0; k < dump_words; k = k + 1) $fwrite(fd, "%032h\n", dut.ddr3.mem[k]);
            $fclose(fd);
        end
        $display("TB_PASS lines=%0d leds=%b phases=%0d", lines, led, ph_n);
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
        $fatal(1, "timeout waiting for the reader");
    end
    // Each line as "<simulation time in ns at its first byte>\t<text>".
    always begin : receiver
        integer b;
        @(negedge uart_tx);
        #(BIT_NS * 1.5);
        byte_r = 8'd0;
        for (b = 0; b < 8; b = b + 1) begin
            byte_r[b] = uart_tx;
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
            if (first == "z" || first == "t") -> finish_later;
        end
    end
endmodule
`default_nettype wire
