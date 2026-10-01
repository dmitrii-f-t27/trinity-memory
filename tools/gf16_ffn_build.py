"""Pinned compiler and actual clocked full GF16 FFN simulation."""
import ctypes
from pathlib import Path
from tools import gf16_wide_build as base
from tools import gf16_ffn_vectors as vectors

ROOT = base.ROOT


def generate(work):
    work=Path(work).resolve();rtl,_=base.generate(work)
    source=ROOT/'t27/rtl/gf16_ffn.t27'
    data=base.run([base.COMPILER,'gen-verilog',source])
    if data != base.run([base.COMPILER,'gen-verilog',source]):
        raise ValueError('nondeterministic GF16 FFN RTL')
    path=work/'gf16_ffn.v';path.write_text(data);rtl.append(path)
    helper=work/'ffn_helpers.t27';helper.write_text(source.read_text().split('var state:')[0])
    c=work/'ffn_helpers.c';c.write_text(base.run([base.COMPILER,'gen-c',helper]))
    lib=work/'ffn_helpers.so'
    base.run(['cc','-std=c11','-O2','-shared','-fPIC',c,'-o',lib])
    return rtl,ctypes.CDLL(str(lib))


def compile_sim(work, rtl, shape, simulator='iverilog', mem_base=2, out_base=1, test_params=None):
    work=Path(work).resolve();work.mkdir(parents=True,exist_ok=True)
    h,i,o=shape
    params={'H':h,'I':i,'O':o,'MEM_BASE':mem_base,'OUT_BASE':out_base}
    if test_params:params.update(test_params)
    sources=[*rtl,ROOT/'rtl/t27/gf16_wide_norm.v',ROOT/'rtl/t27/gf16_ffn.v',ROOT/'tests/tb_gf16_ffn.v']
    if simulator == 'verilator':
        base.run(['verilator','--binary','--timing','--top-module','tb_gf16_ffn','-Wno-fatal','-j','4',
                  '--Mdir',work/'obj_dir',*[f'-G{k}={v}' for k,v in params.items()],*sources],timeout=240)
        command=[work/'obj_dir/Vtb_gf16_ffn']
    else:
        exe=work/'sim.vvp'
        base.run(['iverilog','-g2012','-s','tb_gf16_ffn','-o',exe,
                  *[f'-Ptb_gf16_ffn.{k}={v}' for k,v in params.items()],*sources],timeout=120)
        command=['vvp',exe]
    return command


def run_sim(work, command, inp, expected):
    work=Path(work).resolve();work.mkdir(parents=True,exist_ok=True)
    capture=work/'capture.txt'
    log=base.run([*command,'+input='+str(inp),'+output='+str(capture)],timeout=1800)
    (work/'rtl.log').write_text(log)
    return vectors.validate(capture.read_bytes(),expected)


def simulate(work, rtl, model, x, expected, simulator='iverilog', mem_base=2, out_base=1):
    work=Path(work).resolve();work.mkdir(parents=True,exist_ok=True)
    inp=vectors.write_inputs(work,model,x)
    shape=(model['shapes']['gate'][1],model['shapes']['gate'][0],model['shapes']['down'][0])
    command=compile_sim(work,rtl,shape,simulator,mem_base,out_base)
    return run_sim(work,command,inp,expected)
