// Testbench of the DDR3 UART loader (issue #63 part 2), driven by tests/test_ddr3_loader.py.
//
// The whole top fpga/ax7203/ddr3/tms_ddr3_loader_ax7203.v with its t27 cores, UberDDR3's
// ddr3_top replaced by the behavioural Wishbone memory of tests/sim_ddr3_loader_model.v
// (folded 512 MiB store with a background pattern, byte selects, random stalls and ack
// latency, calibration delay; our code, not UberDDR3's) and the PLL by a pass-through stub
// (the controller clock is the 200 MHz input here). The host is uart_host_model of
// tests/tb_uart_loader.v (a script of bytes, waits, glitches, resets; every received byte to
// +capture=); its `h` command drives the model's bench controls: bit 0 holds o_wb_stall
// high (the port takes no request), bit 1 freezes the ack queue (acks withheld).
//
// Monitors (each prints one line at the end, or stops the run with $fatal):
// - TBWB: requests taken and acks per master at the arbiter; an ack reaching a master with
//   none of its own requests outstanding, or reaching the master that does not own the port,
//   is counted (misrouted, must be 0); owner changes; the most requests outstanding.
// - TBKEPT: every read the Wishbone master answers from its kept word is compared with the
//   memory model's word at that clock (hits, mismatches: must be 0), so the kept word is shown
//   to be coherent with every write, the other master's included.
// - MODEL: the memory model's writes, partial writes (fewer than 16 selects), reads, acks.
// - TBPORT: the longest run of clocks in which the loader waited for a frame (P_HUNT) with
//   nothing presented or outstanding on its master and still held the port (m0_cyc), so the
//   arbiter could not give it to master 1 (must be at most 1: wb_cyc is registered).
// - TBRACE (+race_late_ack=1): after a read-back the loader gave up on while its read waits
//   for an ack the bench withholds (`h 2`), the bench releases that ack so that it reaches the
//   master in the clock the next read-back asks for its first data byte (P_RB_SEND at rb_k 9,
//   then P_RB_NEXT); state 2 when it did. The ack must be dropped, not answer that request.
// - TBCLK: the status line `clocks` (line 20) against the loader's clock counter in the first
//   clock of that line's P_RESP: the counter minus the line's value must be 2 in every such
//   line (the line carries the count of its P_STATUS settle clock, the clock before P_STATUS
//   sampled it, and P_RESP begins one clock after that; build a9a56541 sent the previous line's
//   copy, a whole line older).
// With `define DDR3_LOADER_READER the #62 reader is master 1; its report lines (uart_tx2)
// are decoded into +capture2= as "<time in ns>\t<line>", for tools/fpga-ddr3-capture.py.
// With `define DDR3_MATVEC (tests/test_ddr3_matvec_top.py) the top carries the device matvec;
// TBMV prints the line arbiter's collisions (must be 0) and lines per side, the feed's runs,
// timeouts, dropped and stray acks (stray must be 0) and the matvec's runs.
`timescale 1ps/1ps
`default_nettype none

// Simulation stand-in for the PLL: every output is the input clock, LOCKED after 16 cycles.
module PLLE2_ADV #(
    parameter BANDWIDTH = "OPTIMIZED", parameter COMPENSATION = "INTERNAL", parameter STARTUP_WAIT = "FALSE",
    parameter real CLKIN1_PERIOD = 5.0, parameter integer DIVCLK_DIVIDE = 1, parameter integer CLKFBOUT_MULT = 5,
    parameter real CLKFBOUT_PHASE = 0.0,
    parameter integer CLKOUT0_DIVIDE = 1, parameter real CLKOUT0_PHASE = 0.0, parameter real CLKOUT0_DUTY_CYCLE = 0.5,
    parameter integer CLKOUT1_DIVIDE = 1, parameter real CLKOUT1_PHASE = 0.0, parameter real CLKOUT1_DUTY_CYCLE = 0.5,
    parameter integer CLKOUT2_DIVIDE = 1, parameter real CLKOUT2_PHASE = 0.0, parameter real CLKOUT2_DUTY_CYCLE = 0.5,
    parameter integer CLKOUT3_DIVIDE = 1, parameter real CLKOUT3_PHASE = 0.0, parameter real CLKOUT3_DUTY_CYCLE = 0.5
) (
    input wire CLKIN1, CLKIN2, CLKINSEL, CLKFBIN, RST, PWRDWN, DCLK, DEN, DWE,
    input wire [6:0] DADDR, input wire [15:0] DI,
    output wire CLKFBOUT, CLKOUT0, CLKOUT1, CLKOUT2, CLKOUT3, CLKOUT4, CLKOUT5,
    output reg LOCKED, output wire [15:0] DO, output wire DRDY
);
    integer n = 0;
    initial LOCKED = 1'b0;
    always @(posedge CLKIN1) begin
        n = n + 1;
        if (n == 16) LOCKED <= 1'b1;
    end
    assign {CLKFBOUT, CLKOUT0, CLKOUT1, CLKOUT2, CLKOUT3} = {5{CLKIN1}};
    assign {CLKOUT4, CLKOUT5, DRDY} = 3'b000;
    assign DO = 16'd0;
