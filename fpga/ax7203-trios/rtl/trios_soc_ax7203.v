// AX7203 (XC7A200T-FBG484-2) wrapper: 200 MHz LVDS oscillator -> IBUFDS -> BUFG, a
// 3-bit counter, and its bit 2 on a second BUFG gives 25 MHz for the core (the same
// no-PLL scheme as the trace player and the block-RAM bench, which ran on this board).
`timescale 1ns/1ps
`default_nettype none
module trios_soc_ax7203 #(
    parameter integer BAUD_DIV = 217,
    parameter [31:0] BUILD_ID = 32'h0
) (
    input  wire       clk200_p,
    input  wire       clk200_n,
    output wire [3:0] led,
    output wire       uart_tx,
    input  wire       uart_rx
);
    wire clk200_raw, clk200, clk25;
    reg [2:0] div = 3'd0;
    IBUFDS clk_ibufds (.I(clk200_p), .IB(clk200_n), .O(clk200_raw));
    BUFG clk200_bufg (.I(clk200_raw), .O(clk200));
    always @(posedge clk200) div <= div + 3'd1;
    BUFG clk_bufg (.I(div[2]), .O(clk25));

    trios_soc_core #(.BAUD_DIV(BAUD_DIV), .BUILD_ID(BUILD_ID)) core (
        .clk(clk25), .led(led), .uart_tx(uart_tx), .uart_rx(uart_rx));
endmodule
`default_nettype wire
