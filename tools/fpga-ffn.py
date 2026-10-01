"""Prepare cached full-layer FFN vectors, simulate generated RTL, or verify captures."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.environ.setdefault('TRINITY_FIXTURES_OFFLINE','1')
import ffn_vectors as fv


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    sub=ap.add_subparsers(dest='action',required=True)
    prep=sub.add_parser('prepare'); prep.add_argument('--output',type=Path,required=True)
    prep.add_argument('--seed',type=int,default=27); prep.add_argument('--zero',action='store_true')
    prep.add_argument('--run',type=int,default=1)
    prep.add_argument('--trace',choices=('full','result'),default='full',help='GF16 result mode buffers y until computation ends')
    prep.add_argument('--gf16-ffn',action='store_true',help='prepare the exact gf16-ffn-v1 profile')
    sim=sub.add_parser('simulate'); sim.add_argument('--vectors',type=Path,required=True)
    sim.add_argument('--output',type=Path,required=True)
    check=sub.add_parser('check'); check.add_argument('--vectors',type=Path,required=True)
    check.add_argument('--capture',type=Path,required=True); check.add_argument('--output',type=Path,required=True)
    args=ap.parse_args()
    if args.action=='prepare':
        if args.trace != 'full' and not args.gf16_ffn:
            ap.error('--trace=result requires --gf16-ffn')
        args.output.mkdir(parents=True,exist_ok=False)
        from trinity_memory.matvec import activations
        model=fv.fr.load_ffn(); x8=[0]*2560 if args.zero else list(activations(2560,args.seed))
        if args.gf16_ffn:
            from tools import gf16_ffn_reference as gfref, gf16_ffn_vectors as gfv, gf16_reference as codec
            for name in ('w_post','w_sub'):
                model[name]=[codec.encode(codec.f32_bits(v)) for v in model[name]]
            model['scales']={k:codec.encode(codec.f32_bits(v)) for k,v in model['scales'].items()}
            x=[codec.encode(codec.f32_bits(v)) for v in x8]
            gfv.write_inputs(args.output,model,x,args.run,args.trace)
            ref=gfref.evaluate(model,x)
            ref.update(run=args.run,trace=args.trace,seed=args.seed,zero=args.zero,shape=[2560,6912,2560],
                       packed_sha256=model['packed_sha256'])
            (args.output/'reference.json').write_text(json.dumps(ref,indent=1)+'\n')
            print('Prepared GF16',args.output);return 0
        x=[v<<16 for v in x8]
        fv.write_inputs(args.output,model,x,args.run)
        expected,sats=fv.fr.fpga_q16(model,x)
        errors=fv.fr.compare(fv.fr.reference_f64(model,x8),expected)
        ref={'run':args.run,'seed':args.seed,'zero':args.zero,'shape':[2560,6912,2560],
             'expected':expected,'saturations':sats,'f64_error':errors,'packed_sha256':model['packed_sha256']}
        (args.output/'reference.json').write_text(json.dumps(ref,indent=1)+'\n')
        print('Prepared',args.output); return 0
    ref=json.loads((args.vectors/'reference.json').read_text())
    if ref.get('profile')=='gf16-ffn-v1':
        from tools import gf16_ffn_vectors as gfv, gf16_ffn_build as gfb
        if args.action=='check':
            if args.output.exists():raise ValueError('preserve earlier report: output exists')
            result=gfv.validate(args.capture.read_bytes(),ref,ref.get('run',1))
        else:
            args.output.mkdir(parents=True,exist_ok=False);work=args.output.resolve()
            rtl,_=gfb.generate(work/'generated')
            shape=tuple(len(ref['stages'][s]) for s in ('h','g','y'))
            command=gfb.compile_sim(work,rtl,shape,'verilator')
            # Keep the run ID in the transport expectation, including run 2.
            raw=work/'capture.txt'
            log=gfb.base.run([*command,'+input='+str((args.vectors/'input.mem').resolve()),'+output='+str(raw)],timeout=1800)
            (work/'simulation.log').write_text(log)
            result=gfv.validate(raw.read_bytes(),ref,ref.get('run',1))
            args.output=work/'result.json'
        args.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result));return 0
    if args.action=='check':
        if args.output.exists(): raise ValueError('preserve earlier report: output exists')
        result=fv.validate(args.capture.read_bytes(),ref['expected'],ref['saturations'],ref['run'])
    else:
        args.output.mkdir(parents=True,exist_ok=False)
        work=args.output.resolve(); compiler=ROOT/'build/compiler/target/release/t27c'
        source_names=['t27/rtl/ffn_wide.t27','t27/rtl/fpga_ffn.t27','rtl/t27/ffn.v','tests/tb_ffn.v']
        sources={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in source_names}
        rtl=[]
        for name in ('ffn_wide','fpga_ffn'):
            p=work/(name+'.v'); rtl.append(p)
            with p.open('w') as out:
                subprocess.run([str(compiler),'gen-verilog',str(ROOT/'t27/rtl'/(name+'.t27'))],stdout=out,check=True)
        h,i,o=ref['shape']
        cmd=['verilator','--binary','--timing','--top-module','tb_ffn','-Wno-fatal','-j','4',
             f'-GH={h}',f'-GI={i}',f'-GO={o}','--Mdir',str(work/'obj_dir'),*map(str,rtl),
             str(ROOT/'rtl/t27/ffn.v'),str(ROOT/'tests/tb_ffn.v')]
        (work/'command.json').write_text(json.dumps(cmd,indent=2)+'\n')
        with (work/'build.log').open('w') as out:
            subprocess.run(cmd,stdout=out,stderr=subprocess.STDOUT,check=True)
        raw=work/'capture.txt'
        with (work/'simulation.log').open('w') as out:
            subprocess.run([str(work/'obj_dir/Vtb_ffn'),'+input='+str((args.vectors/'input.mem').resolve()),
                            '+output='+str(raw)],stdout=out,stderr=subprocess.STDOUT,check=True,timeout=300)
        result=fv.validate(raw.read_bytes(),ref['expected'],ref['saturations'],ref['run'])
        if any(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()!=h for p,h in sources.items()):
            raise RuntimeError('source changed during simulation')
        result.update(sources=sources,simulator=subprocess.check_output(['verilator','--version'],text=True).strip(),
                      input_sha256=hashlib.sha256((args.vectors/'input.mem').read_bytes()).hexdigest(),
                      f64_error=ref['f64_error'])
        args.output=work/'result.json'
    args.output.write_text(json.dumps(result,indent=2)+'\n'); print(json.dumps(result)); return 0


if __name__=='__main__':
    raise SystemExit(main())
