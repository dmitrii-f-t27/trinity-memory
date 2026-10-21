// Testbench of the TriOS mesh SoC core: plays the vectors of host/mesh_vectors.py over
// the UART (8N1, the same BAUD_DIV as the board), one request at a time, and writes every
// byte the core transmits to build/trios/resp.hex. The comparison is done by
// host/mesh_run.py sim-compare. Run from the repository root.
`timescale 1ns/1ps
module tb_trios_soc;
    `include "nsteps.vh"
    parameter integer BAUD_DIV = 217;
    localparam integer CLK_NS = 40;                 // 25 MHz
    localparam integer BIT_NS = BAUD_DIV * CLK_NS;

    reg clk = 1'b0;
    always #(CLK_NS/2) clk = ~clk;
    reg  rx = 1'b1;
    wire tx;
    wire [3:0] led;

    trios_soc_core #(.BAUD_DIV(BAUD_DIV), .BUILD_ID(32'h5EED0001)) dut (
        .clk(clk), .led(led), .uart_tx(tx), .uart_rx(rx));

    reg [7:0] stepmem [0:2*NSTEPS-1];
    reg [7:0] reqmem  [0:NREQ-1];
    initial begin
        $readmemh("build/trios/steps.mem", stepmem);
        $readmemh("build/trios/req.mem", reqmem);
    end

    integer fd;
    integer ncap = 0;
    reg [7:0] cap;
    integer k;
    initial fd = $fopen("build/trios/resp.hex", "w");
    always begin : txmodel
        @(negedge tx);
        #(BIT_NS/2);
        if (tx === 1'b0) begin
            for (k = 0; k < 8; k = k + 1) begin
                #(BIT_NS);
                cap[k] = tx;
            end
            #(BIT_NS);                               // stop bit
            $fwrite(fd, "%02x\n", cap);
            ncap = ncap + 1;
        end
    end

    task send_byte(input [7:0] b);
        integer n;
        begin
            rx = 1'b0; #(BIT_NS);
            for (n = 0; n < 8; n = n + 1) begin rx = b[n]; #(BIT_NS); end
            rx = 1'b1; #(BIT_NS);
        end
    endtask

    always @(posedge clk) if (dut.trap) begin
        $display("TB_TRAP at %0t", $time);
        $fclose(fd);
        $finish;
    end

    integer i, j, rlen, plen, cum, ridx, waited;
    initial begin
        cum = 0; ridx = 0;
        #2000;
        for (i = 0; i < NSTEPS; i = i + 1) begin
            rlen = stepmem[2*i];
            plen = stepmem[2*i+1];
            for (j = 0; j < rlen; j = j + 1) begin
                send_byte(reqmem[ridx]);
                ridx = ridx + 1;
            end
            cum = cum + plen;
            waited = 0;
            while (ncap < cum && waited < 6000000) begin
                @(posedge clk);
                waited = waited + 1;
            end
            if (ncap < cum) begin
                $display("TB_TIMEOUT step %0d captured %0d need %0d", i, ncap, cum);
                $fclose(fd);
                $finish;
            end
        end
        #(BIT_NS * 20);
        $display("TB_GUARD dropped=%0d overrun_flag=%0d", dut.guard.dropped, dut.guard.overrun);
        $display("TB_DONE %0d response bytes in %0t", ncap, $time);
        $fclose(fd);
        $finish;
    end
endmodule
