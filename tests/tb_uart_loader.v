// Testbenches of the UART loader (issue #63), driven by tests/test_uart_loader.py.
//
// uart_host_model plays the host at the bit level from a script (+script=<file>)
// and records what the device sends (+capture=<file>). One command per line:
//   p <ps>          bit period of the bytes the host sends (the device's rate times
//                   a small mismatch, as a real oscillator would have)
//   d <ps>          bit period the host decodes the device's bytes with
//   g <ps>          idle time before every byte sent (default 0: back to back)
//   b <hex>         send one byte, 8N1
//   f <hex>         send one byte whose stop bit is low (framing error), then one bit high
//   w <ps>          stay idle
//   t <n> <ps>      wait until n bytes have been received in total, at most <ps>
//   l <ps>          hold the line low for <ps> (a glitch, a break, a port that opens)
//   r <ps>          hold the reset button (rst_n) low for <ps>
//   x <ps>          the same, for a reset that cuts off what the device is sending:
//                   before the button is released the received-byte count restarts
//                   at 0 and "X <ps>" goes to the capture (hold it for more than one
//                   character, so that a character cut short is complete by then)
//   h <n>           port faults of loader_stall_sink (tb_uart_loader_ports only), a bit
//                   mask: 1 holds wr_ready low, 2 holds wr_idle low (a write that never
//                   finishes), 4 holds rd_ready low; h 0 ends them
//   m <id>          write a marker with the current time to the capture
//   e               end
// Capture lines: "R <ps> <hex>" per byte received (time of its stop-bit sample),
// "M <ps> <id>" per marker, "T <ps> <n>" when a wait gave up with n bytes received,
// "X <ps>" per x command.
//
// tb_uart_loader runs the board top (fpga/ax7203/tms_uart_loader.v, 200 MHz
// differential clock, the t27 store); tb_uart_loader_ports runs the t27 receiver,
// loader, line emitter and transmitter on a 25 MHz clock with loader_stall_sink
// behind the write and read ports. SINK_MODE 0: a behavioural memory whose wr_ready
// and rd_ready are pseudo-random, whose read data comes back 1-16 clocks after the
// request and whose wr_idle stays low for up to 15 clocks after each write (the
// backpressure a DDR3 Wishbone master will put on the loader). SINK_MODE 1: a memory
// without latency, always ready, whose rd_valid and rd_data answer a request in the
// clock that accepts it (combinationally), the earliest the port contract allows.
`timescale 1ps/1ps
`default_nettype none

