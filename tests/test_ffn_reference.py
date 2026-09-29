"""The layer-0 FFN oracle (tools/ffn_reference.py): rounding primitives, the
Q16.16/Q32.32 datapath on a small synthetic layer, and, when the fixture
cache is present, the real BitNet layer-0 chain against its committed report.

Never from the network: TRINITY_FIXTURES_OFFLINE=1, skip on fixtures.CacheMiss
(fail under TRINITY_REQUIRE_CACHED=1), the same contract as tests/test_matvec.
"""
import json
import math
import os
from pathlib import Path
import random
import unittest

ROOT = Path(__file__).resolve().parent.parent
REQUIRE_CACHED = os.environ.get("TRINITY_REQUIRE_CACHED") == "1"
os.environ.setdefault("TRINITY_FIXTURES_OFFLINE", "1")

import tools.ffn_reference as fr  # noqa: E402

from trinity_memory import fixtures as fx  # noqa: E402


def skip_or_fail(test: unittest.TestCase, why: str):
    if REQUIRE_CACHED:
        test.fail(f"TRINITY_REQUIRE_CACHED=1: {why}")
    test.skipTest(why)


class RoundingTest(unittest.TestCase):
    def test_rne_halves_to_even(self):
        self.assertEqual(fr.rne(4, 1), 2)          # 2.0
        self.assertEqual(fr.rne(5, 1), 2)          # 2.5 -> 2 (even)
        self.assertEqual(fr.rne(7, 1), 4)          # 3.5 -> 4 (even)
        self.assertEqual(fr.rne(-5, 1), -2)        # -2.5 -> -2
        self.assertEqual(fr.rne(6, 1), 3)          # 3.0

    def test_div_rne_halves_to_even(self):
        self.assertEqual(fr.div_rne(5, 2), 2)
        self.assertEqual(fr.div_rne(7, 2), 4)
        self.assertEqual(fr.div_rne(-5, 2), -2)

    def test_to_q(self):
        self.assertEqual(fr.to_q(1.0), 1 << 16)
        self.assertEqual(fr.to_q(-0.5), -(1 << 15))
        self.assertEqual(fr.to_q(2.0 ** -16), 1)
        self.assertEqual(fr.to_q(1.0, 32), 1 << 32)


