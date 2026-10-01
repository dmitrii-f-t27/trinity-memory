#!/usr/bin/env python3
"""Pinned real-input full GF16 FFN experiment; optional full pipeline RTL."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.environ['TORCH_COMPILE_DISABLE']='1'
os.environ['TRINITY_FIXTURES_OFFLINE']='1'
from tools import capture_bitnet_layer0 as capture
from tools import gf16_ffn_reference as ref, gf16_ffn_vectors as vectors, gf16_ffn_build as build


SOURCES=['t27/rtl/gf16_scalar.t27','tools/gf16_wide_build.py','t27/rtl/gf16_ffn.t27','t27/rtl/gf16_wide_norm.t27','t27/rtl/ffn_wide.t27',
       'rtl/t27/gf16_ffn.v','rtl/t27/gf16_wide_norm.v','tools/gf16_ffn_reference.py',
       'tools/gf16_ffn_vectors.py','tools/gf16_ffn_build.py','tools/replay_gf16_ffn.py','tests/tb_gf16_ffn.v',
 'tools/gf16_reference.py','tools/gf16_wide_reference.py','tools/ffn_reference.py',
 'tools/ffn_vectors.py','tools/bitnet_ffn_runtime.py','tools/capture_bitnet_layer0.py']

def sha(data):return hashlib.sha256(data).hexdigest()


def replay(report_path, captures, work, rtl_mode):
    source_hashes={p:sha((ROOT/p).read_bytes()) for p in SOURCES}
    import numpy as np
    import torch
    import transformers
    from transformers import BitNetConfig,BitNetQuantConfig
    from transformers.models.bitnet.modeling_bitnet import BitNetDecoderLayer
    from transformers.integrations.bitnet import replace_with_bitnet_linear
    from tools import bitnet_ffn_runtime as rt, ffn_reference as old
    for name,version in capture.VERSIONS.items():
        if importlib.metadata.version(name).split('+')[0]!=version:
            raise ValueError('runtime changed: '+name)
    for name,digest in capture.UPSTREAM.items():
        if sha((Path(transformers.__file__).parent/name).read_bytes())!=digest:
            raise ValueError('upstream source changed: '+name)
    report=json.loads(report_path.read_text())
    if report['input_lock_sha256']!=sha(capture.LOCK.read_bytes()):
        raise ValueError('input lock changed')
    for name,digest in report['source_sha256'].items():
        if sha((ROOT/name).read_bytes())!=digest:raise ValueError('capture source changed: '+name)
    arrays=np.load(captures,allow_pickle=False)
    for c in report['cases']:
        for k,digest in c['capture_sha256_f32'].items():
            if sha(arrays[f'prompt{c["prompt_index"]}_{k}'].astype('<f4').tobytes())!=digest:
                raise ValueError('captured tensor hash mismatch')
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
    model=old.load_ffn()
    for name in ('gate','up','down'):
        actual=getattr(layer.mlp,name+'_proj').weight.detach().float().numpy().astype(np.int8)
        if not np.array_equal(np.array(model[name],dtype=np.int8).reshape(model['shapes'][name]),actual):
            raise ValueError('fixture/runtime ternary matrix mismatch: '+name)
    for name in ('w_post','w_sub'):
        model[name]=rt.gf16_words(np.asarray(model[name],dtype=np.float32)).tolist()
    model['scales']={k:int(rt.gf16_words(np.array([v],dtype=np.float32))[0]) for k,v in model['scales'].items()}
    for name,weight in (('w_post',layer.post_attention_layernorm.weight),('w_sub',layer.mlp.ffn_sub_norm.weight)):
        if model[name]!=rt.gf16_words(weight.detach().float().numpy()).tolist():
            raise ValueError('fixture/runtime norm weight mismatch')
    for name in ('gate','up','down'):
        actual=int(rt.gf16_words(getattr(layer.mlp,name+'_proj').weight_scale.detach().float().numpy().reshape(-1))[0])
        if model['scales'][name]!=actual:raise ValueError('fixture/runtime scale mismatch')
    work.mkdir(parents=True,exist_ok=True)
    command=None;rtl_results=[];cases=[]
    if rtl_mode!='none':
        rtl,_=build.generate(work/'generated')
        command=build.compile_sim(work/'simulator',rtl,(2560,6912,2560),'verilator')
    with torch.no_grad():
        for prompt,tokens in enumerate(ids):
            x=torch.from_numpy(arrays[f'prompt{prompt}_x'])
            bf,_,_=rt.explicit_ffn(layer,x,'bf16')
            for k,v in bf.items():
                if not torch.equal(v.view(torch.int32),torch.from_numpy(arrays[f'prompt{prompt}_{k}']).view(torch.int32)):
                    raise ValueError('BF16 calibration mismatch')
            fp,_,_=rt.explicit_ffn(layer,x,'f32')
            sw,_,sw_codes=rt.explicit_ffn(layer,x,'gf16',wide_product=True)
            xwords=rt.gf16_words(x[0].numpy());stage_rows={k:[] for k in ('h','g','u','a','s','y')}
            code_rows={'h':[],'s':[]};details=[]
            for token,words in enumerate(xwords):
                words=words.tolist();result=ref.evaluate(model,words)
                details.append({'token':token,'x_sha256_u16':sha(np.array(words,dtype='<u2').tobytes()),
                    'stage_sha256_u32':{k:sha(np.array(v,dtype='<u4').tobytes()) for k,v in result['stages'].items()},
                    'actquant_sha256_u16':{k:sha(np.array(v,dtype='<u2').tobytes()) for k,v in result['actquant'].items()},
                    'code_sha256_i8':{k:sha(np.array(v,dtype=np.int8).tobytes()) for k,v in result['codes'].items()}})
                for k,v in result['stages'].items():
                    stage_rows[k].append(np.array(v,dtype=np.uint32).view(np.float32) if k=='a' else rt.DECODE[v])
                for k,v in result['codes'].items():code_rows[k].append(v)
                selected=rtl_mode=='all' or (rtl_mode=='selected' and (token==len(tokens)-1 or (prompt==0 and token==0)))
                if selected:
                    casework=work/f'prompt-{prompt}-token-{token}';casework.mkdir(parents=True,exist_ok=True)
                    inp=vectors.write_inputs(casework,model,words)
                    (casework/'reference.json').write_text(json.dumps(result)+'\n')
                    sim=build.run_sim(casework,command,inp,result)
                    rtl_results.append({'prompt':prompt,'token':token,**sim})
                print(f'prompt {prompt} token {token}: integer oracle'+(' + full RTL PASS' if selected else ' PASS'),flush=True)
            new={k:torch.from_numpy(np.array(v))[None] for k,v in stage_rows.items()}
            cases.append({'prompt':prompt,'tokens':len(tokens),'rows':details,
                'vs_previous_gf16':{k:rt.metrics(sw[k],v) for k,v in new.items()},
                'vs_f32_actquant':{k:rt.metrics(fp[k],v) for k,v in new.items()},
                'bf16_y_vs_f32':rt.metrics(fp['y'],bf['y']),
                'actquant_changes_vs_previous':{k:int((torch.tensor(code_rows[k],dtype=torch.int8)[None]!=sw_codes[t]).sum())
                                              for k,t in (('h','g'),('s','y'))}})
    arrays.close()
    if source_hashes != {p:sha((ROOT/p).read_bytes()) for p in SOURCES}:
        raise ValueError('source changed during GF16 FFN replay')
    return {'schema':'trinity.gf16-ffn.v1','profile':ref.PROFILE,
        'scope':'complete layer-0 FFN arithmetic; no attention, residual, later layers, logits or generation-quality claim',
        'source_sha256':source_hashes,
        'compiler':(ROOT/'native/compiler.lock').read_text().strip(),'runtime':capture.VERSIONS,'machine':platform.machine(),
        'capture_report_sha256':sha(report_path.read_bytes()),'input_lock_sha256':report['input_lock_sha256'],
        'cases':cases,'rtl_mode':rtl_mode,'rtl':rtl_results}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--capture-report',type=Path,default=ROOT/'reports/numeric/bitnet-layer0-runtime.json')
    ap.add_argument('--captures',type=Path,default=capture.CACHE/'captures.npz')
    ap.add_argument('--work',type=Path,default=ROOT/'build/gf16-ffn/replay')
    ap.add_argument('--rtl',choices=('none','selected','all'),default='selected')
    ap.add_argument('--output',type=Path,default=ROOT/'reports/numeric/gf16-ffn.json')
    args=ap.parse_args();result=replay(args.capture_report,args.captures,args.work.resolve(),args.rtl)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n');print(args.output)


if __name__=='__main__':main()
