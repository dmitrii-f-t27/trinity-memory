// Cycle-exact trace replay for the joined path ternary_stream_dot_t27
// (specs/memory/stream_compute.t27, issue #15): ready-capable storage -> join
// -> framed dot pipeline.
// stimulus word: [0] rst, [1] start, [2] load_en, [8:3] load_addr, [18:9] load_code,
//                [19] act_valid, [59:20] act_data (five int8 lanes), [60] out_ready
// expected word: [31:0] result (two's complement, sign-extended from ACC_WIDTH),
//                [32] out_error, [33] out_valid, [34] act_ready, [35] load_ready,
//                [36] busy, [37] beat
// Stimulus is applied at the falling edge; outputs are compared one time unit
// after the rising edge with the same stimulus still applied.
`timescale 1ns/1ps
`default_nettype none
module tb_spec_join_trace;
    parameter integer DENSE5 = 1;
    parameter integer TRIT_COUNT = 12;
    parameter integer ACC_WIDTH = 32;
    parameter integer CYCLES = 1;
    localparam integer CODE_WIDTH = (DENSE5 != 0) ? 8 : 10;
    localparam integer WORDS = (TRIT_COUNT + 4) / 5;
    localparam integer ADDR_WIDTH = (WORDS > 1) ? $clog2(WORDS) : 1;
    reg clk = 1'b0;
    always #5 clk = !clk;
    reg rst = 1'b1;
    reg start = 1'b0;
    reg load_en = 1'b0;
    reg [5:0] load_addr_full = 6'd0;
    reg [9:0] load_code_full = 10'd0;
    reg act_valid = 1'b0;
    reg [39:0] act_data = 40'd0;
    reg out_ready = 1'b0;
    wire busy, load_ready, act_ready, beat, out_valid, out_error;
    wire signed [ACC_WIDTH-1:0] out_result;
    ternary_stream_dot_t27 #(.DENSE5(DENSE5), .TRIT_COUNT(TRIT_COUNT), .ACC_WIDTH(ACC_WIDTH)) dut (
        .clk(clk), .rst(rst), .start(start), .busy(busy), .load_en(load_en),
        .load_addr(load_addr_full[ADDR_WIDTH-1:0]), .load_code(load_code_full[CODE_WIDTH-1:0]),
        .load_ready(load_ready), .act_valid(act_valid), .act_ready(act_ready), .act_data(act_data),
        .beat(beat), .out_valid(out_valid), .out_ready(out_ready), .out_result(out_result), .out_error(out_error)
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
            rst = stimulus[cycle][0];
            start = stimulus[cycle][1];
            load_en = stimulus[cycle][2];
            load_addr_full = stimulus[cycle][8:3];
            load_code_full = stimulus[cycle][18:9];
            act_valid = stimulus[cycle][19];
            act_data = stimulus[cycle][59:20];
            out_ready = stimulus[cycle][60];
            @(posedge clk);
            #1;
            widened = $signed(out_result);
            if (widened !== $signed(expected[cycle][31:0]) || out_error !== expected[cycle][32] ||
                out_valid !== expected[cycle][33] || act_ready !== expected[cycle][34] ||
                load_ready !== expected[cycle][35] || busy !== expected[cycle][36] || beat !== expected[cycle][37])
                $fatal(1, "TRACE_MISMATCH cycle=%0d result=%0d/%0d error=%b/%b valid=%b/%b act_ready=%b/%b load_ready=%b/%b busy=%b/%b beat=%b/%b",
                       cycle, widened, $signed(expected[cycle][31:0]), out_error, expected[cycle][32],
                       out_valid, expected[cycle][33], act_ready, expected[cycle][34],
                       load_ready, expected[cycle][35], busy, expected[cycle][36], beat, expected[cycle][37]);
        end
        $display("TRACE_PASS cycles=%0d", CYCLES);
        $finish;
    end
endmodule
`default_nettype wire
