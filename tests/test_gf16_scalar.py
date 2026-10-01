"""Clocked scalar datapath: exact rounding, gradual underflow and handshake."""
from pathlib import Path
import random
import tempfile
import unittest
from tools import gf16_ffn_reference as ffn, gf16_wide_reference as ref
from tools import gf16_wide_build as build


class Scalar(unittest.TestCase):
    def test_clocked_arithmetic(self):
        if not build.COMPILER.is_file():self.skipTest('pinned compiler required in native CI')
        with tempfile.TemporaryDirectory(prefix='gf16-scalar-') as tmp:
            work=Path(tmp);rtl=work/'scalar.v'
            rtl.write_text(build.run([build.COMPILER,'gen-verilog',build.ROOT/'t27/rtl/gf16_scalar.t27']))
            rows=[];mask=(1<<64)-1
            def add(op,x=0,y=0,r=0,d=1,neg=False,aux=0,expected=0,fault=0):
                values=[op,x,y,r&mask,r>>64,d&mask,d>>64,int(neg),aux&0xffffffff,expected,fault]
                rows.append(''.join(f'{v:016x}' for v in values))
            # Every finite word, including both zero signs, through rational packing.
            for word in range(65536):
                if not ref.finite(word):continue
                n=abs(ffn.fixed(word));add(3,n&mask,n>>64,neg=bool(word&0x8000),expected=word)
            rng=random.Random(111)
            edges=[0,0x8000,1,0x8001,511,512,513,0x3e00,0x3dff,0x7dff,0xfdff]
            pairs=[(a,b) for a in edges for b in edges]
            pairs += [(rng.randrange(0x7e00)|rng.choice([0,0x8000]),rng.randrange(0x7e00)|rng.choice([0,0x8000])) for _ in range(10000)]
            for a,b in pairs:
                add(1,a,b,expected=ref.product(a,b));add(2,a,b,expected=ref.multiply(a,b))
                p=ref.product(a,b);block=max(((p>>23)&255)-167,-54)
                expected=ref.scale_integer((p&0x7fffff)|(0x800000 if p&0x7fffffff else 0),((p>>23)&255)-150-block)
                add(4,p,aux=block,expected=expected)
            # Exact/half/sticky boundaries, including integer ties with zero remainder.
            for q in [0,1,511,512,513,1023,1024,1025,1027,(1<<71)-(1<<60),1<<84]:
                for r in [0,1,2,3]:add(3,q&mask,q>>64,r,4,expected=ref.encode_ratio(q*4+r,4<<39))
            for _ in range(10000):
                q=rng.getrandbits(rng.randrange(129));d=rng.getrandbits(126) or 1;r=rng.randrange(d)
                add(3,q&mask,q>>64,r,d,expected=ref.encode_ratio(q*d+r,d<<39))
            for op in (1,2):
                for a in [0x7e00,0xffff,65536]:add(op,a,0x3e00,fault=1)
            add(3,d=0,fault=1);add(3,r=1,d=1,fault=1);add(3,d=1<<127,fault=1)
            add(4,0x7f800000,fault=1);add(4,1,fault=1);add(4,aux=-55,fault=1)
            memory=work/'vectors.mem';memory.write_text('\n'.join(rows)+'\n')
            tb=work/'tb.v'
            tb.write_text(f'''`timescale 1ns/1ps
module tb;
reg clk=0;always #5 clk=~clk;
reg rst_n=0,en=1,start=0,negative=0;
reg [31:0] mode=0;reg signed [31:0] aux=0;
reg [63:0] x=0,y=0,r0=0,r1=0,d0=1,d1=0;
wire busy,done,fault;wire [63:0] value;
reg [703:0] data[0:{len(rows)-1}];
reg [63:0] expected;reg expected_fault;integer i,clocks=0,latency;
TrinityGf16ScalarT27 dut(.clk(clk),.rst_n(rst_n),.en(en),.start(start),.mode(mode),.x(x),.y(y),.r0(r0),.r1(r1),.d0(d0),.d1(d1),.negative(negative),.aux(aux),.busy(busy),.done(done),.fault(fault),.value(value));
task tick;begin @(negedge clk);#1;end endtask
always @(negedge clk) begin clocks=clocks+1;en=(clocks%13!=0);end
initial begin
$readmemh("{memory}",data);tick();tick();rst_n=1;tick();
for(i=0;i<{len(rows)};i=i+1) begin
while(!en)tick();
mode=data[i][703:640];x=data[i][639:576];y=data[i][575:512];
r0=data[i][511:448];r1=data[i][447:384];d0=data[i][383:320];d1=data[i][319:256];
negative=data[i][255:192];aux=data[i][191:128];expected=data[i][127:64];expected_fault=data[i][63:0];
start=1;tick();start=0;latency=0;
while(!done)begin tick();latency=latency+1;if(latency>300)$fatal(1,"timeout i=%d state=%d",i,dut.state);end
if(fault!==expected_fault || (!fault && value!==expected))
$fatal(1,"i=%d op=%d x=%h y=%h r=%h:%h d=%h:%h got=%h expected=%h fault=%b",i,mode,x,y,r1,r0,d1,d0,value,expected,fault);
while(!en)tick();tick();
end
// Interrupt an in-flight multiply and verify the reset interface.
mode=1;x=16'h7dff;y=16'h7dff;start=1;while(!en)tick();tick();start=0;tick();rst_n=0;tick();
if(busy || done || fault || value) $fatal(1,"reset");
$display("PASS scalar cases={len(rows)}");$finish;
end
endmodule
''')
            build.run(['verilator','--binary','--timing','--top-module','tb','-Wno-fatal','-j','4','--Mdir',work/'obj_dir',rtl,tb],timeout=180)
            output=build.run([work/'obj_dir/Vtb'],timeout=180)
            self.assertIn(f'PASS scalar cases={len(rows)}',output);print(output.strip())


if __name__=='__main__':unittest.main()
