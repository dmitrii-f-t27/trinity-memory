"""Claim gguf_verdicts: the t27 verdict engine on committed GGUF header fixtures.

counts(root) never reads a verdict from a report. It
  1. checks that fixtures/gguf-verdicts/cases.json is exactly what
     tools/evidence/_gguf_verdicts_cases.py writes (headers made byte by byte,
     expectations written by hand from the runtime tables and reader rules);
  2. generates C with the pinned t27c from t27/json.t27, formats.t27,
     runtimes.t27 and live.t27, builds tools/evidence/gguf_verdicts_driver.c
     against it and replays every fixture header through tlv_model (four
     runtimes), tlv_native, tlv_ternary_count, tlv_file_verdict and tlv_walk;
  3. runs tools/generate-runtime-tables.py --check and counts the type rows
     of specs/runtimes/*.json that t27/runtimes.t27 restates;
  4. recomputes, from the per-file records of the retained real-file replay
     (reports/live/legacy-q2_0-2026-09-29/scan-replay.json), the counts that
     replay established; it does not re-run the upstream readers.
No network, no hardware. Counts, in the order of tmgv_accept:
  headers (each also has its native runtime and file verdict equal to the record),
  verdict_cells, layout_diagnoses, table_rows, retained_legacy_confirmed.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
STATUS_NAMES = {}  # filled from t27/live.t27 and formats.t27 constants
FILE = {0: 'ok', 1: 'refused', 2: 'no_ternary_layout', 3: 'undecided', 4: 'other_runtime'}
RUN = {0: 'accepts', 1: 'refuses', 2: 'ignores_rotation'}
NATIVE = {0: 'other', 1: 'llama.cpp', 2: 'prismml', 3: 'bitnet.cpp', 4: 'mortar.cpp'}
NONE = (1 << 64) - 1


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tokens(root):
    tokens = {}
    for file, prefix in (('t27/live.t27', 'TLV_ERR_'), ('t27/formats.t27', 'TF_ERR_')):
        for name, value in re.findall(r'const (' + prefix + r'[A-Z0-9_]+): i32 = (-\d+);', (root / file).read_text()):
            tokens.setdefault(int(value), name[len(prefix):].lower())
    return tokens


def _generate_and_run(root, cases):
    proof = _load(root / 'tools' / 'evidence-proof.py', 'evidence_proof_for_gguf_verdicts')
    tool = proof.compiler(root)  # pin check of the compiler checkout
    cc = os.environ.get('CC', 'cc')
    with tempfile.TemporaryDirectory(prefix='gguf-verdicts-') as directory:
        work = Path(directory)
        for module in ('json', 'formats', 'runtimes', 'live'):
            header = subprocess.check_output([str(tool), 'gen-c', f't27/{module}.t27'], cwd=root, text=True)
            (work / f'{module}.h').write_text(header)
        subprocess.run([cc, '-std=c11', '-O1', '-Wno-parentheses-equality', '-I', str(work),
                        str(root / 'tools/evidence/gguf_verdicts_driver.c'), '-o', str(work / 'driver')], check=True)
        stdin = ''.join(f'{c["file_size"]} {c["header"]}\n' for c in cases)
        out = subprocess.run([str(work / 'driver')], input=stdin, text=True, capture_output=True, check=True).stdout
    results, current = [], None
    for line in out.splitlines():
        fields = line.split()
        if fields[0] == 'R':
            if current is None:
                current = {'runs': {}}
            r, verdict, status, part, record, expected, found = map(int, fields[1:])
            current['runs'][r] = (verdict, status, record, expected, found)
        else:
            native, ternary, file_verdict, fits, read = map(int, fields[1:])
            current.update(native=native, ternary=ternary, file_verdict=file_verdict, fits=fits, read=read)
            results.append(current)
            current = None
    if len(results) != len(cases):
        raise ValueError('driver answered for a different number of headers')
    return results


def _replay(root):
    document = json.loads((root / 'fixtures/gguf-verdicts/cases.json').read_text())
    generator = _load(HERE / '_gguf_verdicts_cases.py', 'gguf_verdicts_cases')
    if document != generator.document():
        raise ValueError('fixtures/gguf-verdicts/cases.json is not what the generator writes')
    cases, runtimes = document['cases'], document['runtimes']
    tokens = _tokens(root)
    results = _generate_and_run(root, cases)
    cells = file_verdicts = diagnoses = 0
    for case, got in zip(cases, results):
        for runtime, want in case['expect'].items():
            verdict, status, record, expected, found = got['runs'][runtimes.index(runtime) + 1]
            say = RUN.get(verdict, 'no_verdict')
            if say != want['verdict']:
                raise ValueError(f'{case["name"]} {runtime}: verdict {say}, recorded {want["verdict"]}')
            if 'status' in want:
                token = tokens.get(status)
                if token != want['status']:
                    raise ValueError(f'{case["name"]} {runtime}: status {token}, recorded {want["status"]}')
            for field, value in (('record', record), ('expected', expected), ('found', found)):
                if field in want and want[field] != value:
                    raise ValueError(f'{case["name"]} {runtime}: {field} {value}, recorded {want[field]}')
            cells += 1
        if NATIVE[got['native']] != case['native'] or FILE[got['file_verdict']] != case['file_verdict']:
            raise ValueError(f'{case["name"]}: native/file verdict {NATIVE[got["native"]]}/{FILE[got["file_verdict"]]}')
        file_verdicts += 1
        want_fits = case['fits'].get('PQ2_0', 0)
        if got['fits'] != want_fits or set(case['fits']) - {'PQ2_0'}:
            raise ValueError(f'{case["name"]}: layout diagnosis {got["fits"]}, recorded {want_fits}')
        if want_fits:
            diagnoses += 1
    if file_verdicts != len(cases):
        raise ValueError('a header lacks its file verdict')
    return len(cases), cells, diagnoses


def _table_rows(root):
    subprocess.run([os.sys.executable, str(root / 'tools/generate-runtime-tables.py'), '--check'], cwd=root, check=True)
    rows = 0
    for path in sorted((root / 'specs/runtimes').glob('*.json')):
        rows += len(json.loads(path.read_text())['types'])
    return rows


def _retained(root):
    report = json.loads((root / 'reports/live/legacy-q2_0-2026-09-29/scan-replay.json').read_text())
    files = agreements = legacy = 0
    for repo in report['repositories']:
        for model in repo.get('models', []):
            upstream = [u for f in model['files'] for u in f['upstream'].values()]
            files += len(model['files'])
            agreements += sum(1 for u in upstream if u['agrees'])
            fits = model.get('problems', {}).get('extent', {}).get('fits', {})
            refused_everywhere = (model['verdict'] == 'refused' and upstream
                                  and all(u['agrees'] and not u['accepted'] for u in upstream))
            if refused_everywhere and fits.get('PQ2_0', 0) > 0 and any(t == 42 for t, _ in model['ggml_types']):
                legacy += 1
    replay = report['replay']
    if (files, agreements) != (replay['files'], replay['agree']) or agreements != 3 * files or replay['disagree']:
        raise ValueError('retained replay summary differs from its per-file records')
    return legacy


def counts(root):
    headers, cells, diagnoses = _replay(root)
    return (headers, cells, diagnoses, _table_rows(root), _retained(root))


CLAIM = {
    'name': 'gguf_verdicts',
    'issues': [50, 51],
    'scope': ('t27 verdict engine (t27/live.t27, t27/runtimes.t27) replayed as generated C over committed synthetic '
              'GGUF header fixtures, the runtime tables regenerated from specs/runtimes/*.json, and counts recomputed '
              'from the retained real-file replay of 2026-09-29; no network scan, no upstream reader re-run'),
    'spec': 'specs/memory/gguf_verdicts_evidence.t27',
    'vectors': 'conformance/memory_gguf_verdicts_evidence.json',
    'accept': 'tmgv_accept',
    'expected': (14, 34, 2, 147, 9),
    'manifest': 'reports/live/gguf-verdicts-evidence/evidence-manifest.json',
    'bind': [
        'specs/memory/gguf_verdicts_evidence.t27',
        't27/json.t27', 't27/formats.t27', 't27/runtimes.t27', 't27/live.t27',
        'specs/runtimes/*.json', 'tools/generate-runtime-tables.py', 'native/compiler.lock',
        'tools/evidence/gguf_verdicts.py', 'tools/evidence/_gguf_verdicts_cases.py',
        'tools/evidence/gguf_verdicts_driver.c', 'fixtures/gguf-verdicts/cases.json',
        'reports/live/legacy-q2_0-2026-09-29/scan-replay.json',
    ],
    'counts': counts,
}
