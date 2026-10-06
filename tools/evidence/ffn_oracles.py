"""Claim ffn_oracles: numeric oracles for #91 (synthetic FFN reference contract)
and #103 (GF16 conversion profile, C/RTL conformance). Not hardware evidence.
The counts are parsed from the output of the existing unittest modules, run
offline as subprocesses; any failed, skipped or missing test raises."""
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def _run(args, root, env_drop=()):
    env = {k: v for k, v in os.environ.items() if k not in env_drop}
    env['TRINITY_FIXTURES_OFFLINE'] = '1'
    r = subprocess.run([sys.executable, '-m', 'unittest', '-v', *args], cwd=root, env=env,
                       text=True, capture_output=True)
    out = r.stdout + r.stderr
    if r.returncode:
        raise ValueError('verifier failed: ' + ' '.join(args) + '\n' + out)
    ran = int(re.search(r'^Ran (\d+) tests?', out, re.M).group(1))
    ok = len(re.findall(r' \.\.\. ok$', out, re.M))
    if ran != ok or re.search(r'skipped|FAIL|ERROR', out):
        raise ValueError('tests skipped or not all ok: ' + ' '.join(args))
    return ran, out


def _one(pattern, out):
    m = re.findall(pattern, out, re.M)
    if len(m) != 1:
        raise ValueError('verifier output lacks ' + pattern)
    return m[0]


def counts(root):
    # Only the synthetic classes: the real-layer test needs the uncommitted fixture cache.
    ffn, _ = _run(['tests.test_ffn_reference.RoundingTest', 'tests.test_ffn_reference.DatapathTest'],
                  root, env_drop=('TRINITY_REQUIRE_CACHED',))
    codec, out = _run(['tests.test_gf16_codec'], root)
    enc, rt = re.fullmatch(r'(\d+) encode vectors; (\d+) decode/round trips',
                           _one(r'^GF16 C: (.*)$', out)).groups()
    rtl = _one(r'^PASS GF16 rtl (\d+) vectors$', out)
    mapped = _one(r'^PASS GF16 mapped (\d+) vectors$', out)
    return (ffn, codec, int(enc), int(rt), int(rtl), int(mapped))


CLAIM = {
    'name': 'ffn_oracles',
    'issues': [91, 103],
    'scope': 'numeric oracles replayed offline from committed files; synthetic FFN layer tests and '
             'the GF16 codec C/RTL conformance; not hardware evidence; real-layer FFN report and #105 '
             'need the uncommitted fixture cache and are not covered',
    'spec': 'specs/memory/ffn_oracles_evidence.t27',
    'vectors': 'conformance/memory_ffn_oracles_evidence.json',
    'accept': 'tmfo_accept',
    'expected': [10, 5, 314883, 65536, 314883, 314883],
    'manifest': 'reports/numeric/ffn-oracles-evidence-manifest.json',
    'bind': [
        'tools/evidence/ffn_oracles.py', 'specs/memory/ffn_oracles_evidence.t27',
        'tools/ffn_reference.py', 'tests/test_ffn_reference.py',
        'tools/gf16_reference.py', 'tests/test_gf16_codec.py', 't27/rtl/gf16_codec.t27',
        'docs/gf16-contract.md', 'native/compiler.lock', 'trinity_memory/*.py',
    ],
    'counts': counts,
}