module uart_host_model (
    output reg       rx_line,
    input  wire      tx_line,
    output reg       button_n,
    output reg [2:0] hang
);
    reg [8*8:1]  cmd;
    reg [63:0]   arg, arg2;
    reg [63:0]   tx_bit, dec_bit, gap, deadline;
    integer      fd, cap, status, i;
    integer      rx_count;
    reg [1023:0] script_name, capture_name;
    reg [7:0]    rbyte;

    initial begin
        rx_line = 1'b1;
        button_n = 1'b1;
        hang = 3'd0;
        tx_bit = 64'd640000;
        dec_bit = 64'd640000;
        gap = 0;
        rx_count = 0;
        if (!$value$plusargs("script=%s", script_name)) begin $display("TB_FAIL no +script"); $finish; end
        if (!$value$plusargs("capture=%s", capture_name)) begin $display("TB_FAIL no +capture"); $finish; end
        fd = $fopen(script_name, "r");
        cap = $fopen(capture_name, "w");
        if (fd == 0 || cap == 0) begin $display("TB_FAIL cannot open script or capture"); $finish; end
    end

    task send_byte(input [7:0] value, input bad_stop);
        begin
            if (gap != 0) #(gap);
            rx_line = 1'b0;
            #(tx_bit);
            for (i = 0; i < 8; i = i + 1) begin
                rx_line = value[i];
                #(tx_bit);
            end
            rx_line = !bad_stop;
            #(tx_bit);
            if (bad_stop) begin
                rx_line = 1'b1;
                #(tx_bit);
            end
            rx_line = 1'b1;
        end
    endtask

    // Decoder of the device's transmitter: sample every bit in its middle.
    initial begin : decoder
        integer k;
        #1;
        forever begin
            @(negedge tx_line);
            #(dec_bit / 2);
            if (tx_line == 1'b0) begin
                for (k = 0; k < 8; k = k + 1) begin
                    #(dec_bit);
                    rbyte[k] = tx_line;
                end
                #(dec_bit);
                $fdisplay(cap, "R %0d %02x%s", $time, rbyte, tx_line ? "" : " ferr");
                rx_count = rx_count + 1;
            end
        end
    end

    initial begin : script
        #2;
        forever begin
            status = $fscanf(fd, "%s", cmd);
            if (status != 1) begin
                $display("TB_FAIL script ended without e");
                $fclose(cap);
                $finish;
            end
            case (cmd)
                "p": begin status = $fscanf(fd, "%d", tx_bit); end
                "d": begin status = $fscanf(fd, "%d", dec_bit); end
                "g": begin status = $fscanf(fd, "%d", gap); end
                "b": begin status = $fscanf(fd, "%h", arg); send_byte(arg[7:0], 1'b0); end
                "f": begin status = $fscanf(fd, "%h", arg); send_byte(arg[7:0], 1'b1); end
                "w": begin status = $fscanf(fd, "%d", arg); #(arg); end
                "t": begin
                    status = $fscanf(fd, "%d %d", arg, arg2);
                    deadline = $time + arg2;
                    while (rx_count < arg && $time < deadline) #(100000);
                    if (rx_count < arg) $fdisplay(cap, "T %0d %0d", $time, rx_count);
                end
                "l": begin status = $fscanf(fd, "%d", arg); rx_line = 1'b0; #(arg); rx_line = 1'b1; end
                "r": begin status = $fscanf(fd, "%d", arg); button_n = 1'b0; #(arg); button_n = 1'b1; end
                "x": begin
                    status = $fscanf(fd, "%d", arg);
                    button_n = 1'b0;
                    #(arg);
                    rx_count = 0;
                    $fdisplay(cap, "X %0d", $time);
                    button_n = 1'b1;
                end
                "m": begin status = $fscanf(fd, "%d", arg); $fdisplay(cap, "M %0d %0d", $time, arg); end
                "h": begin status = $fscanf(fd, "%d", arg); hang = arg[2:0]; end
                "e": begin
                    $fclose(cap);
                    $display("TB_PASS script done at %0d ps, %0d bytes received", $time, rx_count);
                    $finish;
                end
                default: begin $display("TB_FAIL unknown script command %s", cmd); $fclose(cap); $finish; end
            endcase
        end
    end
endmodule

module tb_uart_loader;
    parameter integer BAUD_DIV = 16;
    parameter integer TIMEOUT_CLOCKS = 3840;
    parameter integer PROBATION_CLOCKS = 48000;
    reg clk200 = 1'b0;
    always #2500 clk200 = ~clk200;
    wire rx_line, tx_line, button_n;
    wire [3:0] led;
    uart_host_model host (.rx_line(rx_line), .tx_line(tx_line), .button_n(button_n), .hang());
    tms_uart_loader_ax7203 #(
        .CLOCK_MODE(1), .BAUD_DIV(BAUD_DIV), .TIMEOUT_CLOCKS(TIMEOUT_CLOCKS),
        .PROBATION_CLOCKS(PROBATION_CLOCKS), .BUILD_ID(32'h5eed1063)
    ) dut (
        .clk200_p(clk200), .clk200_n(~clk200), .rst_n(button_n), .led(led),
        .uart_tx(tx_line), .uart_rx(rx_line)
    );
endmodule

module loader_stall_sink #(
    parameter integer MODE = 0
) (
    input  wire        clk,
    input  wire [2:0]  hang,
    input  wire        wr_valid,
    input  wire [31:0] wr_addr,
    input  wire [31:0] wr_data,
    input  wire        wr_last,
    input  wire        rd_req,
    input  wire [31:0] rd_addr,
    output wire        wr_ready,
    output wire        wr_idle,
    output wire        rd_ready,
    output wire        rd_valid,
    output wire [31:0] rd_data,
    output reg  [31:0] writes,
    output reg  [31:0] stalls
);
    reg [7:0]  mem [0:262143];
    reg [31:0] lfsr = 32'h1063cafe;
    reg [4:0]  pending = 0;
    reg [4:0]  latency = 0;
    reg        rd_busy = 1'b0;
    reg [31:0] rd_hold = 0;
    reg        wr_ready_q = 1'b0, rd_ready_q = 1'b0, rd_valid_q = 1'b0;
    reg [31:0] rd_data_q = 0;
    wire       wr_ready_raw = MODE == 1 ? 1'b1 : wr_ready_q;
    wire       rd_ready_raw = MODE == 1 ? 1'b1 : rd_ready_q;
    assign wr_ready = wr_ready_raw && !hang[0];
    assign rd_ready = rd_ready_raw && !hang[2];
    assign wr_idle  = pending == 0;
    assign rd_valid = MODE == 1 ? (rd_req && rd_ready) : rd_valid_q;
    assign rd_data  = MODE == 1 ? {24'd0, mem[rd_addr[17:0]]} : rd_data_q;
    initial begin
        writes = 0; stalls = 0;
    end
    always @(posedge clk) begin
        lfsr <= {lfsr[30:0], lfsr[31] ^ lfsr[21] ^ lfsr[1] ^ lfsr[0]};
        wr_ready_q <= lfsr[3] & lfsr[7];
        rd_ready_q <= lfsr[5];
        rd_valid_q <= 1'b0;
        if (wr_valid && !wr_ready) stalls <= stalls + 1;
        if (wr_valid && wr_ready) begin
            mem[wr_addr[17:0]] <= wr_data[7:0];
            writes <= writes + 1;
            // MODE 1: the write is done at this edge. hang[1]: it never finishes.
            pending <= (MODE == 1 && !hang[1]) ? 5'd0 : {1'b0, lfsr[11:8]} | {4'd0, hang[1]};
        end else if (pending != 0 && !hang[1]) begin
            pending <= pending - 1;
        end
        if (MODE == 0) begin
            if (rd_req && rd_ready && !rd_busy) begin
                rd_busy <= 1'b1;
                latency <= {1'b0, lfsr[15:12]};
                rd_hold <= rd_addr;
            end else if (rd_busy) begin
                if (latency == 0) begin
                    rd_valid_q <= 1'b1;
                    rd_data_q <= {24'd0, mem[rd_hold[17:0]]};
                    rd_busy <= 1'b0;
                end else begin
                    latency <= latency - 1;
                end
            end
        end
    end
endmodule

module tb_uart_loader_ports;
    parameter integer BAUD_DIV = 16;
    parameter integer TIMEOUT_CLOCKS = 3840;
    parameter integer PROBATION_CLOCKS = 48000;
    parameter integer SINK_MODE = 0;
    reg clk = 1'b0;
    always #20000 clk = ~clk;
    wire rx_line, tx_line, button_n;
    wire [2:0] hang;
    uart_host_model host (.rx_line(rx_line), .tx_line(tx_line), .button_n(button_n), .hang(hang));

    wire rst;
    TrinityFpgaResetT27 resetgen (
        .clk(clk), .rst_n(1'b1), .en(1'b1), .ready(), .button_n(button_n),
        .stage1(), .stage2(), .hold(), .blink(), .reset(rst), .heartbeat()
    );
    wire [31:0] div_now, rx_data, rx_bytes, rx_ferrs, rx_false;
    wire        rx_valid, rx_ferr;
    TrinityFpgaUartRxT27 receiver (
        .clk(clk), .rst_n(!rst), .en(1'b1), .ready(),
        .uart_rx(rx_line), .baud_div(div_now),
        .valid(rx_valid), .data(rx_data), .ferr(rx_ferr),
        .bytes(rx_bytes), .framing_errors(rx_ferrs), .false_starts(rx_false)
    );
    wire        em_tx_start, ld_tx_start, tx_busy, line_idle, line_go;
    wire [31:0] em_tx_byte, ld_tx_byte, line_tag, line_a;
    wire [63:0] line_b;
    TrinityFpgaUartTxT27 transmitter (
        .clk(clk), .rst_n(!rst), .en(1'b1), .ready(),
        .start(em_tx_start | ld_tx_start), .data(ld_tx_start ? ld_tx_byte : em_tx_byte), .baud_div(div_now),
        .busy(tx_busy), .shift(), .count(), .bits(), .tx(tx_line)
    );
    TrinityFpgaLineEmitterT27 emitter (
        .clk(clk), .rst_n(!rst), .en(1'b1), .ready(),
        .go(line_go), .tag(line_tag), .a(line_a), .b(line_b), .tx_busy(tx_busy),
        .line_busy(), .pos(), .tag_q(), .a_q(), .b_q(), .tx_start(em_tx_start), .tx_byte(em_tx_byte), .idle(line_idle)
    );
    wire        wr_valid, wr_last, wr_ready, wr_idle, rd_req, rd_ready, rd_valid;
    wire [31:0] wr_addr, wr_data, rd_addr, rd_data, sink_writes, sink_stalls;
    loader_stall_sink #(.MODE(SINK_MODE)) sink (
        .clk(clk), .hang(hang), .wr_valid(wr_valid), .wr_addr(wr_addr), .wr_data(wr_data), .wr_last(wr_last),
        .rd_req(rd_req), .rd_addr(rd_addr), .wr_ready(wr_ready), .wr_idle(wr_idle), .rd_ready(rd_ready),
        .rd_valid(rd_valid), .rd_data(rd_data), .writes(sink_writes), .stalls(sink_stalls)
    );
    TrinityFpgaUartLoaderT27 loader (
        .clk(clk), .rst_n(!rst), .en(1'b1), .ready(),
        .rx_valid(rx_valid), .rx_data(rx_data), .rx_ferr(rx_ferr),
        .rx_bytes(rx_bytes), .rx_ferrs(rx_ferrs), .rx_false(rx_false),
        .line_idle(line_idle), .tx_busy(tx_busy), .wr_ready(wr_ready), .wr_idle(wr_idle),
        .rd_ready(rd_ready), .rd_valid(rd_valid), .rd_data(rd_data),
        .store_log2(32'd18), .default_div(BAUD_DIV), .timeout_clocks(TIMEOUT_CLOCKS),
        .probation_clocks(PROBATION_CLOCKS), .build_id(32'h5eed1064),
        .line_go(line_go), .line_tag(line_tag), .line_a(line_a), .line_b(line_b),
        .tx_start(ld_tx_start), .tx_byte(ld_tx_byte),
        .wr_valid(wr_valid), .wr_addr(wr_addr), .wr_data(wr_data), .wr_last(wr_last),
        .rd_req(rd_req), .rd_addr(rd_addr), .div_now(div_now)
    );
    // The last byte of every commit carries wr_last; report the sink's view at the end.
    reg [31:0] lasts = 0;
    always @(posedge clk) if (wr_valid && wr_ready && wr_last) lasts <= lasts + 1;
    final $display("SINK writes=%0d stalls=%0d lasts=%0d", sink_writes, sink_stalls, lasts);
endmodule
`default_nettype wire
