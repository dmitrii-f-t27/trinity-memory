"""Claim fixtures_provenance: real-weight fixtures from pinned public checkpoints (issue #31).

DEPENDS ON A FETCHED CACHE. The real-weight byte ranges are not in git. They live
in build/fixtures, filled anonymously from Hugging Face by tools/fetch-fixtures.py
and verified against fixtures/manifest.json. counts() fails if that cache is
absent or incomplete; the cache files themselves are not bound (their integrity
is the sha256 check that counts() performs and counts).

counts(root) runs no network and no hardware. It
  1. runs `python3 tools/fetch-fixtures.py --offline` (strict: every range present
     with exact length and sha256, no unlisted cache file) and reads its summary;
  2. independently re-reads every range of fixtures/manifest.lock.json from the
     cache with its own code, checks length (end - begin) and sha256, counts
     ranges and bytes, and demands equality with the summary of step 1 and with
     the manifest itself (the lock must be what the manifest lists);
  3. runs tests/test_fixtures.py (fault-injecting local HTTP server) and requires
     a clean OK with no skips; the number of tests is a count;
  4. runs `python3 tools/bitnet_audit.py --offline` and compares its output byte
     for byte with reports/ternary-check/bitnet-audit-2026-09-23.json.
Counts, in the order of tmfp_accept: models (each with a full 40-hex revision),
ranges re-hashed, bytes hashed, fixture tests run, audit report equal (1).
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

AUDIT = 'reports/ternary-check/bitnet-audit-2026-09-23.json'


def _run(root, args):
    return subprocess.run([sys.executable, *args], cwd=root, capture_output=True, text=True)


def counts(root):
    root = Path(root)
    cache = root / 'build' / 'fixtures'
    if not cache.is_dir() or not any(cache.iterdir()):
        raise ValueError('build/fixtures is absent or empty: run tools/fetch-fixtures.py (or restore the CI cache) first')
    manifest = json.loads((root / 'fixtures/manifest.json').read_text())
    lock = json.loads((root / 'fixtures/manifest.lock.json').read_text())

    # 1. the fetch tool's own strict offline verification
    fetch = _run(root, ['tools/fetch-fixtures.py', '--offline'])
    if fetch.returncode != 0:
        raise ValueError('fetch-fixtures --offline failed:\n' + fetch.stdout + fetch.stderr)
    m = re.fullmatch(r'(\d+) manifest ranges: fetched 0 \(0\.0 MiB\), verified in cache (\d+) \([\d.]+ MiB\), 0 errors\n', fetch.stdout)
    if not m or m.group(1) != m.group(2):
        raise ValueError('unexpected fetch-fixtures summary: ' + fetch.stdout)
    tool_ranges = int(m.group(2))

    # 2. independent re-hash of every locked range
    models = manifest['models']
    for model in models:
        if not re.fullmatch(r'[0-9a-f]{40}', model['revision']):
            raise ValueError('revision is not a full commit sha: ' + model['repo'])
    revisions = {m_['repo']: m_['revision'] for m_ in models}
    ranges = bytes_hashed = 0
    prefix_cache = {}
    for key, digest in sorted(lock.items()):
        match = re.fullmatch(r'(.+)@([0-9a-f]{40})/(.+)#(\d+)-(\d+)', key)
        if not match or revisions.get(match.group(1)) != match.group(2) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('lock entry does not match the manifest models: ' + key)
        repo, revision, name, begin, end = match.group(1), match.group(2), match.group(3), int(match.group(4)), int(match.group(5))
        directory = cache / repo.replace('/', '--') / revision / name
        single = directory / f'{begin}-{end}.bin'
        if single.is_file():
            data = single.read_bytes()
        else:  # prefix chunk, stored concatenated in prefix.bin
            if directory not in prefix_cache:
                path = directory / 'prefix.bin'
                prefix_cache[directory] = path.read_bytes() if path.is_file() else b''
            data = prefix_cache[directory][begin:end]
        if len(data) != end - begin or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('range missing or differs from the manifest: ' + key)
        ranges += 1
        bytes_hashed += len(data)
    if ranges != tool_ranges or ranges != len(lock) or end_range_count(manifest) != ranges:
        raise ValueError('range counts disagree: tool %d, own %d, lock %d, manifest %d'
                         % (tool_ranges, ranges, len(lock), end_range_count(manifest)))

    # 3. unit tests, no skips
    tests = _run(root, ['-m', 'unittest', 'tests.test_fixtures'])
    out = tests.stderr
    ran = re.search(r'^Ran (\d+) tests? in', out, re.M)
    if tests.returncode != 0 or not ran or not re.search(r'^OK$', out, re.M) or 'skipped' in out:
        raise ValueError('tests/test_fixtures.py failed or skipped something:\n' + out)

    # 4. the audit replay equals its committed report
    audit = subprocess.run([sys.executable, 'tools/bitnet_audit.py', '--offline'], cwd=root, capture_output=True)
    if audit.returncode != 0:
        raise ValueError('bitnet_audit --offline failed:\n' + audit.stderr.decode())
    equal = int(audit.stdout == (root / AUDIT).read_bytes())
    if not equal:
        raise ValueError('bitnet audit output differs from ' + AUDIT)
    return (len(models), ranges, bytes_hashed, int(ran.group(1)), equal)


def end_range_count(manifest):
    total = 0
    for model in manifest['models']:
        total += len(model['ranges'])
    return total


CLAIM = {
    'name': 'fixtures_provenance',
    'issues': [31],
    'scope': 'sha256-pinned byte ranges of six public checkpoints, re-hashed from the fetched cache build/fixtures (not committed, not re-fetched); not a check of the Hugging Face side',
    'spec': 'specs/memory/fixtures_provenance_evidence.t27',
    'vectors': 'conformance/memory_fixtures_provenance_evidence.json',
    'accept': 'tmfp_accept',
    'expected': (6, 554, 227142652, 26, 1),
    'manifest': 'fixtures/evidence-manifest.json',
    'bind': [
        'fixtures/manifest.json', 'fixtures/manifest.lock.json',
        'tools/fetch-fixtures.py', 'tools/bitnet_audit.py',
        'trinity_memory/__init__.py', 'trinity_memory/fixtures.py', 'trinity_memory/formats.py',
        'tests/test_fixtures.py', AUDIT,
        'tools/evidence/fixtures_provenance.py',
    ],
    'counts': counts,
}
