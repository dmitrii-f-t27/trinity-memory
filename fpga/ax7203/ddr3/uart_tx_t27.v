// The `uart_tx` module that UberDDR3's controller instantiates when it is read
// with `define UART_DEBUG_BIST` (ddr3_controller.v 79d8fd3e L3516-3528: its
// debug text at 9600 baud), made of our t27 transmitter (t27/rtl/fpga_uart_tx.t27,
// 8N1). Wiring only; used by the UART_DEBUG_BIST build of #61
// (make -C fpga/ax7203 ... DDR3_UART_DEBUG_BIST=1), not by any other build.
//
// UberDDR3's handshake: it raises uart_tx_en with the byte while uart_tx_busy is
// low and lowers it once busy is seen; the t27 transmitter takes `start` only
// while idle and stays busy through the stop bit, so a byte is sent once.
// Bit period: CLK_HZ / BIT_RATE clocks. UberDDR3 passes CLK_HZ =
// (1_000_000 / CONTROLLER_CLK_PERIOD) * 1_000_000 = 83,000,000 for 12,000 ps, so
// at the real 83.33 MHz the line runs at 83.33e6 / 8645 = 9,640 baud (+0.4 % from
// 9600, inside a UART receiver's tolerance).
`timescale 1ns/1ps
`default_nettype none
module uart_tx #(
    parameter integer BIT_RATE = 9600,
    parameter integer CLK_HZ = 100_000_000,
    parameter integer PAYLOAD_BITS = 8,     // 8 only (the t27 transmitter is 8N1)
    parameter integer STOP_BITS = 1         // 1 only
) (
    input  wire       clk,
    input  wire       resetn,
    output wire       uart_txd,
    output wire       uart_tx_busy,
    input  wire       uart_tx_en,
    input  wire [7:0] uart_tx_data
);
    localparam integer BAUD_DIV = CLK_HZ / BIT_RATE;
    TrinityFpgaUartTxT27 tx (
        .clk(clk), .rst_n(resetn), .en(1'b1), .ready(),
        .start(uart_tx_en), .data({24'd0, uart_tx_data}), .baud_div(BAUD_DIV),
        .busy(uart_tx_busy), .shift(), .count(), .bits(), .tx(uart_txd)
    );
endmodule
`default_nettype wire
