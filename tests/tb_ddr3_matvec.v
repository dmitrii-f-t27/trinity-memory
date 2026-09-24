// Icarus bench of t27/rtl/fpga_ddr3_matvec.t27 (issue #64), driven by tests/test_ddr3_matvec.py.
// Writes the activation words through the module's write port, starts one run, offers the bus
// words of +words with random gaps (+gap percent, +seed), optionally stray words before the run
// reaches RUN (+stray_before) and after the last word (+stray_after), and records every byte the
// line emitter hands to the transmitter (+out, one hex byte per line). The transmitter is a model
// that stays busy +txbusy clocks per byte. The bench's own counts of the offered words go to
// +summary: words offered in RUN, clocks from the first to the last word, clocks of that span with
// no word, clocks from RUN to the first word, stray words offered, clocks with in_valid while
// in_ready was low, and whether in_ready was ever low.
`timescale 1ns / 1ps
module tb_ddr3_matvec;
    reg clk = 1'b0;
    always #6 clk = ~clk;
    reg rst_n = 1'b0;

    reg [127:0] words [0:262143];
    reg [63:0] acts [0:10239];
    integer nwords, nacts, rows, cols, fmt, seq, gap, seed, stray_before, stray_after, txbusy, max_clocks;
    integer fout, fsum, i, w, clocks;
    reg [1023:0] words_file, acts_file, out_file, summary_file;

    reg start = 1'b0;
    reg in_valid = 1'b0;
    reg [63:0] in_lo = 64'd0, in_hi = 64'd0;
    reg act_we = 1'b0;
    reg [31:0] act_entry = 0, act_bank = 0;
    reg [63:0] act_data = 64'd0;

    wire line_go, line_idle, tx_start, done, busy, in_ready;
    wire [31:0] line_tag, line_a, tx_byte, st;
    wire [63:0] line_b;
    reg tx_busy = 1'b0;
    integer tx_count = 0;

    TrinityFpgaDdr3MatvecT27 dut(
        .clk(clk), .rst_n(rst_n), .en(1'b1), .start(start),
        .cfg_rows(rows[31:0]), .cfg_cols(cols[31:0]), .cfg_fmt(fmt[31:0]), .cfg_seq(seq[31:0]),
        .in_valid(in_valid), .in_lo(in_lo), .in_hi(in_hi),
        .act_we(act_we), .act_entry(act_entry), .act_bank(act_bank), .act_data(act_data),
        .line_idle(line_idle),
        .line_go(line_go), .line_tag(line_tag), .line_a(line_a), .line_b(line_b),
        .done(done), .busy(busy), .in_ready(in_ready), .st(st));

    TrinityFpgaLineEmitterT27 emitter(
        .clk(clk), .rst_n(rst_n), .en(1'b1), .go(line_go), .tag(line_tag), .a(line_a), .b(line_b),
        .tx_busy(tx_busy), .tx_start(tx_start), .tx_byte(tx_byte), .idle(line_idle));

    // Transmitter model: busy for txbusy clocks after each byte.
    always @(posedge clk) begin
        if (tx_start) begin
            $fwrite(fout, "%02x\n", tx_byte[7:0]);
            tx_busy <= 1'b1;
            tx_count <= txbusy;
        end else if (tx_count > 1) begin
            tx_count <= tx_count - 1;
        end else begin
            tx_busy <= 1'b0;
            tx_count <= 0;
        end
    end

    // The bench's own counts.
    integer offered_run = 0, first_clock = -1, last_clock = -1, gap_clocks = 0, run_clock = -1;
    integer stray_offered = 0, not_ready_offers = 0, ever_not_ready = 0, clock_now = 0;
    integer fed = 0;
    reg feeding = 1'b0;
    always @(posedge clk) begin
        clock_now <= clock_now + 1;
        if (!in_ready) ever_not_ready <= 1;
        if (in_valid && !in_ready) not_ready_offers <= not_ready_offers + 1;
    end

    task tick;
        begin
            @(posedge clk);
            #1;
        end
    endtask

    initial begin
        if (!$value$plusargs("words=%s", words_file)) $fatal(1, "+words missing");
        if (!$value$plusargs("acts=%s", acts_file)) $fatal(1, "+acts missing");
        if (!$value$plusargs("out=%s", out_file)) $fatal(1, "+out missing");
        if (!$value$plusargs("summary=%s", summary_file)) $fatal(1, "+summary missing");
        if (!$value$plusargs("nwords=%d", nwords)) nwords = 0;
        if (!$value$plusargs("nacts=%d", nacts)) nacts = 0;
        if (!$value$plusargs("rows=%d", rows)) rows = 1;
        if (!$value$plusargs("cols=%d", cols)) cols = 1;
        if (!$value$plusargs("fmt=%d", fmt)) fmt = 1;
        if (!$value$plusargs("seq=%d", seq)) seq = 0;
        if (!$value$plusargs("gap=%d", gap)) gap = 0;
        if (!$value$plusargs("seed=%d", seed)) seed = 1;
        if (!$value$plusargs("stray_before=%d", stray_before)) stray_before = 0;
        if (!$value$plusargs("stray_after=%d", stray_after)) stray_after = 0;
        if (!$value$plusargs("txbusy=%d", txbusy)) txbusy = 2;
        if (!$value$plusargs("max_clocks=%d", max_clocks)) max_clocks = 5000000;
        if (nwords > 0) $readmemh(words_file, words, 0, nwords - 1);
        if (nacts > 0) $readmemh(acts_file, acts, 0, nacts - 1);
        fout = $fopen(out_file, "w");
        repeat (4) tick;
        rst_n = 1'b1;
        tick;
        // Activations: word i -> entry i / 10, bank i % 10.
        for (i = 0; i < nacts; i = i + 1) begin
            act_we = 1'b1; act_entry = i / 10; act_bank = i % 10; act_data = acts[i];
            tick;
        end
        act_we = 1'b0;
        tick;
        start = 1'b1;
        tick;
        start = 1'b0;
        // Stray words while the module is still in SETUP or MASK.
        for (i = 0; i < stray_before; i = i + 1) begin
            in_valid = 1'b1; in_lo = $random(seed); in_hi = $random(seed);
            stray_offered = stray_offered + 1;
            tick;
        end
        in_valid = 1'b0;
        clocks = 0;
        while (st != 3 && st != 5 && clocks < max_clocks) begin tick; clocks = clocks + 1; end
        run_clock = clock_now;
        if (st == 3) begin
            w = 0;
            while (w < nwords) begin
                if (($random(seed) & 32'h7fffffff) % 100 < gap) begin
                    in_valid = 1'b0;
                    if (first_clock >= 0) gap_clocks = gap_clocks + 1;
                end else begin
                    in_valid = 1'b1;
                    in_lo = words[w][63:0];
                    in_hi = words[w][127:64];
                    if (first_clock < 0) first_clock = clock_now;
                    last_clock = clock_now;
                    offered_run = offered_run + 1;
                    w = w + 1;
                end
                tick;
            end
            in_valid = 1'b0;
            tick;
            for (i = 0; i < stray_after; i = i + 1) begin
                in_valid = 1'b1; in_lo = $random(seed); in_hi = $random(seed);
                stray_offered = stray_offered + 1;
                tick;
                in_valid = 1'b0;
                tick;
            end
        end
        in_valid = 1'b0;
        clocks = 0;
        while (!done && clocks < max_clocks) begin tick; clocks = clocks + 1; end
        repeat (60) tick;
        fsum = $fopen(summary_file, "w");
        $fwrite(fsum, "offered %0d\nfirst_to_last %0d\ngap_clocks %0d\nlatency %0d\nstray %0d\nnot_ready_offers %0d\never_not_ready %0d\ndone %0d\n",
                offered_run, (first_clock >= 0) ? (last_clock - first_clock + 1) : 0, gap_clocks,
                (first_clock >= 0) ? (first_clock - run_clock) : 0, stray_offered, not_ready_offers, ever_not_ready,
                (clocks < max_clocks) ? 1 : 0);
        $fclose(fsum);
        $fclose(fout);
        $finish;
    end
endmodule