endmodule

module tb_ddr3_loader;
    parameter integer BAUD_DIV = 16;
    parameter integer TIMEOUT_CLOCKS = 3840;
    parameter integer PROBATION_CLOCKS = 48000;
    parameter integer READER_TRITS = 1237;
    parameter integer READER_RUNS = 0;
    parameter integer READER_UART_DIV = 4;
    parameter integer FEED_CAP = 64;
    parameter integer FEED_WATCHDOG = 32768;
    reg clk200 = 1'b0;
    always #2500 clk200 = ~clk200;
    wire rx_line, tx_line, button_n, tx2;
    wire [2:0] hang;
    wire [3:0] led;
    wire ck_p, ck_n, reset_n, cke, cs_n, ras_n, cas_n, we_n, odt;
    wire [14:0] addr;
    wire [2:0] ba;
    wire [15:0] dq;
    wire [1:0] dqs_p, dqs_n, dm;
    uart_host_model host (.rx_line(rx_line), .tx_line(tx_line), .button_n(button_n), .hang(hang));
    tms_ddr3_loader_ax7203 #(
        .BUILD_ID(32'h5eed1063), .UART_DIV(BAUD_DIV), .TIMEOUT_CLOCKS(TIMEOUT_CLOCKS),
        .PROBATION_CLOCKS(PROBATION_CLOCKS)
`ifdef DDR3_LOADER_READER
        , .READER_TRITS(READER_TRITS), .READER_RUNS(READER_RUNS), .READER_UART_DIV(READER_UART_DIV)
`endif
`ifdef DDR3_MATVEC
        , .FEED_CAP(FEED_CAP), .FEED_WATCHDOG(FEED_WATCHDOG)
`endif
    ) dut (
        .clk200_p(clk200), .clk200_n(~clk200), .rst_n(button_n), .led(led), .uart_tx(tx_line), .uart_rx(rx_line),
`ifdef DDR3_LOADER_READER
        .uart_tx2(tx2),
`endif
        .ddr3_ck_p(ck_p), .ddr3_ck_n(ck_n), .ddr3_reset_n(reset_n), .ddr3_cke(cke), .ddr3_cs_n(cs_n),
        .ddr3_ras_n(ras_n), .ddr3_cas_n(cas_n), .ddr3_we_n(we_n), .ddr3_odt(odt), .ddr3_addr(addr), .ddr3_ba(ba),
        .ddr3_dq(dq), .ddr3_dqs_p(dqs_p), .ddr3_dqs_n(dqs_n), .ddr3_dm(dm)
    );
