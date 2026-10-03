"""Offline replay of retained attention simulation and physical raw evidence.

Run from any directory with Python and this repository. No hardware, compiler,
model download or fixture cache is used. Expected payloads are de-duplicated
in the archive, while every independent physical readback stream is retained.
"""
from pathlib import Path, PurePosixPath
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
sys.path.insert(0,str(ROOT))
from tools import gf16_attn_vectors as vectors


def sha(data):return hashlib.sha256(data).hexdigest()


def inside(root,name):
    p=PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts:
        raise ValueError('unsafe evidence member '+name)
    return root.joinpath(*p.parts)


def main():
    manifest=json.loads((HERE/'evidence-manifest.json').read_text())
    assert manifest['schema']=='trinity-attention-board-evidence-v1'
    assert manifest['archive']['file']=='board-evidence.tar.gz'
    archive=HERE/manifest['archive']['file']
    assert archive.stat().st_size==manifest['archive']['bytes']
    assert sha(archive.read_bytes())==manifest['archive']['sha256']
    expected=manifest['files']
    with tempfile.TemporaryDirectory(prefix='attn-evidence-') as tmp:
        work=Path(tmp);seen=set()
        with tarfile.open(archive,'r:gz') as tf:
            for member in tf:
                assert member.isfile() and member.name in expected and member.name not in seen
                data=tf.extractfile(member).read();rec=expected[member.name]
                assert len(data)==rec['bytes'] and sha(data)==rec['sha256'],member.name
                p=inside(work,member.name);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data);seen.add(member.name)
        assert seen==set(expected)
        for name,h in manifest['payload_links'].items():
            assert re.fullmatch('[0-9a-f]{64}',h)
            p=inside(work,name);assert not p.exists() and name.startswith('vectors/')
            source=work/'payloads'/(h+'.bin');assert sha(source.read_bytes())==h
            p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,p)
        bit_sha=sha((work/'build/attention.bit').read_bytes())
        build=json.loads((work/'build/build.json').read_text())
        boot=json.loads((work/'boot/boot.json').read_text())
        assert build['ddr3']['source_tree']['head']==manifest['hardware_commit']
        assert not build['ddr3']['source_tree']['dirty']
        assert build['commit']==manifest['hardware_commit'][:8]
        assert build==json.loads((HERE/'build-2e2e336c/build.json').read_text())
        assert bit_sha==manifest['bitstream_sha256']==build['bitstream']['sha256']==boot['bitstream_sha256']
        assert build['nextpnr']['routed'] and build['nextpnr']['clocks_routed']
        assert all(c['verdict']=='PASS' and c['target_mhz']==60 for c in build['nextpnr']['clocks_routed'])
        assert all(boot['checks'].values()) and boot['checks']['gf16_attn_header']
        assert boot['run']['dna']=='0x00389c0c2d85e85c'
        assert all(0<boot['run'][k]['temp']<70 and boot['run'][k]['maxtemp']<70 for k in ['xadc_before','xadc_after'])
        assert boot['uart_sha256']==sha((work/'boot/uart-raw.txt').read_bytes())
        sims=json.loads((work/'simulation/results.json').read_text())
        assert {r['case'] for r in sims}=={c+'-'+m for c in manifest['cases'] for m in ['full','result']}
        for r in sims:
            assert r['pass'] and not any(r['saturations'].values())
            for name,h in r['source_sha256'].items():
                assert sha((work/'source'/name).read_bytes())==h
            case,mode=r['case'].split('-');ref=json.loads((work/'vectors'/case/'reference.json').read_text())
            checked=vectors.validate((work/'simulation'/r['case']/'capture.txt').read_bytes(),ref['stages'],r['run'],r['positions'],mode=='result')
            assert all(r[k]==v for k,v in checked.items()),r['case']
        controls=json.loads((work/'simulation/mapped-control-results.json').read_text())
        assert len(controls)==4 and all(r['pass'] for r in controls)
        print('Eight source-hashed full-dimension simulations PASS',flush=True)
        published=json.loads((HERE/'physical-results.json').read_text())
        assert set(published)==set(manifest['cases'])
        for case in manifest['cases']:
            output=work/('rechecked-'+case+'.json')
            subprocess.run([sys.executable,str(ROOT/'tools/verify_gf16_attn_board.py'),
                '--run',str(work/'runs'/case),'--vectors',str(work/'vectors'/case),
                '--boot',str(work/'boot/boot.json'),'--output',str(output)],cwd=ROOT,check=True)
            fresh=json.loads(output.read_text());saved=json.loads((work/'verified'/(case+'.json')).read_text())
            assert saved==published[case]
            assert saved['tool_commit']==manifest['hardware_commit']
            # Permit future verifier code changes, but require every retained
            # measurement, raw hash and coverage result to reproduce exactly.
            fresh.pop('verifier_sha256');saved.pop('verifier_sha256')
            assert fresh==saved,case
        print('PASS: four physical full/result pairs, raw CRC payload coverage, timing, boot and simulations')


if __name__=='__main__':main()