class DatapathTest(unittest.TestCase):
    def _tiny_model(self, seed=7):
        rng = random.Random(seed)
        model = {
            "gate": [rng.choice((-1, 0, 1)) for _ in range(6 * 8)],
            "up": [rng.choice((-1, 0, 1)) for _ in range(6 * 8)],
            "down": [rng.choice((-1, 0, 1)) for _ in range(4 * 6)],
            "w_post": [1.0, 0.5, -0.25, 1.25, 0.75, -1.0, 0.5, 0.5],
            "w_sub": [0.5, 1.0, -0.5, 0.25, 0.75, 1.0],
            "scales": {"gate": 0.125, "up": 0.0625, "down": 0.25},
            "shapes": {"gate": [6, 8], "up": [6, 8], "down": [4, 6]},
            "packed_sha256": {},
        }
        return model

    def test_small_input_uses_epsilon_inside_square_root(self):
        model = self._tiny_model()
        x = [1e-6] * 8
        ref = fr.reference_f64(model, x)
        denominator = math.sqrt(1e-12 + fr.EPS)
        for got, weight in zip(ref["h"], model["w_post"]):
            self.assertAlmostEqual(got, 1e-6 / denominator * weight, places=14)

        # One Q16.16 unit separates sqrt(mean + eps) from an rms clamp.
        fixed, _ = fr.fpga_q16(model, [1] * 8)
        denominator = math.sqrt((1 / 65536) ** 2 + fr.EPS)
        for got, weight in zip(fixed["h"], model["w_post"]):
            want = fr.to_q((1 / 65536) / denominator * weight)
            self.assertLessEqual(abs(got - want), 1)

    def test_small_wide_norm_uses_same_epsilon_contract(self):
        model = {"gate": [1], "up": [1], "down": [1],
                 "w_post": [1.0], "w_sub": [1.0],
                 "scales": {"gate": 2**-10, "up": 2**-10, "down": 1.0},
                 "shapes": {"gate": [1, 1], "up": [1, 1], "down": [1, 1]}}
        fixed, _ = fr.fpga_q16(model, [1 << fr.Q])
        a = fixed["a"][0] / (1 << fr.QQ)
        want = fr.to_q(a / math.sqrt(a * a + fr.EPS))
        self.assertLessEqual(abs(fixed["s"][0] - want), 1)

    def test_wide_product_clamps_to_signed_64_and_counts(self):
        model = self._tiny_model()
        model["gate"] = model["up"] = [1] * 48
        model["w_post"] = [1.0] * 8
        model["scales"] = {"gate": 32767.0, "up": 32767.0, "down": 1.0}
        positive, counts = fr.fpga_q16(model, [1 << fr.Q] * 8)
        self.assertEqual(positive["a"], [(1 << 63) - 1] * 6)
        self.assertEqual(counts["a"], 6)
        model["up"] = [-1] * 48
        negative, counts = fr.fpga_q16(model, [1 << fr.Q] * 8)
        self.assertEqual(negative["a"], [-(1 << 63)] * 6)
        self.assertEqual(counts["a"], 6)

    def test_zero_input_is_finite_and_zero(self):
        ref = fr.reference_f64(self._tiny_model(), [0] * 8)
        fixed, counts = fr.fpga_q16(self._tiny_model(), [0] * 8)
        self.assertTrue(all(v == 0 for stage in ref.values() for v in stage))
        self.assertTrue(all(v == 0 for stage in fixed.values() for v in stage))
        self.assertTrue(all(v == 0 for v in counts.values()))

    def test_tiny_layer_inside_guard(self):
        model = self._tiny_model()
        x = [3, -5, 7, 1, -2, 4, 0, -8]
        ref = fr.reference_f64(model, x)
        q16, sat = fr.fpga_q16(model, [t << fr.Q for t in x])
        self.assertTrue(all(s == 0 for s in sat.values()), sat)
        for name, values in ref.items():
            bits = fr.STAGE_BITS[name]
            peak = max(abs(fr.to_q(v, bits)) for v in values)
            worst = max(abs(q - fr.to_q(v, bits)) for q, v in zip(q16[name], values))
            self.assertLessEqual(worst, fr.GUARD_ABS + peak * fr.GUARD_REL,
                                 f"stage {name}: {worst} of peak {peak}")

    def test_rmsnorm_is_single_rounding(self):
        # The norm divides the exact numerator once: on this input the Q16.16
        # result equals the f64 reference rounded, to the unit.
        model = self._tiny_model()
        x = [t << fr.Q for t in (3, -5, 7, 1, -2, 4, 0, -8)]
        stages, _ = fr.fpga_q16(model, x)
        mean = math.fsum((t / 65536.0) ** 2 for t in x) / len(x)
        want = [t / 65536.0 / math.sqrt(mean + fr.EPS) * w for t, w in zip(x, model["w_post"])]
        for got, w in zip(stages["h"], want):
            self.assertLessEqual(abs(got - fr.to_q(w)), 1)


class FixtureLayerTest(unittest.TestCase):
    def test_real_layer_matches_committed_report(self):
        path = ROOT / "reports/ffn/reference-2026-09-28.json"
        if not path.is_file():
            skip_or_fail(self, f"committed report {path.name} is absent")
        try:
            model = fr.load_ffn()
        except fx.CacheMiss as miss:
            skip_or_fail(self, f"fixture cache: {miss}")
        report = json.loads(path.read_text())
        ref = fr.reference_f64(model, report["activations_int8"])
        q16, sat = fr.fpga_q16(model, [t << fr.Q for t in report["activations_int8"]])
        stages = fr.compare(ref, q16)
        self.assertEqual(sat, report["saturations"])
        self.assertTrue(all(count == 0 for count in sat.values()), sat)
        self.assertEqual(fr.sha256_le32(q16["y"]), report["y_q16_sha256_le"])
        self.assertEqual(fr.sha256_le32([fr.to_q(v) for v in ref["y"]]),
                         report["y_f64_q16_sha256_le"])
        self.assertEqual(stages, report["stages"])
        self.assertTrue(all(not stage["outside_guard"] for stage in stages.values()), stages)
        self.assertEqual(report["oracle_scope"], fr.ORACLE_SCOPE)
        self.assertEqual(report["rmsnorm_contract"], fr.RMSNORM_CONTRACT)
        self.assertEqual(report["upstream_sources"], fr.UPSTREAM_SOURCES)
        self.assertEqual(report["widths"]["wide_sumsq_unsigned_bits"], 139)
        self.assertEqual(report["verdict"], "pass")


if __name__ == "__main__":
    unittest.main()
