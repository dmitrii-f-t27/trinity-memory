// Five ternary weights x five signed int8 activations, framed ready/valid dot.
// DENSE5=1 selects base-3 byte words; DENSE5=0 selects five 2-bit lanes.
// Checked accumulation: any malformed beat or signed overflow poisons the frame.
`timescale 1ns/1ps
`default_nettype none
module trinity_dot_stream #(
    parameter integer DENSE5 = 1,
    parameter integer ACC_WIDTH = 32  // Must be at least 12.
) (
    input wire clk,
    input wire rst,
    input wire in_valid,
    output wire in_ready,
    input wire [9:0] in_code,
    input wire [39:0] in_activations,
    input wire [4:0] in_mask,
    input wire in_last,
    output reg out_valid,
    input wire out_ready,
    output reg signed [ACC_WIDTH-1:0] out_result,
    output reg out_error
);
    wire [9:0] decoded;
    wire code_valid;
    generate
        if (DENSE5 != 0) begin : dense
            ternary_dense5_decoder decoder (
                .code(in_code[7:0]), .trits(decoded), .valid(code_valid)
            );
        end else begin : baseline
            ternary_baseline5_decoder decoder (
                .code(in_code), .trits(decoded), .valid(code_valid)
            );
        end
    endgenerate

    reg signed [11:0] group_sum;
    reg group_error;
    reg signed [11:0] activation;
    integer lane;
    always @* begin
        group_sum = 12'sd0;
        group_error = !code_valid;
        activation = 12'sd0;
        if (DENSE5 != 0) begin
            case (in_code[9:8])
                2'b00: begin end
                default: group_error = 1'b1;
            endcase
        end
        case (in_last)
            1'b0: begin
                case (in_mask)
                    5'b11111: begin end
                    default: group_error = 1'b1;
                endcase
            end
            1'b1: begin
                case (in_mask)
                    5'b00000, 5'b00001, 5'b00011,
                    5'b00111, 5'b01111, 5'b11111: begin end
                    default: group_error = 1'b1;
                endcase
            end
            default: group_error = 1'b1;
        endcase
        for (lane = 0; lane < 5; lane = lane + 1) begin
            // Widen before negation: -(-128) is +128, not int8 wraparound.
            activation = {{4{in_activations[lane*8+7]}},
                          in_activations[lane*8 +: 8]};
            case (in_mask[lane])
                1'b1: begin
                    case (decoded[lane*2 +: 2])
                        2'b00: begin end
                        2'b01: group_sum = group_sum + activation;
                        2'b10: group_sum = group_sum - activation;
                        default: group_error = 1'b1;
                    endcase
                end
                1'b0: begin
                    // Canonical padding applies to BOTH streams.
                    case ({decoded[lane*2 +: 2], in_activations[lane*8 +: 8]})
                        10'd0: begin end
                        default: group_error = 1'b1;
                    endcase
                end
                default: group_error = 1'b1;
            endcase
        end
    end

    reg stage_valid;
    reg signed [11:0] stage_sum;
    reg stage_error;
    reg stage_last;
    reg stage_empty;
    reg signed [ACC_WIDTH-1:0] accumulator;
    reg frame_error;
    reg frame_active;
    wire accumulate_ready = !out_valid || out_ready;
    assign in_ready = !rst && (!stage_valid || accumulate_ready);
    wire signed [ACC_WIDTH:0] extended_sum =
        $signed({accumulator[ACC_WIDTH-1], accumulator}) +
        $signed({{(ACC_WIDTH-11){stage_sum[11]}}, stage_sum});
    wire overflow = extended_sum[ACC_WIDTH] != extended_sum[ACC_WIDTH-1];
    wire next_error = frame_error || stage_error || overflow ||
                      (stage_empty && frame_active);

    always @(posedge clk) begin
        if (rst) begin
            stage_valid <= 1'b0;
            stage_sum <= 12'sd0;
            stage_error <= 1'b0;
            stage_last <= 1'b0;
            stage_empty <= 1'b0;
            accumulator <= {ACC_WIDTH{1'b0}};
            frame_error <= 1'b0;
            frame_active <= 1'b0;
            out_valid <= 1'b0;
            out_result <= {ACC_WIDTH{1'b0}};
            out_error <= 1'b0;
        end else begin
            if (out_valid && out_ready)
                out_valid <= 1'b0;
            if (stage_valid && accumulate_ready) begin
                stage_valid <= 1'b0;
                if (stage_last) begin
                    out_valid <= 1'b1;
                    out_error <= next_error;
                    out_result <= next_error ? {ACC_WIDTH{1'b0}} :
                                               extended_sum[ACC_WIDTH-1:0];
                    accumulator <= {ACC_WIDTH{1'b0}};
                    frame_error <= 1'b0;
                    frame_active <= 1'b0;
                end else begin
                    accumulator <= next_error ? {ACC_WIDTH{1'b0}} :
                                                extended_sum[ACC_WIDTH-1:0];
                    frame_error <= next_error;
                    frame_active <= 1'b1;
                end
            end
            // A simultaneously retired group can be replaced every clock.
            if (in_valid && in_ready) begin
                stage_valid <= 1'b1;
                stage_sum <= group_sum;
                stage_error <= group_error;
                stage_last <= in_last;
                stage_empty <= (in_mask == 5'b00000);
            end
        end
    end
endmodule
`default_nettype wire
