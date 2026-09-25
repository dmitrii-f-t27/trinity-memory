// AX7203 DDR3 build with the UART loader (issue #63, part 2; make -C fpga/ax7203 ...
// DDR3_APP=loader, x16 only). Wiring only: every state machine and rule is executable
// t27 compiled by the pinned t27c:
//   t27/rtl/fpga_reset.t27          reset generator (button, PLL lock) and heartbeat
//   t27/rtl/fpga_uart_rx.t27        8N1 receiver
//   t27/rtl/fpga_ddr3_loader.t27    the loader of part 1 with the calibration gate and status
//   t27/rtl/fpga_line_emitter.t27   response lines
//   t27/rtl/fpga_uart_tx.t27        transmitter (shared: lines and read-back frames)
//   t27/rtl/fpga_loader_wb.t27      Wishbone master behind the loader's write and read ports
//   t27/rtl/fpga_wb_arbiter.t27     the user port shared between two masters
// and UberDDR3's ddr3_top (GPL-3.0-or-later, fetched at the commit in uberddr3.lock into
// build/uberddr3/, never committed). Clocking, reset and the ddr3_top parameters are
// those of tms_ddr3_ax7203.v (#60-#62), which is left unchanged so that the pattern-test
// and reader builds keep their netlists; the loader runs on the controller clock.
//
// UART (N15 TX, P20 RX, 8N1): 115200 baud from the controller clock by default
// (UART_DIV 0: 723 clocks per bit at 83.33 MHz); TIMEOUT_CLOCKS (0: 50 ms) is the
// loader's inter-byte timeout and port watchdog, PROBATION_CLOCKS (0: 3 s) the time a
// new baud rate must be confirmed within (docs/uart-loader.md). The store is UberDDR3's
// memory: 2^STORE_LOG2 bytes (29: 512 MiB, chip U6 in x16).
// LEDs: [0] heartbeat, [1] o_calib_complete, [2] a chunk committed since reset,
// [3] a nak sent since reset.
//
// Master 1 of the arbiter is idle in the board build. Read with `define
// DDR3_LOADER_READER (simulation only, tests/test_ddr3_loader.py) the #62 burst reader
// (tms_ddr3_reader.v, t27/rtl/fpga_ddr3_reader.t27) is master 1, with its report lines on
// a second line emitter and transmitter (uart_tx2); nothing of it exists otherwise.
//
// Read with `define DDR3_MATVEC (make ... DDR3_APP=matvec, issue #64) the build carries the
// device matvec: the loader runs protocol 4 (`matvec` high: activation frames X into the
// matvec's banks, matvec frames M), t27/rtl/fpga_ddr3_matvec.t27 computes y = W x from the
// words t27/rtl/fpga_matvec_feed.t27 reads as master 1 (FEED_CAP requests outstanding at
// most, FEED_WATCHDOG clocks without progress end a run with an abort), and
// t27/rtl/fpga_line_arbiter.t27 shares the line emitter between the loader's lines and the
// matvec's Y and Z lines (docs/bridge.md, "Wire protocol extension"). The two defines
// exclude each other.
`timescale 1ns/1ps
`default_nettype none
module tms_ddr3_loader_ax7203 #(
    parameter integer PLL_MULT = 5,             // VCO = 200 MHz * PLL_MULT
    parameter integer DDR_DIV = 3,              // DDR3 clock = VCO / DDR_DIV
    parameter [31:0] BUILD_ID = 32'h0,
    parameter integer UART_DIV = 0,             // clocks per UART bit after reset; 0 = 115200 from the controller clock
    parameter integer TIMEOUT_CLOCKS = 0,       // 0 = 50 ms of the controller clock
    parameter integer PROBATION_CLOCKS = 0,     // 0 = 3 s of the controller clock
    parameter integer STORE_LOG2 = 29           // 512 MiB: 2^25 bursts of 16 bytes (x16)
