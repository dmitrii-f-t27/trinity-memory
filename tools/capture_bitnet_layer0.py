#!/usr/bin/env python3
"""Pinned text -> embedding rows -> upstream layer 0 -> FFN/ActQuant comparison.

No full model/checkpoint is loaded. Optional runtime dependencies are pinned in
tools/bitnet-capture-requirements.txt. Network reads require --fetch.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import struct
import sys
import urllib.request

os.environ['TORCH_COMPILE_DISABLE'] = '1'
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from trinity_memory import fixtures as fx

REPO = 'microsoft/bitnet-b1.58-2B-4T'
REV = '04c3b9ad9361b824064a1f25ea60a8be9599b127'
LOCK = ROOT / 'fixtures/bitnet-layer0-capture.json'
CACHE = ROOT / 'build/bitnet-capture'
METADATA = {
    'config.json': '2b43e80788972e6d53967b01ac3609d7df23cf0aabb0c866e51be694a59c1149',
    'tokenizer.json': 'e134af98b985517b4f068e3755ae90d4e9cd2d45d328325dc503f1c6b2d06cc7',
    'tokenizer_config.json': 'd27b698683435b0b0dd544a591a30196b2b63f5fd4c8c64b9060a84dc185386f',
}
PROMPTS = [
    'Explain why the sky appears blue.',
    'What is seven multiplied by eight?',
    'Объясни, зачем компьютеру нужна память.',
    'def square(x):\n    return x * x\n',
]
VERSIONS = {'torch':'2.11.0', 'transformers':'5.14.1', 'numpy':'2.4.3',
            'tokenizers':'0.22.2', 'huggingface-hub':'1.11.0', 'safetensors':'0.8.0'}
UPSTREAM = {
    'integrations/bitnet.py':'bb5b7f1108fa20b4ed8ad9e7eb0adc65c2974b9a4a2e8acf4fc3d230f67b4d05',
    'models/bitnet/modeling_bitnet.py':'a23741c59696ab92a53db28b697a3ce54400fafd301222a53a578cf072a5d47c',
    'activations.py':'4b1a469c24be4e4c6320536ad55a7337d24de04c3ecc71597fbf0267f1560612',
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def metadata(fetch):
    from huggingface_hub import hf_hub_download
    folder = CACHE/'metadata'; folder.mkdir(parents=True,exist_ok=True)
    for name, expected in METADATA.items():
        target=folder/name
        if not target.exists():
            source=hf_hub_download(REPO,name,revision=REV,token=False,local_files_only=not fetch)
            data=Path(source).read_bytes()
            if sha(data)!=expected: raise ValueError('metadata hash mismatch: '+name)
            target.write_bytes(data)
        if sha(target.read_bytes())!=expected: raise ValueError('metadata hash mismatch: '+name)
    return folder


def range_bytes(remote, item, fetch):
    begin,end=item['begin'],item['end']
    if not 0 <= begin < end <= remote.size:raise ValueError('range outside checkpoint')
    target=CACHE/'ranges'/f'{begin}-{end}.bin'
    legacy=remote.directory/f'{begin}-{end}.bin'
    if target.exists(): data=target.read_bytes()
    elif legacy.exists(): data=legacy.read_bytes()
    elif fetch:
        request=urllib.request.Request(remote.url,headers={'Range':f'bytes={begin}-{end-1}'})
        with urllib.request.urlopen(request,timeout=60) as response:
            if response.status!=206 or response.headers.get('Content-Range')!=f'bytes {begin}-{end-1}/{remote.size}':
                raise ValueError('server did not return requested range')
            data=response.read(end-begin+1)
    else: raise fx.CacheMiss(f'missing capture range {begin}-{end}; use --fetch')
    if len(data)!=end-begin or ('sha256'in item and sha(data)!=item['sha256']):
        raise ValueError(f'range mismatch {begin}-{end}')
    known=remote.entry.ranges.get((begin,end))
    if known and sha(data)!=known['sha256']:raise ValueError('existing fixture manifest hash mismatch')
    if not target.exists(): fx.write_atomic(target,data)
    return data


def inputs(fetch, record):
    from tokenizers import Tokenizer
    folder=metadata(fetch)
    # Execute the checkpoint's serialized pipeline unchanged, including BOS.
    tokenizer=Tokenizer.from_file(str(folder/'tokenizer.json'))
    ids=[tokenizer.encode(p,add_special_tokens=True).ids for p in PROMPTS]
    remote=fx.remote(REPO,'model.safetensors',offline=not fetch)
    prefix=remote.prefix(1048576)
    offset=8+struct.unpack('<Q',prefix[:8])[0]
    header=json.loads(prefix[8:offset])
    names=[n for n in header if n.startswith('model.layers.0.')]
    ranges=[]
    for name in names:
        entry=header[name]
        ranges.append({'name':name,'begin':offset+entry['data_offsets'][0],
                       'end':offset+entry['data_offsets'][1]})
    embed=header['model.embed_tokens.weight']
    if embed['dtype']!='BF16' or embed['shape']!=[128256,2560]:raise ValueError('unexpected embedding geometry')
    for token in sorted({v for seq in ids for v in seq}):
        begin=offset+embed['data_offsets'][0]+token*2560*2
        ranges.append({'name':f'embedding.{token}','begin':begin,'end':begin+5120})
    base={'schema':'trinity.bitnet-layer0-inputs.v1','repo':REPO,'revision':REV,
          'metadata_sha256':METADATA,'prompts':PROMPTS,'token_ids':ids,'ranges':ranges,
          'header_sha256':sha(prefix[:offset]),'file_size':remote.size,
          'tensors':{name:header[name] for name in names+['model.embed_tokens.weight']}}
    if record:
        if LOCK.exists():raise ValueError('input lock already exists; review before replacing it')
        with ThreadPoolExecutor(max_workers=4) as pool:
            payloads=list(pool.map(lambda item:range_bytes(remote,item,fetch),ranges))
        for item,data in zip(ranges,payloads):item['sha256']=sha(data)
        LOCK.write_text(json.dumps(base,indent=2)+'\n')
    else:
        saved=json.loads(LOCK.read_text())
        geometry=[{k:v for k,v in item.items() if k!='sha256'} for item in saved['ranges']]
        if {**saved,'ranges':geometry}!=base:raise ValueError('input geometry/tokenizer differs from lock')
        ranges=saved['ranges']
        with ThreadPoolExecutor(max_workers=4) as pool:
            payloads=list(pool.map(lambda item:range_bytes(remote,item,fetch),ranges))
    return folder,ids,header,dict(zip((item['name'] for item in ranges),payloads))


def capture(fetch=False, record=False):
    import numpy as np
    import torch
    import transformers
    from transformers import BitNetConfig,BitNetQuantConfig
    from transformers.models.bitnet.modeling_bitnet import BitNetDecoderLayer,BitNetRotaryEmbedding
    from transformers.integrations.bitnet import replace_with_bitnet_linear
    from tools import bitnet_ffn_runtime as numeric
    for name,want in VERSIONS.items():
        actual=importlib.metadata.version(name).split('+')[0]
        if actual!=want:raise ValueError(f'{name}: {actual}, expected {want}')
    for name,want in UPSTREAM.items():
        if sha((Path(transformers.__file__).parent/name).read_bytes())!=want:
            raise ValueError('upstream source changed: '+name)
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    denormal_api_supported=bool(torch.set_flush_denormal(False))
    folder,ids,header,payload=inputs(fetch,record)
    config=BitNetConfig.from_dict(json.loads((folder/'config.json').read_text()))
    config._attn_implementation='eager'
    with torch.device('meta'):
        layer=BitNetDecoderLayer(config,0).to(dtype=torch.bfloat16)
        layer=replace_with_bitnet_linear(layer,quantization_config=BitNetQuantConfig(**config.quantization_config))
        layer=layer.to(dtype=torch.bfloat16)
    state={}
    for name,entry in header.items():
        if not name.startswith('model.layers.0.'):continue
        dtype=torch.bfloat16 if entry['dtype']=='BF16' else torch.uint8
        state[name.removeprefix('model.layers.0.')]=torch.frombuffer(bytearray(payload[name]),dtype=dtype).reshape(entry['shape'])
    layer.load_state_dict(state,strict=True,assign=True)
    layer.eval()
    rotary=BitNetRotaryEmbedding(config,device='cpu')
    stages={}
    def before(name):
        def hook(module,args): stages[name]=args[0].detach().float().clone()
        return hook
    def after(name):
        def hook(module,args,out): stages[name]=out.detach().float().clone()
        return hook
    layer.post_attention_layernorm.register_forward_pre_hook(before('x'))
    layer.post_attention_layernorm.register_forward_hook(after('h'))
    for name,mod in [('g',layer.mlp.gate_proj),('u',layer.mlp.up_proj),('s',layer.mlp.ffn_sub_norm),('y',layer.mlp.down_proj)]:
        mod.register_forward_hook(after(name))
    layer.mlp.ffn_sub_norm.register_forward_pre_hook(before('a'))
    cases=[]; arrays={}
    with torch.no_grad():
        for i,tokens in enumerate(ids):
            print(f'capture prompt {i}: {len(tokens)} tokens',file=sys.stderr,flush=True)
            embeddings=torch.stack([torch.frombuffer(bytearray(payload[f'embedding.{t}']),dtype=torch.bfloat16) for t in tokens])[None]
            position=torch.arange(len(tokens))[None]
            mask=torch.triu(torch.full((len(tokens),len(tokens)),torch.finfo(torch.bfloat16).min,dtype=torch.bfloat16),diagonal=1)[None,None]
            layer(embeddings,attention_mask=mask,position_embeddings=rotary(embeddings,position))
            captured=dict(stages)
            # Explicit evaluation calls no upstream forward methods or hooks.
            paths={p:numeric.explicit_ffn(layer,captured['x'],p) for p in ('bf16','f32')}
            calibration={k:numeric.metrics(captured[k],paths['bf16'][0][k]) for k in captured}
            for k in calibration:
                calibration[k]['bit_mismatches_f32'] = int((captured[k].view(torch.int32) != paths['bf16'][0][k].view(torch.int32)).sum())
            if any(m['bit_mismatches_f32'] for m in calibration.values()):
                raise ValueError('BF16 control differs from upstream: '+json.dumps(calibration))
            failures={}
            for p,wide in [('gf16',False),('gf16_wide_product',True)]:
                try:paths[p]=numeric.explicit_ffn(layer,captured['x'],'gf16',wide_product=wide)
                except numeric.RangeFailure as error:
                    paths[p]=(error.stages,error.rounds,error.codes)
                    failures[p]=error.boundary
            case={'prompt_index':i,'tokens':len(tokens),'calibration':calibration,
                  'capture_sha256_f32':{k:numeric.digest(v) for k,v in captured.items()},
                  'reference_peaks':{k:float(v.abs().max()) for k,v in captured.items()},
                  'profiles':{}}
            for p,(values,events,codes) in paths.items():
                case['profiles'][p]={'status':'nonfinite' if p in failures else 'finite',
                                     'blocked_at':failures.get(p),
                                     'vs_f32_actquant':{k:numeric.metrics(paths['f32'][0][k],v) for k,v in values.items()},
                                     'rounding_events':events,'actquant_code_changes_vs_bf16':
                                     {k:int((v!=paths['bf16'][2][k]).sum()) for k,v in codes.items()},
                                     'per_token_y_nmse_vs_f32':
                                     [numeric.metrics(paths['f32'][0]['y'][0,t],values['y'][0,t])['nmse']
                                      for t in range(len(tokens))] if 'y' in values else None}
            cases.append(case)
            for k,v in captured.items():arrays[f'prompt{i}_{k}']=v.numpy()
    CACHE.mkdir(parents=True,exist_ok=True)
    np.savez_compressed(CACHE/'captures.npz',**arrays)
    return {'schema':'trinity.bitnet-layer0-runtime.v1','model':{'repo':REPO,'revision':REV},
            'scope':'text-derived layer-0 states; local FFN replacement only; no later layers, logits or generation quality',
            'runtime':VERSIONS,'upstream_sha256':UPSTREAM,'machine':platform.machine(),
            'device':'CPU, one thread, deterministic, eager, torch.compile disabled',
            'denormal_flush_requested':False,'denormal_api_supported':denormal_api_supported,
            'input_lock_sha256':sha(LOCK.read_bytes()),'total_tokens':sum(map(len,ids)),
            'source_sha256':{p:sha((ROOT/p).read_bytes()) for p in ('tools/capture_bitnet_layer0.py','tools/bitnet_ffn_runtime.py','tools/gf16_reference.py','tools/bitnet-capture-requirements.txt')},
            'cases':cases}


def main():
    ap=argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--fetch',action='store_true')
    ap.add_argument('--record-lock',action='store_true')
    ap.add_argument('--output',type=Path,default=ROOT/'reports/numeric/bitnet-layer0-runtime.json')
    args=ap.parse_args()
    result=capture(args.fetch,args.record_lock)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    print(args.output)
    if any(c['profiles']['gf16_wide_product']['status']!='finite' for c in result['cases']):
        print('wide-product candidate failed; diagnostics retained in report',file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
