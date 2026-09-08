`timescale 1ns/1ps
`default_nettype none
module stream_checker #(
    parameter integer TRIT_COUNT = 12,
    parameter integer WORDS = (TRIT_COUNT + 4) / 5,
    parameter integer ADDR_WIDTH = (WORDS > 1) ? $clog2(WORDS) : 1
) (output reg done);
    reg clk = 0;
    always #5 clk = !clk;
    reg rst, start, load_en;
    reg [ADDR_WIDTH-1:0] load_addr;
    reg [7:0] dense_load;
    reg [9:0] baseline_load;
    wire dense_busy, base_busy, dense_ready, base_ready;
    wire dense_valid, base_valid, dense_last, base_last;
    wire dense_code_valid, base_code_valid;
    wire [4:0] dense_mask, base_mask;
    wire [9:0] dense_trits, base_trits;
    reg [7:0] codes [0:WORDS-1];
    reg [9:0] lanes [0:WORDS-1];
    reg [9:0] expected [0:WORDS-1];
    reg [4:0] masks [0:WORDS-1];
    integer word_index, lane, trit, scale, encoded, lane_code;

    ternary_dense5_stream #(.TRIT_COUNT(TRIT_COUNT)) dense (
        .clk(clk), .rst(rst), .start(start), .busy(dense_busy),
        .load_en(load_en), .load_addr(load_addr), .load_code(dense_load),
        .load_ready(dense_ready), .out_valid(dense_valid), .out_last(dense_last),
        .out_code_valid(dense_code_valid), .out_lane_mask(dense_mask), .out_trits(dense_trits)
    );
    ternary_baseline5_stream #(.TRIT_COUNT(TRIT_COUNT)) baseline (
        .clk(clk), .rst(rst), .start(start), .busy(base_busy),
        .load_en(load_en), .load_addr(load_addr), .load_code(baseline_load),
        .load_ready(base_ready), .out_valid(base_valid), .out_last(base_last),
        .out_code_valid(base_code_valid), .out_lane_mask(base_mask), .out_trits(base_trits)
    );

    task tick;
        @(posedge clk);
        #1;
    endtask

    task check_idle;
        if ({dense_busy, dense_valid, dense_last, dense_code_valid, dense_mask, dense_trits} !== 19'd0 ||
            {base_busy, base_valid, base_last, base_code_valid, base_mask, base_trits} !== 19'd0)
            $fatal(1, "TRIT_COUNT=%0d expected idle outputs", TRIT_COUNT);
    endtask

    task load_memory;
        begin
            for (integer index = 0; index < WORDS; index = index + 1) begin
                if (!dense_ready || !base_ready) $fatal(1, "memory not ready to load");
                load_en = 1;
                load_addr = index;
                dense_load = codes[index];
                baseline_load = lanes[index];
                tick;
            end
            load_en = 0;
            tick;
            check_idle;
        end
    endtask

    task start_stream;
        begin
            start = 1;
            tick;
            if (!dense_busy || !base_busy || dense_valid || base_valid)
                $fatal(1, "TRIT_COUNT=%0d start acceptance/latency mismatch", TRIT_COUNT);
            start = 0;
        end
    endtask

    task check_word(input integer index, input integer invalid_mode);
        begin
            if (!dense_valid || !base_valid || !dense_busy || !base_busy)
                $fatal(1, "TRIT_COUNT=%0d missing stream word %0d", TRIT_COUNT, index);
            if (dense_last !== (index == WORDS-1) || base_last !== (index == WORDS-1))
                $fatal(1, "incorrect final-word flag");
            if (dense_mask !== masks[index] || base_mask !== masks[index])
                $fatal(1, "incorrect final-lane mask");
            if (invalid_mode == 1 && index == 0) begin
                if (dense_code_valid !== 0 || dense_trits !== 10'd0)
                    $fatal(1, "invalid dense code not rejected");
            end else if (!dense_code_valid || dense_trits !== expected[index])
                $fatal(1, "TRIT_COUNT=%0d dense word %0d mismatch got=%h expected=%h", TRIT_COUNT,
                    index, dense_trits, expected[index]);
            if (invalid_mode == 2 && index == 0) begin
                if (base_code_valid !== 0 || base_trits !== 10'd0)
                    $fatal(1, "invalid baseline code not rejected");
            end else if (!base_code_valid || base_trits !== expected[index])
                $fatal(1, "TRIT_COUNT=%0d baseline word %0d mismatch", TRIT_COUNT, index);
            if (dense_ready || base_ready) $fatal(1, "load allowed while busy");
        end
    endtask

    task consume_stream(input integer invalid_mode, input integer exercise_busy);
        begin
            for (integer index = 0; index < WORDS; index = index + 1) begin
                // Busy starts/writes must not alter the current or next stream.
                start = exercise_busy;
                load_en = exercise_busy;
                load_addr = 0;
                dense_load = 255;
                baseline_load = 1023;
                tick;
                check_word(index, invalid_mode);
            end
            start = 0;
            load_en = 0;
            tick;
            check_idle;
            tick;
            check_idle;
        end
    endtask

    initial begin
        done = 0;
        rst = 1;
        start = 0;
        load_en = 0;
        load_addr = 0;
        dense_load = 0;
        baseline_load = 0;
        for (word_index = 0; word_index < WORDS; word_index = word_index + 1) begin
            scale = 1;
            encoded = 0;
            lanes[word_index] = 0;
            expected[word_index] = 0;
            masks[word_index] = 0;
            for (lane = 0; lane < 5; lane = lane + 1) begin
                trit = ((word_index * 5 + lane) % 3) - 1;
                // Poison padding with a valid nonzero trit to test output masking.
                if (word_index * 5 + lane >= TRIT_COUNT) trit = 1;
                lane_code = (trit == -1) ? 2 : ((trit == 1) ? 1 : 0);
                encoded = encoded + (trit + 1) * scale;
                scale = scale * 3;
                lanes[word_index] = lanes[word_index] | (lane_code << (lane * 2));
                if (word_index * 5 + lane < TRIT_COUNT) begin
                    expected[word_index] = expected[word_index] | (lane_code << (lane * 2));
                    masks[word_index][lane] = 1;
                end
            end
            codes[word_index] = encoded;
        end

        tick;
        check_idle;
        if (dense_ready || base_ready) $fatal(1, "load ready during reset");
        rst = 0;
        tick;
        load_memory;
        start_stream;
        consume_stream(0, 1);
        start_stream;
        consume_stream(0, 0);

        // Reset aborts an active operation and preserves memory for a full restart.
        start_stream;
        tick;
        check_word(0, 0);
        rst = 1;
        tick;
        check_idle;
        rst = 0;
        tick;
        start_stream;
        consume_stream(0, 0);

        load_en = 1;
        load_addr = 0;
        dense_load = 255;
        baseline_load = lanes[0];
        tick;
        load_en = 0;
        start_stream;
        consume_stream(1, 0);

        load_en = 1;
        load_addr = 0;
        dense_load = codes[0];
        baseline_load = lanes[0] | 10'd3;
        tick;
        load_en = 0;
        start_stream;
        consume_stream(2, 0);
        $display("PASS stream: TRIT_COUNT=%0d WORDS=%0d reset/latency/masking/busy/end/invalid", TRIT_COUNT, WORDS);
        done = 1;
    end
endmodule

module tb_streams;
    wire done1, done10, done12, done320;
    stream_checker #(.TRIT_COUNT(1)) check1 (.done(done1));
    stream_checker #(.TRIT_COUNT(10)) check10 (.done(done10));
    stream_checker #(.TRIT_COUNT(12)) check12 (.done(done12));
    stream_checker #(.TRIT_COUNT(320)) check320 (.done(done320));
    initial begin
        wait (done1 && done10 && done12 && done320);
        $display("PASS all stream configurations");
        $finish;
    end
    initial begin
        #100000;
        $fatal(1, "stream test timeout");
    end
endmodule
`default_nettype wire
