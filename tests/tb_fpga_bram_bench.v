// Pre-silicon check of the AX7203 block-RAM trit packing bench: runs the whole
// design with a simulated 200 MHz differential clock, decodes the 20-byte UART
// report lines into the file named by +capture=, so tools/fpga-bram-capture.py
// --from-file applies the comparison it applies to the board. After the first
// run's totals line it sends the byte "r" (three falling edges) on uart_rx; the
// bench must start exactly one more run, so the test fails on a third header.
`timescale 1ns/1ps
`default_nettype none
module tb_fpga_bram_bench;
    parameter integer CLOCK_MODE = 1;
    parameter integer TICK_DIV = 8;
    parameter integer BAUD_DIV = 4;
    parameter integer ONLY = -1;
    localparam real BIT_NS = 5.0 * TICK_DIV * BAUD_DIV;
    reg clk200 = 1'b0;
    always #2.5 clk200 = !clk200;
    reg rst_n = 1'b0;
    reg uart_rx = 1'b1;
    wire [3:0] led;
    wire uart_tx;
    tms_bram_bench_ax7203 #(.CLOCK_MODE(CLOCK_MODE), .TICK_DIV(TICK_DIV), .BAUD_DIV(BAUD_DIV), .ONLY(ONLY), .BUILD_ID(32'h5eedc0de)) dut (
        .clk200_p(clk200), .clk200_n(!clk200), .rst_n(rst_n), .led(led), .uart_tx(uart_tx), .uart_rx(uart_rx)
    );
    reg [4095:0] path;
    integer fd, i, headers, lines, totals;
    reg [7:0] byte_r, first, trigger;
    reg at_line_start;
    event send_trigger, finish_later;
    // Drive one UART frame of `trigger` on uart_rx from a separate process.
    always @(send_trigger) begin
        trigger = "r";
        #(BIT_NS * 4);
        uart_rx = 1'b0;
        #(BIT_NS);
        for (i = 0; i < 8; i = i + 1) begin
            uart_rx = trigger[i];
            #(BIT_NS);
        end
        uart_rx = 1'b1;
    end
    // After the second run, wait long enough for a spurious third header.
    always @(finish_later) begin
        #(BIT_NS * 20 * 40);
        $fclose(fd);
        if (headers != 2) $fatal(1, "expected exactly two runs, saw %0d headers", headers);
        $display("TB_PASS two runs, one per trigger; lines=%0d leds=%b", lines, led);
        $finish;
    end
    initial begin
        if (!$value$plusargs("capture=%s", path)) $fatal(1, "missing plusarg capture=");
        fd = $fopen(path, "w");
        headers = 0; lines = 0; totals = 0; at_line_start = 1'b1; first = 8'd0;
        #200 rst_n = 1'b1;
    end
    initial begin
        #400_000_000;
        $fatal(1, "timeout waiting for the bench");
    end
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
        $fwrite(fd, "%c", byte_r);
        if (at_line_start) begin
            first = byte_r;
            at_line_start = 1'b0;
            if (byte_r == "H") headers = headers + 1;
        end
        if (byte_r == 8'h0a) begin
            lines = lines + 1;
            at_line_start = 1'b1;
            if (first == "D") begin
                totals = totals + 1;
                $display("TB_DONE_LINE run=%0d lines=%0d leds=%b", totals, lines, led);
                if (totals == 1) -> send_trigger;
                if (totals == 2) -> finish_later;
            end
        end
    end
endmodule
`default_nettype wire
