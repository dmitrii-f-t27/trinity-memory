"""GF16 FFN DDR3 framing and strict trace validation (no device access)."""
import hashlib
import json
from pathlib import Path
import re
from tools import gf16_wide_reference as wide

MAGIC = 0x47464631


def regions(model, x, run=1):
    inner, hidden = model['shapes']['gate']
    output, dc = model['shapes']['down']
    if (not 1 <= hidden <= 2560 or not 1 <= inner <= 6912 or not 1 <= output <= 2560 or
            dc != inner or model['shapes']['up'] != [inner, hidden] or len(x) != hidden or
            len(model['w_post']) != hidden or len(model['w_sub']) != inner or not 0 < run < 2**32):
        raise ValueError('GF16 FFN shape/run mismatch')
    result = {'doorbell': (64, [run | (MAGIC << 32)]),
              'scales': (80, [model['scales'][s] for s in ('gate','up','down')]),
              'x': (4096, x), 'post': (8192, model['w_post']), 'sub': (12288, model['w_sub'])}
    for name, (_, words) in result.items():
        if name != 'doorbell' and not all(map(wide.finite, words)):
            raise ValueError('nonfinite GF16 input: '+name)
    for name, base in (('gate',65536),('up',393216),('down',720896)):
        rows, cols = model['shapes'][name]
        weights = model[name]
        if len(weights) != rows*cols:
            raise ValueError('weight shape mismatch')
        words = []
        for row in range(rows):
            for column in range(0, cols, 64):
                word = 0
                for lane in range(min(64, cols-column)):
                    value = weights[row*cols+column+lane]
                    if value not in (-1,0,1):
                        raise ValueError('invalid ternary weight')
                    word |= {0:0,1:1,-1:2}[int(value)] << (lane*2)
                words.append(word)
        result[name] = (base, words)
    return result


def write_inputs(work, model, x, run=1):
    work = Path(work); work.mkdir(parents=True, exist_ok=True)
    manifest = {}
    with (work/'input.mem').open('w') as memory:
        for name, (base, words) in regions(model,x,run).items():
            payload = b''.join(int(v).to_bytes(16,'little') for v in words)
            path = work/(name+'.bin'); path.write_bytes(payload)
            manifest[name] = {'word_address':base,'byte_address':base*16,'file':path.name,
                              'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}
            memory.write(f'@{base:x}\n')
            memory.writelines(f'{int(word):032x}\n' for word in words)
    (work/'inputs.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return work/'input.mem'


def expected_lines(result, run=1):
    s = result['stages']; sequence = [('d',run,1)]
    for stage in ('h','p','g','u','sub','v','y'):
        if stage == 'sub':
            for i, (a,b) in enumerate(zip(s['a'],s['s'])):
                sequence.extend([('a',i,a),('s',i,b)])
        elif stage in ('p','v'):
            source = 'h' if stage == 'p' else 's'
            sequence.extend((stage,i,v | ((c & 255) << 16)) for i,(v,c) in
                            enumerate(zip(result['actquant'][source],result['codes'][source])))
        else:
            sequence.extend((stage,i,v) for i,v in enumerate(s[stage]))
    return sequence


def validate(raw, result, run=1):
    if not raw or any(re.fullmatch(rb'[A-Za-z][0-9a-fA-F]{18}\n', line) is None
                      for line in raw.splitlines(keepends=True)):
        raise ValueError('malformed or truncated GF16 UART line')
    lines = [(t.decode(),int(a,16),int(b,16)) for t,a,b in
             re.findall(rb'([A-Za-z])([0-9a-fA-F]{8})([0-9a-fA-F]{10})\n',raw)]
    headers = [(a,b) for t,a,b in lines if t=='G']
    shape = (len(result['stages']['h']),len(result['stages']['g']))
    if headers and headers != [shape]:
        raise ValueError('wrong GF16 header')
    if any(t == 'E' for t,_,_ in lines):
        raise ValueError('GF16 device reported error')
    data = [v for v in lines if v[0] not in ('G','A')]
    wanted = expected_lines(result,run)
    if data[:len(wanted)] != wanted:
        for i,(got,want) in enumerate(zip(data,wanted)):
            if got != want:
                raise ValueError(f'GF16 trace item {i}: {got} != {want}')
        raise ValueError('incomplete GF16 trace')
    summary = data[len(wanted):]
    if (len(summary) != 4 or [v[:2] for v in summary] !=
            [('c',run),('k',0),('k',1),('z',run)] or summary[-1][2] != 0):
        raise ValueError('GF16 completion/counters missing, duplicated or out of order')
    total, report, memory = [v[2] for v in summary[:3]]
    if report+memory > total:
        raise ValueError('clock accounting exceeds total')
    return {'pass':True,'run':run,'profile':'gf16-ffn-v1',
            'stage_values':sum(map(len,result['stages'].values())),
            'actquant_values':sum(map(len,result['actquant'].values())),
            'capture_sha256':hashlib.sha256(raw).hexdigest(),
            # The norm engine overlaps its next lane with UART reporting.
            # This residual includes waiting for that engine, not pure work.
            'clock_split':{'total':total,'report_wait':report,'memory_wait':memory,'controller_other':total-report-memory}}


def validate_board_inputs(folder, manifest, result, run=1):
    """Reject profile/layout mistakes before opening UART or JTAG."""
    if result.get('profile') != 'gf16-ffn-v1' or not 0 < run < 2**32:
        raise ValueError('GF16 reference profile/run required')
    if [len(result['stages'][s]) for s in ('h','g','u','a','s','y')] != [2560,6912,6912,6912,6912,2560]:
        raise ValueError('GF16 board dimensions differ')
    for stage, size in (('h',2560),('s',6912)):
        if len(result['actquant'][stage]) != size or len(result['codes'][stage]) != size:
            raise ValueError('GF16 ActQuant dimensions differ')
    bases=dict(doorbell=64,scales=80,x=4096,post=8192,sub=12288,gate=65536,up=393216,down=720896)
    counts=dict(doorbell=1,scales=3,x=2560,post=2560,sub=6912,gate=276480,up=276480,down=276480)
    if set(manifest) != set(bases):raise ValueError('GF16 region manifest differs')
    for name, region in manifest.items():
        if (region['word_address'] != bases[name] or region['byte_address'] != 16*bases[name] or
                region['bytes'] != 16*counts[name] or region['file'] != name+'.bin'):
            raise ValueError('GF16 region layout differs: '+name)
        data=(Path(folder)/region['file']).read_bytes()
        if len(data) != region['bytes'] or hashlib.sha256(data).hexdigest() != region['sha256']:
            raise ValueError('GF16 input payload hash mismatch: '+name)
        if name == 'doorbell':
            if int.from_bytes(data,'little') != (run | (MAGIC << 32)):
                raise ValueError('GF16 doorbell differs')
        elif name in ('x','scales','post','sub'):
            if any(not wide.finite(int.from_bytes(data[i:i+16],'little')) for i in range(0,len(data),16)):
                raise ValueError('invalid GF16 input word: '+name)
