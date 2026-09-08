`timescale 1ns/1ps
`default_nettype none
module tb_decoders;
    reg [7:0] dense_code;
    reg [3:0] sparse_code;
    reg [9:0] baseline_code;
    wire [9:0] dense_trits, baseline_trits;
    wire [7:0] sparse_trits;
    wire dense_valid, sparse_valid, baseline_valid;
    reg [10:0] dense_expected [0:255];
    reg [8:0] sparse_expected [0:15];
    reg [10:0] baseline_expected [0:1023];
    integer code;

    ternary_dense5_decoder dense (.code(dense_code), .trits(dense_trits), .valid(dense_valid));
    ternary_sparse41_decoder sparse (.code(sparse_code), .trits(sparse_trits), .valid(sparse_valid));
    ternary_baseline5_decoder baseline (.code(baseline_code), .trits(baseline_trits), .valid(baseline_valid));

    initial begin
        $readmemh("dense5_expected.mem", dense_expected);
        $readmemh("sparse41_expected.mem", sparse_expected);
        $readmemh("baseline5_expected.mem", baseline_expected);
        dense_code = 0;
        sparse_code = 0;
        baseline_code = 0;
        for (code = 0; code < 256; code = code + 1) begin
            dense_code = code;
            #1;
            if ({dense_valid, dense_trits} !== dense_expected[code])
                $fatal(1, "dense5 mismatch code=%0d got=%h expected=%h", code,
                    {dense_valid, dense_trits}, dense_expected[code]);
        end
        for (code = 0; code < 16; code = code + 1) begin
            sparse_code = code;
            #1;
            if ({sparse_valid, sparse_trits} !== sparse_expected[code])
                $fatal(1, "sparse41 mismatch code=%0d", code);
        end
        for (code = 0; code < 1024; code = code + 1) begin
            baseline_code = code;
            #1;
            if ({baseline_valid, baseline_trits} !== baseline_expected[code])
                $fatal(1, "baseline5 mismatch code=%0d", code);
        end
        // Unknown inputs should fail closed in simulation.
        dense_code = 8'bx;
        sparse_code = 4'bx;
        baseline_code = 10'bx;
        #1;
        if ({dense_valid, dense_trits} !== 11'd0 ||
            {sparse_valid, sparse_trits} !== 9'd0 ||
            {baseline_valid, baseline_trits} !== 11'd0)
            $fatal(1, "unknown input did not fail closed");
        $display("PASS decoders: dense5=256 sparse41=16 baseline5=1024 plus unknown inputs");
        $finish;
    end
endmodule
`default_nettype wire
