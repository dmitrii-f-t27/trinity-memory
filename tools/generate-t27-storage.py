#!/usr/bin/env python3
"""Specialize native .t27 storage capacity and generate its Verilog adapters.

Build glue only: the storage/sequencer/decoder algorithms stay in t27/rtl.
A capacity specialization changes a source constant before invoking t27c.
No legacy Verilog datapath or Python decoder algorithm is generated here.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
MAX_WORDS=(2**31-5)//5
sys.path.insert(0,str(ROOT/"tools"))
from t27_compiler import resolve_compiler,verify_compiler,check_lexer

def replace_exact(source: str, old: str, new: str, count: int=1) -> str:
    actual=source.count(old)
    if actual!=count:
        raise RuntimeError(f'Native template changed: expected {count} copies of {old!r}, found {actual}')
    return source.replace(old,new)

def compiler_path(explicit: str|None) -> Path:
    return resolve_compiler(explicit)

def run(command: list[str]) -> str:
    result=subprocess.run(command,capture_output=True,text=True,timeout=180)
    if result.returncode:raise RuntimeError(f'Command failed: {command}\n{result.stdout}\n{result.stderr}')
    return result.stdout

def sha(data: bytes) -> str:return hashlib.sha256(data).hexdigest()

def generate(words: int, logical_default: int, compiler: Path, destination: Path, root: Path=ROOT) -> dict:
    if type(words)is not int or not 1<=words<=MAX_WORDS:raise ValueError(f'WORDS must be 1..{MAX_WORDS}')
    if type(logical_default)is not int or not 1<=logical_default<=words*5:raise ValueError('Default logical trits must fit physical group capacity')
    compiler=verify_compiler(compiler,root)
    source_path=root/'t27/rtl/stream_storage.t27';view_path=root/'t27/rtl/stream_view.t27';adapter_path=root/'rtl/t27/streams.v'
    original=source_path.read_text();view=view_path.read_text();adapter=adapter_path.read_text()
    source=replace_exact(original,'const CAPACITY: u32 = 64;',f'const CAPACITY: u32 = {words};')
    source=replace_exact(source,'logical extent (1..64)',f'logical extent (1..{words})')
    adapter=replace_exact(adapter,'Physical native RAM capacity is 64 groups.',f'Physical native RAM capacity is {words} groups.')
    adapter=replace_exact(adapter,'parameter integer WORDS = 64,',f'parameter integer WORDS = {words},')
    adapter=replace_exact(adapter,'WORDS > 64',f'WORDS > {words}')
    adapter=replace_exact(adapter,'WORDS=1..64',f'WORDS=1..{words}')
    adapter=replace_exact(adapter,'parameter integer TRIT_COUNT = 320,',f'parameter integer TRIT_COUNT = {logical_default},',3)
    adapter=replace_exact(adapter,'TRIT_COUNT > 320',f'TRIT_COUNT > {words*5}')
    adapter=replace_exact(adapter,'TRIT_COUNT=1..320',f'TRIT_COUNT=1..{words*5}')
    destination=destination.resolve()
    if destination in {source_path.parent.resolve(),adapter_path.parent.resolve()}:raise ValueError("Output directory would overwrite native source templates")
    destination.mkdir(parents=True,exist_ok=True)
    generated={}
    for name,contents in [('stream_storage',source),('stream_view',view)]:
        (destination/f'{name}.t27').write_text(contents)
    lexer_log=check_lexer(compiler,destination)
    (destination/'lexer.log').write_text(lexer_log)
    for name in ['stream_storage','stream_view']:
        spec=destination/f'{name}.t27'
        parsed=run([str(compiler),'parse-complete','--show',str(spec)])
        if 'nothing discarded' not in parsed:raise RuntimeError(f'Unconsumed source in {spec}: {parsed}')
        code=run([str(compiler),'gen-verilog',str(spec)])
        if any(text in code for text in ['ENTRY POINT REFUSED','NO DATA PORTS','TODO']):raise RuntimeError(f'Incomplete generated RTL: {spec}')
        if name=='stream_storage':
            match=re.search(r'reg\s+\[15:0\]\s+memory\s*\[0:(\d+)\]',code)
            if not match or int(match[1])+1!=words:raise RuntimeError('Generated memory does not have the requested physical group capacity')
        generated[name+'.v']=code
        (destination/f'{name}.parse.log').write_text(parsed)
    generated['streams.v']=adapter
    for name,contents in generated.items():(destination/name).write_text(contents)
    manifest={
        'kind':'native-t27-storage-capacity-specialization','physical_group_capacity':words,
        'default_logical_trits':logical_default,'maximum_logical_trits':words*5,
        'native_element_bits':16,'supported_code_widths':[8,10],
        'legacy_datapath_used':False,'simulation_performed_by_generator':False,
        'compiler_path':str(compiler),'compiler_sha256':sha(compiler.read_bytes()),
        'compiler_revision':(root/'native/compiler.lock').read_text().strip(),'lexer_discarded_characters':0,
        'source_sha256':{str(p.relative_to(root)):sha(p.read_bytes()) for p in [source_path,view_path,adapter_path]},
        'specialized_spec_sha256':{'stream_storage.t27':sha(source.encode()),'stream_view.t27':sha(view.encode())},
        'generated_sha256':{name:sha(code.encode()) for name,code in generated.items()},
    }
    (destination/'storage-specialization.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return manifest

def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    sizes=parser.add_mutually_exclusive_group(required=True)
    sizes.add_argument('--words',type=int,help='Physical capacity in five-trit groups')
    sizes.add_argument('--trits',type=int,help='Default logical trits; allocate ceil(trits/5) groups')
    parser.add_argument('--compiler');parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:
        words=args.words if args.words is not None else (args.trits+4)//5
        trits=args.trits if args.trits is not None else words*5
        result=generate(words,trits,compiler_path(args.compiler),args.output)
    except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired) as error:
        print(error,file=sys.stderr);return 2
    print(f'Generated native storage: {result["physical_group_capacity"]} groups, default {result["default_logical_trits"]} trits; no synthesis or hardware measurements')
    return 0
if __name__=='__main__':raise SystemExit(main())
