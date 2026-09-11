// Simulation-only models of the two Xilinx primitives the top uses.
`timescale 1ns/1ps
module IBUFDS (input wire I, input wire IB, output wire O);
    assign O = I;
endmodule
module BUFG (input wire I, output wire O);
    assign O = I;
endmodule
