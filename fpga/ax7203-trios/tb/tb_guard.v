// Directed test of the generated RX guard (t27/trios_uart_guard.t27 -> TriosUartGuardT27):
// 16 pushes fill the queue, the 17th is dropped and sets the sticky overrun without
// touching the queued bytes, a read of the (modelled) status does not clear it, `clear`
// does, a push and a clear in one clock keep the flag, pops free slots in order, and
// `quiet` rises only after quiet_clocks inactive clocks and falls on activity.
`timescale 1ns/1ps
module tb_guard;
    reg clk = 0; always #5 clk = ~clk;
    reg rst_n = 0, push = 0, pop = 0, clear = 0, active = 0;
    reg [31:0] qc = 32'd20;
    wire [31:0] level, wr, rd, slot, dropped, idle;
    wire store, overrun, quiet, empty, full, ready;
    TriosUartGuardT27 g (.clk(clk), .rst_n(rst_n), .en(1'b1), .ready(ready),
        .push(push), .pop(pop), .clear(clear), .active(active), .quiet_clocks(qc),
        .level(level), .wr(wr), .rd(rd), .slot(slot), .store(store), .overrun(overrun),
        .dropped(dropped), .idle(idle), .quiet(quiet), .empty(empty), .full(full));
    integer errors = 0, i;
    task check(input cond, input [255:0] what);
        if (!cond) begin errors = errors + 1; $display("FAIL: %0s (level=%0d overrun=%b dropped=%0d quiet=%b)", what, level, overrun, dropped, quiet); end
    endtask
    task tick; begin @(posedge clk); #1; end endtask
    reg [3:0] seen;
    initial begin
        repeat (3) tick; rst_n = 1; tick;
        check(empty && !full && !overrun && level == 0, "after reset");
        for (i = 0; i < 16; i = i + 1) begin
            push = 1; tick; push = 0;
            check(store && slot == i, "slot of accepted push");
            tick;
        end
        check(full && level == 16 && !overrun && dropped == 0, "16 bytes fill the queue without overrun");
        push = 1; tick; push = 0; tick;
        check(overrun && dropped == 1 && level == 16 && wr == 0 && !store, "17th push dropped and flagged, queue untouched");
        for (i = 0; i < 5; i = i + 1) tick;
        check(overrun, "overrun is sticky");
        pop = 1; tick; pop = 0; tick;
        check(level == 15 && rd == 1 && overrun, "pop frees a slot, flag stays");
        push = 1; clear = 1; tick; push = 0; clear = 0; tick;
        check(level == 16 && !overrun, "push into the freed slot while clearing: accepted, flag cleared");
        push = 1; tick; push = 0; tick;
        check(overrun && dropped == 2, "push to a full queue sets the flag again");
        push = 1; clear = 1; tick; push = 0; clear = 0; tick;
        check(overrun && dropped == 3, "push and clear in one clock keep the flag");
        clear = 1; tick; clear = 0; tick;
        check(!overrun && dropped == 3, "clear drops the flag, dropped counter keeps counting history");
        for (i = 0; i < 16; i = i + 1) begin pop = 1; tick; pop = 0; tick; end
        check(empty && level == 0 && rd == 1, "all bytes popped in order, wraps");
        pop = 1; tick; pop = 0; tick;
        check(level == 0 && rd == 1, "pop on an empty queue is ignored");
        push = 1; pop = 1; tick; push = 0; pop = 0; tick;
        check(level == 1, "push with a simultaneous pop of an empty queue");
        pop = 1; tick; pop = 0; tick;
        // quiet
        active = 0;
        repeat (30) tick;
        check(quiet, "quiet after quiet_clocks inactive clocks with an empty queue");
        active = 1; tick; tick;
        check(!quiet, "activity ends quiet");
        active = 0; repeat (10) tick;
        check(!quiet, "quiet needs the full window again");
        repeat (20) tick;
        check(quiet, "quiet again");
        push = 1; tick; push = 0; tick; tick;
        check(!quiet && level == 1, "a queued byte keeps the line not quiet");
        if (errors == 0) $display("TB_GUARD_PASS"); else $display("TB_GUARD_FAIL %0d", errors);
        $finish;
    end
endmodule
