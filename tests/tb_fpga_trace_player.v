// Pre-silicon check of the AX7203 trace player: runs the whole design with a
// simulated 200 MHz differential clock, decodes the 20-byte UART report lines and writes
// it to the file named by +capture=, so tools/fpga-capture.py --from-file can
// apply the same comparison it applies to the real board. The second run
// (triggered by a start bit on uart_rx) is stopped at its header line.
`timescale 1ns/1ps
`default_nettype none
module tb_fpga_trace_player;
    parameter integer CLOCK_MODE = 1;
    parameter integer TICK_DIV = 8;
    parameter integer BAUD_DIV = 4;
    localparam real BIT_NS = 5.0 * TICK_DIV * BAUD_DIV;
    reg clk200 = 1'b0;
    always #2.5 clk200 = !clk200;
    reg rst_n = 1'b0;
    reg uart_rx = 1'b1;
    wire [3:0] led;
    wire uart_tx;
    tms_trace_player_ax7203 #(.CLOCK_MODE(CLOCK_MODE), .TICK_DIV(TICK_DIV), .BAUD_DIV(BAUD_DIV), .BUILD_ID(32'h5eedc0de)) dut (
        .clk200_p(clk200), .clk200_n(!clk200), .rst_n(rst_n), .led(led), .uart_tx(uart_tx), .uart_rx(uart_rx)
    );
    reg [4095:0] path;
    integer fd, i, headers, lines;
    reg [7:0] byte_r, first;
    reg at_line_start;
    event done_line;
    // trigger a second run with a start bit from a separate process, so the
    // receiver above keeps decoding while uart_rx is driven
    always @(done_line) begin
        #(BIT_NS * 4);
        uart_rx = 1'b0;
        #(BIT_NS * 10);
        uart_rx = 1'b1;
    end
    initial begin
        if (!$value$plusargs("capture=%s", path)) $fatal(1, "missing plusarg capture=");
        fd = $fopen(path, "w");
        headers = 0; lines = 0; at_line_start = 1'b1; first = 8'd0;
        #200 rst_n = 1'b1;
    end
    initial begin
        #400_000_000;
        $fatal(1, "timeout waiting for the player");
    end
    always begin
        @(negedge uart_tx);
        #(BIT_NS * 1.5);
        byte_r = 8'd0;
        for (i = 0; i < 8; i = i + 1) begin
            byte_r[i] = uart_tx;
            #(BIT_NS);
        end
        if (uart_tx !== 1'b1) $fatal(1, "framing error: stop bit low");
        $fwrite(fd, "%c", byte_r);
        if (at_line_start) begin
            first = byte_r;
            at_line_start = 1'b0;
            if (byte_r == "H") begin
                headers = headers + 1;
                if (headers == 2) begin
                    $fclose(fd);
                    $display("TB_PASS second run started after uart_rx trigger; lines=%0d leds=%b", lines, led);
                    $finish;
                end
            end
        end
        if (byte_r == 8'h0a) begin
            lines = lines + 1;
            at_line_start = 1'b1;
            if (first == "D") begin
                $display("TB_DONE_LINE lines=%0d leds=%b", lines, led);
                -> done_line;
            end
        end
    end
endmodule
`default_nettype wire
