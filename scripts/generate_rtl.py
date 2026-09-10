#!/usr/bin/env python3
"""Generate/check RTL directly from executable t27/rtl sources.

This is compiler/build glue. It contains no decoder or storage algorithms.
The original v0.2 decoder generator is retained only under tests/reference.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"tools"))
from t27_compiler import resolve_compiler,verify_compiler,check_lexer


def run(compiler:Path,command:str,source:Path)->str:
    arguments=[str(compiler),command]
    if command=='parse-complete':arguments.append('--show')
    arguments.append(str(source))
    process=subprocess.run(arguments,capture_output=True,text=True,timeout=180)
    if process.returncode:raise RuntimeError(f'Compiler failed on {source}:\n{process.stdout}\n{process.stderr}')
    return process.stdout

def main()->int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true',help='regenerate with t27c and compare the generated output')
    parser.add_argument('--compiler');parser.add_argument('--output-directory',type=Path,default=ROOT/'build/t27/rtl')
    args=parser.parse_args()
    try:
        compiler=verify_compiler(resolve_compiler(args.compiler));output=args.output_directory.resolve();files={};inputs={}
        if output==(ROOT/'rtl/t27').resolve():raise RuntimeError('Output would overwrite native wiring adapters')
        lexer_log=check_lexer(compiler,ROOT/'t27/rtl')
        for source in sorted((ROOT/'t27/rtl').glob('*.t27')):
            parsed=run(compiler,'parse-complete',source)
            if 'nothing discarded' not in parsed:raise RuntimeError(f'Unconsumed source: {source}\n{parsed}')
            generated=run(compiler,'gen-verilog',source)
            if any(marker in generated for marker in ['ENTRY POINT REFUSED','NO DATA PORTS','TODO']):raise RuntimeError(f'Incomplete generated RTL: {source}')
            files[output/(source.stem+'.v')]=generated
            inputs[str(source.relative_to(ROOT))]=hashlib.sha256(source.read_bytes()).hexdigest()
        if not files:raise RuntimeError('No t27 RTL sources found')
        different=[str(path) for path,text in files.items() if not path.is_file() or path.read_text()!=text]
        if args.check:
            if different:print('Generated native RTL differs: '+', '.join(different),file=sys.stderr);return 1
        else:
            output.mkdir(parents=True,exist_ok=True)
            for path,text in files.items():path.write_text(text)
            manifest={'evidence':'t27-compiler-generation','legacy_datapath_used':False,
                'compiler_sha256':hashlib.sha256(compiler.read_bytes()).hexdigest(),'source_sha256':inputs,
                'compiler_revision':(ROOT/'native/compiler.lock').read_text().strip(),'lexer_discarded_characters':0,
                'generated_sha256':{path.name:hashlib.sha256(text.encode()).hexdigest() for path,text in files.items()}}
            (output/'standalone-lexer.log').write_text(lexer_log)
            (output/'rtl-generation.json').write_text(json.dumps(manifest,indent=2)+'\n')
    except (OSError,RuntimeError,subprocess.TimeoutExpired) as error:print(error,file=sys.stderr);return 2
    print(f'{"Verified" if args.check else "Generated"} {len(files)} native t27 RTL modules; simulation/synthesis are separate checks')
    return 0
if __name__=='__main__':raise SystemExit(main())