`ifdef DDR3_MATVEC
    ,
    parameter [31:0] FEED_CAP = 32'd64,
    parameter [31:0] FEED_WATCHDOG = 32'd32768
`endif
`ifdef DDR3_LOADER_READER
    ,
    parameter [31:0] READER_TRITS = 32'd1237,
    parameter [31:0] READER_SEED = 32'h00000062,
    parameter [31:0] READER_CAP = 32'd64,
    parameter [31:0] READER_RUNS = 32'd0,
    parameter [31:0] READER_WATCHDOG = 32'd16777216,
    parameter integer READER_UART_DIV = 4
`endif
) (
    input  wire         clk200_p,
    input  wire         clk200_n,
    input  wire         rst_n,
    output wire [3:0]   led,
    output wire         uart_tx,
    input  wire         uart_rx,
`ifdef DDR3_LOADER_READER
    output wire         uart_tx2,
`endif
    output wire         ddr3_ck_p,
    output wire         ddr3_ck_n,
    output wire         ddr3_reset_n,
    output wire         ddr3_cke,
    output wire         ddr3_cs_n,
    output wire         ddr3_ras_n,
    output wire         ddr3_cas_n,
    output wire         ddr3_we_n,
    output wire         ddr3_odt,
    output wire [14:0]  ddr3_addr,
    output wire [2:0]   ddr3_ba,
    inout  wire [15:0]  ddr3_dq,
    inout  wire [1:0]   ddr3_dqs_p,
    inout  wire [1:0]   ddr3_dqs_n,
    output wire [1:0]   ddr3_dm
);
    localparam integer BYTE_LANES = 2;
    localparam integer VCO_PS = 5000 / PLL_MULT;
    localparam integer DDR3_PS = VCO_PS * DDR_DIV;
    localparam integer CTRL_PS = 4 * DDR3_PS;
    localparam integer CTRL_HZ = 200000000 / (4 * DDR_DIV) * PLL_MULT;
    localparam integer BAUD_DIV = UART_DIV != 0 ? UART_DIV : (CTRL_HZ + 57600) / 115200;   // 723 at 83.33 MHz
    localparam integer TIMEOUT = TIMEOUT_CLOCKS != 0 ? TIMEOUT_CLOCKS : CTRL_HZ / 20;         // 50 ms
    localparam integer PROBATION = PROBATION_CLOCKS != 0 ? PROBATION_CLOCKS : 3 * CTRL_HZ;   // 3 s
    localparam integer WB_ADDR = 15 + 10 + 3 - 3;
`ifdef DDR3_MATVEC
`ifdef DDR3_LOADER_READER
    DDR3_MATVEC_and_DDR3_LOADER_READER_exclude_each_other both_defined();
