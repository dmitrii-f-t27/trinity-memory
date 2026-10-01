"""Verify the compact, committed board captures without hardware or weight files."""
import gzip
import hashlib
import json
from pathlib import Path
import struct
import sys

BASE=Path(__file__).resolve().parent
ROOT=BASE.parents[2]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tools'))
from tools import gf16_ffn_vectors as vectors
from tools.ffn_vectors import doorbell_acknowledged


def read(folder,name):
    path=folder/name
    return path.read_bytes() if path.exists() else gzip.decompress((folder/(name+'.gz')).read_bytes())


boot=json.loads(read(BASE/'boot','boot.json'))
build=json.loads(read(BASE/'build','build.json'))
assert all(boot['checks'].values()) and boot['checks']['gf16_ffn_header']
assert all(c['verdict']=='PASS' and c['target_mhz']==60 for c in build['nextpnr']['clocks_routed'])
assert boot['bitstream_sha256']==build['bitstream']['sha256']
numeric=json.loads((ROOT/'reports/numeric/gf16-ffn.json').read_text())
for name in (sys.argv[1:] or ('bos','zero')):
    folder=BASE/name;ref=json.loads(read(folder,'reference.json'))
    raw=read(folder,'capture.txt');result=json.loads(read(folder,'result.json'))
    checked=vectors.validate(raw,ref,ref['run'])
    assert doorbell_acknowledged(raw)
    assert all(result[k]==v for k,v in checked.items())
    assert result['bitstream_sha256']==boot['bitstream_sha256'] and result['capture_baud']==460800
    independent=json.loads(read(folder,'independent-verification.json'))
    assert all(independent[k]==v for k,v in checked.items())
    assert independent['reference_sha256']==hashlib.sha256(read(folder,'reference.json')).hexdigest()
    assert independent['manifest_sha256']==hashlib.sha256(read(folder,'inputs.json')).hexdigest()
    assert independent['hardware_peak_temperature_c']<70
    if name=='bos':
        row=numeric['cases'][0]['rows'][0]
        for stage,values in ref['stages'].items():
            assert hashlib.sha256(struct.pack('<'+'I'*len(values),*values)).hexdigest()==row['stage_sha256_u32'][stage]
        for stage,values in ref['actquant'].items():
            assert hashlib.sha256(struct.pack('<'+'H'*len(values),*values)).hexdigest()==row['actquant_sha256_u16'][stage]
            codes=ref['codes'][stage]
            assert hashlib.sha256(struct.pack('<'+'b'*len(codes),*codes)).hexdigest()==row['code_sha256_i8'][stage]
    print(name,'PASS',checked['stage_values'],'stage +',checked['actquant_values'],'ActQuant values')
