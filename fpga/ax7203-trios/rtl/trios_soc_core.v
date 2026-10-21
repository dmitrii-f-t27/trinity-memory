// TriOS mesh SoC core for the AX7203: a PicoRV32 (RV32I, no extensions) with 32 KiB of
// block RAM (code, data, stack; initialised from the firmware image), a UART and four
// LEDs. Clock-agnostic: the board wrapper (trios_soc_ax7203.v) provides the 25 MHz
// clock, the testbench provides the same clock in simulation, so the simulated core and
// the synthesized core are the same RTL.
//
// Memory map (contract: fpga/ax7203-trios/docs/CONTRACT.md):
//   0x0000_0000 .. 0x0000_7FFF  RAM, reset vector 0x0000_0000
//   0x1000_0000  UART data   (write: transmit byte; read: pop received byte)
//   0x1000_0004  UART status: bit0 tx busy, bit1 rx queue not empty, bit2 rx overrun (sticky),
//                bit3 rx quiet, bit4 rx framing error (sticky). Write: bit2 clears the overrun flag,
//                bit4 clears the framing-error flag. A status read clears nothing.
//   0x1000_0008  LED[1:0] (write), read back
//   0x1000_000C  BUILD_ID (read)
`timescale 1ns/1ps
`default_nettype none
module trios_soc_core #(
    parameter integer BAUD_DIV = 217,        // clocks per UART bit (25 MHz / 115200)
    parameter integer QUIET_CLOCKS = 138880, // 64 character times of silence (BAUD_DIV * 10 * 64)
    parameter [31:0] BUILD_ID = 32'h0
) (
    input  wire       clk,
    output wire [3:0] led,
    output wire       uart_tx,
    input  wire       uart_rx
);
    // ---- power-on reset: 255 clocks after configuration ----
    reg [7:0] rstcnt = 8'd0;
    wire resetn = &rstcnt;
    always @(posedge clk) if (!resetn) rstcnt <= rstcnt + 8'd1;

    // ---- CPU ----
    wire        mem_valid, mem_instr, trap;
    reg         mem_ready;
    wire [31:0] mem_addr, mem_wdata;
    wire [3:0]  mem_wstrb;
    reg  [31:0] mem_rdata;   // combinational (always @*)

    picorv32 #(
        .ENABLE_COUNTERS(0), .ENABLE_COUNTERS64(0), .ENABLE_REGS_16_31(1),
        .ENABLE_REGS_DUALPORT(1), .LATCHED_MEM_RDATA(0), .TWO_STAGE_SHIFT(1),
        .BARREL_SHIFTER(1), .TWO_CYCLE_COMPARE(0), .TWO_CYCLE_ALU(0),
        .COMPRESSED_ISA(0), .CATCH_MISALIGN(1), .CATCH_ILLINSN(1),
        .ENABLE_PCPI(0), .ENABLE_MUL(0), .ENABLE_FAST_MUL(0), .ENABLE_DIV(0),
        .ENABLE_IRQ(0), .ENABLE_IRQ_QREGS(0), .ENABLE_IRQ_TIMER(0), .ENABLE_TRACE(0),
        .REGS_INIT_ZERO(0), .MASKED_IRQ(32'h0), .LATCHED_IRQ(32'hffffffff),
        .PROGADDR_RESET(32'h0000_0000), .PROGADDR_IRQ(32'h0000_0010),
        .STACKADDR(32'hffff_ffff)
    ) cpu (
        .clk(clk), .resetn(resetn), .trap(trap),
        .mem_valid(mem_valid), .mem_instr(mem_instr), .mem_ready(mem_ready),
        .mem_addr(mem_addr), .mem_wdata(mem_wdata), .mem_wstrb(mem_wstrb),
        .mem_rdata(mem_rdata),
        .mem_la_read(), .mem_la_write(), .mem_la_addr(), .mem_la_wdata(), .mem_la_wstrb(),
        .pcpi_valid(), .pcpi_insn(), .pcpi_rs1(), .pcpi_rs2(),
        .pcpi_wr(1'b0), .pcpi_rd(32'h0), .pcpi_wait(1'b0), .pcpi_ready(1'b0),
        .irq(32'h0), .eoi(), .trace_valid(), .trace_data()
    );

    // ---- bus: one wait state, requests held by the CPU until mem_ready ----
    wire sel_ram = (mem_addr[31:15] == 17'd0);
    wire sel_io  = (mem_addr[31:28] == 4'h1);
    wire access  = mem_valid && !mem_ready;
    wire ram_en  = access && sel_ram;
    wire [12:0] ram_a = mem_addr[14:2];

    reg [7:0] m0 [0:8191];
    reg [7:0] m1 [0:8191];
    reg [7:0] m2 [0:8191];
    reg [7:0] m3 [0:8191];
    reg [7:0] r0, r1, r2, r3;
`ifndef SYNTHESIS
    integer zi;
    initial for (zi = 0; zi < 8192; zi = zi + 1) begin m0[zi] = 8'h0; m1[zi] = 8'h0; m2[zi] = 8'h0; m3[zi] = 8'h0; end
`endif
`include "ram_init.vh"
    always @(posedge clk) if (ram_en) begin
        r0 <= m0[ram_a]; if (mem_wstrb[0]) m0[ram_a] <= mem_wdata[7:0];
    end
    always @(posedge clk) if (ram_en) begin
        r1 <= m1[ram_a]; if (mem_wstrb[1]) m1[ram_a] <= mem_wdata[15:8];
    end
    always @(posedge clk) if (ram_en) begin
        r2 <= m2[ram_a]; if (mem_wstrb[2]) m2[ram_a] <= mem_wdata[23:16];
    end
    always @(posedge clk) if (ram_en) begin
        r3 <= m3[ram_a]; if (mem_wstrb[3]) m3[ram_a] <= mem_wdata[31:24];
    end

    // ---- UART transmitter ----
    reg [9:0]  tx_shift = 10'h3ff;
    reg [3:0]  tx_bits  = 4'd0;
    reg [15:0] tx_div   = 16'd0;
    wire       tx_busy  = (tx_bits != 4'd0);
    assign uart_tx = tx_shift[0];
    wire io_wr_data   = access && sel_io && (mem_addr[3:2] == 2'd0) && (|mem_wstrb);
    wire io_rd_data   = access && sel_io && (mem_addr[3:2] == 2'd0) && !(|mem_wstrb);
    wire io_wr_status = access && sel_io && (mem_addr[3:2] == 2'd1) && mem_wstrb[0];
    always @(posedge clk) begin
        if (!resetn) begin
            tx_shift <= 10'h3ff; tx_bits <= 4'd0; tx_div <= 16'd0;
        end else if (tx_busy) begin
            if (tx_div == BAUD_DIV - 1) begin
                tx_div <= 16'd0;
                tx_shift <= {1'b1, tx_shift[9:1]};
                tx_bits <= tx_bits - 4'd1;
            end else tx_div <= tx_div + 16'd1;
        end else if (io_wr_data) begin
            tx_shift <= {1'b1, mem_wdata[7:0], 1'b0};
            tx_bits <= 4'd10;
            tx_div <= 16'd0;
        end
    end

    // ---- UART receiver (8N1, sampled at mid-bit) into a 16-byte queue ----
    // The queue bookkeeping (level, pointers, sticky overrun, quiet detection) is the
    // generated t27 module TriosUartGuardT27 (fpga/ax7203-trios/t27/trios_uart_guard.t27);
    // only the data slots are here. Nothing is overwritten: a byte offered to a full
    // queue is dropped and flagged until the CPU clears the flag.
    reg rx_s0 = 1'b1, rx_s1 = 1'b1;
    always @(posedge clk) begin rx_s0 <= uart_rx; rx_s1 <= rx_s0; end
    reg [3:0]  rx_state = 4'd0;      // 0 idle, 1 start, 2..9 data, 10 stop
    reg [15:0] rx_div = 16'd0;
    reg [7:0]  rx_shift = 8'd0;
    reg [7:0]  rx_byte = 8'd0;
    reg        rx_pulse = 1'b0;      // one clock: rx_byte is a good character
    reg        rx_ferr = 1'b0;       // sticky: a stop bit was low
    always @(posedge clk) begin
        rx_pulse <= 1'b0;
        if (!resetn) begin
            rx_state <= 4'd0; rx_div <= 16'd0; rx_ferr <= 1'b0;
        end else begin
            if (io_wr_status && mem_wdata[4]) rx_ferr <= 1'b0;
            case (rx_state)
            4'd0: if (!rx_s1) begin rx_state <= 4'd1; rx_div <= 16'd0; end
            4'd1: if (rx_div == (BAUD_DIV/2)) begin
                      rx_div <= 16'd0;
                      if (!rx_s1) rx_state <= 4'd2; else rx_state <= 4'd0;
                  end else rx_div <= rx_div + 16'd1;
            default: if (rx_div == BAUD_DIV - 1) begin
                      rx_div <= 16'd0;
                      if (rx_state == 4'd10) begin
                          rx_state <= 4'd0;
                          if (rx_s1) begin rx_byte <= rx_shift; rx_pulse <= 1'b1; end
                          else rx_ferr <= 1'b1;
                      end else begin
                          rx_shift <= {rx_s1, rx_shift[7:1]};
                          rx_state <= rx_state + 4'd1;
                      end
                  end else rx_div <= rx_div + 16'd1;
            endcase
        end
    end

    wire        g_empty, g_full, g_store, g_overrun, g_quiet, g_ready;
    wire [31:0] g_level, g_wr, g_rd, g_slot, g_dropped, g_idle;
    TriosUartGuardT27 guard (
        .clk(clk), .rst_n(resetn), .en(1'b1), .ready(g_ready),
        .push(rx_pulse), .pop(io_rd_data && !g_empty), .clear(io_wr_status && mem_wdata[2]),
        .active((rx_state != 4'd0) || !rx_s1), .quiet_clocks(QUIET_CLOCKS),
        .level(g_level), .wr(g_wr), .rd(g_rd), .slot(g_slot), .store(g_store),
        .overrun(g_overrun), .dropped(g_dropped), .idle(g_idle), .quiet(g_quiet),
        .empty(g_empty), .full(g_full)
    );
    reg [127:0] fifo = 128'd0;
    always @(posedge clk) if (g_store) fifo[8*g_slot[3:0] +: 8] <= rx_byte;
    wire [7:0] rx_head = fifo[8*g_rd[3:0] +: 8];

    // ---- LEDs ----
    reg [1:0] led_reg = 2'b00;
    reg [24:0] hb = 25'd0;
    reg trap_seen = 1'b0;
    always @(posedge clk) begin
        hb <= hb + 25'd1;
        if (trap) trap_seen <= 1'b1;
        if (access && sel_io && (mem_addr[3:2] == 2'd2) && mem_wstrb[0]) led_reg <= mem_wdata[1:0];
    end
    assign led = {hb[24], trap_seen, led_reg};

    // ---- read data and ready: RAM data is valid in the ready cycle, I/O data is registered ----
    reg        sel_ram_q = 1'b0;
    reg [31:0] io_q = 32'd0;
    always @(posedge clk) begin
        mem_ready <= 1'b0;
        if (resetn && access) begin
            mem_ready <= 1'b1;
            sel_ram_q <= sel_ram;
            case (mem_addr[3:2])
                2'd0: io_q <= {24'd0, rx_head};
                2'd1: io_q <= {27'd0, rx_ferr, g_quiet, g_overrun, !g_empty, tx_busy};
                2'd2: io_q <= {30'd0, led_reg};
                default: io_q <= BUILD_ID;
            endcase
        end
    end
    always @* mem_rdata = sel_ram_q ? {r3, r2, r1, r0} : io_q;
endmodule
`default_nettype wire
