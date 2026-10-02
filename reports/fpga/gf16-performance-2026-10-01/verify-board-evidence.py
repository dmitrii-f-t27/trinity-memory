"""Verify compact physical captures and receipts without a board or weight files.

Full DDR byte reconstruction is performed by tools/verify_gf16_ffn_board.py
against local payloads/raw streams; its hashed receipts are checked here.
"""
import gzip
import hashlib
import json
from pathlib import Path
import re
import struct
import sys

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
from tools import gf16_ffn_vectors as vectors
from tools.ffn_vectors import doorbell_acknowledged


def read(folder, name):
    path = folder / name
    return path.read_bytes() if path.exists() else gzip.decompress((folder / (name + '.gz')).read_bytes())


def document(folder, name):
    return json.loads(read(folder, name))


def digest(data):
    return hashlib.sha256(data).hexdigest()


numeric = json.loads((ROOT / 'reports/numeric/gf16-ffn-performance-bounded-replay.json').read_text())
comparison = document(BASE, 'board-performance.json')
assert comparison['kind'] == 'physical-board' and comparison['controller_hz'] == 60000000
records = {}
for variant, names in [('baseline', ('bos',)), ('accelerated', ('bos', 'zero'))]:
    base = BASE / variant
    build = document(base / 'build', 'build.json')
    boot = document(base / 'boot', 'boot.json')
    assert all(boot['checks'].values()) and boot['checks']['gf16_ffn_header']
    assert boot['bitstream_sha256'] == build['bitstream']['sha256']
    assert boot['run']['dna'] == '0x00389c0c2d85e85c'
    assert int(boot['run']['idcode'], 16) == 0x03636093
    assert digest(read(base / 'boot', 'uart-raw.txt')) == boot['uart_sha256']
    clocks = build['nextpnr']['clocks_routed']
    assert clocks and all(c['verdict'] == 'PASS' and c['target_mhz'] == 60 for c in clocks)
    for name in ('nextpnr.log', 'yosys.log', 'yosys_stat.txt', 'tms_ddr3_loader_ax7203.xdc'):
        assert digest(read(base / 'build', name)) == build['files'][name]
    source = read(base / 'build', 'source_tree.txt').decode().strip()
    assert source.startswith(build['commit']) and source == comparison['variants'][variant]['source_commit']
    for name in names:
        folder = base / name
        ref_bytes = read(folder, 'reference.json')
        ref = json.loads(ref_bytes)
        manifest_bytes = read(folder, 'inputs.json')
        manifest = json.loads(manifest_bytes)
        independent = document(folder, 'independent-verification.json')
        assert independent['reference_sha256'] == digest(ref_bytes)
        assert independent['manifest_sha256'] == digest(manifest_bytes)
        assert independent['bitstream_sha256'] == boot['bitstream_sha256']
        assert independent['verifier_sha256'] == digest((BASE / 'verify-raw-board.py').read_bytes())
        assert independent['uart_restored'] == 115200
        assert independent['hardware_peak_temperature_c'] < 70
        pair_ref = document(folder / 'result-only', 'reference.json')
        assert pair_ref == {**ref, 'run': ref['run'] + 1, 'trace': 'result'}
        descriptor = ((ref['run'] + 1) | (vectors.MAGIC << 32) | (1 << 64)).to_bytes(16, 'little')
        assert read(folder / 'result-only', 'doorbell.bin') == descriptor
        modes = {}
        for mode, subfolder, reference in [('full', folder, ref), ('result', folder / 'result-only', pair_ref)]:
            raw = read(subfolder, 'capture.txt')
            checked = vectors.validate(raw, reference, reference['run'])
            assert doorbell_acknowledged(raw)
            result = document(subfolder, 'result.json')
            receipt = independent if mode == 'full' else independent['paired_result_only']
            assert all(result[k] == receipt[k] == v for k, v in checked.items())
            assert result['bitstream_sha256'] == boot['bitstream_sha256']
            assert result['capture_baud'] == 460800
            expected_loop = 265420800 if variant == 'baseline' else 160911360
            assert checked['projection_loop_clocks'] == expected_loop
            if mode == 'result':
                assert result['input_reuse'] == receipt['input_reuse'] == {
                    'source': '../', 'first_run': ref['run'], 'manifest_sha256': digest(manifest_bytes),
                    'doorbell_sha256': digest(descriptor),
                    'scope': 'same freshly uploaded/readback-verified inputs; only descriptor changed'}
            modes[mode] = checked
        for region, saved in independent['readbacks'].items():
            label = 'qualification' if region == 'qualification' else 'load-' + region
            receipt = document(folder, label + '.json')
            assert receipt['pass'] and all(receipt['checks'].values()) and receipt['baud'] == 921600
            assert receipt['transfer']['payload_bytes'] == saved['bytes']
            assert receipt['rx_raw']['sha256_uncompressed'] == saved['raw_sha256']
            if region != 'qualification':
                assert saved['payload_sha256'] == manifest[region]['sha256']
                assert saved['bytes'] == manifest[region]['bytes']
        assert sum(r['bytes'] for r in independent['readbacks'].values()) == 13496368
        for label, divisor in [('uart-fast', 65), ('uart-capture', 130), ('uart-restored', 521)]:
            receipt = document(folder, label + '.json')
            assert receipt['switch']['switched'] and receipt['after']['baud_div'] == divisor
            assert receipt['rx_sha256'] == digest(read(folder, label + '.rx.bin'))
        thermal = [json.loads(line) for line in read(folder, 'thermal.jsonl').splitlines()]
        assert thermal and all(t['returncode'] == 0 and 0 < t['temperature_c'] < 70 for t in thermal)
        peaks = [float(re.search(r'"maxtemp"\s*:\s*([0-9.]+)', t['output']).group(1)) for t in thermal]
        assert max(peaks) == independent['hardware_peak_temperature_c']
        if name == 'bos':
            row = numeric['cases'][0]['rows'][0]
            for key, group, fmt in [('stage_sha256_u32', 'stages', 'I'), ('actquant_sha256_u16', 'actquant', 'H'), ('code_sha256_i8', 'codes', 'b')]:
                for stage, values in ref[group].items():
                    assert digest(struct.pack('<' + fmt * len(values), *values)) == row[key][stage]
        else:
            # Multiplication by signed norm weights may preserve negative zero.
            # The exact sign bits are already checked against the saved oracle.
            for stage, values in ref['stages'].items():
                mask = 0x7fffffff if stage == 'a' else 0x7fff
                assert all(v & mask == 0 for v in values)
            assert all(v == 0 for group in ('actquant', 'codes') for values in ref[group].values() for v in values)
        assert comparison['variants'][variant]['runs'][name] == modes
        records[(variant, name)] = (modes, manifest)
        print(variant, name, 'full + result exact physical captures PASS')

a, am = records[('baseline', 'bos')]
b, bm = records[('accelerated', 'bos')]
for key in ('gate', 'up', 'down', 'scales', 'post', 'sub', 'x'):
    assert am[key]['sha256'] == bm[key]['sha256'] and am[key]['bytes'] == bm[key]['bytes']
for mode in ('full', 'result'):
    old, new = a[mode]['clock_split']['total'], b[mode]['clock_split']['total']
    assert old > new
    assert comparison['ratios'][mode] == old / new
    assert comparison['seconds'][mode] == {'baseline': old / 60000000, 'accelerated': new / 60000000}
print('Identical BOS input payloads; physical active-window comparison PASS')
