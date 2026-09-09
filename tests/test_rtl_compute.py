"""Public RTL execution checks and protocol regression using actual Icarus."""

import os
import random
import shutil
import tempfile
import unittest
from unittest.mock import patch

from trinity_memory.rtl_compute import RTLSimulationError, _rtl_directory, run_rtl_dot


HAVE_ICARUS = bool(shutil.which("iverilog") and shutil.which("vvp"))


class RTLComputeInputTests(unittest.TestCase):
    def test_validation_rejects_unsupported_types_and_lengths(self):
        cases = [([2], [1], "dense5"), ([True], [1], "dense5"),
                 ([1], [128], "dense5"), ([1], [-129], "dense5"),
                 ([1], [True], "dense5"), ([1], [1.0], "dense5"),
                 ([1, 0], [1], "dense5"), ([1], [1], "sparse41")]
        for weights, activations, codec in cases:
            with self.subTest(weights=weights, activations=activations, codec=codec):
                with self.assertRaises(ValueError):
                    run_rtl_dot(weights, activations, codec)

    def test_explicit_rtl_path_fails_without_tools(self):
        with patch("trinity_memory.rtl_compute.shutil.which", return_value=None):
            with self.assertRaisesRegex(RTLSimulationError, "no software fallback"):
                run_rtl_dot([1], [7])

    def test_explicit_source_root_does_not_silently_use_another_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"TRINITY_MEMORY_RTL_ROOT": temporary}):
                with self.assertRaisesRegex(RTLSimulationError, "RTL sources unavailable"):
                    _rtl_directory()


@unittest.skipUnless(HAVE_ICARUS, "Icarus Verilog required for actual RTL simulation")
class RTLComputeSimulationTests(unittest.TestCase):
    def test_public_api_int8_edges_empty_tail_and_codec_agreement(self):
        rng = random.Random(728)
        fixtures = [([], []), ([-1], [-128]), ([1], [127]),
                    ([-1] * 5, [-128] * 5),
                    ([1, -1, 0, -1, 1, 1], [-128, -128, 127, 127, 127, -128]),
                    ([rng.choice((-1, 0, 1)) for _ in range(517)],
                     [rng.randrange(-128, 128) for _ in range(517)])]
        for weights, activations in fixtures:
            with self.subTest(count=len(weights)):
                dense = run_rtl_dot(weights, activations, "dense5", seed=2026)
                baseline = run_rtl_dot(weights, activations, "baseline2", seed=2026)
                expected = sum(w * a for w, a in zip(weights, activations))
                self.assertEqual(dense["result"], expected)
                self.assertEqual(baseline["result"], expected)
                self.assertEqual(dense["cycles"], baseline["cycles"])
                self.assertEqual(dense["evidence"], "rtl-simulation")
                self.assertGreaterEqual(dense["output_stalls"], 3)
                self.assertFalse(dense["error"])
                groups = max(1, (len(weights) + 4) // 5)
                self.assertEqual(dense["encoded_weight_bits"], groups * 8)
                self.assertEqual(baseline["encoded_weight_bits"], groups * 10)

    def test_protocol_reset_backpressure_malformed_frames_and_overflow(self):
        # Importing this test driver does not run its CLI.
        from scripts.test_dot_rtl import overflow_suite, protocol_suite
        for codec in ("dense5", "baseline2"):
            for seed in (27, 307):
                with self.subTest(codec=codec, seed=seed):
                    report = protocol_suite(codec, seed)
                    self.assertEqual(report["checked_results"], 57)
                    self.assertEqual(report["rejected_frames"], 7)
                    self.assertEqual(report["resets"], 2)
            with self.subTest(codec=codec, accumulator_bits=12):
                report = overflow_suite(codec)
                self.assertEqual(report["checked_results"], 6)
                self.assertEqual(report["rejected_frames"], 3)


if __name__ == "__main__":
    unittest.main()
