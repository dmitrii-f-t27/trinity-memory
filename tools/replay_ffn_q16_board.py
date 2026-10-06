#!/usr/bin/env python3
"""Offline replay of the retained Q16 FFN board runs of issue #92.

Reads only files committed under reports/fpga/ffn-q16-2026-09-29-eecc619f/.
Never opens a serial port, a JTAG cable or the network.

Recomputed from raw retained data:
  * the compressed UART capture of each run (seed27, zero) is parsed and every
    one of its 32768 stage values, the saturation counters, the order of the
    lines, the clock counter and the doorbell acknowledgement are compared with
    the retained integer reference through ffn_vectors.validate();
  * the SHA-256 of the capture, the qualification payload and every payload
    named in inputs.json are recomputed or compared with the receipts.
Counted from the host's per-chunk loader receipts (the raw readback streams
*.rx.bin.gz are not retained, so the CRC readback itself is not re-decoded):
  * every chunk acknowledged, every readback chunk crc_ok, readback identical.
"""
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import ffn_vectors as fv  # noqa: E402

DIR = ROOT / 'reports/fpga/ffn-q16-2026-09-29-eecc619f'
RUNS = (('seed27', 'vectors-seed27', 1), ('zero', 'vectors-zero', 2))
LOADS = ('qualification', 'gate', 'up', 'down', 'scales', 'post', 'sub', 'x')
SOURCE_COMMIT = 'eecc619f6e2bc49958f969b455ac67f8158be5d9'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def need(condition, message):
    if not condition:
        raise ValueError(message)


def gz_json(path):
    return json.loads(gzip.open(path).read())


def receipt(run_dir, name, regions):
    path = run_dir / ('qualification.json.gz' if name == 'qualification' else f'load-{name}.json.gz')
    r = gz_json(path)
    need(r['schema'] == 'trinity.uart-loader-capture.v2' and r['pass'] is True, f'{path.name}: not a passing receipt')
    need(all(r['checks'].values()) and r['checks'], f'{path.name}: a receipt check failed')
    need(not any(r['faults'].values()), f'{path.name}: faults were injected')
    need(r['tool_revision']['tracked_changes'] is False, f'{path.name}: tool tree was dirty')
    p, t = r['payload'], r['transfer']
    if name == 'qualification':
        data = bytes(range(256)) * 128           # fully recomputable: fpga-ffn-run.py writes exactly this
        need(p['bytes'] == len(data) and p['sha256'] == sha(data) and r['addr'] == 0x2000000, 'qualification payload')
    else:
        region = regions[name]
        need(p['bytes'] == region['bytes'] and p['sha256'] == region['sha256'], f'{name}: payload differs from inputs.json')
        need(r['addr'] == region['byte_address'], f'{name}: address differs from inputs.json')
    chunk = r['chunk']
    chunks = -(-p['bytes'] // chunk)
    need(t['chunks'] == t['acked'] == chunks and t['payload_bytes'] == p['bytes'], f'{name}: chunk count')
    need(t['identical'] is True and t['first_mismatch'] is None and t['retransmits'] == {}, f'{name}: readback not identical')
    need(t['readback_bytes'] == p['bytes'], f'{name}: readback length')
    per = t['per_chunk']
    need(len(per) == chunks, f'{name}: per-chunk records')
    for i, c in enumerate(per):
        need(c['index'] == i and c['addr'] == r['addr'] + i * chunk and c['acked'] is True and c['reason'] == 'ok'
             and len(c['attempts']) == 1 and c['attempts'][0]['reply']['check_ok'] is True
             and c['attempts'][0]['reply']['reason'] == 0, f'{name}: chunk {i} not acknowledged first try')
    reads = t['reads']
    need(len(reads) == chunks, f'{name}: readback chunk count')
    covered = 0
    for i, c in enumerate(reads):
        last = c['attempts'][-1]['reply']
        need(c['addr'] == r['addr'] + covered and last['kind'] == 'readback' and last['crc_ok'] is True
             and last['len'] == c['len'], f'{name}: readback chunk {i} CRC')
        covered += c['len']
    need(covered == p['bytes'], f'{name}: readback does not cover the payload')
    return chunks


def replay_run(run, vectors, run_id, evidence=DIR):
    """One run directory of `evidence`, against the stage 5 vectors (vectors is a path under DIR)."""
    run_dir, vec_dir = evidence / run, DIR / vectors
    regions = json.loads((vec_dir / 'inputs.json').read_text())
    reference = gz_json(vec_dir / 'reference.json.gz')
    need(reference['run'] == run_id and reference['shape'] == [2560, 6912, 2560], f'{run}: reference identity')
    raw = gzip.open(run_dir / 'capture.txt.gz').read()
    outcome = fv.validate(raw, reference['expected'], reference['saturations'], run_id)
    need(fv.doorbell_acknowledged(raw), f'{run}: doorbell not acknowledged')
    result = json.loads((run_dir / 'result.json').read_text())
    need(result['pass'] and result['doorbell_ack'] and result['capture_sha256'] == outcome['capture_sha256']
         and result['capture_bytes'] == outcome['capture_bytes'] and result['cycles'] == outcome['cycles']
         and result['saturations'] == outcome['saturations'], f'{run}: result.json differs from the recomputed capture')
    need(sum(outcome['saturations'].values()) == 0, f'{run}: saturations')
    if 'clock_split' in outcome:
        need(result['clock_split'] == outcome['clock_split'], f'{run}: clock split differs from the recomputed one')
    boot = json.loads((evidence / 'boot/boot.json').read_text())
    command = json.loads((run_dir / 'command.json').read_text())
    need(result['bitstream_sha256'] == boot['bitstream_sha256'] == command['bitstream_sha256'], f'{run}: bitstream identity')
    chunks = sum(receipt(run_dir, name, regions) for name in LOADS)
    return outcome['stage_values'], len(LOADS), chunks, reference['packed_sha256'], outcome.get('clock_split')


def source_identity():
    """The measured RTL/host sources are those of eecc619f when the full history is present."""
    manifest = json.loads((DIR / 'source-manifest.json').read_text())
    need(manifest['hardware_source_commit'] == SOURCE_COMMIT, 'source manifest commit')
    exists = subprocess.run(['git', 'cat-file', '-e', SOURCE_COMMIT + '^{commit}'], cwd=ROOT, capture_output=True)
    if exists.returncode:
        return False  # shallow checkout: the manifest alone is bound by evidence-manifest.json
    for name, digest in manifest['sha256'].items():
        blob = subprocess.run(['git', 'show', f'{SOURCE_COMMIT}:{name}'], cwd=ROOT, capture_output=True, check=True).stdout
        need(sha(blob) == digest, 'source manifest differs from the commit: ' + name)
    return True


def replay():
    totals, packed = [0, 0, 0, 0], set()
    for run, vectors, run_id in RUNS:
        values, loads, chunks, pk, _ = replay_run(run, vectors, run_id)
        totals[0] += 1; totals[1] += values; totals[2] += loads; totals[3] += chunks
        packed.add(json.dumps(pk, sort_keys=True))
    need(len(packed) == 1, 'the two runs used different weights')
    return {'runs': totals[0], 'stage_values': totals[1], 'loads': totals[2], 'chunks': totals[3],
            'source_blobs_checked_in_git': source_identity()}


if __name__ == '__main__':
    print(json.dumps(replay()))
