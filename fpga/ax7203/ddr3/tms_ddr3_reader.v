// DDR3 read path of the AX7203 (issue #62): wiring only. The reader, consumer (A),
// its decoders and encoders and the report arbiter are executable t27
// (t27/rtl/fpga_ddr3_reader.t27); this file splits the 128-bit Wishbone words into
// the u64 ports the pinned t27 compiler emits (at most 64 bits per port).
// Instantiated by tms_ddr3_ax7203.v when it is read with `define DDR3_READER
// (make -C fpga/ax7203 ... DDR3_APP=reader); x16 only.
//
// Wishbone byte order (checked in Icarus by tests/test_ddr3_reader.py against the memory
// model's stored 128-bit words and the host model's encodings):
//   Wishbone data, 128 bits: byte n (n = 0..15) at bits [8n+7:8n]; the region's byte k is
//     byte k % 16 of Wishbone word (burst address) k / 16.
//   rdata_lo = rdata[63:0] (bytes 0-7), rdata_hi = rdata[127:64] (bytes 8-15);
//     the write data is {c_hi, c_lo} likewise.
// Where the bytes then sit on the DDR3 bus is UberDDR3's: by its source at 79d8fd3e
// (ddr3_controller.v, stage2_data[(DQ_BITS*LANES)*beat + 8*lane +: 8]; ddr3_phy.v, the OSERDES
// D inputs) byte n goes on beat n / 2, byte lane n % 2. That is read from the source only:
// the Icarus memory model stores whole words (no beats or lanes), and a write-read round trip
// on the board cannot see a permutation applied the same way in both directions.
`timescale 1ns/1ps
`default_nettype none
module tms_ddr3_reader #(
    parameter [31:0] TRITS = 32'd17694720,   // logical trits per run (1 .. 2^31 - 1)
    parameter [31:0] SEED = 32'h00000062,
    parameter [31:0] CAP = 32'd64,           // requests outstanding at most (>= 1)
    parameter [31:0] RUNS = 32'd0,           // runs (baseline2, dense5, ...), 0 = until reset
    parameter [31:0] WATCHDOG = 32'd16777216,
    parameter [31:0] LANES_BUS = 32'd2       // byte lanes, reported on the q line
) (
    input  wire         clk,
    input  wire         rst_n,
    input  wire         calib,
    input  wire         stall,
    input  wire         ack,
    input  wire [127:0] rdata,
    input  wire         s_go,
    input  wire [31:0]  s_tag,
    input  wire [31:0]  s_a,
    input  wire [63:0]  s_b,
    input  wire         line_idle,
    output wire         wb_cyc,
    output wire         wb_stb,
    output wire         wb_we,
    output wire [31:0]  wb_addr,
    output wire [31:0]  wb_sel,
    output wire [127:0] wb_wdata,
    output wire         line_go,
    output wire [31:0]  line_tag,
    output wire [31:0]  line_a,
    output wire [63:0]  line_b,
    output wire         status_idle
);
    wire [63:0] c_lo, c_hi;
    TrinityFpgaDdr3ReaderT27 reader (
        .clk(clk), .rst_n(rst_n), .en(1'b1), .ready(),
        .calib(calib), .stall(stall), .ack(ack), .rdata_lo(rdata[63:0]), .rdata_hi(rdata[127:64]),
        .trits(TRITS), .seed(SEED), .cap(CAP), .runs(RUNS), .watchdog(WATCHDOG), .lanes_bus(LANES_BUS),
        .s_go(s_go), .s_tag(s_tag), .s_a(s_a), .s_b(s_b), .line_idle(line_idle),
        .c_lo(c_lo), .c_hi(c_hi),
        .wb_cyc(wb_cyc), .wb_stb(wb_stb), .wb_we(wb_we), .wb_addr(wb_addr), .wb_sel(wb_sel),
        .line_go(line_go), .line_tag(line_tag), .line_a(line_a), .line_b(line_b), .status_idle(status_idle)
    );
    assign wb_wdata = {c_hi, c_lo};
endmodule
`default_nettype wire
