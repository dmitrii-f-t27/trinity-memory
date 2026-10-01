"""Portable, strict verification of committed GF16 performance captures."""
import gzip
import hashlib
import json
from pathlib import Path
import sys
import struct

BASE=Path(__file__).resolve().parent
ROOT=BASE.parents[2]
sys.path.insert(0,str(ROOT))
from tools import gf16_ffn_vectors as vectors

reference_bytes=gzip.decompress((BASE/'reference-bos.json.gz').read_bytes())
reference=json.loads(reference_bytes)
report=json.loads((ROOT/'reports/numeric/gf16-ffn-performance.json').read_text())
assert report['kind']=='simulation'
shape=(len(reference['stages']['h']),len(reference['stages']['g']),len(reference['stages']['y']))
h,i,o=shape
trits=2*h*i+o*i
words=2*i*((h+63)//64)+o*((i+63)//64)
for variant,record in report['variants'].items():
    assert record['reference_sha256']==hashlib.sha256(reference_bytes).hexdigest()
    assert tuple(record['shape'])==shape
    for mode in ('full','result'):
        raw=gzip.decompress((BASE/'simulation'/variant/mode/'capture.txt.gz').read_bytes())
        checked=vectors.validate(raw,{**reference,'trace':mode},reference['run'])
        assert checked==record['runs'][mode],(variant,mode)
        expected=5*trits if variant=='baseline' else 3*trits+2*words
        assert checked['projection_loop_clocks']==expected
        print(variant,mode,'PASS',checked['stage_values'],'stage,',checked['actquant_values'],'AQ; active',checked['clock_split']['total'])
a=report['variants']['baseline'];b=report['variants']['accelerated']
assert a['input_payloads']==b['input_payloads']
for mode in ('full','result'):
    old=a['runs'][mode];new=b['runs'][mode]
    assert old['clock_split']['total']>new['clock_split']['total']
    assert report['ratios'][mode]=={
        'active_clocks_baseline_over_accelerated':old['clock_split']['total']/new['clock_split']['total'],
        'projection_loop_baseline_over_accelerated':old['projection_loop_clocks']/new['projection_loop_clocks']}

replay=json.loads((ROOT/'reports/numeric/gf16-ffn-performance-replay.json').read_text())
prior=json.loads((ROOT/'reports/numeric/gf16-ffn.json').read_text())
assert [c['rows'] for c in replay['cases']]==[c['rows'] for c in prior['cases']]
assert sum(len(c['rows']) for c in replay['cases'])==45
assert len(replay['rtl'])==5
for path,digest in replay['source_sha256'].items():
    assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest,path
for record in replay['rtl']:
    folder=BASE/'simulation/replay'/f"prompt-{record['prompt']}-token-{record['token']}"
    raw=gzip.decompress((folder/'capture.txt.gz').read_bytes())
    ref=json.loads(gzip.decompress((folder/'reference.json.gz').read_bytes()))
    checked=vectors.validate(raw,ref,record['run'])
    assert checked=={k:v for k,v in record.items() if k not in ('prompt','token')}
    row=replay['cases'][record['prompt']]['rows'][record['token']]
    for key,group,fmt in [('stage_sha256_u32','stages','I'),('actquant_sha256_u16','actquant','H'),('code_sha256_i8','codes','b')]:
        for name,values in ref[group].items():
            assert hashlib.sha256(struct.pack('<'+fmt*len(values),*values)).hexdigest()==row[key][name]
    print(folder.name,'exact replay PASS')
print('45 input rows unchanged; all replay source hashes match the current tree')

linux_path=ROOT/'reports/numeric/gf16-ffn-performance-replay-linux.json'
if linux_path.exists():
    linux=json.loads(linux_path.read_text())
    assert linux['machine']=='x86_64' and replay['machine']=='arm64'
    assert linux['source_sha256']==replay['source_sha256']
    assert [c['rows'] for c in linux['cases']]==[c['rows'] for c in replay['cases']]
    assert linux['rtl']==replay['rtl']
    measured=json.loads((ROOT/'reports/numeric/gf16-ffn-performance-linux.json').read_text())
    assert measured['sources_sha256']==b['sources_sha256']
    for mode in ('full','result'):
        for key in ('clock_split','phase_clocks','projection_loop_clocks','stage_values','actquant_values'):
            assert measured['runs'][mode][key]==b['runs'][mode][key]
    print('Linux x86_64 / macOS arm64 exact replay and phase-counter parity PASS')
