#!/usr/bin/env python3
"""Replay the bounded kernel on pinned real layer-0 activations, optionally RTL."""
import argparse
import ctypes
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys

os.environ['TORCH_COMPILE_DISABLE']='1'
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools import capture_bitnet_layer0 as capture
from tools import gf16_wide_reference as ref, gf16_wide_build as build


def sha(data):return hashlib.sha256(data).hexdigest()


def replay(report_path, arrays_path, work, rtl_count):
    import numpy as np
    import torch
    import torch.nn.functional as F
    import transformers
    from transformers import BitNetConfig, BitNetQuantConfig
    from transformers.models.bitnet.modeling_bitnet import BitNetDecoderLayer
    from transformers.integrations.bitnet import replace_with_bitnet_linear
    from tools import bitnet_ffn_runtime as rt
    for name,version in capture.VERSIONS.items():
        if importlib.metadata.version(name).split('+')[0]!=version:raise ValueError('runtime version changed: '+name)
    for name,digest in capture.UPSTREAM.items():
        if sha((Path(transformers.__file__).parent/name).read_bytes())!=digest:raise ValueError('upstream source changed')
    report=json.loads(report_path.read_text())
    if report['input_lock_sha256']!=sha(capture.LOCK.read_bytes()):raise ValueError('capture input lock changed')
    for name,digest in report['source_sha256'].items():
        if sha((ROOT/name).read_bytes())!=digest:raise ValueError('capture source changed: '+name)
    arrays=np.load(arrays_path,allow_pickle=False)
    for c in report['cases']:
        for k,digest in c['capture_sha256_f32'].items():
            if sha(arrays[f'prompt{c["prompt_index"]}_{k}'].astype('<f4').tobytes())!=digest:
                raise ValueError('raw captured tensor hash mismatch')
    torch.set_num_threads(1);torch.use_deterministic_algorithms(True);torch.set_flush_denormal(False)
    folder,ids,header,payload=capture.inputs(False,False)
    config=BitNetConfig.from_dict(json.loads((folder/'config.json').read_text()))
    with torch.device('meta'):
        layer=BitNetDecoderLayer(config,0).to(dtype=torch.bfloat16)
        layer=replace_with_bitnet_linear(layer,quantization_config=BitNetQuantConfig(**config.quantization_config))
        layer=layer.to(dtype=torch.bfloat16)
    state={}
    for name,entry in header.items():
        if name.startswith('model.layers.0.'):
            dtype=torch.bfloat16 if entry['dtype']=='BF16' else torch.uint8
            state[name.removeprefix('model.layers.0.')]=torch.frombuffer(bytearray(payload[name]),dtype=dtype).reshape(entry['shape'])
    layer.load_state_dict(state,strict=True,assign=True);layer.eval()
    rtl,lib=build.generate(work)
    lib.product.argtypes=[ctypes.c_uint32]*2;lib.product.restype=ctypes.c_uint32
    lib.quantize.argtypes=[ctypes.c_uint32,ctypes.c_int32];lib.quantize.restype=ctypes.c_uint64
    lib.multiply.argtypes=[ctypes.c_uint32]*2;lib.multiply.restype=ctypes.c_uint32
    lib.unit_round.argtypes=[ctypes.c_uint64]*5+[ctypes.c_uint32];lib.unit_round.restype=ctypes.c_uint32
    weight=rt.gf16_words(layer.mlp.ffn_sub_norm.weight.detach().float().numpy()).tolist()
    rows=[];selected=[];cases=[];mask=(1<<64)-1
    with torch.no_grad():
        for i,tokens in enumerate(ids):
            x=torch.from_numpy(arrays[f'prompt{i}_x'])
            bf,_,_=rt.explicit_ffn(layer,x,'bf16')
            for k,v in bf.items():
                expected=torch.from_numpy(arrays[f'prompt{i}_{k}'])
                if not torch.equal(v.view(torch.int32),expected.view(torch.int32)):raise ValueError('BF16 recalibration failed')
            fp,_,_=rt.explicit_ffn(layer,x,'f32')
            sw,_,_=rt.explicit_ffn(layer,x,'gf16',wide_product=True)
            gate=rt.gf16_words(sw['g'][0].numpy());up=rt.gf16_words(sw['u'][0].numpy())
            new_s=[];detail=[]
            for token,(g,u) in enumerate(zip(gate,up)):
                g,u=g.tolist(),u.tolist();r=ref.row(g,u,weight)
                actual_product=sw['a'][0,token].numpy().view(np.uint32).tolist()
                if r['product']!=actual_product:raise ValueError('product differs from FP32 control')
                den=r['root']
                for a,b,p,m,unit,w,out in zip(g,u,r['product'],r['magnitudes'],r['unit'],weight,r['output']):
                    if lib.product(a,b)!=p or lib.quantize(p,r['block_exponent'])!=m:
                        raise ValueError('generated C product/scaling mismatch')
                    q,rem=divmod(m<<71,den)
                    if lib.unit_round(q,rem&mask,rem>>64,den&mask,den>>64,(p>>16)&0x8000)!=unit:
                        raise ValueError('generated C rational rounding mismatch')
                    if lib.multiply(unit,w)!=out:raise ValueError('generated C norm-weight multiply mismatch')
                if r['overflow']:raise ValueError('wide candidate output overflow')
                new_s.append(rt.DECODE[r['output']])
                rows.append((g,u,weight))
                # Last token of each different text, plus the common BOS once.
                if token==len(tokens)-1 or (i==0 and token==0):selected.append(len(rows)-1)
                detail.append({'token_index':token,'block_exponent':r['block_exponent'],
                    'sum_bits':r['sum_squares'].bit_length(),'root_bits':r['root'].bit_length(),
                    'product_sha256_f32':sha(np.array(r['product'],dtype='<u4').tobytes()),
                    'unit_sha256_gf16':sha(np.array(r['unit'],dtype='<u2').tobytes()),
                    'output_sha256_gf16':sha(np.array(r['output'],dtype='<u2').tobytes())})
            new_s=torch.from_numpy(np.array(new_s))[None]
            quant,new_codes,_=rt.actquant(new_s,'gf16')
            _,old_codes,_=rt.actquant(sw['s'],'gf16')
            dot=rt.round_storage(F.linear(quant,layer.mlp.down_proj.weight.float()),'gf16')
            scale=rt.round_storage(layer.mlp.down_proj.weight_scale.float(),'gf16')
            y=rt.round_storage(dot*scale,'gf16')
            cases.append({'prompt_index':i,'tokens':len(tokens),'rows':detail,
                's_vs_software_gf16':rt.metrics(sw['s'],new_s),
                'down_actquant_code_changes_vs_software':int((new_codes!=old_codes).sum()),
                'y_vs_software_gf16':rt.metrics(sw['y'],y),
                'y_vs_f32_actquant':rt.metrics(fp['y'],y),
                'bf16_y_vs_f32_actquant':rt.metrics(fp['y'],bf['y']),
                'y_sha256_f32':rt.digest(y)})
            print(f'prompt {i}: {len(tokens)} rows, s mismatches {cases[-1]["s_vs_software_gf16"]["mismatches"]}',flush=True)
    arrays.close()
    chosen=list(range(len(rows))) if rtl_count=='all' else selected if rtl_count=='selected' else []
    rtl_result=None
    if chosen:
        sim=build.simulate(work,rtl,[rows[i] for i in chosen],simulator='verilator',controls=False)
        (work/'rtl.log').write_text(sim)
        rtl_result={'simulator':build.run(['verilator','--version']).strip(),
                    'rows':chosen,'values':sum(len(rows[i][0]) for i in chosen),'log':sim}
    names=['t27/rtl/gf16_wide_norm.t27','t27/rtl/ffn_wide.t27','rtl/t27/gf16_wide_norm.v',
           'tools/gf16_wide_reference.py','tools/gf16_wide_build.py','tools/replay_gf16_wide.py']
    result={'schema':'trinity.gf16-wide-norm.v1','profile':'gf16-wide-norm-v1',
        'source_sha256':{p:sha((ROOT/p).read_bytes()) for p in names},
        'compiler':(ROOT/'native/compiler.lock').read_text().strip(),'runtime':capture.VERSIONS,
        'machine':platform.machine(),'capture_report_sha256':sha(report_path.read_bytes()),
        'input_lock_sha256':report['input_lock_sha256'],'rows':len(rows),
        'products_exact_vs_f32_control':sum(len(r[0]) for r in rows),
        'c_helpers_exact_values':sum(len(r[0]) for r in rows),'cases':cases,'rtl':rtl_result}
    return result


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--capture-report',type=Path,default=ROOT/'reports/numeric/bitnet-layer0-runtime.json')
    ap.add_argument('--captures',type=Path,default=capture.CACHE/'captures.npz')
    ap.add_argument('--work',type=Path,default=ROOT/'build/gf16-wide/replay')
    ap.add_argument('--rtl',choices=('none','selected','all'),default='selected')
    ap.add_argument('--output',type=Path,default=ROOT/'reports/numeric/gf16-wide-normalization.json')
    args=ap.parse_args();args.work=args.work.resolve()
    result=replay(args.capture_report,args.captures,args.work,args.rtl)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    print(args.output)


if __name__=='__main__':main()
