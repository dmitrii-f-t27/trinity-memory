"""Native public API parity, independent frozen oracle, and missing-library failure."""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests/reference"))
from trinity_memory import codecs, container
from trinity_memory import tensorpack as tp
from trinity_memory import _native
from trinity_memory_reference import codecs as reference_codecs
from trinity_memory_reference import container as reference_container
from trinity_memory_reference import tensorpack as reference_tp


class NativeFacadeTests(unittest.TestCase):
    def test_frozen_reference_hashes_are_unchanged(self):
        manifest = json.loads((ROOT / "tests/reference/manifest.json").read_text())
        for name, digest in manifest["files_sha256"].items():
            with self.subTest(name=name):
                path = ROOT / "tests/reference/trinity_memory_reference" / name
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_all_codec_bytes_and_metadata_match_independent_reference(self):
        rng = random.Random(20260910)
        for codec in codecs.CODECS:
            for count in (0, 1, 2, 4, 5, 8, 17, 22, 23, 31, 128, 517):
                c = codecs.CODECS[codec]
                if c.max_nonzero is None:
                    values = [rng.choice((-1, 0, 1)) for _ in range(count)]
                else:
                    # Data synthesis uses the frozen oracle's exhaustive states.
                    values = []
                    while len(values) < count:
                        values.extend(rng.choice(reference_codecs.SPARSE_STATES[codec]))
                    values = values[:count]
                with self.subTest(codec=codec, count=count):
                    expected = reference_codecs.pack(values, codec)
                    self.assertEqual(codecs.pack(values, codec), expected)
                    self.assertEqual(codecs.unpack(expected, count, codec), values)
                    blob = reference_container.encode_file(values, codec)
                    self.assertEqual(container.encode_file(values, codec), blob)
                    self.assertEqual(container.decode_file(blob), values)
                    self.assertEqual(container.inspect_file(blob), reference_container.inspect_file(blob))

    def test_tensorpack_unicode_float_scaling_and_multi_tensor_byte_parity(self):
        scales = (5e-324, 0.0001, 1e-5, 1.0, 1e15, 1e16, 1.7976931348623157e308)
        for scale in scales:
            source = [tp.Tensor("слой/信号", (2, 3), (-1, 0, 1, 1, -1, 0),
                                "dense22", (scale, scale), 0, ("выход", "вход")),
                      tp.Tensor("scalar", (), (1,), "baseline2")]
            reference = [reference_tp.Tensor(**asdict(tensor)) for tensor in source]
            blob = reference_tp.encode_tensors(reference)
            with self.subTest(scale=scale):
                self.assertEqual(tp.encode_tensors(source), blob)
                self.assertEqual(tp.decode_tensors(blob), source)
                self.assertEqual(tp.inspect_tensorpack(blob), reference_tp.inspect_tensorpack(blob))

    def test_no_python_fallback_when_explicit_library_is_missing(self):
        environment = dict(os.environ, TRINITY_MEMORY_NATIVE_LIBRARY=str(ROOT / "no-such-native-library.so"))
        completed = subprocess.run([sys.executable, "-c", "from trinity_memory import pack;pack([1])"],
                                   cwd=ROOT, env=environment, capture_output=True, text=True)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("No Python fallback", completed.stderr)

    def test_projection_does_not_silently_round_python_integers(self):
        from trinity_memory.sparsity import project_topk
        self.assertEqual(project_topk([2**53, 2**53 + 1], 2, 1), [0, 1])
        self.assertEqual(project_topk([2**53, 2**53 + 2], 2, 1), [0, 1])

    def test_exact_projection_matches_reference_for_mixed_numeric_inputs(self):
        from trinity_memory.sparsity import project_topk
        from trinity_memory_reference.sparsity import project_topk as reference_topk
        import math
        rng=random.Random(2709)
        boundaries=[0,-0.0,1,-1,2**53,2**53+1,2**64-1,2**80+1,
                    float(2**53),math.nextafter(float(2**53),math.inf),
                    1e100,-1e100,2**1023,2**1023+1]
        for case in range(100):
            values=[rng.choice(boundaries)*rng.choice((-1,1)) for _ in range(rng.randrange(1,30))]
            block=rng.randrange(1,12); k=rng.randrange(block+1)
            with self.subTest(case=case,block=block,k=k):
                self.assertEqual(project_topk(values,block,k),reference_topk(values,block,k))

    def test_invalid_conformance_with_rtl_is_a_fixture_error(self):
        import tempfile
        from trinity_memory.conformance import run_conformance
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "invalid.json"
            source.write_text("{}")
            with self.assertRaises(ValueError):
                run_conformance(source, rtl=True)

    def test_invalid_signal_codec_retains_codec_error_type(self):
        from trinity_memory.edge import signal_model
        with self.assertRaises(codecs.CodecError):
            signal_model("sparse41")

    def test_native_module_abi_is_loaded(self):
        # The public adapters call the same compiled symbols audited in native tests.
        self.assertTrue(_native.library().tm_api_tmem_metadata)
        self.assertTrue(_native.library().tm_rtl_run_frames)
        self.assertTrue(_native.library().tm_bridge_request)


if __name__ == "__main__":
    unittest.main()
