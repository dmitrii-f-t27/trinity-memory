// AX7203 DDR3 build (issues #60, #61): UberDDR3's ddr3_top with its built-in
// self-test, our clocking, reset, LEDs, a calibration status line on the UART
// and, with PATTERN_TEST 1, our pattern test on the user Wishbone port. Wiring
// only: the status and test logic is executable t27
// (t27/rtl/fpga_ddr3_status.t27, fpga_ddr3_pattern.t27, fpga_reset.t27,
// fpga_uart_tx.t27, fpga_line_emitter.t27), the controller and its PHY are UberDDR3 at the commit
// pinned in uberddr3.lock (GPL-3.0-or-later, fetched into build/uberddr3/ by
// `make -C fpga/ax7203 ddr3-fetch`, never committed here).
//
// Memory: two Micron MT41J256M16HA-125 (4 Gb, x16) in one rank, sharing
// address, command and CK (bank 34); DQ, DQS and DM in bank 35. BYTE_LANES 2 is
// the lower chip (U6, DQ[15:0]); the upper chip still sees every command.
// BYTE_LANES 4 is the whole 32-bit bus.
//
// Clocking: the 200 MHz oscillator (R4/T4) drives one PLLE2_ADV,
// VCO = 200 MHz * PLL_MULT (DIVCLK 1):
//   CLKOUT0  VCO / (4 * DDR_DIV)          controller clock (4:1 controller)
//   CLKOUT1  VCO / DDR_DIV                DDR3 clock
//   CLKOUT2  VCO / PLL_MULT = 200 MHz     IDELAYCTRL reference
//   CLKOUT3  VCO / DDR_DIV, phase 90      DDR3 clock at 90 degrees (ODELAY_SUPPORTED 0)
// PLL_MULT 5, DDR_DIV 3 gives 83.33 / 333.33 / 200 / 333.33 MHz (VCO 1000 MHz),
// UberDDR3's periods 12,000 and 3,000 ps. PLL_MULT 8, DDR_DIV 5 gives 80 / 320
// MHz (VCO 1600 MHz, 12,500 and 3,125 ps). Every output goes through a BUFG.
//
// Reset: the active-low key on T6 and the PLL's LOCKED pass through the t27
// reset generator on the controller clock (two-stage synchronizer plus a
// 128-clock hold); its reset holds ddr3_top, the status reporter and the UART.
//
// LEDs: [0] heartbeat (controller clock), [1] o_calib_complete,
//       [2] PLL locked, [3] the controller returned to IDLE at least once since
//       reset (`recalibrated`: a wrong self-test read or a failed alignment step).
// UART (N15, 115200 8N1): see t27/rtl/fpga_ddr3_status.t27 for the H and S lines
// and t27/rtl/fpga_ddr3_pattern.t27 for the pattern test's lines. UBER_UART 1
// hands N15 to ddr3_top's debug UART instead (9600 8N1, UberDDR3's own text; only
// with UberDDR3 read under `define UART_DEBUG_BIST`, make DDR3_UART_DEBUG_BIST=1).
//
// Pattern test (PATTERN_TEST 1): after o_calib_complete, rounds of a write of
// every burst address below PATTERN_BURSTS with address-unique data, a read-back
// and compare, then the same with the complement (PATTERN_ROUNDS rounds, 0 =
// until reset); PATTERN_HOLD controller clocks between the last write and the
// first read of a pass. The test's line requests and the status reporter's share
// the emitter through the arbiter in the t27 module. PATTERN_TEST 0 leaves the
// user port idle as in the #60 builds (cyc 1, stb 0, sel all ones).
`timescale 1ns/1ps
`default_nettype none
module tms_ddr3_ax7203 #(
    parameter integer BYTE_LANES = 2,           // 2: x16 (chip U6), 4: x32
    parameter integer PLL_MULT = 5,             // VCO = 200 MHz * PLL_MULT (800..1866 MHz for -2)
    parameter integer DDR_DIV = 3,              // DDR3 clock = VCO / DDR_DIV
    parameter integer REPORT_PERIOD = 67108864, // controller clocks between status lines without a change
    parameter [31:0] BUILD_ID = 32'h0,
    parameter integer PATTERN_TEST = 0,         // 1: the pattern test drives the user Wishbone port
    parameter [31:0] PATTERN_BURSTS = 32'd33554432, // burst addresses tested, from 0 (2^25 = all: 512 MiB x16, 1 GiB x32)
    parameter [31:0] PATTERN_SEED = 32'h00000061,
    parameter [31:0] PATTERN_HOLD = 32'd0,      // controller clocks between a pass's last write ack and its first read
    parameter [31:0] PATTERN_ROUNDS = 32'd0,    // rounds (true + complement pass), 0 = until reset
    parameter [31:0] PATTERN_WATCHDOG = 32'd16777216, // clocks without progress after which the test stops
    parameter integer UART_DIV = 0,             // clocks per UART bit; 0 = 115200 baud from the controller clock
    parameter integer UBER_UART = 0             // 1: N15 carries ddr3_top's own debug UART (9600 baud) instead of
                                                // our H/S lines; read only when UART_DEBUG_BIST is defined
) (
    input  wire                      clk200_p,
    input  wire                      clk200_n,
    input  wire                      rst_n,
    output wire [3:0]                led,
    output wire                      uart_tx,
    output wire                      ddr3_ck_p,
    output wire                      ddr3_ck_n,
    output wire                      ddr3_reset_n,
    output wire                      ddr3_cke,
    output wire                      ddr3_cs_n,
    output wire                      ddr3_ras_n,
    output wire                      ddr3_cas_n,
    output wire                      ddr3_we_n,
    output wire                      ddr3_odt,
    output wire [14:0]               ddr3_addr,
    output wire [2:0]                ddr3_ba,
    inout  wire [8*BYTE_LANES-1:0]   ddr3_dq,
    inout  wire [BYTE_LANES-1:0]     ddr3_dqs_p,
    inout  wire [BYTE_LANES-1:0]     ddr3_dqs_n,
    output wire [BYTE_LANES-1:0]     ddr3_dm
);
    localparam integer VCO_PS = 5000 / PLL_MULT;           // 1000 ps at PLL_MULT 5
    localparam integer DDR3_PS = VCO_PS * DDR_DIV;         // 3000 ps
    localparam integer CTRL_PS = 4 * DDR3_PS;              // 12000 ps
    localparam integer CTRL_HZ = 200000000 / (4 * DDR_DIV) * PLL_MULT;
    localparam integer BAUD_DIV = UART_DIV != 0 ? UART_DIV : (CTRL_HZ + 57600) / 115200; // 723 at 83.33 MHz
    localparam integer WB_ADDR = 15 + 10 + 3 - 3;           // ROW + COL + BA - log2(8), burst addressed
    localparam integer WB_DATA = 64 * BYTE_LANES;           // 8 bits x lanes x 4:1 x 2 (DDR)

    // ---- clocks ----
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

    // ---- reset and heartbeat ----
    wire rst, heartbeat;
    TrinityFpgaResetT27 resetgen (
        .clk(clk_ctrl), .rst_n(1'b1), .en(1'b1), .ready(), .button_n(rst_n & pll_locked),
        .stage1(), .stage2(), .hold(), .blink(), .reset(rst), .heartbeat(heartbeat)
    );

    // ---- UberDDR3 ----
    wire        calib_complete;
    wire [31:0] debug1;
