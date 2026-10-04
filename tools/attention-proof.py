#!/usr/bin/env python3
"""Replay the sealed attention evidence contract and retained raw measurements.

This revalidates the published qualification; it does not operate hardware.
"""
import argparse
import hashlib
import itertools
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = Path('specs/memory/attention_evidence.t27')
VECTORS = Path('conformance/memory_attention_evidence.json')
EVIDENCE = Path('reports/fpga/attn-2026-10-03')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def vectors(root=ROOT):
    rows = []
    for flags in itertools.product([False, True], repeat=3):
        for pairs, simulations, controls in itertools.product([0, 3, 4, 5], [0, 7, 8, 9], [0, 3, 4, 5]):
            values = [*flags, pairs, simulations, controls]
            # Independent contract oracle; the consumer executes generated C.
            expected = all(flags) and (pairs, simulations, controls) == (4, 8, 4)
            rows.append({'input': values, 'expected': expected})
    return {'spec_path': str(SPEC), 'spec_hash': 'sha256:' + sha((root / SPEC).read_bytes()), 'vectors': rows}


def current_sources(root, manifest):
    sources = {name[7:]: row for name, row in manifest['files'].items() if name.startswith('source/')}
    required = {'t27/rtl/gf16_attn.t27', 'rtl/t27/gf16_attn.v', 'tools/verify_gf16_attn_board.py'}
    if not required.issubset(sources):
        raise ValueError('missing attention source binding')
    for name, record in sources.items():
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('unsafe source binding')
        data = (root / path).read_bytes()
        if len(data) != record['bytes'] or sha(data) != record['sha256']:
            raise ValueError('current source differs from measured source: ' + name)
    return sources


def archive_exact(root, manifest):
    record = manifest['archive']
    if record['file'] != 'board-evidence.tar.gz':
        raise ValueError('unexpected archive path')
    data = (root / EVIDENCE / record['file']).read_bytes()
    if len(data) != record['bytes'] or sha(data) != record['sha256']:
        raise ValueError('retained archive changed')
    return root / EVIDENCE / record['file']


def compiler(root):
    compiler_root = Path(os.environ['T27_ROOT']).resolve()
    pin = (root / 'native/compiler.lock').read_text().strip()
    actual = subprocess.check_output(['git', '-C', str(compiler_root), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != pin:
        raise ValueError('compiler pin mismatch')
    subprocess.run(['git', '-C', str(compiler_root), 'diff', '--quiet', 'HEAD', '--', 'bootstrap', 'Cargo.toml', 'Cargo.lock'], check=True)
    return compiler_root / 'target/release/t27c'


def native_replay(root, rows, actual=None):
    tool = compiler(root)
    subprocess.run([str(tool), 'seal', '--verify', str(SPEC)], cwd=root, check=True)
    header = subprocess.check_output([str(tool), 'gen-c', str(SPEC)], cwd=root, text=True)
    cc = os.environ.get('CC', 'cc')
    with tempfile.TemporaryDirectory(prefix='attention-proof-') as directory:
        work = Path(directory)
        (work / 'proof.h').write_text(header)
        (work / 'tests.c').write_text('#define T27_TEST_MAIN\n#include "proof.h"\n')
        subprocess.run([cc, '-std=c11', '-O1', str(work / 'tests.c'), '-o', str(work / 'tests')], check=True)
        subprocess.run([str(work / 'tests')], check=True)
        body = ['#include "proof.h"', 'int main(void) {']
        for row in rows:
            values = ','.join(str(int(v)) for v in row['input'])
            body.append(f'if (tmae_accept({values}) != {int(row["expected"])}) return 1;')
        if actual is not None:
            body.append('if (!tmae_accept(' + ','.join(str(int(v)) for v in actual) + ')) return 2;')
        body += ['return 0;', '}']
        (work / 'replay.c').write_text('\n'.join(body) + '\n')
        subprocess.run([cc, '-std=c11', '-O1', str(work / 'replay.c'), '-o', str(work / 'replay')], check=True)
        subprocess.run([str(work / 'replay')], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write-vectors', action='store_true')
    parser.add_argument('--vectors-only', action='store_true')
    parser.add_argument('--output', type=Path, default=ROOT / 'build/attention-proof/result.json')
    args = parser.parse_args()
    expected = vectors()
    if args.write_vectors:
        (ROOT / VECTORS).write_text(json.dumps(expected, indent=2) + '\n')
        return
    committed = json.loads((ROOT / VECTORS).read_text())
    if committed != expected:
        raise ValueError('conformance vectors stale or altered')
    if args.vectors_only:
        native_replay(ROOT, committed['vectors'])
        print(f'PASS {len(committed["vectors"])} committed attention evidence vectors')
        return
    if not __debug__:
        raise ValueError('raw evidence replay requires Python assertions')
    manifest = json.loads((ROOT / EVIDENCE / 'evidence-manifest.json').read_text())
    sources = current_sources(ROOT, manifest)
    archive = archive_exact(ROOT, manifest)
    subprocess.run([os.sys.executable, str(ROOT / EVIDENCE / 'verify-evidence.py')], cwd=ROOT, check=True)
    with tarfile.open(archive, 'r:gz') as retained:
        simulations = json.load(retained.extractfile('simulation/results.json'))
        controls = json.load(retained.extractfile('simulation/mapped-control-results.json'))
    pairs = json.loads((ROOT / EVIDENCE / 'physical-results.json').read_text())
    actual = [True, True, True, len(pairs), len(simulations), len(controls)]
    native_replay(ROOT, committed['vectors'], actual)
    current_sources(ROOT, manifest)
    archive_exact(ROOT, manifest)
    result = {
        'schema': 'trinity-attention-evidence-proof-v1',
        'scope': 'retained layer-0 attention/residual qualification; not a new board run',
        'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'source_dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip()),
        'spec_path': str(SPEC), 'spec_hash': committed['spec_hash'],
        'vectors_executed': len(committed['vectors']),
        'physical_pairs_replayed': len(pairs), 'simulations_replayed': len(simulations),
        'mapped_controls_replayed': len(controls), 'source_hashes': sources,
        'archive_sha256': manifest['archive']['sha256'],
        'hardware_commit': manifest['hardware_commit'], 'pass': True,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(f'PASS retained attention evidence and {len(committed["vectors"])} native conformance vectors: {args.output}')


if __name__ == '__main__':
    main()
