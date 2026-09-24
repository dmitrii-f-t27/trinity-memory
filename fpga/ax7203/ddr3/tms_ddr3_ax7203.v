// AX7203 DDR3 build (issue #60): UberDDR3's ddr3_top with its built-in
// self-test, our clocking, reset, LEDs and a calibration status line on the
// UART. Wiring only: the status logic is executable t27
// (t27/rtl/fpga_ddr3_status.t27, fpga_reset.t27, fpga_uart_tx.t27,
// fpga_line_emitter.t27), the controller and its PHY are UberDDR3 at the commit
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
// UART (N15, 115200 8N1): see t27/rtl/fpga_ddr3_status.t27 for the H and S lines.
`timescale 1ns/1ps
`default_nettype none
module tms_ddr3_ax7203 #(
    parameter integer BYTE_LANES = 2,           // 2: x16 (chip U6), 4: x32
    parameter integer PLL_MULT = 5,             // VCO = 200 MHz * PLL_MULT (800..1866 MHz for -2)
    parameter integer DDR_DIV = 3,              // DDR3 clock = VCO / DDR_DIV
    parameter integer REPORT_PERIOD = 67108864, // controller clocks between status lines without a change
    parameter [31:0] BUILD_ID = 32'h0
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
    localparam integer BAUD_DIV = (CTRL_HZ + 57600) / 115200; // 723 at 83.33 MHz
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
        // No user traffic in this build: the bus is idle and the self-test runs
        // inside calibration. The pattern test over Wishbone is issue #61.
        .i_wb_cyc(1'b1), .i_wb_stb(1'b0), .i_wb_we(1'b0),
        .i_wb_addr({WB_ADDR{1'b0}}), .i_wb_data({WB_DATA{1'b0}}), .i_wb_sel({(WB_DATA/8){1'b1}}), .i_aux(4'd0),
        .o_wb_stall(), .o_wb_ack(), .o_wb_err(), .o_wb_data(), .o_aux(),
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

    // ---- status line: reporter, line emitter, UART transmitter ----
    wire        line_go, line_idle, recalibrated, tx_start, tx_busy;
    wire [31:0] line_tag, line_a, tx_byte;
    wire [63:0] line_b;
    TrinityFpgaDdr3StatusT27 status (
        .clk(clk_ctrl), .rst_n(!rst), .en(1'b1), .ready(),
        .state(debug1), .calib(calib_complete), .line_idle(line_idle), .build_id(BUILD_ID),
        .lanes(BYTE_LANES), .period_ps(DDR3_PS), .period(REPORT_PERIOD),
        .line_go(line_go), .line_tag(line_tag), .line_a(line_a), .line_b(line_b), .recalibrated(recalibrated)
    );
    TrinityFpgaLineEmitterT27 emitter (
        .clk(clk_ctrl), .rst_n(!rst), .en(1'b1), .ready(),
        .go(line_go), .tag(line_tag), .a(line_a), .b(line_b), .tx_busy(tx_busy),
        .line_busy(), .pos(), .tag_q(), .a_q(), .b_q(), .tx_start(tx_start), .tx_byte(tx_byte), .idle(line_idle)
    );
    TrinityFpgaUartTxT27 uart (
        .clk(clk_ctrl), .rst_n(!rst), .en(1'b1), .ready(),
        .start(tx_start), .data(tx_byte), .baud_div(BAUD_DIV),
        .busy(tx_busy), .shift(), .count(), .bits(), .tx(uart_tx)
    );

    assign led = {recalibrated, pll_locked, calib_complete, heartbeat};
endmodule
`default_nettype wire
