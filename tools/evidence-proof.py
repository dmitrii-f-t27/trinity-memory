#!/usr/bin/env python3
"""Replay the sealed evidence contract of every registered claim.

A claim (tools/evidence/<name>.py) ties one or more closed issues to retained
measurements or reports. For each claim this tool:

  1. checks that the committed conformance vectors are exactly the truth table
     of the claim's acceptance rule (an independent oracle written here);
  2. checks that every bound evidence file and source still has the sha256 and
     size recorded in the claim's evidence manifest, and that no file the claim
     binds is missing from the manifest;
  3. runs the existing offline verifiers of the claim (never hardware) and
     reads the counts they establish;
  4. verifies the seal of the claim's spec with the pinned native compiler,
     generates C from the spec, runs the spec's own tests, replays every
     committed vector through the generated acceptance function, and finally
     feeds the counts of step 3 to the same function.

The sealed spec only states what must be counted; the counting is done by the
verifiers named in the claim. A claim with an altered file, a changed spec, a
stale vector or a wrong count is rejected. This revalidates retained evidence;
it does not operate hardware or fetch anything.
"""
import argparse
import hashlib
import importlib.util
import itertools
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CLAIMS = ROOT / 'tools' / 'evidence'
MANIFEST_SCHEMA = 'trinity-evidence-manifest-v1'
FLAGS = 3  # source_current, evidence_exact, replay_passed


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load_claims(directory=CLAIMS):
    claims = {}
    for path in sorted(directory.glob('*.py')):
        if path.name.startswith('_'):
            continue
        module = importlib.util.spec_from_file_location('evidence_claim_' + path.stem, path)
        loaded = importlib.util.module_from_spec(module)
        module.loader.exec_module(loaded)
        claim = loaded.CLAIM
        claim['module'] = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
        if claim['name'] in claims or claim['name'] != path.stem:
            raise ValueError('claim name must equal its file name and be unique: ' + path.name)
        claims[claim['name']] = claim
    return claims


def variants(expected):
    return sorted({0, max(expected - 1, 0), expected, expected + 1, expected + 2})


def vectors(claim, root=ROOT):
    """The acceptance rule on its edges, from an oracle independent of the spec.

    The rule is a conjunction, so the table holds: every flag combination at the
    exact counts, and with all flags true each count altered on its own, all
    counts zero and all counts one too high. The browser reads this file on every
    check, so it stays small instead of growing with the product of the counts.
    """
    expected = tuple(claim['expected'])
    ok = (True,) * FLAGS

    def row(flags, counts):
        return {'input': [*flags, *counts], 'expected': bool(all(flags) and tuple(counts) == expected)}

    rows = [row(flags, expected) for flags in itertools.product([False, True], repeat=FLAGS)]
    for index, value in enumerate(expected):
        for other in variants(value):
            if other != value:
                counts = list(expected)
                counts[index] = other
                rows.append(row(ok, counts))
    rows.append(row(ok, [0] * len(expected)))
    rows.append(row(ok, [e + 1 for e in expected]))
    spec = root / claim['spec']
    return {'spec_path': claim['spec'], 'spec_hash': 'sha256:' + sha(spec.read_bytes()), 'vectors': rows}


def bound_files(claim, root=ROOT):
    names = set()
    for pattern in claim['bind']:
        matches = sorted(p for p in root.glob(pattern) if p.is_file())
        if not matches:
            raise ValueError('bound pattern matches nothing: ' + pattern)
        for path in matches:
            relative = PurePosixPath(path.relative_to(root).as_posix())
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('unsafe binding: ' + str(relative))
            names.add(str(relative))
    names.discard(claim['manifest'])
    return sorted(names)


def build_manifest(claim, root=ROOT):
    files = {}
    for name in bound_files(claim, root):
        data = (root / name).read_bytes()
        files[name] = {'bytes': len(data), 'sha256': sha(data)}
    return {'schema': MANIFEST_SCHEMA, 'claim': claim['name'], 'issues': claim['issues'], 'files': files}


def check_manifest(claim, root=ROOT):
    manifest = json.loads((root / claim['manifest']).read_text())
    if manifest.get('schema') != MANIFEST_SCHEMA or manifest.get('claim') != claim['name']:
        raise ValueError('wrong evidence manifest')
    recorded = manifest['files']
    current = bound_files(claim, root)
    if sorted(recorded) != current:
        raise ValueError('bound files differ from the manifest: ' + ', '.join(sorted(set(recorded) ^ set(current))))
    for name, record in recorded.items():
        data = (root / name).read_bytes()
        if len(data) != record['bytes'] or sha(data) != record['sha256']:
            raise ValueError('evidence differs from the manifest: ' + name)
    return manifest


