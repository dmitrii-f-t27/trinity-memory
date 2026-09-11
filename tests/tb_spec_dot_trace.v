// Cycle-exact trace replay for trinity_dot_stream_t27 (specs/memory/stream_compute.t27).
// stimulus word: [9:0] code, [49:10] activations, [54:50] mask, [55] last,
//                [56] reset, [57] in_valid, [58] out_ready
// expected word: [31:0] result (two's complement, sign-extended from ACC_WIDTH),
//                [32] out_error, [33] out_valid, [34] in_ready
// Stimulus is applied at the falling edge; outputs are compared one time unit
// after the rising edge with the same stimulus still applied.
`timescale 1ns/1ps
`default_nettype none
module tb_spec_dot_trace;
    parameter integer DENSE5 = 1;
    parameter integer ACC_WIDTH = 32;
    parameter integer CYCLES = 1;
    reg clk = 1'b0;
    always #5 clk = !clk;
    reg rst = 1'b1;
    reg in_valid = 1'b0;
    wire in_ready;
    reg [9:0] in_code = 10'd0;
    reg [39:0] in_activations = 40'd0;
    reg [4:0] in_mask = 5'd0;
    reg in_last = 1'b0;
    wire out_valid;
    reg out_ready = 1'b0;
    wire signed [ACC_WIDTH-1:0] out_result;
    wire out_error;
    trinity_dot_stream_t27 #(.DENSE5(DENSE5), .ACC_WIDTH(ACC_WIDTH)) dut (
        .clk(clk), .rst(rst), .in_valid(in_valid), .in_ready(in_ready),
        .in_code(in_code), .in_activations(in_activations),
        .in_mask(in_mask), .in_last(in_last), .out_valid(out_valid),
        .out_ready(out_ready), .out_result(out_result), .out_error(out_error)
    );
    reg [63:0] stimulus [0:CYCLES-1];
    reg [63:0] expected [0:CYCLES-1];
    reg [4095:0] stimulus_path;
    reg [4095:0] expected_path;
    integer cycle;
    reg signed [31:0] widened;
    initial begin
        if (!$value$plusargs("stimulus=%s", stimulus_path) || !$value$plusargs("expected=%s", expected_path))
            $fatal(1, "missing plusargs stimulus= expected=");
        $readmemh(stimulus_path, stimulus);
        $readmemh(expected_path, expected);
        for (cycle = 0; cycle < CYCLES; cycle = cycle + 1) begin
            @(negedge clk);
            in_code = stimulus[cycle][9:0];
            in_activations = stimulus[cycle][49:10];
            in_mask = stimulus[cycle][54:50];
            in_last = stimulus[cycle][55];
            rst = stimulus[cycle][56];
            in_valid = stimulus[cycle][57];
            out_ready = stimulus[cycle][58];
            @(posedge clk);
            #1;
            widened = $signed(out_result);
            if (in_ready !== expected[cycle][34] || out_valid !== expected[cycle][33] ||
                out_error !== expected[cycle][32] || widened !== $signed(expected[cycle][31:0]))
                $fatal(1, "TRACE_MISMATCH cycle=%0d in_ready=%b/%b out_valid=%b/%b out_error=%b/%b result=%0d/%0d",
                       cycle, in_ready, expected[cycle][34], out_valid, expected[cycle][33],
                       out_error, expected[cycle][32], widened, $signed(expected[cycle][31:0]));
        end
        $display("TRACE_PASS cycles=%0d", CYCLES);
        $finish;
    end
endmodule
`default_nettype wire
