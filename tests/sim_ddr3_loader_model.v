// Behavioural stand-in for UberDDR3's ddr3_top in the simulation of the DDR3 UART loader
// (issue #63 part 2, tests/test_ddr3_loader.py): the user Wishbone port and the
// calibration outputs only, with ddr3_top's port list and parameters at the pinned commit
// so that fpga/ax7203/ddr3/tms_ddr3_loader_ax7203.v elaborates unchanged. Our code
// (Apache-2.0), not derived from UberDDR3; no DDR3 pins are modelled. It follows
// tests/sim_ddr3_top_model.v (#61/#62) and adds what the loader's tests need:
//
// - The whole 2^25-burst address space of x16 (512 MiB) in a folded store: burst address a
//   lives in slot a mod 2^MEM_BITS with its tag a >> MEM_BITS. A slot never written reads
//   as the background word bg(a) (a function of the full address, below), so a read of
//   untouched memory returns known, address-dependent bytes, not zeros. An access to a slot
//   that holds another tag stops the run ($fatal "alias"): the tests choose their regions
//   so that none collide, and a collision cannot pass unnoticed.
// - Byte selects: a write stores the bytes whose i_wb_sel bit is set (UberDDR3 drives DM
//   from i_wb_sel, ddr3_controller.v L1296 at 79d8fd3e); writes with fewer than 16 selects
//   are counted (partial).
// - Calibration: o_debug1 walks through states 1..22 for +calib_clocks= clocks (default
//   3000), then 23 with o_calib_complete; o_wb_stall is high until then.
// - Stalls: pseudo-random on +stall_pct= percent of the clocks (default 20) and a
//   refresh-like window of 24 clocks every 700; in-order acks +ack_min= .. +ack_min= +
//   +ack_span= - 1 clocks after the request (defaults 6 and 8), at most one per clock,
//   never before the previous one; the read data are the stored word at the time the
//   request is taken. +lfsr_seed= seeds the pseudo-random sequence.
// - Bench controls (regs set by the testbench): bench_stall holds o_wb_stall high (a port
//   that takes no request), bench_hold freezes the ack queue (acks withheld, a write or read
//   that does not finish); both release what they held when they go low.
// - Protocol checks ($fatal): a stalled request must stay presented unchanged until taken.
// - +preload=<file> (the matvec build's tests, tests/test_ddr3_matvec_top.py): lines
//   "<burst address, hex> <128-bit word, hex>" stored before the first clock, as if written
//   (a weight image without its upload through the UART); counted in `preloaded`, not in
//   `writes`.
//
// Background word of burst address a (Python: tests/test_ddr3_loader.py, bg_word):
//   32-bit lane k (k = 0..3, bits 32k+31..32k) = (a * 32'h9E3779B1) ^ (32'h5EED0000 + k * 32'h01010101)
//   (the product modulo 2^32).
`timescale 1ns/1ps
`default_nettype none
module ddr3_top #(
    parameter CONTROLLER_CLK_PERIOD = 12000,
    parameter DDR3_CLK_PERIOD = 3000,
    parameter ROW_BITS = 15,
    parameter COL_BITS = 10,
    parameter BA_BITS = 3,
    parameter BYTE_LANES = 2,
    parameter AUX_WIDTH = 4,
    parameter DUAL_RANK_DIMM = 0,
    parameter SPEED_BIN = 3,
    parameter SDRAM_CAPACITY = 5,
    parameter MICRON_SIM = 0,
    parameter ODELAY_SUPPORTED = 0,
    parameter SECOND_WISHBONE = 0,
    parameter DLL_OFF = 0,
    parameter WB_ERROR = 0,
    parameter BIST_MODE = 1,
    parameter ECC_ENABLE = 0,
    parameter DIC = 2'b00,
    parameter RTT_NOM = 3'b011,
    parameter SELF_REFRESH = 2'b00,
    parameter MEM_BITS = 16,
    localparam WB_ADDR_BITS = ROW_BITS + COL_BITS + BA_BITS - 3,
    localparam WB_DATA_BITS = 64 * BYTE_LANES,
    localparam WB_SEL_BITS = WB_DATA_BITS / 8
) (
    input  wire i_controller_clk, i_ddr3_clk, i_ref_clk, i_ddr3_clk_90, i_rst_n,
    input  wire i_wb_cyc, i_wb_stb, i_wb_we,
    input  wire [WB_ADDR_BITS-1:0] i_wb_addr,
    input  wire [WB_DATA_BITS-1:0] i_wb_data,
    input  wire [WB_SEL_BITS-1:0] i_wb_sel,
    input  wire [AUX_WIDTH-1:0] i_aux,
    output reg  o_wb_stall,
    output reg  o_wb_ack,
    output wire o_wb_err,
    output reg  [WB_DATA_BITS-1:0] o_wb_data,
    output wire [AUX_WIDTH-1:0] o_aux,
    input  wire i_wb2_cyc, i_wb2_stb, i_wb2_we,
    input  wire [6:0] i_wb2_addr,
    input  wire [31:0] i_wb2_data,
    input  wire [3:0] i_wb2_sel,
    output wire o_wb2_stall, o_wb2_ack,
    output wire [31:0] o_wb2_data,
    output wire o_ddr3_clk_p, o_ddr3_clk_n, o_ddr3_reset_n, o_ddr3_cke, o_ddr3_cs_n,
    output wire o_ddr3_ras_n, o_ddr3_cas_n, o_ddr3_we_n,
    output wire [ROW_BITS-1:0] o_ddr3_addr,
    output wire [BA_BITS-1:0] o_ddr3_ba_addr,
    inout  wire [8*BYTE_LANES-1:0] io_ddr3_dq,
    inout  wire [BYTE_LANES-1:0] io_ddr3_dqs, io_ddr3_dqs_n,
    output wire [BYTE_LANES-1:0] o_ddr3_dm,
    output wire o_ddr3_odt,
    output reg  o_calib_complete,
    output reg  [31:0] o_debug1,
    input  wire i_user_self_refresh,
    output wire uart_tx
);
    localparam integer QDEPTH = 64;
    localparam integer SLOTS = 1 << MEM_BITS;
    assign o_wb_err = 1'b0;
    assign o_aux = {AUX_WIDTH{1'b0}};
    assign {o_wb2_stall, o_wb2_ack} = 2'b00;
    assign o_wb2_data = 32'd0;
    assign {o_ddr3_clk_p, o_ddr3_clk_n, o_ddr3_reset_n, o_ddr3_cke, o_ddr3_cs_n} = 5'b01011;
    assign {o_ddr3_ras_n, o_ddr3_cas_n, o_ddr3_we_n, o_ddr3_odt} = 4'b1110;
    assign o_ddr3_addr = {ROW_BITS{1'b0}};
    assign o_ddr3_ba_addr = {BA_BITS{1'b0}};
    assign o_ddr3_dm = {BYTE_LANES{1'b0}};
    assign uart_tx = 1'b1;

    reg [WB_DATA_BITS-1:0] mem [0:SLOTS-1];
    reg [31:0] tag [0:SLOTS-1];
    reg [SLOTS-1:0] used;
    reg [WB_DATA_BITS-1:0] q_data [0:QDEPTH-1];
    reg [63:0] q_due [0:QDEPTH-1];
    reg [QDEPTH-1:0] q_read;
    integer q_head, q_tail, q_count, k;
    integer calib_clocks, stall_pct, ack_min, ack_span;
    reg [31:0] lfsr_seed;
    reg [63:0] now, last_due;
    reg [31:0] lfsr;
    reg held, held_we;
    reg [WB_ADDR_BITS-1:0] held_addr;
    reg [WB_DATA_BITS-1:0] held_data;
    reg [WB_SEL_BITS-1:0] held_sel;
    reg [WB_DATA_BITS-1:0] word;
    reg take;
    // Bench controls and counts.
    reg bench_stall, bench_hold;
    integer writes, reads, partial, acks, preloaded, preload_fd;
    reg [4095:0] preload_path;
    reg [31:0] preload_addr;
    reg [WB_DATA_BITS-1:0] preload_word;

    initial begin
        if (!$value$plusargs("calib_clocks=%d", calib_clocks)) calib_clocks = 3000;
        if (!$value$plusargs("stall_pct=%d", stall_pct)) stall_pct = 20;
        if (!$value$plusargs("ack_min=%d", ack_min)) ack_min = 6;
        if (!$value$plusargs("ack_span=%d", ack_span)) ack_span = 8;
        if (!$value$plusargs("lfsr_seed=%h", lfsr_seed)) lfsr_seed = 32'hace1;
        if (lfsr_seed == 32'd0) $fatal(1, "model: +lfsr_seed must not be 0");
        used = {SLOTS{1'b0}};
        bench_stall = 1'b0;
        bench_hold = 1'b0;
        writes = 0; reads = 0; partial = 0; acks = 0; preloaded = 0;
        if ($value$plusargs("preload=%s", preload_path)) begin
            preload_fd = $fopen(preload_path, "r");
            if (preload_fd == 0) $fatal(1, "model: cannot open +preload");
            while ($fscanf(preload_fd, "%h %h\n", preload_addr, preload_word) == 2) begin
                store(preload_addr, preload_word, {WB_SEL_BITS{1'b1}});
                preloaded = preloaded + 1;
            end
            $fclose(preload_fd);
        end
    end

    function [WB_DATA_BITS-1:0] bg(input [31:0] a);
        integer j;
        reg [31:0] h;
        begin
            h = a * 32'h9E3779B1;
            for (j = 0; j < WB_DATA_BITS / 32; j = j + 1)
                bg[32*j +: 32] = h ^ (32'h5EED0000 + j * 32'h01010101);
        end
    endfunction

    // The stored word of burst address a (the background if never written).
    function [WB_DATA_BITS-1:0] peek(input [31:0] a);
        reg [31:0] s;
        begin
            s = a % SLOTS;
            if (used[s] && tag[s] != a / SLOTS)
                $fatal(1, "model: alias, burst %0d and burst %0d share slot %0d", a, tag[s] * SLOTS + s, s);
            peek = used[s] ? mem[s] : bg(a);
        end
    endfunction

    task store(input [31:0] a, input [WB_DATA_BITS-1:0] d, input [WB_SEL_BITS-1:0] sel);
        reg [31:0] s;
        begin
            word = peek(a);
            for (k = 0; k < WB_SEL_BITS; k = k + 1)
                if (sel[k]) word[8*k +: 8] = d[8*k +: 8];
            s = a % SLOTS;
            mem[s] = word;
            tag[s] = a / SLOTS;
            used[s] = 1'b1;
        end
    endtask

    always @(posedge i_controller_clk or negedge i_rst_n) begin
        if (!i_rst_n) begin
            now <= 0;
            o_calib_complete <= 1'b0;
            o_debug1 <= 32'd0;
            o_wb_stall <= 1'b1;
            o_wb_ack <= 1'b0;
            o_wb_data <= {WB_DATA_BITS{1'b0}};
            q_head = 0; q_tail = 0; q_count = 0; last_due = 0;
            lfsr <= lfsr_seed;
            held <= 1'b0;
        end else begin
            now <= now + 1;
            lfsr <= {lfsr[30:0], lfsr[31] ^ lfsr[21] ^ lfsr[1] ^ lfsr[0]};
            if (!o_calib_complete) begin
                o_debug1 <= (now * 22) / calib_clocks + 1;
                if (now >= calib_clocks) begin
                    o_calib_complete <= 1'b1;
                    o_debug1 <= 32'd23;
                end
            end
            if (held && i_wb_cyc && !(i_wb_stb && i_wb_we == held_we && i_wb_addr == held_addr &&
                                       (!held_we || (i_wb_data == held_data && i_wb_sel == held_sel))))
                $fatal(1, "wishbone: a stalled request changed or was withdrawn at clock %0d", now);
            take = i_wb_cyc && i_wb_stb && !o_wb_stall;
            held <= i_wb_cyc && i_wb_stb && o_wb_stall;
            held_we <= i_wb_we;
            held_addr <= i_wb_addr;
            held_data <= i_wb_data;
            held_sel <= i_wb_sel;
            if (take) begin
                if (i_wb_we) begin
                    store(i_wb_addr, i_wb_data, i_wb_sel);
                    writes = writes + 1;
                    if (i_wb_sel != {WB_SEL_BITS{1'b1}}) partial = partial + 1;
                end else begin
                    reads = reads + 1;
                end
                q_data[q_tail] = i_wb_we ? {WB_DATA_BITS{1'b0}} : peek(i_wb_addr);
                q_read[q_tail] = !i_wb_we;
                q_due[q_tail] = (now + ack_min + (lfsr % ack_span) > last_due) ? now + ack_min + (lfsr % ack_span)
                                                                               : last_due + 1;
                last_due = q_due[q_tail];
                q_tail = (q_tail + 1) % QDEPTH;
                q_count = q_count + 1;
            end
            o_wb_ack <= 1'b0;
            if (q_count > 0 && q_due[q_head] <= now && !bench_hold) begin
                o_wb_ack <= 1'b1;
                acks = acks + 1;
                o_wb_data <= q_read[q_head] ? q_data[q_head] : {WB_DATA_BITS{1'b0}};
                q_head = (q_head + 1) % QDEPTH;
                q_count = q_count - 1;
            end
            o_wb_stall <= !o_calib_complete || q_count + (take ? 1 : 0) >= QDEPTH - 2 ||
                          (lfsr[15:8] * 100 < stall_pct * 256) || (now % 700) < 24 || bench_stall;
        end
    end
endmodule
`default_nettype wire
