// Issue #115. Variable latency/stall memory and report backpressure for the
// gf16-attn-v1 controller, S=1 (position 0).
`timescale 1ns/1ps
module tb_gf16_attn;
    parameter H=8, KV=4, HEADS=2, HD=4, KVH=1;
    parameter MEM_BASE=2, OUT_BASE=1;
    parameter RESET_AT=0, CALIB_LOSS_AT=0, SPURIOUS_ACK_AT=0, EXPECT_ERROR=0, REPEATS=1;
    reg clk=0; always #5 clk=~clk;
    reg rst_n=0, ack=0;
    wire cyc, stb, go, s_idle;
    wire [31:0] addr, tag, a, state, index, row, col;
    wire [63:0] b;
    wire active, pending;
    reg [127:0] data=0;
    reg [127:0] mem[0:1048575];
    integer tick=0, wait_left=0, output_wait=0, fd, completed=0, j;
    reg [63:0] observed_total=0, observed_report=0, observed_memory=0;
    reg was_active=0;
    integer p;
    reg [31:0] address=0, stalled_address=0;
    reg stalled_last=0;
    wire stall=(tick%7==0) || wait_left!=0;
    reg [4095:0] input_file, output_file;
    wire calib=rst_n && (CALIB_LOSS_AT==0 || tick<CALIB_LOSS_AT);
    TrinityGf16AttnT27 dut(.clk(clk),.rst_n(rst_n),.en(1'b1),.calib(calib),.stall(stall),.ack(ack),
        .rdata_lo(data[63:0]),.rdata_hi(data[127:64]),.hidden(H),.kv(KV),.heads(HEADS),
        .kv_heads(KVH),.head_dim(HD),
        .line_idle(output_wait==0),.s_go(1'b0),.s_tag(32'd0),.s_a(32'd0),.s_b(64'd0),
        .wb_cyc(cyc),.wb_stb(stb),.wb_addr(addr),.line_go(go),.line_tag(tag),.line_a(a),.line_b(b),
        .s_idle(s_idle),.state(state),.index(index),.row(row),.col(col),.active(active),.pending(pending));
    always @(posedge clk) begin
        tick<=tick+1; ack<=0;
        if (rst_n) begin
            if(dut.active && (index>=8192 || row>=8192 || col>=8192))
                $fatal(1,"live attention RAM index exceeds physical address width");
            was_active<=dut.active;
            if (dut.active) begin
                if (!was_active) begin
                    observed_total=0;observed_report=0;observed_memory=0;
                end
                observed_total=observed_total+1;
                if(state==8 || state==300 || state==301) observed_report=observed_report+1;
                if(state==200 || state==201) observed_memory=observed_memory+1;
            end
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
                if(tag==99 && b!==observed_total) $fatal(1,"total counter mismatch");
                if(tag==118 && a==0 && b!==observed_report) $fatal(1,"report counter mismatch");
                if(tag==118 && a==1 && b!==observed_memory) $fatal(1,"memory counter mismatch");
                if (tag==69) begin
                    if(EXPECT_ERROR!=0 && a==EXPECT_ERROR) begin
                        $display("PASS expected attention error %0d",a);$fclose(fd);$finish;
                    end
                    $fatal(1,"attention error %d",a);
                end
                if (tag==122) begin
                    if(EXPECT_ERROR!=0) $fatal(1,"expected error missing");
                    completed=completed+1;
                    if(completed==REPEATS) begin $display("PASS attention clocks=%0d",tick); $fclose(fd); $finish; end
                    // A second zero vector exercises state/RAM reuse without reset.
                    for(j=0;j<H;j=j+1) mem[4096+j]=0;
                    mem[64][31:0]=completed+1;
                end
            end
            if(tick>600000000) $fatal(1,"timeout state=%d",state);
        end else begin
            wait_left<=0;ack<=0;output_wait<=0;stalled_last<=0;was_active<=0;
        end
        if(SPURIOUS_ACK_AT!=0 && tick==SPURIOUS_ACK_AT) ack<=1;
        if(RESET_AT!=0 && tick==RESET_AT) rst_n<=0;
        if(RESET_AT!=0 && tick==RESET_AT+3) begin
            $fclose(fd);fd=$fopen(output_file,"w");rst_n<=1;
        end
    end
    initial begin
        if(!$value$plusargs("input=%s",input_file)) $fatal(1,"input missing");
        if(!$value$plusargs("output=%s",output_file)) $fatal(1,"output missing");
        $readmemh(input_file,mem); fd=$fopen(output_file,"w");
        repeat(3) @(negedge clk); rst_n=1;
    end
endmodule
