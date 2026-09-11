// Cycle-exact trace replay for the native storage stream (sequencer + view).
// stimulus word: [0] rst, [1] start, [2] load_en, [8:3] load_addr, [18:9] load_code,
//                [19] out_stall (downstream not ready; the sequencer holds the word)
// expected word: [9:0] out_trits, [14:10] out_lane_mask, [15] out_code_valid,
//                [16] out_last, [17] out_valid, [18] busy, [19] load_ready
`timescale 1ns/1ps
`default_nettype none
module tb_spec_storage_trace;
    parameter integer DENSE5 = 1;
    parameter integer TRIT_COUNT = 12;
    parameter integer CYCLES = 1;
    localparam integer WORDS = (TRIT_COUNT + 4) / 5;
    localparam integer ADDR_WIDTH = (WORDS > 1) ? $clog2(WORDS) : 1;
    reg clk = 1'b0;
    always #5 clk = !clk;
    reg rst = 1'b1;
    reg start = 1'b0;
    reg load_en = 1'b0;
    reg [5:0] load_addr_full = 6'd0;
    reg [9:0] load_code_full = 10'd0;
    reg out_stall = 1'b0;
    wire busy, load_ready, out_valid, out_last, out_code_valid;
    wire [4:0] out_lane_mask;
    wire [9:0] out_trits;
    // The adapter with the downstream ready exposed; the dense5/baseline5
    // wrappers tie it high and are covered by the RTL parity suite.
    ternary_stream_adapter_t27 #(.DENSE5(DENSE5), .CODE_WIDTH((DENSE5 != 0) ? 8 : 10), .TRIT_COUNT(TRIT_COUNT)) dut (
        .clk(clk), .rst(rst), .start(start), .busy(busy), .load_en(load_en),
        .load_addr(load_addr_full[ADDR_WIDTH-1:0]), .load_code(load_code_full[((DENSE5 != 0) ? 8 : 10)-1:0]),
        .out_ready(!out_stall),
        .load_ready(load_ready), .out_valid(out_valid), .out_last(out_last),
        .out_code_valid(out_code_valid), .out_lane_mask(out_lane_mask), .out_trits(out_trits)
    );
    reg [63:0] stimulus [0:CYCLES-1];
    reg [63:0] expected [0:CYCLES-1];
    reg [4095:0] stimulus_path;
    reg [4095:0] expected_path;
    integer cycle;
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
            out_stall = stimulus[cycle][19];
            @(posedge clk);
            #1;
            if (out_trits !== expected[cycle][9:0] || out_lane_mask !== expected[cycle][14:10] ||
                out_code_valid !== expected[cycle][15] || out_last !== expected[cycle][16] ||
                out_valid !== expected[cycle][17] || busy !== expected[cycle][18] ||
                load_ready !== expected[cycle][19])
                $fatal(1, "TRACE_MISMATCH cycle=%0d trits=%0d/%0d mask=%0d/%0d code_valid=%b/%b last=%b/%b valid=%b/%b busy=%b/%b load_ready=%b/%b",
                       cycle, out_trits, expected[cycle][9:0], out_lane_mask, expected[cycle][14:10],
                       out_code_valid, expected[cycle][15], out_last, expected[cycle][16],
                       out_valid, expected[cycle][17], busy, expected[cycle][18], load_ready, expected[cycle][19]);
        end
        $display("TRACE_PASS cycles=%0d", CYCLES);
        $finish;
    end
endmodule
`default_nettype wire
