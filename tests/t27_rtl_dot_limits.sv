// Five-bit checked-accumulator boundary and X-input tests for native t27 RTL.
`timescale 1ns/1ps
`default_nettype none
module tb_t27_dot_limits;
    parameter integer DENSE5 = 1;
    reg clk = 0;
    always #5 clk = !clk;
    reg rst = 1;
    reg in_valid = 0;
    wire in_ready;
    reg [9:0] in_code;
    reg [39:0] in_activations = 0;
    reg [4:0] in_mask = 0;
    reg in_last = 0;
    wire out_valid;
    reg out_ready = 0;
    wire signed [4:0] out_result;
    wire out_error;
    integer scored = 0;
    integer errors = 0;
    trinity_dot_stream_t27 #(.DENSE5(DENSE5), .ACC_WIDTH(5)) dut (
        .clk(clk), .rst(rst), .in_valid(in_valid), .in_ready(in_ready),
        .in_code(in_code), .in_activations(in_activations), .in_mask(in_mask),
        .in_last(in_last), .out_valid(out_valid), .out_ready(out_ready),
        .out_result(out_result), .out_error(out_error)
    );

    task send;
        input [9:0] code;
        input [39:0] acts;
        input [4:0] mask;
        input last;
        begin
            @(negedge clk);
            in_code = code;
            in_activations = acts;
            in_mask = mask;
            in_last = last;
            in_valid = 1;
            while (!in_ready) @(negedge clk);
            @(posedge clk);
            @(negedge clk);
            in_valid = 0;
        end
    endtask

    task check_result;
        input integer expected;
        input expected_error;
        integer held;
        begin
            while (!out_valid) @(negedge clk);
            for (held = 0; held < 3; held = held + 1) begin
                if ($signed(out_result) !== expected || out_error !== expected_error || !out_valid)
                    $fatal(1, "native5 result %0d expected=%0d/%b actual=%0d/%b",
                           scored, expected, expected_error, out_result, out_error);
                @(negedge clk);
            end
            out_ready = 1;
            @(posedge clk);
            @(negedge clk);
            out_ready = 0;
            scored = scored + 1;
            if (expected_error) errors = errors + 1;
        end
    endtask

    localparam [9:0] UNIT = DENSE5 ? 10'd122 : 10'd1;
    localparam [9:0] ZERO = DENSE5 ? 10'd121 : 10'd0;
    initial begin
        repeat (2) @(negedge clk);
        rst = 0;
        send(UNIT, 40'd15, 5'd1, 1); check_result(15, 0);
        send(UNIT, 40'd240, 5'd1, 1); check_result(-16, 0);
        send(UNIT, 40'd16, 5'd1, 1); check_result(0, 1);
        send(UNIT, 40'd239, 5'd1, 1); check_result(0, 1);
        // Overflow remains sticky even after exact mathematical cancellation.
        send(UNIT, 40'd16, 5'd31, 0);
        send(UNIT, 40'd240, 5'd1, 1); check_result(0, 1);
        send(10'bxxxxxxxxxx, 40'd0, 5'd31, 1); check_result(0, 1);
        send(ZERO, 40'd0, 5'b0x001, 1); check_result(0, 1);
        // An unknown padded activation must not be silently discarded.
        send(ZERO, {24'd0, 8'bxxxxxxxx, 8'd0}, 5'd1, 1); check_result(0, 1);
        send(UNIT, 40'd7, 5'd1, 1); check_result(7, 0);
        $display("PASS native t27 five-bit DENSE5=%0d results=%0d expected_errors=%0d",
                 DENSE5, scored, errors);
        $finish;
    end
    initial begin
        #100000;
        $fatal(1, "native five-bit test timed out");
    end
endmodule
`default_nettype wire
