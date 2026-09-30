// Issue #92. Variable latency/stall memory and report backpressure.
`timescale 1ns/1ps
module tb_ffn;
    parameter H=8, I=6, O=4;
    // Minimum memory latency and report-line busy time; the clock split test
    // varies them and expects identical compute clocks.
    parameter MEM_BASE=2, OUT_BASE=1;
    reg clk=0; always #5 clk=~clk;
    reg rst_n=0, ack=0;
    wire cyc, stb, go, s_idle;
    wire [31:0] addr,tag,a,state;
    wire [63:0] b;
    reg [127:0] data=0;
    reg [127:0] mem[0:1048575];
    integer tick=0, wait_left=0, output_wait=0, fd, finish_wait=0;
    reg [31:0] address=0, stalled_address=0;
    reg stalled_last=0;
    wire stall=(tick%7==0) || wait_left!=0;
    reg [4095:0] input_file, output_file;
    trinity_ffn_t27 dut(.clk(clk),.rst_n(rst_n),.calib(rst_n),.stall(stall),.ack(ack),
        .rdata_lo(data[63:0]),.rdata_hi(data[127:64]),.hidden(H),.inner(I),.output_rows(O),
        .line_idle(output_wait==0),.s_go(1'b0),.s_tag(32'd0),.s_a(32'd0),.s_b(64'd0),
        .wb_cyc(cyc),.wb_stb(stb),.wb_addr(addr),.line_go(go),.line_tag(tag),.line_a(a),.line_b(b),
        .s_idle(s_idle),.state(state));
    always @(posedge clk) begin
        tick<=tick+1; ack<=0;
        if (rst_n) begin
            if (stalled_last && (!stb || addr!==stalled_address)) $fatal(1,"withdrawn request");
            stalled_last<=stb && stall; stalled_address<=addr;
            if (wait_left>0) begin
                wait_left<=wait_left-1;
                if (wait_left==1) begin ack<=1; data<=mem[address]; end
            end
            if (cyc && stb && !stall) begin
                if (addr>=1048576 || ^mem[addr]===1'bx) $fatal(1,"uninitialized read %h",addr);
                address<=addr; wait_left<=MEM_BASE+(tick%3);
            end
            if (output_wait>0) output_wait<=output_wait-1;
            if (go) begin
                if (output_wait!=0) $fatal(1,"output while busy");
                output_wait<=OUT_BASE+(tick%5);
                $fdisplay(fd,"%c%08x%010x",tag[7:0],a,b[39:0]);
                if (tag==69) $fatal(1,"FFN error %d",a);
                if (tag==122) begin $display("PASS FFN clocks=%0d",tick); $fclose(fd); $finish; end
            end
            if(tick>300000000) $fatal(1,"timeout state=%d",state);
        end
    end
    initial begin
        if(!$value$plusargs("input=%s",input_file)) $fatal(1,"input missing");
        if(!$value$plusargs("output=%s",output_file)) $fatal(1,"output missing");
        $readmemh(input_file,mem); fd=$fopen(output_file,"w");
        repeat(3) @(negedge clk); rst_n=1;
    end
endmodule
