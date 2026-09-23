#!/usr/bin/env python3
"""Install a built wheel outside the checkout and verify its actual native paths."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import venv
import zipfile

SMOKE = r'''
import hashlib,json,pathlib,sys
import trinity_memory as tm
from trinity_memory import _native as n
from trinity_memory.bridge import BridgeServer,BridgeClient
from trinity_memory.tensorpack import Tensor,encode_tensors,decode_tensors
from trinity_memory.rtl_compute import run_rtl_dot
package=pathlib.Path(tm.__file__).resolve().parent
assert 'site-packages' in str(package),package
runtime=package/'_native_runtime'
manifest=json.loads((runtime/'manifest.json').read_text())
assert manifest['version']==tm.__version__=='0.3.0'
for name,digest in manifest['files'].items():
    assert hashlib.sha256((runtime/name).read_bytes()).hexdigest()==digest,name
for name in ('codecs.wasm','formats.wasm'):
    assert name in manifest['files'],name
    assert (runtime/name).read_bytes()[:8]==b'\0asm\1\0\0\0',name
assert n.library().tm_bridge_request
assert str(runtime) in n.library()._name
assert n.executable().parent==runtime
weights=[-1,0,1,1,-1,1]; acts=[-128,0,127,-128,127,1]
blob=tm.encode_file(weights)
assert tm.decode_file(blob)==weights
pack=encode_tensors([Tensor('w',(6,),tuple(weights))])
assert decode_tensors(pack)[0].values==tuple(weights)
with BridgeServer() as server:
    client=BridgeClient(server.url)
    handle=client.upload(pack)
    assert client.read(handle)==pack
    assert client.dot(handle,'w',acts)['accumulators']==[1]
    client.delete(handle)
    assert server.object_count==server.stored_bytes==0
report={'package':str(package),'compiler_revision':manifest['compiler_revision'],'native_files':len(manifest['files']),
        'tmem':True,'tensorpack':True,'loopback_http':True,'rtl':False}
if sys.argv[1]=='rtl':
    for codec in ('dense5','baseline2'):
        witness=run_rtl_dot(weights,acts,codec)
        assert witness['result']==1 and witness['evidence']=='rtl-simulation'
    report['rtl']=True
print(json.dumps(report))
'''


# Instantiates the shipped formats.wasm (no imports) and decodes one TQ2_0
# block through it: 256 codes of 2 (+1) and scale 0x3c00.
WASM_PROBE = r'''
const {readFileSync} = require('node:fs');
const module = new WebAssembly.Module(readFileSync(process.argv[1]));
if (WebAssembly.Module.imports(module).length !== 0) throw new Error('formats.wasm has imports');
const api = new WebAssembly.Instance(module, {}).exports;
for (const name of ['tf_decode_blocks', 'tf_gguf_check', 'tf_safetensors_check'])
  if (typeof api[name] !== 'function') throw new Error(`missing export ${name}`);
const base = api.memory.buffer.byteLength;
api.memory.grow(1);
const bytes = new Uint8Array(api.memory.buffer, base, 66);
bytes.fill(0xaa, 0, 64); bytes[64] = 0x00; bytes[65] = 0x3c;
const values = base + 128, scales = base + 128 + 1024;
const status = Number(api.tf_decode_blocks(2, base, 66, 256, values, 256, scales, 1));
const out = new Int32Array(api.memory.buffer, values, 256);
if (status !== 0 || !out.every((v) => v === 1) || new Uint32Array(api.memory.buffer, scales, 1)[0] !== 0x3c00)
  throw new Error(`decode through formats.wasm failed: ${status}`);
console.log(JSON.stringify(WebAssembly.Module.exports(module).length));
'''


def instantiate_formats_wasm(path: Path) -> dict:
    node=shutil.which('node')
    if node is None:
        raise AssertionError('node is required to instantiate the shipped formats.wasm')
    result=subprocess.run([node,'-e',WASM_PROBE,str(path)],check=True,capture_output=True,text=True)
    return {'exports':json.loads(result.stdout),'decoded_block':True}


def verify(wheel: Path, rtl: bool):
    wheel = wheel.resolve()
    if wheel.name.endswith('-any.whl') or '-py3-none-' not in wheel.name:
        raise AssertionError('Expected a py3-none platform wheel')
    with zipfile.ZipFile(wheel) as archive:
        paths=archive.namelist()
        assert not any('/reference/' in name or 'trinity_memory_reference/' in name for name in paths)
        assert any(name.endswith('_native_runtime/manifest.json') for name in paths)
        for name in ('codecs.wasm','formats.wasm'):
            assert any(path.endswith('_native_runtime/'+name) for path in paths),name
    with tempfile.TemporaryDirectory(prefix='trinity-installed-wheel-') as temporary:
        root=Path(temporary); environment=root/'venv'
        venv.EnvBuilder(with_pip=True).create(environment)
        python=environment/'bin/python'
        subprocess.run([str(python),'-m','pip','install','--no-deps',str(wheel)],check=True,capture_output=True)
        env=dict(os.environ)
        for key in ('PYTHONPATH','TRINITY_MEMORY_NATIVE_LIBRARY','TRINITY_MEMORY_NATIVE_CLI',
                    'TRINITY_MEMORY_RTL_ROOT','TRINITY_T27_RTL_ROOT'):
            env.pop(key,None)
        result=subprocess.run([str(python),'-c',SMOKE,'rtl' if rtl else 'software'],cwd=root,env=env,
                              check=True,capture_output=True,text=True)
        report=json.loads(result.stdout)
        report['formats_wasm']=instantiate_formats_wasm(Path(report['package'])/'_native_runtime'/'formats.wasm')
        source=root/'weights.json'; blob=root/'weights.tmem'; recovered=root/'out.json'
        source.write_text('[-1,0,1]')
        for command in (['pack',str(source),str(blob)],['unpack',str(blob),str(recovered)]):
            subprocess.run([str(environment/'bin/trinity-memory'),*command],cwd=root,env=env,check=True,capture_output=True)
        assert json.loads(recovered.read_text())==[-1,0,1]
        report['cli']=True
        return report


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--wheel',required=True,type=Path)
    parser.add_argument('--rtl',action='store_true')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args(); report=verify(args.wheel,args.rtl)
    text=json.dumps(report,indent=2)+'\n'
    if args.output: args.output.write_text(text)
    print(text,end='')

if __name__=='__main__': main()
