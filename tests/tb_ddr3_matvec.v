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
    parameter [31:0] DADDR = 32'h0000_0040, AADDR = 32'h0000_1000, WADDR = 32'h0000_4000;
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
        .line_go(line_go), .line_tag(line_tag), .line_a(line_a), .line_b(line_b),
        .s_go(1'b0), .s_tag(32'd0), .s_a(32'd0), .s_b(64'd0)
    );

    // Behavioural Wishbone memory on the loader model's discipline
    // (tests/sim_ddr3_loader_model.v): one folded index per burst address (addr & 32767: the module's wb_addr already counts 128-bit words); a taken
    // request (wb_stb and not stall, cyc held) captures its word at the take,
    // and the ack comes with the data as registered outputs — both NBAs of one
    // clock — ack_min .. ack_min + ack_span clocks later, in order (a due time
    // never before the previous entry's + 1). stall is decided ahead: raised
    // while calibration is pending, when the queue is near full, or a share of
    // idle clocks at random.
    reg [63:0] mem [0:65535];
    parameter integer QDEPTH = 8;
    integer stall_pct = 0, ack_min = 0, ack_span = 4, calib_after = 40;
    integer lfsr = 32'h5eed0062;
    integer clocks = 0, taken = 0, acked = 0;
    reg [63:0] q_lo [0:QDEPTH-1];
    reg [63:0] q_hi [0:QDEPTH-1];
    integer q_due [0:QDEPTH-1];
    integer q_head = 0, q_tail = 0, q_count = 0, last_due = 0;
    wire take_now = wb_cyc && wb_stb && !stall;

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
        if (take_now) begin
            q_lo[q_tail] = mem[(wb_addr & 32767) * 2];
            q_hi[q_tail] = mem[(wb_addr & 32767) * 2 + 1];
            q_due[q_tail] = (clocks + ack_min + (nextrand(0) % (ack_span + 1)) > last_due)
                            ? clocks + ack_min + (nextrand(0) % (ack_span + 1)) : last_due + 1;
            last_due = q_due[q_tail];
            q_tail = (q_tail + 1) % QDEPTH;
            q_count = q_count + 1;
            taken = taken + 1;
        end
        ack <= 1'b0;
        if (q_count > 0 && q_due[q_head] <= clocks) begin
            ack <= 1'b1;
            rdata_lo <= q_lo[q_head];
            rdata_hi <= q_hi[q_head];
            q_head = (q_head + 1) % QDEPTH;
            q_count = q_count - 1;
            acked = acked + 1;
        end
        stall <= (clocks < calib_after) || (q_count + (take_now ? 1 : 0) >= QDEPTH - 2) ||
                 (q_count == 0 && (nextrand(0) % 100) < stall_pct);
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
