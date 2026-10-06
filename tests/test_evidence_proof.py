"""The evidence registry must reject altered, missing and unlisted evidence and stale vectors."""
import importlib.util
import json
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('evidence_proof', Path(__file__).parents[1] / 'tools/evidence-proof.py')
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)

CLAIM = {'name': 'demo', 'issues': [1], 'spec': 'specs/demo.t27', 'vectors': 'conformance/demo.json', 'accept': 'demo_accept',
         'expected': (2, 5), 'manifest': 'reports/demo/evidence-manifest.json', 'bind': ['reports/demo/*.txt'],
         'scope': 'demo', 'counts': lambda root: (2, 5)}


def tree(directory):
    root = Path(directory)
    (root / 'specs').mkdir()
    (root / 'specs/demo.t27').write_text('module Demo {}')
    (root / 'reports/demo').mkdir(parents=True)
    (root / 'reports/demo/a.txt').write_text('one')
    (root / 'reports/demo/b.txt').write_text('two')
    return root


class Vectors(unittest.TestCase):
    def test_only_all_flags_and_exact_counts_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = proof.vectors(CLAIM, tree(directory))['vectors']
        accepted = [r['input'] for r in rows if r['expected']]
        self.assertEqual(accepted, [[True, True, True, 2, 5]])
        for flag in range(3):
            row = [True, True, True, 2, 5]
            row[flag] = False
            self.assertIn({'input': row, 'expected': False}, rows)
        for index, other in [(3, 1), (3, 3), (4, 4), (4, 6)]:
            row = [True, True, True, 2, 5]
            row[index] = other
            self.assertIn({'input': row, 'expected': False}, rows)

    def test_vectors_are_bound_to_the_spec_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = tree(directory)
            first = proof.vectors(CLAIM, root)['spec_hash']
            (root / 'specs/demo.t27').write_text('module Demo { }')
            self.assertNotEqual(first, proof.vectors(CLAIM, root)['spec_hash'])


class Manifest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = tree(self.directory.name)
        (self.root / CLAIM['manifest']).write_text(json.dumps(proof.build_manifest(CLAIM, self.root)))

    def tearDown(self):
        self.directory.cleanup()

    def test_exact_tree_passes(self):
        self.assertEqual(len(proof.check_manifest(CLAIM, self.root)['files']), 2)

    def test_changed_byte_is_rejected(self):
        (self.root / 'reports/demo/a.txt').write_text('One')
        with self.assertRaisesRegex(ValueError, 'differs from the manifest'):
            proof.check_manifest(CLAIM, self.root)

    def test_size_change_is_rejected(self):
        (self.root / 'reports/demo/a.txt').write_text('one ')
        with self.assertRaisesRegex(ValueError, 'differs from the manifest'):
            proof.check_manifest(CLAIM, self.root)

    def test_missing_file_is_rejected(self):
        (self.root / 'reports/demo/b.txt').unlink()
        with self.assertRaisesRegex(ValueError, 'bound files differ'):
            proof.check_manifest(CLAIM, self.root)

    def test_unlisted_file_is_rejected(self):
        (self.root / 'reports/demo/c.txt').write_text('three')
        with self.assertRaisesRegex(ValueError, 'bound files differ'):
            proof.check_manifest(CLAIM, self.root)

    def test_pattern_that_matches_nothing_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'matches nothing'):
            proof.bound_files({**CLAIM, 'bind': ['reports/none/*.txt']}, self.root)

    def test_manifest_of_another_claim_is_rejected(self):
        manifest = json.loads((self.root / CLAIM['manifest']).read_text())
        manifest['claim'] = 'other'
        (self.root / CLAIM['manifest']).write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'wrong evidence manifest'):
            proof.check_manifest(CLAIM, self.root)


class Registry(unittest.TestCase):
    def test_claim_name_must_equal_its_file_name(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'one.py').write_text("CLAIM = {'name': 'two', 'issues': [1]}\n")
            with self.assertRaisesRegex(ValueError, 'must equal its file name'):
                proof.load_claims(Path(directory))

    def test_helper_modules_with_an_underscore_are_not_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / '_helper.py').write_text('raise RuntimeError("must not load")\n')
            self.assertEqual(proof.load_claims(Path(directory)), {})

    def test_committed_claims_list_their_issues_and_vectors_are_current(self):
        claims = proof.load_claims()
        for claim in claims.values():
            self.assertTrue(claim['issues'])
            proof.vectors_current(claim)


@unittest.skipUnless(os.environ.get('T27_ROOT'), 'requires the pinned native compiler')
class NativeReplay(unittest.TestCase):
    def test_altered_expected_vector_is_rejected_by_generated_code(self):
        claim = next(iter(proof.load_claims().values()))
        rows = proof.vectors_current(claim)['vectors']
        rows[0]['expected'] = not rows[0]['expected']
        with self.assertRaises(subprocess.CalledProcessError):
            proof.native_replay(claim, proof.ROOT, rows)

    def test_wrong_count_is_rejected_by_generated_code(self):
        claim = next(iter(proof.load_claims().values()))
        rows = proof.vectors_current(claim)['vectors']
        wrong = [True, True, True, *[e + 1 for e in claim['expected']]]
        with self.assertRaises(subprocess.CalledProcessError):
            proof.native_replay(claim, proof.ROOT, rows, wrong)


if __name__ == '__main__':
    unittest.main()
