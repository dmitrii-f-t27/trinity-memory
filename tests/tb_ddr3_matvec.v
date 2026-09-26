// Testbench of the DDR3 device matvec (issue #64), driven by tests/test_ddr3_matvec.py.
//
// The module t27/rtl/fpga_ddr3_matvec.t27 alone (the board top is wiring only):
// a behavioural Wishbone memory stands in for the arbiter and UberDDR3's user
// port — preloaded from +doorbell=, +acts= and +weights= hex files written by
// the test at the folded word indices of the DUT's addresses — with +stall_pct=,
// +ack_min= and +ack_span= knobs and a calibration delay. One device word is
// two 64-bit file words, lo then hi; the fold is (addr >> 2) & 32767, so every
// region the test uses must stay inside a 512 KiB window without overlap. The
// line emitter is a stub: after line_go it holds line_idle low for +line_clocks=
// clocks. Every report line is appended to +capture= as
// "<time ns>\t<tag char>\t<a>\t<b>". The simulation ends +stop_after= clocks
// after the last line, or at +timeout_clocks=.
`timescale 1ps/1ps
`default_nettype none
module tb_ddr3_matvec;
    parameter integer CLK_PS = 12000;   // 83.33 MHz controller clock
    parameter [31:0] COLS = 2560, ROWS = 8, FMT = 1;
    parameter [31:0] DADDR = 32'h0000_0040, AADDR = 32'h0000_1000, WADDR = 32'h0000_8000;
    parameter [31:0] MAGIC = 32'h74337633, CAP = 4, WATCHDOG = 32'd16777216, POLL_DIV = 64;

    reg clk = 1'b0, calib = 1'b0;
    always #(CLK_PS/2) clk = ~clk;

    reg stall = 1'b0, ack = 1'b0;
    reg [63:0] rdata_lo = 64'd0, rdata_hi = 64'd0;
    wire wb_cyc, wb_stb;
    wire [31:0] wb_addr;
    wire line_go;
    wire [31:0] line_tag, line_a;
    wire [63:0] line_b;
    reg line_idle = 1'b1;

    TrinityFpgaDdr3MatvecT27 dut (
        .clk(clk), .rst_n(1'b1), .en(1'b1), .ready(),
        .calib(calib), .stall(stall), .ack(ack), .rdata_lo(rdata_lo), .rdata_hi(rdata_hi),
        .cols(COLS), .rows(ROWS), .fmt(FMT), .waddr(WADDR), .aaddr(AADDR), .daddr(DADDR),
        .magic(MAGIC), .cap(CAP), .watchdog(WATCHDOG), .poll_div(POLL_DIV), .line_idle(line_idle),
        .wb_cyc(wb_cyc), .wb_stb(wb_stb), .wb_addr(wb_addr),
        .line_go(line_go), .line_tag(line_tag), .line_a(line_a), .line_b(line_b)
    );

    // Behavioural Wishbone memory: the fold of (addr >> 2) & 32767. A request is
    // taken on a clock with wb_stb and not stall (the contract the module relies
    // on: it never withdraws a request, so stall decides before the request's
    // clock — raised at random while idle, or when the queue is full). Acks come
    // in order, the word's data valid in the ack's clock, as UberDDR3 drives it.
    reg [63:0] mem [0:65535];
    parameter integer QMAX = 8;
    integer stall_pct = 0, ack_min = 0, ack_span = 4, calib_after = 40;
    integer lfsr = 32'h5eed0062;
    integer clocks = 0, taken = 0, acked = 0;
    integer q_addr [0:QMAX-1];
    integer q_wait [0:QMAX-1];
    integer q_head = 0, q_len = 0;
    integer idx;

    function [31:0] nextrand;
        input dummy;
        begin
            lfsr = {lfsr << 13} ^ lfsr; lfsr = {lfsr << 17} ^ lfsr; lfsr = lfsr ^ (lfsr >> 5);
            nextrand = lfsr;
        end
    endfunction

    always @(posedge clk) begin
        clocks = clocks + 1;
        if (clocks == calib_after) calib <= 1'b1;
        ack <= 1'b0;
        if (clocks >= calib_after) begin
            // The taken request of this clock, if the port is not stalled.
            if (wb_stb && !stall) begin
                q_addr[(q_head + q_len) % QMAX] = (wb_addr >> 2) & 32767;
                q_wait[(q_head + q_len) % QMAX] = ack_min + (nextrand(0) % (ack_span + 1)) + 2;
                q_len = q_len + 1;
                taken = taken + 1;
            end
            // Decide the stall of the next clock: the queue must never overflow,
            // and a share of idle clocks raise it at random.
            stall <= (q_len >= QMAX - 1) || (q_len == 0 && (nextrand(0) % 100) < stall_pct);
            // In-order acks, the word's data valid one clock before the ack
            // (registered together they would race the master's same-edge read:
            // UberDDR3 drives o_wb_data with o_wb_ack, both stable in the ack's
            // clock).
            if (q_len > 0) begin
                q_wait[q_head] = q_wait[q_head] - 1;
                if (q_wait[q_head] == 1) begin
                    idx = q_addr[q_head];
                    rdata_lo <= mem[idx * 2];
                    rdata_hi <= mem[idx * 2 + 1];
                end
                if (q_wait[q_head] == 0) begin
                    ack <= 1'b1;
                    q_head = (q_head + 1) % QMAX;
                    q_len = q_len - 1;
                    acked = acked + 1;
                end
            end
        end else begin
            stall <= 1'b0;
        end
    end

    // Line emitter stub and capture.
    integer line_file, line_clocks = 24, busy_left = 0;
    always @(posedge clk) begin
        if (line_go) begin
            busy_left = line_clocks;
            line_idle <= 1'b0;
            $fdisplay(line_file, "%0d\t%s\t%0d\t%0d", $time / 1000, tag_name(line_tag), line_a, line_b);
        end else if (busy_left > 0) begin
            busy_left = busy_left - 1;
            if (busy_left == 0) line_idle <= 1'b1;
        end
    end

    function [7:0] tag_name;
        input [31:0] tag;
        begin
            case (tag)
                113: tag_name = "q"; 107: tag_name = "k"; 118: tag_name = "v";
                100: tag_name = "d"; 121: tag_name = "y"; 99: tag_name = "c";
                111: tag_name = "o"; 119: tag_name = "w"; 110: tag_name = "n";
                117: tag_name = "u"; 116: tag_name = "t"; 122: tag_name = "z";
                default: tag_name = "?";
            endcase
        end
    endfunction

    integer stop_after = 6000, timeout_clocks = 20000000, quiet = 0;
    integer i;
    string capture, doorbell, acts, weights;
    initial begin
        if (!$value$plusargs("capture=%s", capture)) $fatal(1, "capture required");
        if (!$value$plusargs("doorbell=%s", doorbell)) $fatal(1, "doorbell required");
        if (!$value$plusargs("acts=%s", acts)) $fatal(1, "acts required");
        if (!$value$plusargs("weights=%s", weights)) $fatal(1, "weights required");
        for (i = 0; i < 65536; i = i + 1) mem[i] = 64'd0;
        $readmemh(doorbell, mem);
        $readmemh(acts, mem);
        $readmemh(weights, mem);
        if ($value$plusargs("stall_pct=%d", stall_pct)) ;
        if ($value$plusargs("ack_min=%d", ack_min)) ;
        if ($value$plusargs("ack_span=%d", ack_span)) ;
        if ($value$plusargs("calib_after=%d", calib_after)) ;
        if ($value$plusargs("line_clocks=%d", line_clocks)) ;
        if ($value$plusargs("stop_after=%d", stop_after)) ;
        if ($value$plusargs("timeout_clocks=%d", timeout_clocks)) ;
        line_file = $fopen(capture, "w");
    end

    always @(posedge clk) begin
        if (line_go) quiet = 0;
        else quiet = quiet + 1;
        if (quiet > stop_after) begin
            $display("TB_STOP clocks=%0d taken=%0d acked=%0d", clocks, taken, acked);
            $fclose(line_file);
            $finish;
        end
        if (clocks > timeout_clocks) begin
            $display("TB_TIMEOUT clocks=%0d", clocks);
            $fclose(line_file);
            $finish;
        end
    end
endmodule
`default_nettype wire
