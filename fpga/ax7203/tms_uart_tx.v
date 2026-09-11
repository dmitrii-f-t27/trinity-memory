// 8N1 UART transmitter: one start bit, eight data bits LSB first, one stop bit.
`timescale 1ns/1ps
`default_nettype none
module tms_uart_tx #(
    parameter integer BAUD_DIV = 217   // ticks per bit (25 M ticks/s / 115200 = 217.01)
) (
    input  wire       clk,
    input  wire       tick,            // clock enable; the bit timer counts ticks
    input  wire       rst,
    input  wire [7:0] data,
    input  wire       start,
    output reg        busy,
    output wire       tx
);
    reg [9:0]  shift = 10'h3ff;
    reg [15:0] count = 16'd0;
    reg [3:0]  bits = 4'd0;
    assign tx = busy ? shift[0] : 1'b1;
    always @(posedge clk) if (tick) begin
        if (rst) begin
            busy <= 1'b0;
            shift <= 10'h3ff;
            count <= 16'd0;
            bits <= 4'd0;
        end else if (!busy) begin
            if (start) begin
                shift <= {1'b1, data, 1'b0};
                count <= BAUD_DIV - 1;
                bits <= 4'd10;
                busy <= 1'b1;
            end
        end else if (count != 16'd0) begin
            count <= count - 1'b1;
        end else begin
            shift <= {1'b1, shift[9:1]};
            count <= BAUD_DIV - 1;
            bits <= bits - 1'b1;
            if (bits == 4'd1)
                busy <= 1'b0;
        end
    end
endmodule
`default_nettype wire
