"""Claim ternary_check_matrix: Ternary Check matrix (issue 32) and real-layer matvec (issue 33).

counts(root) re-runs the chain of the CI job `ternary-check`, in check mode, and
counts what the recomputation itself produced:
  1. tools/ternary-check.sh --check with OFFLINE=1: fixture ranges re-verified
     against fixtures/manifest.json (sha256), the pinned t27c rebuilt, formats.wasm
     built, the pinned llama.cpp sources sha256-checked and the TQ1_0/TQ2_0 cells
     re-quantised, then reports/ternary-check.json, .html and repro/ compared
     byte for byte with the committed files (nothing is written under reports/);
  2. TRINITY_REQUIRE_CACHED=1 node tests/matrix_wasm.mjs and tests/matvec_wasm.mjs:
     every real cell and every stored form recomputed in formats.wasm; any skip fails;
  3. python -m unittest tests.test_ternary_check tests.test_matvec, skips counted;
  4. python -m trinity_memory.ternary_check --output <tmp> and
     python -m trinity_memory.matvec --output <tmp> --check: the recomputed
     reports are written to a temporary directory, required to equal the committed
     ones byte for byte, and counted from the recomputed copy.
It needs the sha256-pinned caches (build/fixtures, build/upstream) and the pinned
compiler ($T27_ROOT); it fails if one is absent. No network, no hardware.
Counts, in the order of tmcm_accept: see the spec header.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def _env(root):
    env = dict(os.environ)
    env.update(OFFLINE='1', TRINITY_FIXTURES_OFFLINE='1', TRINITY_UPSTREAM_OFFLINE='1',
               TRINITY_REQUIRE_CACHED='1', PYTHON=sys.executable)
    if not env.get('T27_ROOT'):
        raise ValueError('T27_ROOT must name the pinned t27 compiler checkout')
    return env


def _run(root, command, env, label):
    done = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True)
    if done.returncode:
        raise ValueError(f'{label} failed ({done.returncode}):\n' + (done.stdout + done.stderr)[-3000:])
    return done.stdout + done.stderr


def _caches(root):
    fixtures = root / 'build' / 'fixtures'
    manifest = json.loads((root / 'fixtures' / 'manifest.json').read_text())
    if not fixtures.is_dir() or not any(fixtures.iterdir()):
        raise ValueError('build/fixtures is absent: restore the sha256-pinned fixture cache (tools/fetch-fixtures.py)')
    if not manifest.get('models'):
        raise ValueError('fixtures/manifest.json lists no models')
    if not list((root / 'build' / 'upstream').glob('llama.cpp-*/ggml/src/ggml-quants.c')):
        raise ValueError('build/upstream is absent: the sha256-pinned llama.cpp sources are required')


def _report(root, directory, env):
    out = Path(directory)
    _run(root, [sys.executable, '-m', 'trinity_memory.ternary_check', '--output', str(out)], env, 'ternary_check --output')
    _run(root, [sys.executable, '-m', 'trinity_memory.matvec', '--output', str(out / 'matvec.json'), '--check'], env,
         'matvec --check')
    recomputed = (out / 'ternary-check.json').read_bytes()
    if recomputed != (root / 'reports/ternary-check.json').read_bytes():
        raise ValueError('recomputed ternary-check.json differs from the committed report')
    if (out / 'matvec.json').read_bytes() != (root / 'reports/ternary-check/matvec-2026-09-23.json').read_bytes():
        raise ValueError('recomputed matvec report differs from the committed report')
    return json.loads(recomputed), json.loads((out / 'matvec.json').read_text())


def _count_matrix(report):
    cells = report['cells']
    compared = [c for c in cells if c['status'] != 'not-representable']
    for c in cells:
        if c['status'] == 'not-representable' and not c.get('reasons'):
            raise ValueError(c['id'] + ': not-representable without a reason')
    trit_exact = sum(1 for c in compared if c['trits']['differ'] == 0)
    scale_exact = sum(1 for c in compared if c['scales']['differ'] == 0)
    unexplained = sum(1 for c in compared if (c['trits']['differ'] and not c['trits']['explanation'])
                      or (c['scales']['differ'] and not c['scales']['explanation']))
    unexplained += sum(1 for c in compared if c['status'] == 'match' and (c['trits']['differ'] or c['scales']['differ']))
    status = {s: sum(1 for c in cells if c['status'] == s) for s in ('match', 'mismatch', 'not-representable')}
    if sum(status.values()) != len(cells):
        raise ValueError('a cell has no match / mismatch / not-representable status')
    derived = report['derived']
    explained = sum(1 for d in derived if d['trits']['differ'] and d['trits']['explanation'] == 'tie_split')
    packed_i2s = sum(1 for d in derived if d['triple_check']['hf_packed_vs_i2_s']['differ'] == 0)
    absmean = sum(1 for d in derived if d['triple_check']['bf16_absmean_vs_hf_packed']['differ'] == 0)
    families = {t['id'].split('-')[0] for t in report['tensors']}
    return [len(cells), status['match'], status['mismatch'], status['not-representable'], trit_exact, scale_exact,
            unexplained, len(report['formats']), len(families), packed_i2s, absmean, explained]


def _count_matvec(matvec):
    forms = identical = rows = residual = 0
    for tensor in matvec['tensors']:
        stored = {k: v for k, v in tensor['formats'].items() if 'derived' not in v}
        forms += len(stored)
        if any(v['status'] != 'ok' for v in tensor['formats'].values()):
            raise ValueError('a matvec form is not ok')
        if len({v['accumulators']['sha256_le'] for v in stored.values()}) == 1:
            identical += 1
        names = set(stored)
        for comparison in tensor['comparisons']:
            if {comparison['a'], comparison['b']} <= names:
                rows += comparison['accumulator_rows']['differ']
            elif 'difference_tensor' in comparison:
                residual += comparison['difference_tensor']['rows_where_y_a_minus_y_b_is_not_D_x']['differ']
    return forms, identical, rows, residual


def _node(root, env, script, pattern):
    text = _run(root, ['node', script], env, script)
    found = re.search(pattern, text)
    if not found:
        raise ValueError(f'{script}: unexpected output: {text[-500:]}')
    skipped = re.search(r'(\d+) skipped', text)
    return int(found.group(1)), int(found.group(2)), int(skipped.group(1)) if skipped else 0


def counts(root):
    root = Path(root)
    _caches(root)
    env = _env(root)
    _run(root, ['sh', 'tools/ternary-check.sh', '--check'], env, 'tools/ternary-check.sh --check')
    cells_done, cells_total, cells_skipped = _node(
        root, env, 'tests/matrix_wasm.mjs', r'; (\d+) of (\d+) cells of reports/ternary-check\.json reproduce')
    forms_done, forms_total, forms_skipped = _node(
        root, env, 'tests/matvec_wasm.mjs', r'; (\d+) of (\d+) stored forms of the real layers reproduce')
    if cells_done != cells_total or forms_done != forms_total:
        raise ValueError('formats.wasm did not recompute every cell and stored form')
    text = _run(root, [sys.executable, '-m', 'unittest', 'tests.test_ternary_check', 'tests.test_matvec'], env, 'unittest')
    ran = int(re.search(r'Ran (\d+) tests', text).group(1))
    unit_skipped = re.search(r'skipped=(\d+)', text)
    unit_skipped = int(unit_skipped.group(1)) if unit_skipped else 0
    with tempfile.TemporaryDirectory(prefix='ternary-check-matrix-') as directory:
        report, matvec = _report(root, directory, env)
    matrix = _count_matrix(report)
    forms, identical, rows, residual = _count_matvec(matvec)
    if forms != forms_done:
        raise ValueError('the stored forms of the report differ from the forms formats.wasm recomputed')
    skipped = cells_skipped + forms_skipped + unit_skipped
    cells, match, mismatch, notrep, trit_exact, scale_exact, unexplained, formats, families, packed, absmean, tie = matrix
    return (cells, cells_done, match, mismatch, notrep, trit_exact, scale_exact, unexplained, formats, families,
            packed, absmean, tie, forms_done, identical, rows, residual, ran, skipped)


CLAIM = {
    'name': 'ternary_check_matrix',
    'issues': [32, 33],
    'scope': ('Ternary Check matrix and real-layer matvec recomputed from the sha256-pinned fixture ranges '
              '(build/fixtures), the sha256-pinned llama.cpp quantiser sources (build/upstream) and the pinned t27 '
              'compiler: reports/ternary-check.json, .html, repro/ and the matvec report reproduced byte for byte, '
              'every cell and stored form recomputed again in formats.wasm with none skipped; replay depends on '
              'those fetched caches; no network, no hardware'),
    'spec': 'specs/memory/ternary_check_matrix_evidence.t27',
    'vectors': 'conformance/memory_ternary_check_matrix_evidence.json',
    'accept': 'tmcm_accept',
    'expected': (36, 38, 23, 4, 9, 27, 23, 0, 12, 2, 2, 0, 2, 8, 3, 0, 0, 30, 0),
    'manifest': 'reports/ternary-check/evidence-manifest.json',
    'bind': [
        'specs/memory/ternary_check_matrix_evidence.t27',
        'tools/evidence/ternary_check_matrix.py',
        'native/compiler.lock', 'fixtures/manifest.json',
        'reports/ternary-check.json', 'reports/ternary-check.html', 'reports/ternary-check/repro/*',
        'reports/ternary-check/matvec-2026-09-23.json',
        'tools/ternary-check.sh', 'tools/build-t27.sh', 'tools/build-t27-wasm.sh', 'tools/build-t27-rtl.sh',
        'tools/fetch-fixtures.py', 'tools/fetch-upstream.sh', 'tools/embed-wasm.py',
        'tests/upstream/llama.cpp.lock.json', 'tests/upstream/*.py', 'tests/upstream/*.c', 'tests/upstream/*.h',
        'tests/upstream/run-llamacpp-matrix.sh',
        'tests/matrix_wasm.mjs', 'tests/matvec_wasm.mjs', 'tests/test_ternary_check.py', 'tests/test_matvec.py',
        'trinity_memory/ternary_check.py', 'trinity_memory/ternary_check_html.py', 'trinity_memory/matvec.py',
        'trinity_memory/fixtures.py', 'trinity_memory/formats.py', 'trinity_memory/_native.py', 'trinity_memory/matrix.py',
        't27/formats.t27', 't27/matrix.t27', 't27/matvec.t27', 't27/compute.t27', 't27/random.t27',
        'native/*.c', 'native/*.cpp', 'native/*.h', 'native/wasm-include/*.h',
    ],
    'counts': counts,
}
