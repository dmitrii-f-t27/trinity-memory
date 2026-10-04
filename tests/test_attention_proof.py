"""Current source binding must fail before old passing board receipts are used."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import os
import subprocess

spec = importlib.util.spec_from_file_location('attention_proof', Path(__file__).parents[1] / 'tools/attention-proof.py')
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


class SourceBinding(unittest.TestCase):
    def test_missing_source_binding_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'missing attention'):
            proof.current_sources(Path('.'), {'files': {}})

    def test_changed_measured_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {}
            for name in ['t27/rtl/gf16_attn.t27', 'rtl/t27/gf16_attn.v', 'tools/verify_gf16_attn_board.py']:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'original')
                files['source/' + name] = {'bytes': 8, 'sha256': proof.sha(b'original')}
            self.assertEqual(len(proof.current_sources(root, {'files': files})), 3)
            (root / 't27/rtl/gf16_attn.t27').write_bytes(b'mutation')
            with self.assertRaisesRegex(ValueError, 'differs'):
                proof.current_sources(root, {'files': files})

    def test_archive_hash_and_size_are_both_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / proof.EVIDENCE / 'board-evidence.tar.gz'
            path.parent.mkdir(parents=True)
            path.write_bytes(b'original')
            manifest = {'archive': {'file': path.name, 'bytes': 8, 'sha256': proof.sha(b'original')}}
            self.assertEqual(proof.archive_exact(root, manifest), path)
            for mutation in [b'mutation', b'original-extra']:
                path.write_bytes(mutation)
                with self.assertRaisesRegex(ValueError, 'archive changed'):
                    proof.archive_exact(root, manifest)


@unittest.skipUnless(os.environ.get('T27_ROOT'), 'requires the pinned native compiler')
class NativeReplay(unittest.TestCase):
    def test_altered_expected_vector_is_rejected_by_generated_code(self):
        rows = proof.vectors()['vectors']
        rows[0]['expected'] = True
        with self.assertRaises(subprocess.CalledProcessError):
            proof.native_replay(proof.ROOT, rows)

    def test_changed_spec_cannot_reuse_the_native_seal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = [proof.SPEC, Path('native/compiler.lock'), Path('.trinity/seals/memory_TrinityMemoryAttentionEvidence.json')]
            for name in files:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((proof.ROOT / name).read_bytes())
            path = root / proof.SPEC
            path.write_text(path.read_text().replace('TMAE_PAIRS: u32 = 4', 'TMAE_PAIRS: u32 = 3'))
            with self.assertRaises(subprocess.CalledProcessError):
                proof.native_replay(root, proof.vectors()['vectors'])


if __name__ == '__main__':
    unittest.main()
