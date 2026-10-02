"""The layer-0 attention/residual oracle (tools/attn_reference.py): RoPE
identity at position 0, softmax single-rounding invariants, the epsilon
contract of the norms, the residual clamp, and, when the fixture cache is
present, the real BitNet layer-0 chain against its committed report.

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

import tools.attn_reference as ar  # noqa: E402

from trinity_memory import fixtures as fx  # noqa: E402


def skip_or_fail(test: unittest.TestCase, why: str):
    if REQUIRE_CACHED:
        test.fail(f"TRINITY_REQUIRE_CACHED=1: {why}")
    test.skipTest(why)


TINY_DIMS = {"hidden": 8, "heads": 2, "kv_heads": 1, "head_dim": 4,
             "kv_dim": 4, "group": 2, "pairs": 2}


def tiny_model(seed=7):
    rng = random.Random(seed)
    d = TINY_DIMS

    def trits(rows, cols):
        return [rng.choice((-1, 0, 1)) for _ in range(rows * cols)]

    return {
        "q": trits(d["hidden"], d["hidden"]),
        "k": trits(d["kv_dim"], d["hidden"]),
        "v": trits(d["kv_dim"], d["hidden"]),
        "o": trits(d["hidden"], d["hidden"]),
        "dims": dict(d),
        "w_in": [1.0, 0.5, -0.25, 1.25, 0.75, -1.0, 0.5, 0.5],
        "w_sub": [1.0, 0.75, -0.5, 0.25, 1.25, -1.0, 0.5, 0.5],
        "scales": {"q": 0.125, "k": 0.0625, "v": 0.25, "o": 0.5},
        "packed_sha256": {},
    }


class TinyLayerTest(unittest.TestCase):
    def _run(self, xs_int, model=None, positions=None, theta=ar.ROPE_THETA):
        model = model or tiny_model()
        positions = positions or len(xs_int)
        tables_f64, tables_q16 = ar.rope_tables(positions, theta, TINY_DIMS["head_dim"])
        ref = ar.reference_f64(model, xs_int, tables_f64)
        q16, sat = ar.fpga_q16(model, [[t << ar.Q for t in x] for x in xs_int], tables_q16)
        return ref, q16, sat

    def test_position_zero_rope_identity_and_softmax_returns_v(self):
        xs = [[3, -5, 7, 1, -2, 4, 0, -8]]
        ref, q16, sat = self._run(xs)
        self.assertEqual(q16["qr"], q16["q"])
        self.assertEqual(q16["kr"], q16["k"])
        self.assertEqual(ref["qr"], ref["q"])
        self.assertEqual(ref["kr"], ref["k"])
        # one-entry softmax: the context is exactly v of the same position;
        # with one kv head both query heads share the same four lanes
        self.assertEqual(q16["a"][:4], q16["v"])
        self.assertEqual(q16["a"][4:], q16["v"])
        self.assertEqual(ref["a"][:4], ref["v"])
        self.assertEqual(ref["a"][4:], ref["v"])

    def test_zero_input_is_finite_and_zero(self):
        ref, q16, sat = self._run([[0] * 8, [0] * 8])
        # zero scores give the uniform causal distribution: 1.0 at position 0,
        # 0.5 over the two entries of position 1, per query head
        want_p = [1.0, 1.0] + [0.5] * 4
        want_p_q16 = [1 << ar.Q, 1 << ar.Q] + [1 << (ar.Q - 1)] * 4
        for name, stage in ref.items():
            if name == "p":
                self.assertEqual(stage, want_p)
                continue
            self.assertTrue(all(v == 0 for v in stage), name)
        for name, stage in q16.items():
            if name == "p":
                self.assertEqual(stage, want_p_q16)
                continue
            self.assertTrue(all(v == 0 for v in stage), name)
        self.assertTrue(all(v == 0 for v in sat.values()))

    def test_small_input_uses_epsilon_inside_square_root(self):
        model = tiny_model()
        tables_f64 = ar.rope_tables(1, ar.ROPE_THETA, 4)[0]
        ref = ar.reference_f64(model, [[1e-6] * 8], tables_f64)
        denominator = math.sqrt(1e-12 + ar.EPS)
        for got, weight in zip(ref["n"], model["w_in"]):
            self.assertAlmostEqual(got, 1e-6 / denominator * weight, places=14)

        fixed, _ = ar.fpga_q16(model, [[1] * 8],
                               ar.rope_tables(1, ar.ROPE_THETA, 4)[1])
        denominator = math.sqrt((1 / 65536) ** 2 + ar.EPS)
        for got, weight in zip(fixed["n"], model["w_in"]):
            want = ar.to_q((1 / 65536) / denominator * weight)
            self.assertLessEqual(abs(got - want), 1)

    def test_null_keys_uniform_context_is_rounded_mean_of_values(self):
        model = tiny_model()
        model["k"] = [0] * (TINY_DIMS["kv_dim"] * TINY_DIMS["hidden"])
        xs = [[3, -5, 7, 1, -2, 4, 0, -8], [1, 2, 3, 4, 5, 6, 7, 8], [-7, 6, -5, 4, -3, 2, -1, 0]]
        ref, q16, sat = self._run(xs, model)
        self.assertTrue(all(s == 0 for s in q16["sc"]), q16["sc"])   # all scores zero
        # uniform softmax: the context is the per-lane rounded mean of the
        # causal v rows; both query heads read the same kv head
        for pos in range(3):
            for d in range(4):
                want = ar.div_rne(sum(q16["v"][j * 4 + d] for j in range(pos + 1)), pos + 1)
                self.assertEqual(q16["a"][pos * 8 + d], want)
                self.assertEqual(q16["a"][pos * 8 + 4 + d], want)

    def test_softmax_weights_sum_to_one_per_causal_row(self):
        xs = [[3, -5, 7, 1, -2, 4, 0, -8], [1, 2, 3, 4, 5, 6, 7, 8]]
        _, q16, _ = self._run(xs)
        p, at = q16["p"], 0
        for t in range(2):
            for h in range(TINY_DIMS["heads"]):
                row = p[at:at + t + 1]
                self.assertLessEqual(abs(sum(row) - (1 << ar.Q)), (t + 1) // 2 + 1, (t, h, row))
                at += t + 1
        self.assertEqual(at, len(p))

    def test_residual_saturates_and_counts(self):
        model = tiny_model()
        model["o"] = [1] * (TINY_DIMS["hidden"] * TINY_DIMS["hidden"])
        model["w_sub"] = [4.0] * TINY_DIMS["hidden"]
        model["scales"] = {"q": 2 ** -10, "k": 2 ** -10, "v": 2 ** -10, "o": 32767.0}
        xs = [[1] * 8]
        _, q16, sat = self._run(xs, model)
        self.assertGreater(sat["r"], 0)
        self.assertEqual(q16["r"], [(1 << 31) - 1] * 8)

    def test_tiny_layer_inside_guard(self):
        xs = [[3, -5, 7, 1, -2, 4, 0, -8], [1, 2, 3, 4, 5, 6, 7, 8]]
        ref, q16, sat = self._run(xs)
        self.assertTrue(all(s == 0 for s in sat.values()), sat)
        for name, values in ref.items():
            peak = max(abs(ar.to_q(v, ar.STAGE_BITS[name])) for v in values)
            worst = max(abs(q - ar.to_q(v, ar.STAGE_BITS[name])) for q, v in zip(q16[name], values))
            self.assertLessEqual(worst, ar.GUARD_ABS + peak * ar.GUARD_REL,
                                 f"stage {name}: {worst} of peak {peak}")

    def test_rope_matches_manual_rotate_half_formula(self):
        model = tiny_model()
        tables_f64, _ = ar.rope_tables(2, 1e6, 4)
        ref = ar.reference_f64(model, [[1.0] * 8, [2.0] * 8], tables_f64)
        q = ref["q"][8:16]                    # position 1 owns qr[8:16]
        cos_sin = tables_f64[1]
        for i in range(2):
            cos_f, sin_f = cos_sin[i]
            self.assertAlmostEqual(ref["qr"][8 + i], q[i] * cos_f - q[i + 2] * sin_f, places=12)
            self.assertAlmostEqual(ref["qr"][8 + i + 2], q[i + 2] * cos_f + q[i] * sin_f, places=12)


class FixtureLayerTest(unittest.TestCase):
    def test_real_layer_matches_committed_report(self):
        path = ROOT / "reports/attn/reference-2026-10-02.json"
        if not path.is_file():
            skip_or_fail(self, f"committed report {path.name} is absent")
        try:
            model = ar.load_attn()
        except fx.CacheMiss as miss:
            skip_or_fail(self, f"fixture cache: {miss}")
        report = json.loads(path.read_text())
        tables_f64, tables_q16 = ar.rope_tables(report["positions"])
        ref = ar.reference_f64(model, report["activations_int8"], tables_f64)
        q16, sat = ar.fpga_q16(model, [[t << ar.Q for t in x] for x in report["activations_int8"]],
                               tables_q16)
        stages = ar.compare(ref, q16)
        self.assertEqual(sat, report["saturations"])
        self.assertTrue(all(count == 0 for count in sat.values()), sat)
        hidden = model["dims"]["hidden"]
        self.assertEqual(ar.sha256_le32(q16["r"][-hidden:]), report["r_last_q16_sha256_le"])
        self.assertEqual(ar.sha256_le32([ar.to_q(v) for v in ref["r"][-hidden:]]),
                         report["r_last_f64_q16_sha256_le"])
        self.assertEqual(stages, report["stages"])
        self.assertTrue(all(not stage["outside_guard"] for stage in stages.values()), stages)
        self.assertEqual(report["oracle_scope"], ar.ORACLE_SCOPE)
        self.assertEqual(report["rmsnorm_contract"], ar.RMSNORM_CONTRACT)
        self.assertEqual(report["exp_contract"], ar.EXP_CONTRACT)
        self.assertEqual(report["upstream_sources"], ar.UPSTREAM_SOURCES)
        self.assertEqual(report["seed"], ar.SEED)
        self.assertEqual(report["positions"], ar.POSITIONS)
        for key in ("hidden", "heads", "kv_heads", "head_dim"):
            self.assertEqual(model["dims"][key], report["widths"][key])
        self.assertEqual(report["widths"]["norm_sumsq_bits"], 74)
        self.assertEqual(report["widths"]["proj_acc_bits"], 74)
        self.assertEqual(report["widths"]["score_acc_bits"], 70)
        self.assertEqual(report["widths"]["score_scale_bits"], 86)
        self.assertEqual(report["widths"]["softmax_num_bits"], 51)
        self.assertEqual(report["widths"]["rope_theta"], ar.ROPE_THETA)
        self.assertEqual(report["verdict"], "pass")


if __name__ == "__main__":
    unittest.main()