`endif
`endif

    // ---- clocks (as tms_ddr3_ax7203.v) ----
    wire clk200_in, pll_fb, pll_locked;
    wire ctrl_raw, ddr_raw, ref_raw, ddr90_raw;
    wire clk_ctrl, clk_ddr, clk_ref, clk_ddr_90;
    IBUFDS clk200_ibufds (.I(clk200_p), .IB(clk200_n), .O(clk200_in));
    PLLE2_ADV #(
        .BANDWIDTH("OPTIMIZED"),
        .COMPENSATION("INTERNAL"),
        .STARTUP_WAIT("FALSE"),
        .CLKIN1_PERIOD(5.000),
        .DIVCLK_DIVIDE(1),
        .CLKFBOUT_MULT(PLL_MULT),
        .CLKFBOUT_PHASE(0.000),
        .CLKOUT0_DIVIDE(4 * DDR_DIV), .CLKOUT0_PHASE(0.000),  .CLKOUT0_DUTY_CYCLE(0.500),
        .CLKOUT1_DIVIDE(DDR_DIV),     .CLKOUT1_PHASE(0.000),  .CLKOUT1_DUTY_CYCLE(0.500),
        .CLKOUT2_DIVIDE(PLL_MULT),    .CLKOUT2_PHASE(0.000),  .CLKOUT2_DUTY_CYCLE(0.500),
        .CLKOUT3_DIVIDE(DDR_DIV),     .CLKOUT3_PHASE(90.000), .CLKOUT3_DUTY_CYCLE(0.500)
    ) pll (
        .CLKIN1(clk200_in), .CLKIN2(1'b0), .CLKINSEL(1'b1),
        .CLKFBIN(pll_fb), .CLKFBOUT(pll_fb),
        .CLKOUT0(ctrl_raw), .CLKOUT1(ddr_raw), .CLKOUT2(ref_raw), .CLKOUT3(ddr90_raw),
        .CLKOUT4(), .CLKOUT5(),
        .RST(1'b0), .PWRDWN(1'b0), .LOCKED(pll_locked),
        .DADDR(7'd0), .DCLK(1'b0), .DEN(1'b0), .DI(16'd0), .DWE(1'b0), .DO(), .DRDY()
    );
    BUFG clk_ctrl_bufg   (.I(ctrl_raw),  .O(clk_ctrl));
    BUFG clk_ddr_bufg    (.I(ddr_raw),   .O(clk_ddr));
    BUFG clk_ref_bufg    (.I(ref_raw),   .O(clk_ref));
    BUFG clk_ddr_90_bufg (.I(ddr90_raw), .O(clk_ddr_90));

    // ---- reset and heartbeat; the loader side leaves reset two clocks after the controller ----
    wire rst, heartbeat, app_rst_n;
    TrinityFpgaResetT27 resetgen (
        .clk(clk_ctrl), .rst_n(1'b1), .en(1'b1), .ready(), .button_n(rst_n & pll_locked),
        .stage1(), .stage2(), .hold(), .blink(), .reset(rst), .heartbeat(heartbeat)
    );
    TrinityFpgaResetT27 app_reset (
        .clk(clk_ctrl), .rst_n(1'b1), .en(1'b1), .ready(), .button_n(!rst),
        .stage1(), .stage2(app_rst_n), .hold(), .blink(), .reset(), .heartbeat()
    );

    // ---- UberDDR3 (parameters as tms_ddr3_ax7203.v, x16) ----
    wire         calib_complete;
    wire [31:0]  debug1;
    wire         wb_cyc, wb_stb, wb_we, wb_stall, wb_ack;
    wire [31:0]  wb_addr, wb_sel;
    wire [63:0]  wb_lo, wb_hi;
    wire [127:0] wb_rdata;
    ddr3_top #(
        .CONTROLLER_CLK_PERIOD(CTRL_PS),
        .DDR3_CLK_PERIOD(DDR3_PS),
        .ROW_BITS(15),
        .COL_BITS(10),
        .BA_BITS(3),
        .BYTE_LANES(BYTE_LANES),
        .AUX_WIDTH(4),
        .DUAL_RANK_DIMM(0),
        .SPEED_BIN(3),
        .SDRAM_CAPACITY(4),
        .MICRON_SIM(0),
        .ODELAY_SUPPORTED(0),
        .SECOND_WISHBONE(0),
        .DLL_OFF(0),
        .WB_ERROR(0),
        .BIST_MODE(1),
        .ECC_ENABLE(0),
        .DIC(2'b01),
        .RTT_NOM(3'b001),
        .SELF_REFRESH(2'b00)
    ) ddr3 (
        .i_controller_clk(clk_ctrl), .i_ddr3_clk(clk_ddr), .i_ref_clk(clk_ref), .i_ddr3_clk_90(clk_ddr_90),
        .i_rst_n(!rst),
        .i_wb_cyc(wb_cyc), .i_wb_stb(wb_stb), .i_wb_we(wb_we),
        .i_wb_addr(wb_addr[WB_ADDR-1:0]), .i_wb_data({wb_hi, wb_lo}), .i_wb_sel(wb_sel[15:0]), .i_aux(4'd0),
        .o_wb_stall(wb_stall), .o_wb_ack(wb_ack), .o_wb_err(), .o_wb_data(wb_rdata), .o_aux(),
        .i_wb2_cyc(1'b0), .i_wb2_stb(1'b0), .i_wb2_we(1'b0), .i_wb2_addr(7'd0), .i_wb2_data(32'd0), .i_wb2_sel(4'd0),
        .o_wb2_stall(), .o_wb2_ack(), .o_wb2_data(),
        .o_ddr3_clk_p(ddr3_ck_p), .o_ddr3_clk_n(ddr3_ck_n), .o_ddr3_reset_n(ddr3_reset_n),
        .o_ddr3_cke(ddr3_cke), .o_ddr3_cs_n(ddr3_cs_n),
        .o_ddr3_ras_n(ddr3_ras_n), .o_ddr3_cas_n(ddr3_cas_n), .o_ddr3_we_n(ddr3_we_n),
        .o_ddr3_addr(ddr3_addr), .o_ddr3_ba_addr(ddr3_ba),
        .io_ddr3_dq(ddr3_dq), .io_ddr3_dqs(ddr3_dqs_p), .io_ddr3_dqs_n(ddr3_dqs_n), .o_ddr3_dm(ddr3_dm),
        .o_ddr3_odt(ddr3_odt),
        .o_calib_complete(calib_complete), .o_debug1(debug1),
        .i_user_self_refresh(1'b0), .uart_tx()
    );

    // ---- receiver, transmitter, line emitter ----
    wire [31:0] div_now, rx_data, rx_bytes, rx_ferrs, rx_false;
    wire        rx_valid, rx_ferr;
    TrinityFpgaUartRxT27 receiver (
        .clk(clk_ctrl), .rst_n(app_rst_n), .en(1'b1), .ready(),
        .uart_rx(uart_rx), .baud_div(div_now),
        .valid(rx_valid), .data(rx_data), .ferr(rx_ferr),
        .bytes(rx_bytes), .framing_errors(rx_ferrs), .false_starts(rx_false)
    );
    wire        em_tx_start, ld_tx_start, tx_busy, line_idle, line_go;
    wire [31:0] em_tx_byte, ld_tx_byte, line_tag, line_a;
    wire [63:0] line_b;
    // The emitter's request and idle: the loader's own, or through the line arbiter (DDR3_MATVEC).
    wire        em_go, em_idle;
    wire [31:0] em_tag, em_a;
    wire [63:0] em_b;
    TrinityFpgaUartTxT27 transmitter (
        .clk(clk_ctrl), .rst_n(app_rst_n), .en(1'b1), .ready(),
        .start(em_tx_start | ld_tx_start), .data(ld_tx_start ? ld_tx_byte : em_tx_byte), .baud_div(div_now),
        .busy(tx_busy), .shift(), .count(), .bits(), .tx(uart_tx)
    );
    TrinityFpgaLineEmitterT27 emitter (
        .clk(clk_ctrl), .rst_n(app_rst_n), .en(1'b1), .ready(),
        .go(em_go), .tag(em_tag), .a(em_a), .b(em_b), .tx_busy(tx_busy),
        .line_busy(), .pos(), .tag_q(), .a_q(), .b_q(), .tx_start(em_tx_start), .tx_byte(em_tx_byte), .idle(em_idle)
    );
`ifndef DDR3_MATVEC
    assign {em_go, em_tag, em_a, em_b} = {line_go, line_tag, line_a, line_b};
    assign line_idle = em_idle;
`endif

    // ---- the loader's store: Wishbone master 0 ----
    wire        wr_valid, wr_last, wr_ready, wr_idle, rd_req, rd_ready, rd_valid, port_give_up;
    wire [31:0] wr_addr, wr_data, rd_addr, rd_byte;
    wire        m0_cyc, m0_stb, m0_we, m0_stall, m0_ack, m1_write;
    wire [31:0] m0_addr, m0_sel;
    wire [63:0] m0_lo, m0_hi;
    wire [31:0] w_writes, w_reads, w_hits, w_dropped, w_stray, w_outst, w_lat, w_stalls;
    TrinityFpgaLoaderWbT27 master (
        .clk(clk_ctrl), .rst_n(app_rst_n), .en(1'b1), .ready(),
        .wr_valid(wr_valid), .wr_addr(wr_addr), .wr_data(wr_data), .wr_last(wr_last),
        .rd_req(rd_req), .rd_addr(rd_addr), .stall(m0_stall), .ack(m0_ack),
        .rdata_lo(wb_rdata[63:0]), .rdata_hi(wb_rdata[127:64]), .invalidate(m1_write), .give_up(port_give_up),
        .wb_cyc(m0_cyc), .wb_stb(m0_stb), .wb_we(m0_we), .wb_addr(m0_addr), .wb_sel(m0_sel),
        .wb_lo(m0_lo), .wb_hi(m0_hi), .rd_valid(rd_valid), .rdata(rd_byte),
        .writes(w_writes), .reads(w_reads), .hits(w_hits), .dropped(w_dropped), .stray(w_stray),
        .max_outst(w_outst), .rd_lat_max(w_lat), .cmd_stalls(w_stalls),
        .wr_ready(wr_ready), .wr_idle(wr_idle), .rd_ready(rd_ready)
    );

    // ---- master 1: idle in the board build, the #62 reader in simulation ----
    wire        m1_cyc, m1_stb, m1_we, m1_stall, m1_ack;
    wire [31:0] m1_addr, m1_sel;
    wire [63:0] m1_lo, m1_hi;
`ifdef DDR3_LOADER_READER
    wire        r_go, r_idle, r_tx_start, r_tx_busy, r_cyc;
    wire [31:0] r_tag, r_a, r_tx_byte;
    wire [63:0] r_b;
    wire [127:0] r_wdata;
    tms_ddr3_reader #(
        .TRITS(READER_TRITS), .SEED(READER_SEED), .CAP(READER_CAP), .RUNS(READER_RUNS),
        .WATCHDOG(READER_WATCHDOG), .LANES_BUS(BYTE_LANES)
    ) reader (
        .clk(clk_ctrl), .rst_n(app_rst_n), .calib(calib_complete), .stall(m1_stall), .ack(m1_ack),
        .rdata(wb_rdata),
        .s_go(1'b0), .s_tag(32'd0), .s_a(32'd0), .s_b(64'd0), .line_idle(r_idle),
        .wb_cyc(r_cyc), .wb_stb(m1_stb), .wb_we(m1_we), .wb_addr(m1_addr), .wb_sel(m1_sel), .wb_wdata(r_wdata),
        .line_go(r_go), .line_tag(r_tag), .line_a(r_a), .line_b(r_b), .status_idle()
    );
    // The reader holds its cyc high for ever; it wants the port while it presents a request
    // (the arbiter keeps the port while any of its requests is outstanding).
    assign m1_cyc = m1_stb;
    assign {m1_hi, m1_lo} = r_wdata;
    TrinityFpgaLineEmitterT27 reader_emitter (
        .clk(clk_ctrl), .rst_n(app_rst_n), .en(1'b1), .ready(),
        .go(r_go), .tag(r_tag), .a(r_a), .b(r_b), .tx_busy(r_tx_busy),
        .line_busy(), .pos(), .tag_q(), .a_q(), .b_q(), .tx_start(r_tx_start), .tx_byte(r_tx_byte), .idle(r_idle)
    );
    TrinityFpgaUartTxT27 reader_uart (
        .clk(clk_ctrl), .rst_n(app_rst_n), .en(1'b1), .ready(),
        .start(r_tx_start), .data(r_tx_byte), .baud_div(READER_UART_DIV),
        .busy(r_tx_busy), .shift(), .count(), .bits(), .tx(uart_tx2)
    );
`elsif DDR3_MATVEC
    // ---- master 1: the matvec's feed (reads only), and the device matvec (issue #64) ----
    wire        mv_start, feed_start, act_we, tx_lock, mv_busy, matvec_busy, feed_busy, feed_abort;
    wire        mv_in_ready, feed_valid, mv_line_go, mv_idle;
    wire [31:0] mv_rows, mv_cols, mv_fmt, mv_seq, feed_base, feed_words, act_entry, act_bank;
    wire [31:0] mv_line_tag, mv_line_a;
    wire [63:0] act_data, feed_lo, feed_hi, mv_line_b;
    TrinityFpgaMatvecFeedT27 feed (
        .clk(clk_ctrl), .rst_n(app_rst_n), .en(1'b1), .ready(),
        .start(feed_start), .s_base(feed_base), .s_words(feed_words), .mv_ready(mv_in_ready),
        .stall(m1_stall), .ack(m1_ack), .rdata_lo(wb_rdata[63:0]), .rdata_hi(wb_rdata[127:64]),
        .cap(FEED_CAP), .watchdog(FEED_WATCHDOG),
        .busy(feed_busy), .abort(feed_abort), .wb_cyc(m1_cyc), .wb_stb(m1_stb), .wb_addr(m1_addr),
        .in_valid(feed_valid), .in_lo(feed_lo), .in_hi(feed_hi)
    );
    assign m1_we = 1'b0;
    assign m1_sel = 32'h0000ffff;
    assign {m1_hi, m1_lo} = 128'd0;
    TrinityFpgaDdr3MatvecT27 matvec (
        .clk(clk_ctrl), .rst_n(app_rst_n), .en(1'b1), .ready(),
        .start(mv_start), .cfg_rows(mv_rows), .cfg_cols(mv_cols), .cfg_fmt(mv_fmt), .cfg_seq(mv_seq),
        .in_valid(feed_valid), .in_lo(feed_lo), .in_hi(feed_hi),
        .act_we(act_we), .act_entry(act_entry), .act_bank(act_bank), .act_data(act_data),
        .line_idle(mv_idle), .abort(feed_abort),
        .line_go(mv_line_go), .line_tag(mv_line_tag), .line_a(mv_line_a), .line_b(mv_line_b),
        .busy(matvec_busy), .in_ready(mv_in_ready)
    );
    assign mv_busy = matvec_busy | feed_busy;
    TrinityFpgaLineArbiterT27 line_arbiter (
        .clk(clk_ctrl), .rst_n(app_rst_n), .en(1'b1), .ready(),
        .go0(line_go), .tag0(line_tag), .a0(line_a), .b0(line_b),
        .go1(mv_line_go), .tag1(mv_line_tag), .a1(mv_line_a), .b1(mv_line_b),
        .em_idle(em_idle), .lock0(tx_lock),
        .go(em_go), .tag(em_tag), .a(em_a), .b(em_b), .idle0(line_idle), .idle1(mv_idle)
    );
`else
    assign {m1_cyc, m1_stb, m1_we} = 3'b000;
    assign m1_addr = 32'd0;
    assign m1_sel = 32'd0;
    assign {m1_hi, m1_lo} = 128'd0;
`endif

    // ---- arbiter of the user port ----
    wire [31:0] a_switches, a_stray;
    TrinityFpgaWbArbiterT27 arbiter (
        .clk(clk_ctrl), .rst_n(app_rst_n), .en(1'b1), .ready(),
        .m0_cyc(m0_cyc), .m0_stb(m0_stb), .m0_we(m0_we), .m0_addr(m0_addr), .m0_sel(m0_sel), .m0_lo(m0_lo), .m0_hi(m0_hi),
        .m1_cyc(m1_cyc), .m1_stb(m1_stb), .m1_we(m1_we), .m1_addr(m1_addr), .m1_sel(m1_sel), .m1_lo(m1_lo), .m1_hi(m1_hi),
        .wb_stall(wb_stall), .wb_ack(wb_ack),
        .cyc(wb_cyc), .stb(wb_stb), .we(wb_we), .addr(wb_addr), .sel(wb_sel), .lo(wb_lo), .hi(wb_hi),
        .m0_stall(m0_stall), .m1_stall(m1_stall), .m0_ack(m0_ack), .m1_ack(m1_ack), .m1_write(m1_write),
        .switches(a_switches), .stray(a_stray)
    );

    // ---- loader ----
    wire committed_any, any_nak;
    TrinityFpgaDdr3LoaderT27 loader (
        .clk(clk_ctrl), .rst_n(app_rst_n), .en(1'b1), .ready(),
        .rx_valid(rx_valid), .rx_data(rx_data), .rx_ferr(rx_ferr),
        .rx_bytes(rx_bytes), .rx_ferrs(rx_ferrs), .rx_false(rx_false),
        .line_idle(line_idle), .tx_busy(tx_busy), .wr_ready(wr_ready), .wr_idle(wr_idle),
        .rd_ready(rd_ready), .rd_valid(rd_valid), .rd_data(rd_byte),
        .store_log2(STORE_LOG2), .default_div(BAUD_DIV), .timeout_clocks(TIMEOUT),
        .probation_clocks(PROBATION), .build_id(BUILD_ID),
        .calib(calib_complete), .cal_state(debug1), .wb_writes(w_writes), .wb_reads(w_reads), .wb_hits(w_hits),
        .wb_dropped(w_dropped), .wb_stray(w_stray), .wb_max_outst(w_outst), .wb_rd_lat_max(w_lat),
        .wb_cmd_stalls(w_stalls), .arb_switches(a_switches), .arb_stray(a_stray),
`ifdef DDR3_MATVEC
        .matvec(1'b1), .mv_busy(mv_busy),
        .act_we(act_we), .act_entry(act_entry), .act_bank(act_bank), .act_data(act_data),
        .mv_start(mv_start), .mv_rows(mv_rows), .mv_cols(mv_cols), .mv_fmt(mv_fmt), .mv_seq(mv_seq),
        .feed_start(feed_start), .feed_base(feed_base), .feed_words(feed_words), .tx_lock(tx_lock),
`else
        .matvec(1'b0), .mv_busy(1'b0),
`endif
        .line_go(line_go), .line_tag(line_tag), .line_a(line_a), .line_b(line_b),
        .tx_start(ld_tx_start), .tx_byte(ld_tx_byte),
        .wr_valid(wr_valid), .wr_addr(wr_addr), .wr_data(wr_data), .wr_last(wr_last),
        .rd_req(rd_req), .rd_addr(rd_addr), .port_give_up(port_give_up),
        .div_now(div_now), .committed_any(committed_any), .any_nak(any_nak)
    );

    assign led = {any_nak, committed_any, calib_complete, heartbeat};
endmodule
`default_nettype wire
