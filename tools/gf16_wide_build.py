"""Generate the unchanged .t27 arithmetic and run its clocked RTL oracle replay."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
from tools import gf16_wide_reference as ref

ROOT = Path(__file__).resolve().parents[1]
COMPILER = Path(os.environ.get('T27_ROOT',str(ROOT/'build/compiler')))/'target/release/t27c'
SOURCE = ROOT/'t27/rtl/gf16_wide_norm.t27'


def run(args, **kwargs):
    r = subprocess.run(list(map(str,args)),text=True,capture_output=True,**kwargs)
    if r.returncode:
        raise RuntimeError(f'{args[0]}: {r.stdout}\n{r.stderr}')
    return r.stdout


def generate(work):
    work = Path(work); work.mkdir(parents=True,exist_ok=True)
    pin = (ROOT/'native/compiler.lock').read_text().strip()
    if run(['git','-C',COMPILER.parents[2],'rev-parse','HEAD']).strip()!=pin:
        raise ValueError('compiler pin mismatch')
    rtl=[]
    for name in ('gf16_wide_norm','ffn_wide'):
        spec=ROOT/f't27/rtl/{name}.t27'; target=work/(name+'.v')
        data=run([COMPILER,'gen-verilog',spec])
        if data!=run([COMPILER,'gen-verilog',spec]):raise ValueError('non-deterministic RTL')
        target.write_text(data);rtl.append(target)
    # The pinned C backend emits invalid scalar initializers for controller
    # RAM arrays. Translate the byte-identical pure-function prefix for C
    # conformance, and test the complete controller in RTL. No generated edit.
    prefix=SOURCE.read_text().split('var state:')[0]
    helper=work/'helpers.t27';helper.write_text(prefix)
    c=work/'helpers.c'; c.write_text(run([COMPILER,'gen-c',helper]))
    lib=work/'helpers.so'
    run(['cc','-std=c11','-O2','-Wno-parentheses-equality','-shared','-fPIC',c,'-o',lib])
    return rtl, ctypes.CDLL(str(lib.resolve()))


def simulate(work, rtl, rows, simulator='iverilog', controls=True):
    """Rows are (gate words, up words, norm weights), including full real rows."""
    work=Path(work);work.mkdir(parents=True,exist_ok=True)
    vectors=[];cases=[];offset=0
    for number,(gate,up,weight) in enumerate(rows):
        result=ref.row(gate,up,weight);n=len(gate)
        vectors.extend(''.join(f'{v:08x}' for v in values)+'\n' for values in
                       zip(gate,up,weight,result['product'],result['unit'],result['output']))
        root=result['root'];total=result['sum_squares']
        cases.append(f'''
        begin_row({n});
        for (j=0;j<{n};j=j+1) begin
            if (j%7==0) tick();
            gate_in=data[{offset}+j][191:160]; up_in=data[{offset}+j][159:128];
            weight_in=data[{offset}+j][127:96]; in_valid=1;
            // Observe readiness before the accepting edge, avoiding an NBA race.
            while (!en || !in_ready) tick();
            @(posedge clk);
            tick(); in_valid=0;
        end
        // A doorbell while busy must not restart a row.
        start=1; row_length=0; tick(); start=0;
        while (!done) tick();
        if (read_pos!={offset+n} || fault!==1'b{int(result['overflow']!=0)} ||
            overflow_count!==32'd{result['overflow']} ||
            error_code!==32'd{4 if result['overflow'] else 0}) $fatal(1,"row {number} completion");
        if (dut.kernel.block!==32'h{result['block_exponent'] & 0xffffffff:08x}) $fatal(1,"row {number} block");
        if ({{dut.kernel.root1,dut.kernel.root0}}!==128'h{root:x} ||
            {{dut.kernel.sum1,dut.kernel.sum0}}!==128'h{total:x})
            $fatal(1,"row {number} reduction got sum=%h root=%h",{{dut.kernel.sum1,dut.kernel.sum0}},{{dut.kernel.root1,dut.kernel.root0}});
        $display("PASS row {number} n={n} cycles=%d",cycles); $fflush();
        tick();
        ''')
        offset+=n
    memory=work/'vectors.mem';memory.write_text(''.join(vectors))
    extra=''
    if controls:
        extra='''
        begin_row(0); while(!done) tick(); if(!fault || error_code!=1) $fatal(1,"zero length"); tick();
        begin_row(6913); while(!done) tick(); if(!fault || error_code!=1) $fatal(1,"oversized length"); tick();
        begin_row(1); in_valid=1; gate_in=32'h7e00; up_in=32'h3e00; weight_in=32'h3e00;
        while(!done) tick(); in_valid=0; if(!fault || error_code!=2) $fatal(1,"infinity"); tick();
        begin_row(1); in_valid=1; gate_in=32'h13e00;
        while(!done) tick(); in_valid=0; if(!fault || error_code!=2) $fatal(1,"oversized word"); tick();
        begin_row(1); in_valid=1; gate_in=32'h3e00; up_in=32'h3e00; weight_in=32'h3e00;
        while(dut.kernel.state!=6) tick(); in_valid=0;
        force dut.math_fault=1; while(!done) tick(); release dut.math_fault;
        if(!fault || error_code!=3) $fatal(1,"math fault"); tick();
        begin_row(1); in_valid=1; gate_in=32'h3e00;
        while(dut.kernel.state!=6) tick(); in_valid=0;
        tick(); tick(); rst_n=0; tick(); tick();
        if(busy || done || out_valid || fault) $fatal(1,"mid-row reset");
        rst_n=1; tick();
        '''
    tb=work/'tb.v'
    tb.write_text(f'''`timescale 1ns/1ps
module tb;
reg clk=0; always #5 clk=~clk;
reg rst_n=0,en=1,start=0,in_valid=0,out_ready=0;
reg [31:0] row_length=0,gate_in=0,up_in=0,weight_in=0;
wire in_ready,out_valid,busy,done,fault;
wire [31:0] out_value,out_unit,out_product,index,error_code,overflow_count;
wire [63:0] cycles;
reg [191:0] data[0:{offset-1}];
reg stalled=0; reg [127:0] held;
integer clocks=0,read_pos=0,j;
gf16_wide_norm dut(.*);
always @(negedge clk) begin
    clocks=clocks+1; en=(clocks%17!=0); out_ready=(clocks%5!=0);
end
always @(posedge clk) begin
    if(rst_n) begin
        if(stalled && (!out_valid || {{index,out_product,out_unit,out_value}}!==held)) $fatal(1,"unstable stalled output");
        stalled=out_valid && (!out_ready || !en); held={{index,out_product,out_unit,out_value}};
        if(out_valid && out_ready && en) begin
            if(read_pos>={offset} || {{out_product,out_unit,out_value}}!==data[read_pos][95:0])
                $fatal(1,"lane %d got %h %h %h expected %h root=%h:%h D=%h:%h sum=%h:%h block=%d q=%h",read_pos,out_product,out_unit,out_value,data[read_pos][95:0],dut.kernel.root1,dut.kernel.root0,dut.kernel.d1,dut.kernel.d0,dut.kernel.sum1,dut.kernel.sum0,dut.kernel.block,dut.q0);
            read_pos=read_pos+1;
        end
    end else stalled=0;
end
task tick; begin @(negedge clk); #1; end endtask
task begin_row(input integer n); begin
    while(!en) tick(); row_length=n; start=1;
    @(posedge clk); while(!en) @(posedge clk);
    tick(); start=0;
end endtask
initial begin
    $readmemh("{memory.resolve()}",data);
    tick();tick();rst_n=1;tick();
    {''.join(cases)}
    {extra}
    $display("PASS GF16 wide rows={len(rows)} values={offset}");$finish;
end
initial begin #{max(100000,(offset*750+15000)*10)}; $fatal(1,"timeout state=%d index=%d",dut.kernel.state,index);end
endmodule
''')
    sources=[*rtl,ROOT/'rtl/t27/gf16_wide_norm.v',tb]
    if simulator=='verilator':
        run(['verilator','--binary','--timing','--top-module','tb','-Wno-fatal','-j','4',
             '--Mdir',work/'obj_dir',*sources],timeout=180)
        exe=work/'obj_dir/Vtb';command=[exe]
    else:
        exe=work/'sim.vvp';run(['iverilog','-g2012','-s','tb','-o',exe,*sources]);command=['vvp',exe]
    progress=work/'rtl-progress.log'
    with progress.open('w') as log:
        result=subprocess.run(list(map(str,command)),stdout=log,stderr=subprocess.STDOUT,timeout=900)
    output=progress.read_text()
    if result.returncode:raise RuntimeError('RTL failed: '+output[-5000:])
    if f'PASS GF16 wide rows={len(rows)} values={offset}' not in output:raise ValueError('simulation incomplete')
    return output


def synthesize(work, rtl, xilinx=False):
    work=Path(work);work.mkdir(parents=True,exist_ok=True)
    def quote(path):return '"'+str(path).replace('\\','\\\\').replace('"','\\"')+'"'
    prefix='xilinx' if xilinx else 'generic'
    mapped=work/(prefix+'.v');stats=work/(prefix+'.json')
    sources=list(rtl)+([ROOT/'rtl/t27/gf16_wide_norm.v'] if xilinx else [])
    # Required by the existing AX7203 flow too: retain clocked arrays as RAM.
    commands='read_verilog -nomem2reg '+' '.join(map(quote,sources))+'; '
    commands+=('synth_xilinx -family xc7 -top gf16_wide_norm -flatten -noiopad -noclkbuf; '
               if xilinx else 'proc; opt; memory -nomap; opt; techmap; opt; ')
    commands+='check -assert; write_verilog -noattr '+quote(mapped)+'; tee -o '+quote(stats)+' stat -json'
    log=run(['yosys','-Q','-T','-p',commands],timeout=180)
    (work/(prefix+'.log')).write_text(log)
    if 'Found and reported 0 problems' not in log:raise ValueError('synthesis check missing')
    statistics=json.loads(stats.read_text())
    for module in statistics['modules'].values():
        if module['num_processes'] or any('LATCH' in k.upper() for k in module['num_cells_by_type']):
            raise ValueError('unlowered process or inferred latch')
    return mapped, statistics


def main():
    import argparse
    ap=argparse.ArgumentParser(description='Generate and synthesize the standalone GF16 wide kernel.')
    ap.add_argument('--work',type=Path,default=ROOT/'build/gf16-wide/synthesis')
    ap.add_argument('--output',type=Path,default=ROOT/'reports/numeric/gf16-wide-synthesis.json')
    args=ap.parse_args();args.work=args.work.resolve()
    rtl,_=generate(args.work)
    _,generic=synthesize(args.work,rtl)
    _,xilinx=synthesize(args.work,rtl,xilinx=True)
    names=['t27/rtl/gf16_wide_norm.t27','t27/rtl/ffn_wide.t27','rtl/t27/gf16_wide_norm.v','tools/gf16_wide_build.py']
    result={'schema':'trinity.gf16-wide-synthesis.v1',
        'scope':'standalone out-of-context mapping; no placement, routing, timing or board measurement',
        'compiler':(ROOT/'native/compiler.lock').read_text().strip(),
        'yosys':run(['yosys','-V']).strip(),
        'source_sha256':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in names},
        'generic':generic,'xilinx_xc7':xilinx}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(args.output)


if __name__=='__main__':main()