`ifndef DDR3_LOADER_READER
    assign tx2 = 1'b1;
`endif
    always @(hang) begin
        dut.ddr3.bench_stall = hang[0];
        dut.ddr3.bench_hold = hang[1];
    end

    // ---- monitors ----
    wire mclk = dut.clk_ctrl;
    integer taken0 = 0, taken1 = 0, acks0 = 0, acks1 = 0, misrouted = 0, owner_changes = 0, max_out = 0;
    integer out0 = 0, out1 = 0, kept_hits = 0, kept_bad = 0, drops = 0;
    reg last_owner = 1'b0;
    always @(posedge mclk) begin
        // An owner change (seen one clock after the arbiter's edge) with requests in flight.
        if (dut.arbiter.owner[0] != last_owner) begin
            owner_changes = owner_changes + 1;
            if (out0 != 0 || out1 != 0) misrouted = misrouted + 1000;
            last_owner = dut.arbiter.owner[0];
        end
        if (dut.m0_stb && !dut.m0_stall) begin taken0 = taken0 + 1; out0 = out0 + 1; end
        if (dut.m1_stb && !dut.m1_stall) begin taken1 = taken1 + 1; out1 = out1 + 1; end
        if (dut.wb_ack) begin
            // The ack belongs to the master whose request is oldest; only one owner has any.
            if (dut.m0_ack) begin
                if (out0 == 0 || out1 != 0) misrouted = misrouted + 1;
                acks0 = acks0 + 1; out0 = out0 - 1;
            end else if (dut.m1_ack) begin
                if (out1 == 0 || out0 != 0) misrouted = misrouted + 1;
                acks1 = acks1 + 1; out1 = out1 - 1;
            end else begin
                misrouted = misrouted + 1;
            end
        end
        if (out0 + out1 > max_out) max_out = out0 + out1;
        if (dut.master.read_ack && dut.master.drop_now) drops = drops + 1;
    end
    // Hits are decided before the clock edge: sampled at the falling edge, the model holds every
    // write taken so far and the kept word must equal it.
    always @(negedge mclk) begin
        if (dut.app_rst_n && dut.master.accept_r && dut.master.hit) begin
            kept_hits = kept_hits + 1;
            if ({dut.master.kept_hi, dut.master.kept_lo} !== dut.ddr3.peek(dut.master.kept_addr)) begin
                kept_bad = kept_bad + 1;
                $display("TBKEPT_MISMATCH t=%0t burst=%0d kept=%032h mem=%032h", $time, dut.master.kept_addr,
                         {dut.master.kept_hi, dut.master.kept_lo}, dut.ddr3.peek(dut.master.kept_addr));
            end
        end
    end
    // The loader must not keep the port while it waits for a frame with nothing to do there.
    localparam integer P_HUNT = 1, P_RB_SEND = 18;
    integer idle_held = 0, idle_held_max = 0;
    always @(posedge mclk) begin
        if (dut.loader.pstate == P_HUNT && dut.m0_cyc && !dut.master.wb_stb && dut.master.outst == 0
            && !dut.master.rd_wait)
            idle_held = idle_held + 1;
        else
            idle_held = 0;
        if (idle_held > idle_held_max) idle_held_max = idle_held;
    end
    // +race_late_ack=1: release the withheld ack of an abandoned read on the falling edge before the
    // loader moves from P_RB_SEND (rb_k 9) to P_RB_NEXT, which asks for the first data byte.
    integer race = 0, race_state = 0;
    initial if (!$value$plusargs("race_late_ack=%d", race)) race = 0;
    always @(negedge mclk) begin
        if (race != 0) begin
            if (race_state == 0 && dut.loader.rb_fail && dut.master.rd_wait) race_state = 1;
            if (race_state == 1 && !dut.loader.rb_fail && dut.loader.pstate == P_RB_SEND && dut.loader.rb_k == 9
                && !dut.tx_busy && dut.line_idle && dut.master.rd_wait) begin
                dut.ddr3.bench_hold = 1'b0;
                race_state = 2;
            end
        end
    end
    // The status line `clocks`: its age in the first clock of its P_RESP (settle_n is 0 only there).
    localparam integer P_RESP = 9, TAG_C = 67, ST_CLOCKS = 20;
    integer clk_lines = 0, clk_age_max = 0, clk_age_min = -1;
    reg [31:0] clk_age;
    always @(negedge mclk) begin
        if (dut.loader.pstate == P_RESP && dut.loader.settle_n == 0 && dut.loader.resp_tag == TAG_C
            && dut.loader.resp_a[15:0] == ST_CLOCKS) begin
            clk_age = dut.loader.clocks - dut.loader.resp_v;
            clk_lines = clk_lines + 1;
            if (clk_age > clk_age_max) clk_age_max = clk_age;
            if (clk_age_min < 0 || clk_age < clk_age_min) clk_age_min = clk_age;
        end
    end
    final begin
`ifdef DDR3_MATVEC
        $display("TBMV collisions=%0d lines0=%0d lines1=%0d feed_runs=%0d feed_timeouts=%0d feed_dropped=%0d feed_stray=%0d mv_runs=%0d preloaded=%0d",
                 dut.line_arbiter.collisions, dut.line_arbiter.lines0, dut.line_arbiter.lines1, dut.feed.runs,
                 dut.feed.timeouts, dut.feed.dropped, dut.feed.stray, dut.matvec.runs, dut.ddr3.preloaded);
`endif
        $display("TBCLK lines=%0d age_min=%0d age_max=%0d", clk_lines, clk_age_min, clk_age_max);
        $display("TBPORT idle_held_max=%0d", idle_held_max);
        $display("TBRACE state=%0d", race_state);
        $display("TBWB taken0=%0d acks0=%0d taken1=%0d acks1=%0d misrouted=%0d owner_changes=%0d max_outstanding=%0d drops=%0d",
                 taken0, acks0, taken1, acks1, misrouted, owner_changes, max_out, drops);
        $display("TBKEPT hits=%0d mismatches=%0d", kept_hits, kept_bad);
        $display("MODEL writes=%0d partial=%0d reads=%0d acks=%0d", dut.ddr3.writes, dut.ddr3.partial,
                 dut.ddr3.reads, dut.ddr3.acks);
    end

    // ---- the reader's report lines (master 1), "<ns>\t<line>" per line ----
    localparam integer R_BIT_PS = 5000 * READER_UART_DIV;
    integer fd2 = 0;
    reg [4095:0] path2;
    reg [7:0] rbyte;
    reg at_start = 1'b1;
    initial if ($value$plusargs("capture2=%s", path2)) fd2 = $fopen(path2, "w");
    always begin : reader_lines
        integer b;
        @(negedge tx2);
        #(R_BIT_PS * 3 / 2);
        rbyte = 8'd0;
        for (b = 0; b < 8; b = b + 1) begin
            rbyte[b] = tx2;
            #(R_BIT_PS);
        end
        if (tx2 !== 1'b1) $fatal(1, "reader UART: stop bit low");
        if (fd2 != 0) begin
            if (at_start) begin
                $fwrite(fd2, "%0d\t", $time / 1000);
                at_start = 1'b0;
            end
            $fwrite(fd2, "%c", rbyte);
            if (rbyte == 8'h0a) begin
                at_start = 1'b1;
                $fflush(fd2);
            end
        end
    end
endmodule
`default_nettype wire
