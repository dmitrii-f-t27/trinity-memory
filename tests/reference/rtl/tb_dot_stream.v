// File-driven scoreboard. Stimulus inserts seeded bubbles, stalls, and resets.
// packets: [57]=reset-pending-output ([31:0]=prior results to drain),
//          [56]=reset, [55]=last, [54:50]=mask,
//          [49:10]=five int8 activations, [9:0]=encoded weights.
// expected: [32]=error, [31:0]=signed result.
`timescale 1ns/1ps
`default_nettype none
module tb_dot_stream;
    parameter integer DENSE5 = 1;
    parameter integer ACC_WIDTH = 32;
    parameter integer MAX_PACKETS = 1;
    parameter integer MAX_RESULTS = 1;
    reg clk = 1'b0;
    always #5 clk = !clk;
    reg rst = 1'b1;
    reg in_valid = 1'b0;
    wire in_ready;
    // Start unknown so the first driven code triggers the reused Verilog
    // decoder's always @* even when that first code is zero.
    reg [9:0] in_code;
    reg [39:0] in_activations = 40'd0;
    reg [4:0] in_mask = 5'd0;
    reg in_last = 1'b0;
    wire out_valid;
    reg out_ready = 1'b0;
    wire signed [ACC_WIDTH-1:0] out_result;
    wire out_error;
    trinity_dot_stream #(.DENSE5(DENSE5), .ACC_WIDTH(ACC_WIDTH)) dut (
        .clk(clk), .rst(rst), .in_valid(in_valid), .in_ready(in_ready),
        .in_code(in_code), .in_activations(in_activations),
        .in_mask(in_mask), .in_last(in_last), .out_valid(out_valid),
        .out_ready(out_ready), .out_result(out_result), .out_error(out_error)
    );

    reg [63:0] packets [0:MAX_PACKETS-1];
    reg [63:0] expected [0:MAX_RESULTS-1];
    integer packet_count;
    integer result_count;
    integer position = 0;
    integer received = 0;
    integer cycles = 0;
    integer groups = 0;
    integer input_stalls = 0;
    integer output_stalls = 0;
    integer source_bubbles = 0;
    integer resets = 0;
    integer reset_cycles = 0;
    integer held_cycles = 0;
    integer timeout_cycles;
    integer drain_cycles = 0;
    reg [31:0] rng;
    reg hold_output;
    reg signed [ACC_WIDTH-1:0] held_result;
    reg held_error;
    reg [1023:0] packet_path;
    reg [1023:0] expected_path;

    function [31:0] random_step;
        input [31:0] value;
        reg [31:0] temporary;
        begin
            temporary = value ^ (value << 13);
            temporary = temporary ^ (temporary >> 17);
            random_step = temporary ^ (temporary << 5);
        end
    endfunction

    initial begin
        if (!$value$plusargs("packets=%s", packet_path) ||
            !$value$plusargs("expected=%s", expected_path) ||
            !$value$plusargs("packet_count=%d", packet_count) ||
            !$value$plusargs("result_count=%d", result_count) ||
            !$value$plusargs("seed=%d", rng))
            $fatal(1, "missing testbench plusargs");
        if (packet_count != MAX_PACKETS || result_count != MAX_RESULTS ||
            ACC_WIDTH < 12 || ACC_WIDTH > 32)
            $fatal(1, "invalid simulation dimensions");
        if (rng == 0) rng = 32'h6d2b79f5;
        timeout_cycles = 100 * packet_count + 100 * result_count + 1000;
        $readmemh(packet_path, packets);
        $readmemh(expected_path, expected);
        repeat (2) @(posedge clk);
        forever begin
            @(negedge clk);
            cycles = cycles + 1;
            rng = random_step(rng);
            rst = 1'b0;
            // Every result is deliberately stalled at least three cycles.
            out_ready = (held_cycles >= 3) && (rng[2:0] != 3'b000);
            if (reset_cycles > 0) begin
                rst = 1'b1;
                in_valid = 1'b0;
                out_ready = 1'b0;
                reset_cycles = reset_cycles - 1;
            end else if (!in_valid && position < packet_count &&
                         (packets[position][56] || packets[position][57])) begin
                // Earlier frames may still be blocked in the pipeline. Drain
                // their scored results before discarding this unscored result.
                if (packets[position][56] || received == packets[position][31:0]) begin
                    out_ready = 1'b0;
                    if (packets[position][56] || out_valid) begin
                        rst = 1'b1;
                        reset_cycles = 1;
                        resets = resets + 1;
                        position = position + 1;
                    end
                end
            end else if (!in_valid && position < packet_count) begin
                if (rng[4:3] != 2'b00) begin
                    {in_last, in_mask, in_activations, in_code} = packets[position][55:0];
                    in_valid = 1'b1;
                end else begin
                    source_bubbles = source_bubbles + 1;
                end
            end
            @(posedge clk);
            hold_output = out_valid && !out_ready && !rst;
            held_result = out_result;
            held_error = out_error;
            if (!rst) begin
                if (in_valid && !in_ready) input_stalls = input_stalls + 1;
                if (out_valid && !out_ready) begin
                    output_stalls = output_stalls + 1;
                    held_cycles = held_cycles + 1;
                end
                if (in_valid && in_ready) begin
                    position = position + 1;
                    groups = groups + 1;
                    // Nonblocking avoids racing the DUT's input sampling.
                    in_valid <= 1'b0;
                end
                if (out_valid && out_ready) begin
                    if (received >= result_count)
                        $fatal(1, "unexpected extra output");
                    if (out_error !== expected[received][32] ||
                        $signed(out_result) !== $signed(expected[received][31:0]))
                        $fatal(1, "result %0d mismatch actual=%0d error=%b expected=%0d error=%b",
                               received, out_result, out_error,
                               $signed(expected[received][31:0]), expected[received][32]);
                    $display("DOT_RESULT index=%0d result=%0d error=%0d cycles=%0d",
                             received, out_result, out_error, cycles);
                    received = received + 1;
                    held_cycles = 0;
                end
            end else begin
                held_cycles = 0;
            end
            #1;
            if (hold_output && (out_valid !== 1'b1 || out_result !== held_result ||
                                out_error !== held_error))
                $fatal(1, "result changed under backpressure");
            if (rst && (out_valid !== 1'b0 || in_ready !== 1'b0))
                $fatal(1, "reset failed to flush result or suppress input ready");
            if (position == packet_count && received == result_count &&
                !in_valid && !out_valid && !rst) begin
                drain_cycles = drain_cycles + 1;
                if (drain_cycles == 4) begin
                    $display("DOT_SUMMARY cycles=%0d groups=%0d input_stalls=%0d output_stalls=%0d source_bubbles=%0d resets=%0d",
                             cycles, groups, input_stalls, output_stalls, source_bubbles, resets);
                    $finish;
                end
            end else drain_cycles = 0;
            if (cycles > timeout_cycles)
                $fatal(1, "simulation timeout position=%0d received=%0d", position, received);
        end
    end
endmodule
`default_nettype wire