def compiler(root):
    compiler_root = Path(os.environ['T27_ROOT']).resolve()
    pin = (root / 'native/compiler.lock').read_text().strip()
    actual = subprocess.check_output(['git', '-C', str(compiler_root), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != pin:
        raise ValueError('compiler pin mismatch')
    subprocess.run(['git', '-C', str(compiler_root), 'diff', '--quiet', 'HEAD', '--', 'bootstrap', 'Cargo.toml', 'Cargo.lock'], check=True)
    return compiler_root / 'target/release/t27c'


def native_replay(claim, root, rows, actual=None):
    tool = compiler(root)
    spec = claim['spec']
    subprocess.run([str(tool), 'seal', '--verify', spec], cwd=root, check=True)
    header = subprocess.check_output([str(tool), 'gen-c', spec], cwd=root, text=True)
    cc = os.environ.get('CC', 'cc')
    accept = claim['accept']
    with tempfile.TemporaryDirectory(prefix='evidence-proof-') as directory:
        work = Path(directory)
        (work / 'proof.h').write_text(header)
        (work / 'tests.c').write_text('#define T27_TEST_MAIN\n#include "proof.h"\n')
        subprocess.run([cc, '-std=c11', '-O1', str(work / 'tests.c'), '-o', str(work / 'tests')], check=True)
        subprocess.run([str(work / 'tests')], check=True)
        body = ['#include "proof.h"', 'int main(void) {']
        for row in rows:
            values = ','.join(str(int(v)) for v in row['input'])
            body.append(f'if ({accept}({values}) != {int(row["expected"])}) return 1;')
        if actual is not None:
            body.append(f'if (!{accept}(' + ','.join(str(int(v)) for v in actual) + ')) return 2;')
        body += ['return 0;', '}']
        (work / 'replay.c').write_text('\n'.join(body) + '\n')
        subprocess.run([cc, '-std=c11', '-O1', str(work / 'replay.c'), '-o', str(work / 'replay')], check=True)
        subprocess.run([str(work / 'replay')], check=True)


def vectors_current(claim, root=ROOT):
    committed = json.loads((root / claim['vectors']).read_text())
    if committed != vectors(claim, root):
        raise ValueError('conformance vectors stale or altered: ' + claim['name'])
    return committed


def prove(claim, root=ROOT, vectors_only=False):
    committed = vectors_current(claim, root)
    if vectors_only:
        native_replay(claim, root, committed['vectors'])
        return {'claim': claim['name'], 'vectors_executed': len(committed['vectors']), 'pass': True}
    manifest = check_manifest(claim, root)
    counts = tuple(int(v) for v in claim['counts'](root))
    if len(counts) != len(claim['expected']):
        raise ValueError('wrong number of counts from the verifiers: ' + claim['name'])
    check_manifest(claim, root)
    native_replay(claim, root, committed['vectors'], [True, True, True, *counts])
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=root, text=True).strip())
    return {
        'schema': 'trinity-evidence-proof-v1', 'claim': claim['name'], 'issues': claim['issues'],
        'scope': claim['scope'], 'source_commit': commit, 'source_dirty': dirty,
        'spec_path': claim['spec'], 'spec_hash': committed['spec_hash'],
        'vectors_executed': len(committed['vectors']), 'counts': list(counts),
        'bound_files': len(manifest['files']), 'pass': True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--claim', action='append', help='only this claim (repeatable); default all')
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--write-vectors', action='store_true')
    parser.add_argument('--write-manifest', action='store_true')
    parser.add_argument('--vectors-only', action='store_true')
    parser.add_argument('--output', type=Path, default=ROOT / 'build/evidence-proof')
    args = parser.parse_args()
    claims = load_claims()
    chosen = [claims[name] for name in args.claim] if args.claim else list(claims.values())
    if args.list:
        for claim in claims.values():
            print(claim['name'], ' '.join('#' + str(i) for i in claim['issues']), claim['spec'])
        return 0
    if args.write_vectors or args.write_manifest:
        for claim in chosen:
            if args.write_vectors:
                (ROOT / claim['vectors']).write_text(json.dumps(vectors(claim), indent=2) + '\n')
            if args.write_manifest:
                (ROOT / claim['manifest']).write_text(json.dumps(build_manifest(claim), indent=2, sort_keys=True) + '\n')
        return 0
    if not __debug__:
        raise ValueError('evidence replay requires Python assertions')
    args.output.mkdir(parents=True, exist_ok=True)
    for claim in chosen:
        result = prove(claim, vectors_only=args.vectors_only)
        (args.output / (claim['name'] + '.json')).write_text(json.dumps(result, indent=2) + '\n')
        print(f'PASS {claim["name"]} ({" ".join("#" + str(i) for i in claim["issues"])}): '
              f'{result["vectors_executed"]} vectors' + ('' if args.vectors_only else f', counts {result["counts"]}'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
