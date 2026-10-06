"""Claim ffn_real_layer: #91 (real layer-0 FFN reference report reproduced) and
#105 (GF16/BF16 boundary-quantization report replayed). The real BitNet layer-0
tensors come from the sha256-pinned byte-range cache build/fixtures, filled by
tools/fetch-fixtures.py and verified against fixtures/manifest.json on every
read. Without that cache, or with any skipped test, counts() raises."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]

# Recomputes the real layer from the pinned tensors with the reference's own
# functions and prints what it established. Reads the committed report only to
# compare with it.
RECOMPUTE = r'''
import json, sys
import tools.ffn_reference as fr
model = fr.load_ffn()
report = json.loads(open("reports/ffn/reference-2026-09-28.json").read())
x = report["activations_int8"]
ref = fr.reference_f64(model, x)
q16, sat = fr.fpga_q16(model, [t << fr.Q for t in x])
stages = fr.compare(ref, q16)
assert stages == report["stages"] and sat == report["saturations"]
assert fr.sha256_le32(q16["y"]) == report["y_q16_sha256_le"]
assert fr.sha256_le32([fr.to_q(v) for v in ref["y"]]) == report["y_f64_q16_sha256_le"]
assert report["verdict"] == "pass"
print(json.dumps({
    "weights": sum(len(model[k]) for k in ("gate", "up", "down")),
    "stages_in_guard": sum(1 for s in stages.values() if not s["outside_guard"]),
    "saturations": sum(sat.values()),
}))
'''


def _env(root):
    env = dict(os.environ)
    env.update(TRINITY_FIXTURES_OFFLINE='1', TRINITY_REQUIRE_CACHED='1', PYTHONDONTWRITEBYTECODE='1')
    return env


def _run(args, root):
    r = subprocess.run([sys.executable, *args], cwd=root, env=_env(root), text=True, capture_output=True)
    out = r.stdout + r.stderr
    if r.returncode:
        raise ValueError('verifier failed: ' + ' '.join(args) + '\n' + out[-3000:])
    return r.stdout, out


def _tests(module, root):
    _, out = _run(['-m', 'unittest', '-v', module], root)
    ran = int(re.search(r'^Ran (\d+) tests?', out, re.M).group(1))
    ok = re.findall(r'^(\S+) \((\S+)\) \.\.\. ok$', out, re.M)
    if ran != len(ok) or re.search(r'skipped|FAIL|ERROR', out):
        raise ValueError('tests skipped or not all ok: ' + module)
    return ran, [name for name, _ in ok if '.FixtureLayerTest.' in _]


def counts(root):
    root = Path(root)
    cache = root / 'build/fixtures'
    if not cache.is_dir() or not any(cache.iterdir()):
        raise ValueError('the sha256-pinned fixture cache build/fixtures is missing (tools/fetch-fixtures.py fills it)')
    ffn, real = _tests('tests.test_ffn_reference', root)
    if real != ['test_real_layer_matches_committed_report']:
        raise ValueError('the real-layer test did not pass')
    quant, _ = _tests('tests.test_ffn_quantization', root)
    out, _ = _run(['-c', RECOMPUTE], root)
    layer = json.loads(out)
    _, check = _run(['tools/ffn_quantization.py', '--check'], root)
    if not re.search(r'^PASS FFN quantization report replay$', check, re.M):
        raise ValueError('quantization replay did not pass')
    # --check proved the committed text equals the recomputed report byte for byte.
    report = json.loads((root / 'reports/numeric/ffn-quantization-v1.json').read_text())
    paths = sum(len(c['propagated']) for c in report['cases'])
    return (ffn, len(real), quant, layer['weights'], layer['stages_in_guard'], layer['saturations'],
            len(report['cases']), paths)


CLAIM = {
    'name': 'ffn_real_layer',
    'issues': [91, 105],
    'scope': 'real BitNet layer-0 FFN: the corrected reference report and the GF16/BF16 boundary-quantization '
             'report recomputed from sha256-pinned fetched fixtures (build/fixtures, not committed); '
             'synthetic input probes; not ActQuant, text activations, hardware or performance',
    'spec': 'specs/memory/ffn_real_layer_evidence.t27',
    'vectors': 'conformance/memory_ffn_real_layer_evidence.json',
    'accept': 'tmfr_accept',
    'expected': [11, 1, 9, 53084160, 6, 0, 5, 20],
    'manifest': 'reports/numeric/ffn-real-layer-evidence-manifest.json',
    'bind': [
        'tools/evidence/ffn_real_layer.py', 'specs/memory/ffn_real_layer_evidence.t27',
        'tools/ffn_reference.py', 'tools/ffn_quantization.py', 'tools/gf16_reference.py',
        'tests/test_ffn_reference.py', 'tests/test_ffn_quantization.py',
        'reports/ffn/reference-2026-09-28.json', 'reports/numeric/ffn-quantization-v1.json',
        'fixtures/manifest.json', 'native/compiler.lock', 'trinity_memory/*.py', 't27/*.t27', 'native/*.c',
        'native/*.cpp', 'native/*.h',
    ],
    'counts': counts,
}
