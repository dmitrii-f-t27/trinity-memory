#!/usr/bin/env python3
"""Actual Icarus checks for source-specialized native storage capacities."""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]

def load_generator(path: Path):
    spec=importlib.util.spec_from_file_location('native_storage_generator',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

def execute(command:list[str],cwd:Path)->str:
    p=subprocess.run(command,cwd=cwd,text=True,capture_output=True,timeout=180)
    if p.returncode:raise RuntimeError(f'{command}: exit{p.returncode}\n{p.stdout}\n{p.stderr}')
    return p.stdout

def checks(compiler:Path,root:Path=ROOT,generator_path:Path|None=None)->dict:
    module=load_generator(generator_path or root/'tools/generate-t27-storage.py')
    cases=[(1,1,[1,2,3,4,5]),(65,321,[1,319,320,321,322,323,324,325]),
           (820,4096,[321,4092,4093,4094,4095,4096,4100]),
           (4096,20480,[4096,20479,20480])]
    reports=[]
    with tempfile.TemporaryDirectory(prefix='trinity-storage-specialized-') as temporary:
        work=Path(temporary)
        for capacity,default,counts in cases:
            out=work/f'capacity-{capacity}'
            manifest=module.generate(capacity,default,compiler,out,root)
            checker=(root/'rtl/tb/tb_streams.sv').read_text()
            checker=checker[:checker.index('module tb_streams;')]
            for mode in ['dense5','baseline5']:
                checker=checker.replace(f'ternary_{mode}_stream #(',f'ternary_{mode}_stream_t27 #(')
            top=['module tb_streams;',f'wire [{len(counts)-1}:0] done;']
            top += [f'stream_checker #(.TRIT_COUNT({count})) check_{count} (.done(done[{i}]));' for i,count in enumerate(counts)]
            top += ['ternary_dense5_stream_t27 defaults_dense ();','ternary_baseline5_stream_t27 defaults_baseline ();',
                f'initial if(defaults_dense.TRIT_COUNT != {default} || defaults_baseline.TRIT_COUNT != {default}) $fatal(1,"specialized defaults mismatch");',
                'initial begin wait (&done); $display("PASS specialized native storage"); $finish; end',
                f'initial begin #{capacity*1000+100000}; $fatal(1,"timeout"); end','endmodule','`default_nettype wire','']
            testbench=out/'tb.sv';testbench.write_text(checker+'\n'.join(top))
            executable=out/'storage.vvp'
            sources=[str(out/name) for name in ['stream_storage.v','stream_view.v','streams.v']]
            execute(['iverilog','-g2012','-s','tb_streams','-o',str(executable),*sources,str(testbench)],out)
            output=execute(['vvp',str(executable)],out)
            if output.count('PASS stream:')!=len(counts) or 'PASS specialized native storage' not in output:
                raise RuntimeError('Simulator did not verify every requested logical storage count')
            rejected=[]
            for parameters,diagnostic in [(f'.WORDS({capacity+1})','WORDS=1..'),('.WORDS(0)','WORDS=1..'),('.WORDS(3),.ADDR_WIDTH(1)','sufficient ADDR_WIDTH'),('.CODE_WIDTH(7)','CODE_WIDTH=8/10')]:
                invalid=out/'invalid.sv';invalid.write_text('module invalid;\nternary_stream_storage_t27 #('+parameters+') dut ();\ninitial begin #1;$fatal(1,"invalid configuration accepted");end\nendmodule\n')
                execute(['iverilog','-g2012','-s','invalid','-o',str(executable),*sources,str(invalid)],out)
                result=subprocess.run(['vvp',str(executable)],cwd=out,text=True,capture_output=True,timeout=30)
                if result.returncode==0 or diagnostic not in result.stdout:raise RuntimeError(f'Invalid config not rejected: {parameters}')
                rejected.append(parameters)
            reports.append({'manifest':manifest,'logical_counts_checked':counts,'log':output,'invalid_parameters_rejected':rejected})
        for value in [0,-1,module.MAX_WORDS+1]:
            try:module.generate(value,1,compiler,work/'bad',root)
            except ValueError:pass
            else:raise AssertionError(f'invalid capacity accepted: {value}')
    return {'evidence':'native-t27-specialized-storage-rtl-simulation','legacy_rtl_compiled':False,
            'physical_device_tested':False,'synthesis_performed':False,'cases':reports}

def main()->int:
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--compiler',type=Path,required=True)
    p.add_argument('--output',type=Path,default=ROOT/'build/t27/storage-specialization-validation.json');args=p.parse_args()
    try:report=checks(args.compiler)
    except (RuntimeError,OSError,ValueError,subprocess.TimeoutExpired) as error:print(error,file=sys.stderr);return 2
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,indent=2)+'\n')
    for case in report['cases']:print(f'PASS capacity{case["manifest"]["physical_group_capacity"]}: logical counts {case["logical_counts_checked"]}')
    print(f'Evidence: {args.output.resolve()}');return 0
if __name__=='__main__':raise SystemExit(main())
