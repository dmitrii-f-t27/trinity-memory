// Behavioural stand-in for UberDDR3's ddr3_top in simulation (issue #61): the
// user Wishbone port and the calibration outputs only, with the port list and
// parameters of ddr3_top at the pinned commit so that
// fpga/ax7203/ddr3/tms_ddr3_ax7203.v elaborates unchanged. Our code (Apache-2.0),
// not derived from UberDDR3; no DDR3 pins are modelled (they are left undriven).
//
// Port behaviour, as docs/hardware.md "What the #61 pattern test needs" states
// it for UberDDR3 79d8fd3e: o_wb_stall is high until calibration completes and
// then, pseudo-randomly, on +stall_pct percent of the clocks and for a
// refresh-like window of 24 clocks every 700; a request is taken on a clock with
// i_wb_cyc, i_wb_stb and not o_wb_stall; writes and reads are acknowledged in
// order after 6 to 13 clocks (at most one ack per clock, never held off), and
// o_wb_data is valid with a read's ack; i_wb_cyc low drops every request in
// flight. Writes store the bytes whose i_wb_sel bit is set, at the time they are
// taken; reads return the stored burst at the time they are taken.
//
// Protocol checks ($fatal): a stalled request must stay presented unchanged
// (stb, we, address, data, sel) until it is taken; an address beyond the model's
// 2^MEM_BITS bursts.
//
// Faults (plusargs), for the pattern test's detection tests:
//   +stuck_addr_bit=<b> +stuck_addr_val=<0|1>  address bit b of every request forced
//   +stuck_dq=<j> +stuck_dq_val=<0|1>          DQ j (lane j/8, bit j%8) of every beat of
//                                               every stored write forced
//   +drop_addr=<a> +drop_nth=<n>               the n-th write (from 0) to burst a is not stored
//   +hang_after=<n>                            no request is taken after the n-th
// and +calib_clocks=<n> (default 3000) for the calibration time.
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

    reg [WB_DATA_BITS-1:0] mem [0:(1 << MEM_BITS) - 1];
    reg [WB_DATA_BITS-1:0] q_data [0:QDEPTH-1];
    reg [63:0] q_due [0:QDEPTH-1];
    reg [QDEPTH-1:0] q_read;
    integer q_head, q_tail, q_count, i, k;
    integer calib_clocks, stall_pct, stuck_bit, stuck_val, stuck_dq, stuck_dq_val, drop_nth, hang_after;
    reg [63:0] drop_addr;
    integer drop_seen, taken;
    reg [63:0] now, last_due;
    reg [31:0] lfsr;
    reg held;
    reg held_we;
    reg [WB_ADDR_BITS-1:0] held_addr;
    reg [WB_DATA_BITS-1:0] held_data;
    reg [WB_SEL_BITS-1:0] held_sel;
    reg [WB_ADDR_BITS-1:0] phys;
    reg [WB_DATA_BITS-1:0] word;
    reg take;

    initial begin
        if (!$value$plusargs("calib_clocks=%d", calib_clocks)) calib_clocks = 3000;
        if (!$value$plusargs("stall_pct=%d", stall_pct)) stall_pct = 20;
        if (!$value$plusargs("stuck_addr_bit=%d", stuck_bit)) stuck_bit = -1;
        if (!$value$plusargs("stuck_addr_val=%d", stuck_val)) stuck_val = 0;
        if (!$value$plusargs("stuck_dq=%d", stuck_dq)) stuck_dq = -1;
        if (!$value$plusargs("stuck_dq_val=%d", stuck_dq_val)) stuck_dq_val = 0;
        if (!$value$plusargs("drop_addr=%d", drop_addr)) drop_addr = {64{1'b1}};
        if (!$value$plusargs("drop_nth=%d", drop_nth)) drop_nth = 0;
        if (!$value$plusargs("hang_after=%d", hang_after)) hang_after = -1;
        for (i = 0; i < (1 << MEM_BITS); i = i + 1) mem[i] = {WB_DATA_BITS{1'b0}};
    end

    function [WB_ADDR_BITS-1:0] fault_addr(input [WB_ADDR_BITS-1:0] a);
        begin
            fault_addr = a;
            if (stuck_bit >= 0) fault_addr[stuck_bit] = stuck_val[0];
        end
    endfunction

    // Store a write: bytes with sel set, then the stuck DQ bit in every beat.
    task store(input [WB_ADDR_BITS-1:0] a, input [WB_DATA_BITS-1:0] d, input [WB_SEL_BITS-1:0] sel);
        begin
            word = mem[a];
            for (k = 0; k < WB_SEL_BITS; k = k + 1)
                if (sel[k]) word[8*k +: 8] = d[8*k +: 8];
            if (stuck_dq >= 0)
                for (k = 0; k < 8; k = k + 1)
                    word[8 * (BYTE_LANES * k + stuck_dq / 8) + stuck_dq % 8] = stuck_dq_val[0];
            mem[a] = word;
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
            lfsr <= 32'hace1;
            held <= 1'b0;
            drop_seen = 0;
            taken = 0;
        end else begin
            now <= now + 1;
            lfsr <= {lfsr[30:0], lfsr[31] ^ lfsr[21] ^ lfsr[1] ^ lfsr[0]};
            if (!o_calib_complete) begin
                // Calibration: states 1..22 in equal slices, then 23 (DONE_CALIBRATE).
                o_debug1 <= (now * 22) / calib_clocks + 1;
                if (now >= calib_clocks) begin
                    o_calib_complete <= 1'b1;
                    o_debug1 <= 32'd23;
                end
            end
            // Protocol: a stalled request stays presented, unchanged.
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
                phys = fault_addr(i_wb_addr);
                if (phys >= (1 << MEM_BITS)) $fatal(1, "model: burst address %0d beyond 2^%0d", phys, MEM_BITS);
                if (i_wb_we) begin
                    if (i_wb_addr == drop_addr) begin
                        if (drop_seen != drop_nth) store(phys, i_wb_data, i_wb_sel);  // else: the dropped write
                        drop_seen = drop_seen + 1;
                    end else begin
                        store(phys, i_wb_data, i_wb_sel);
                    end
                end
                q_data[q_tail] = i_wb_we ? {WB_DATA_BITS{1'b0}} : mem[phys];
                q_read[q_tail] = !i_wb_we;
                q_due[q_tail] = (now + 6 + (lfsr[2:0]) > last_due) ? now + 6 + lfsr[2:0] : last_due + 1;
                last_due = q_due[q_tail];
                q_tail = (q_tail + 1) % QDEPTH;
                q_count = q_count + 1;
                taken = taken + 1;
            end
            // In-order acks, one per clock at most.
            o_wb_ack <= 1'b0;
            if (!i_wb_cyc) begin
                q_head = q_tail; q_count = 0;
            end else if (q_count > 0 && q_due[q_head] <= now) begin
                o_wb_ack <= 1'b1;
                o_wb_data <= q_read[q_head] ? q_data[q_head] : {WB_DATA_BITS{1'b0}};
                q_head = (q_head + 1) % QDEPTH;
                q_count = q_count - 1;
            end
            // Stall: before calibration, when the queue is nearly full, on stall_pct percent
            // of the clocks, in a refresh-like window of 24 clocks every 700, and for ever
            // after hang_after requests.
            o_wb_stall <= !o_calib_complete || q_count + (take ? 1 : 0) >= QDEPTH - 2 ||
                          (lfsr[15:8] * 100 < stall_pct * 256) || (now % 700) < 24 ||
                          (hang_after >= 0 && taken + (take ? 1 : 0) >= hang_after);
        end
    end
endmodule
`default_nettype wire
