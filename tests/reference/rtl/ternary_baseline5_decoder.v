// Five independent 2-bit lanes: 00=0, 01=+1, 10=-1, 11=invalid.
`timescale 1ns/1ps
`default_nettype none
module ternary_baseline5_decoder (
    input wire [9:0] code,
    output reg [9:0] trits,
    output reg valid
);
    integer lane;
    always @* begin
        valid = 1'b1;
        for (lane = 0; lane < 5; lane = lane + 1) begin
            case (code[lane*2 +: 2])
                2'b00, 2'b01, 2'b10: begin end
                default: valid = 1'b0;
            endcase
        end
        trits = valid ? code : 10'd0;
    end
endmodule
`default_nettype wire
