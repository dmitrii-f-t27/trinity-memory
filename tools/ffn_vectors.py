"""Issue #92: host vector framing and capture validation, not FPGA arithmetic.

Numerical references come from the corrected ffn_reference oracle. Weight
encoding uses the existing matvec device codec. This module never opens UART.
"""
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ffn_reference as fr
import matvec_device_model as device
import uart_loader_protocol as proto

MAGIC = 0x46464e31
STAGES = ('h', 'g', 'u', 'a', 's', 'y')


def doorbell_acknowledged(raw):
    # Feed only complete A lines to the loader decoder: FFN numerical lines use
    # a different protocol and a large unknown stream makes that parser slow.
    for line in raw.splitlines(keepends=True):
        if line.startswith(b'A'):
            for event in proto.StreamDecoder().feed(line):
                if event.kind == 'line' and event.tag == 'A' and event.check_ok:
                    fields = event.resp()
                    if fields['seq'] == 1 and fields['cmd'] == proto.CMD_LOAD and fields['reason'] == 0:
                        return True
    return False


def regions(model, x, run=1):
    h, i = model['shapes']['gate'][1], model['shapes']['gate'][0]
    o = model['shapes']['down'][0]
    if not (0 < h <= 2560 and 0 < i <= 6912 and 0 < o <= 2560 and len(x) == h):
        raise ValueError('unsupported shape or input length')
    if model['shapes']['up'] != [i,h] or model['shapes']['down'][1] != i:
        raise ValueError('inconsistent FFN shapes')
    if len(model['w_post']) != h or len(model['w_sub']) != i or not 0 < run < 2**32:
        raise ValueError('norm shape or run ID invalid')
    def words32(values):
        if any(not -(1<<31) <= v < (1<<31) for v in values):
            raise ValueError('coefficient/input outside signed Q16.16')
        return [v & 0xffffffff for v in values]
    result = {'doorbell': (64, [run | (MAGIC<<32)]),
              'scales': (80, words32([fr.to_q(model['scales'][s]) for s in ('gate','up','down')])),
              'x': (4096, words32(x)),
              'post': (8192, words32([fr.to_q(v) for v in model['w_post']])),
              'sub': (12288, words32([fr.to_q(v) for v in model['w_sub']]))}
    for name, base in [('gate',65536),('up',393216),('down',720896)]:
        rows, cols = model['shapes'][name]
        if len(model[name]) != rows*cols:
            raise ValueError('weight payload shape mismatch')
        words=[]
        for row in range(rows):
            values=model[name][row*cols:(row+1)*cols] + [0]*(-cols % 64)
            for offset in range(0,len(values),64):
                lo,hi=device.encode_word(0,device.lane_word(values,offset))
                words.append(lo | (hi<<64))
        result[name]=(base,words)
    return result


def write_inputs(work, model, x, run=1):
    work=Path(work)
    manifest={}
    with (work/'input.mem').open('w') as memory:
        for name,(base,words) in regions(model,x,run).items():
            payload=b''.join(v.to_bytes(16,'little') for v in words)
            path=work/(name+'.bin'); path.write_bytes(payload)
            manifest[name]={'word_address':base,'byte_address':base*16,'file':path.name,
                            'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}
            memory.write(f'@{base:x}\n')
            memory.writelines(f'{word:032x}\n' for word in words)
    (work/'inputs.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return work/'input.mem'


def validate(raw, expected, sats, run=1):
    if not raw or any(re.fullmatch(rb'[A-Za-z][0-9a-fA-F]{18}\n',line) is None
                      for line in raw.splitlines(keepends=True)):
        raise ValueError('malformed or truncated UART line')
    lines=[(tag.decode(),int(a,16),int(b,16)) for tag,a,b in
           re.findall(rb'([A-Za-z])([0-9a-fA-F]{8})([0-9a-fA-F]{10})\n',raw)]
    def require(condition, message):
        if not condition: raise ValueError(message)
    require([(a,b) for t,a,b in lines if t=='d']==[(run,0)],'missing/duplicate run start')
    require([(a,b) for t,a,b in lines if t=='z']==[(run,0)],'missing/duplicate completion')
    require(not any(t=='E' for t,_,_ in lines),'device reported fatal error')
    require(all(t in 'FdchHgGuUaJsSyYtzA' for t,_,_ in lines),'unexpected UART tag')
    require(len([b for t,a,b in lines if t=='c'])==1,'missing/duplicate clock counter')
    require([(i,v) for t,i,v in lines if t=='t']==list(enumerate(sats[s] for s in STAGES)),
            'saturation counts differ')
    for stage in STAGES:
        for tag,shift in [(stage,0),('J' if stage=='a' else stage.upper(),32)]:
            got=[(i,v) for t,i,v in lines if t==tag]
            want=[(i,(v>>shift)&0xffffffff) for i,v in enumerate(expected[stage])]
            require(got==want,f'{stage} half {shift}: values/order/count differ')
    sequence=[('d',run)]
    for stage in STAGES:
        sequence.extend((t,i) for i in range(len(expected[stage]))
                        for t in (stage,'J' if stage=='a' else stage.upper()))
    sequence.extend([('c',run),*[('t',i) for i in range(6)],('z',run)])
    require([(t,i) for t,i,_ in lines if t not in ('F','A')]==sequence,'stage/report order differs')
    return {'pass':True,'run':run,'stage_values':sum(len(expected[s]) for s in STAGES),
            'capture_bytes':len(raw),'capture_sha256':hashlib.sha256(raw).hexdigest(),
            'saturations':sats,'cycles':[b for t,a,b in lines if t=='c']}