`ifdef UART_DEBUG_BIST
    // Only in the UART_DEBUG_BIST build: without the define these two nets are not
    // declared, so the netlist of the other builds keeps the net names of 7deeef16
    // (two more public wire names alone change nextpnr's placement of the same cells).
    wire        uber_uart_tx, our_uart_tx;
`endif
    // User Wishbone port: 25-bit burst address, 64 * BYTE_LANES data bits (beat k,
    // lane l at [8*(BYTE_LANES*k+l) +: 8]), one select bit per byte. The t27 test
    // has four 64-bit data words; words at or above BYTE_LANES are unused.
    wire         wb_cyc, wb_stb, wb_we, wb_stall, wb_ack;
    wire [31:0]  wb_addr, wb_sel;
    wire [63:0]  wb_wd0, wb_wd1, wb_wd2, wb_wd3;
    wire [255:0] wb_wdata = {wb_wd3, wb_wd2, wb_wd1, wb_wd0};
    wire [WB_DATA-1:0] wb_rdata;
    wire [255:0] wb_rdata_words = wb_rdata;   // zero-extended to four words
    ddr3_top #(
        .CONTROLLER_CLK_PERIOD(CTRL_PS),
        .DDR3_CLK_PERIOD(DDR3_PS),
        .ROW_BITS(15),
        .COL_BITS(10),
        .BA_BITS(3),
        .BYTE_LANES(BYTE_LANES),
        .AUX_WIDTH(4),
        .DUAL_RANK_DIMM(0),
        .SPEED_BIN(3),               // DDR3-1600 11-11-11 (-125)
        .SDRAM_CAPACITY(4),          // 4 Gb per chip (MT41J256M16); UberDDR3 then uses tRFC 300 ns (8 Gb: 350)
        .MICRON_SIM(0),
        .ODELAY_SUPPORTED(0),        // HR banks only: no ODELAYE2
        .SECOND_WISHBONE(0),
        .DLL_OFF(0),
        .WB_ERROR(0),
        .BIST_MODE(1),               // one self-test pass during calibration: one sweep of the burst counter
                                     // split over three tests, which reads back 3/4 of the bursts (rows
                                     // 8192-24575 of banks 0, 1, 6, 7 are never touched; tools/uberddr3-bist-model.py)
        .ECC_ENABLE(0),
        .DIC(2'b01),                 // output driver RZQ/7
        .RTT_NOM(3'b001),            // RZQ/4
        .SELF_REFRESH(2'b00)
    ) ddr3 (
        .i_controller_clk(clk_ctrl), .i_ddr3_clk(clk_ddr), .i_ref_clk(clk_ref), .i_ddr3_clk_90(clk_ddr_90),
        .i_rst_n(!rst),
        // The self-test runs inside calibration; afterwards the user port carries
        // the pattern test (PATTERN_TEST 1) or stays idle (PATTERN_TEST 0).
        .i_wb_cyc(wb_cyc), .i_wb_stb(wb_stb), .i_wb_we(wb_we),
        .i_wb_addr(wb_addr[WB_ADDR-1:0]), .i_wb_data(wb_wdata[WB_DATA-1:0]), .i_wb_sel(wb_sel[WB_DATA/8-1:0]), .i_aux(4'd0),
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
`ifdef UART_DEBUG_BIST
        .i_user_self_refresh(1'b0), .uart_tx(uber_uart_tx)
`else
        .i_user_self_refresh(1'b0), .uart_tx()
`endif
    );

    // ---- status line and pattern test: reporter, test, line emitter, UART transmitter ----
    wire        s_go, s_idle, line_go, line_idle, recalibrated, tx_start, tx_busy;
    wire [31:0] s_tag, s_a, line_tag, line_a, tx_byte;
    wire [63:0] s_b, line_b;
    // With the test, the test and the status reporter take their reset from `rst`
    // through the two-flip-flop stage of a second reset generator (its hold and
    // heartbeat are unused): the test's many flip-flops then hang off a register,
    // not off the comparator behind `rst`, and both leave reset on the same clock,
    // two clocks after the controller, so the test sees the status reporter's
    // first (H) line request. Without the test the reporter takes `rst` as in #60.
    wire report_rst_n, staged_rst_n;
    TrinityFpgaResetT27 report_reset (
        .clk(clk_ctrl), .rst_n(1'b1), .en(1'b1), .ready(), .button_n(!rst),
        .stage1(), .stage2(staged_rst_n), .hold(), .blink(), .reset(), .heartbeat()
    );
    assign report_rst_n = PATTERN_TEST != 0 ? staged_rst_n : !rst;
    TrinityFpgaDdr3StatusT27 status (
        .clk(clk_ctrl), .rst_n(report_rst_n), .en(1'b1), .ready(),
        .state(debug1), .calib(calib_complete), .line_idle(s_idle), .build_id(BUILD_ID),
        .lanes(BYTE_LANES), .period_ps(DDR3_PS), .period(REPORT_PERIOD),
        .line_go(s_go), .line_tag(s_tag), .line_a(s_a), .line_b(s_b), .recalibrated(recalibrated)
    );
    generate
        if (PATTERN_TEST != 0) begin : pattern
            TrinityFpgaDdr3PatternT27 test (
                .clk(clk_ctrl), .rst_n(report_rst_n), .en(1'b1), .ready(),
                .calib(calib_complete), .stall(wb_stall), .ack(wb_ack),
                .rdata0(wb_rdata_words[63:0]), .rdata1(wb_rdata_words[127:64]),
                .rdata2(wb_rdata_words[191:128]), .rdata3(wb_rdata_words[255:192]),
                .lanes(BYTE_LANES), .bursts(PATTERN_BURSTS), .seed(PATTERN_SEED), .hold(PATTERN_HOLD),
                .rounds(PATTERN_ROUNDS), .watchdog(PATTERN_WATCHDOG),
                .s_go(s_go), .s_tag(s_tag), .s_a(s_a), .s_b(s_b), .line_idle(line_idle),
                .wb_cyc(wb_cyc), .wb_stb(wb_stb), .wb_we(wb_we), .wb_addr(wb_addr), .wb_sel(wb_sel),
                .cur0(wb_wd0), .cur1(wb_wd1), .cur2(wb_wd2), .cur3(wb_wd3),
                .line_go(line_go), .line_tag(line_tag), .line_a(line_a), .line_b(line_b), .status_idle(s_idle)
            );
        end else begin : idle_port
            assign wb_cyc = 1'b1;
            assign wb_stb = 1'b0;
            assign wb_we = 1'b0;
            assign wb_addr = 32'd0;
            assign wb_sel = 32'hffffffff;
            assign {wb_wd3, wb_wd2, wb_wd1, wb_wd0} = 256'd0;
            assign line_go = s_go;
            assign line_tag = s_tag;
            assign line_a = s_a;
            assign line_b = s_b;
            assign s_idle = line_idle;
        end
    endgenerate
    TrinityFpgaLineEmitterT27 emitter (
        .clk(clk_ctrl), .rst_n(!rst), .en(1'b1), .ready(),
        .go(line_go), .tag(line_tag), .a(line_a), .b(line_b), .tx_busy(tx_busy),
        .line_busy(), .pos(), .tag_q(), .a_q(), .b_q(), .tx_start(tx_start), .tx_byte(tx_byte), .idle(line_idle)
    );
    TrinityFpgaUartTxT27 uart (
        .clk(clk_ctrl), .rst_n(!rst), .en(1'b1), .ready(),
        .start(tx_start), .data(tx_byte), .baud_div(BAUD_DIV),
`ifdef UART_DEBUG_BIST
        .busy(tx_busy), .shift(), .count(), .bits(), .tx(our_uart_tx)
    );
    // UBER_UART 1 (the UART_DEBUG_BIST build of #61, `make DDR3_UART_DEBUG_BIST=1`,
    // which reads every source with -DUART_DEBUG_BIST): UberDDR3's self-test text
    // at 9600 baud replaces our lines on N15; our reporter still runs but is not heard.
    assign uart_tx = UBER_UART != 0 ? uber_uart_tx : our_uart_tx;
`else
        .busy(tx_busy), .shift(), .count(), .bits(), .tx(uart_tx)
    );
`endif

    assign led = {recalibrated, pll_locked, calib_complete, heartbeat};
endmodule
`default_nettype wire
