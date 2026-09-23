"""Issue #33: the real-layer matvec of t27/matvec.t27 through its ctypes glue.

The activation stream is compared with CPython's own random module; the
committed report reports/ternary-check/matvec-2026-09-23.json is recomputed
from the fixture cache (never from the network: the tests set
TRINITY_FIXTURES_OFFLINE=1 and skip when a range is missing) and must match
byte for byte. The claims docs/ternary-check.md makes about the real layer are
asserted on the committed report, so they hold without the cache as well.
"""
import ctypes as C
import json
import os
from pathlib import Path
import random
import unittest
from unittest import mock

from trinity_memory import fixtures as fx
from trinity_memory import formats as f
from trinity_memory import matvec as mv
from trinity_memory import ternary_check as tc

ROOT = Path(__file__).resolve().parent.parent


def _i32(values):
    return (C.c_int32 * len(values))(*values)


class ActivationsTest(unittest.TestCase):
    def test_stream_is_cpython_randint(self):
        for seed in (27, 0, 1, -5, 2 ** 40, 2 ** 63 - 1):
            expected = random.Random(seed)
            x = mv.activations(17408, seed)
            self.assertEqual(list(x), [expected.randint(-128, 127) for _ in range(17408)], seed)

    def test_shorter_vectors_are_prefixes(self):
        full = list(mv.activations(17408))
        for count in (1, 2560, 6912):
            self.assertEqual(list(mv.activations(count)), full[:count])


class ProductTest(unittest.TestCase):
    def test_small_product_partials_and_rejections(self):
        rng = random.Random(33)
        rows, cols, group = 5, 192, 64
        weights = [rng.choice((-1, 0, 1)) for _ in range(rows * cols)]
        x = mv.activations(cols)
        partials, y = mv.matvec(_i32(weights), rows, cols, x, group)
        for r in range(rows):
            row = [weights[r * cols + k] * x[k] for k in range(cols)]
            self.assertEqual([partials[r * 3 + j] for j in range(3)],
                             [sum(row[j * group:(j + 1) * group]) for j in range(3)])
            self.assertEqual(y[r], sum(row))
        weights[77] = 2  # 2-bit code 3 decodes to +2: the product refuses it
        with self.assertRaises(f.FormatError) as caught:
            mv.matvec(_i32(weights), rows, cols, x, group)
        self.assertEqual((caught.exception.status, caught.exception.token), (-53, "code"))
        with self.assertRaises(f.FormatError) as caught:
            mv.matvec(_i32(weights), rows, cols, x, 100)
        self.assertEqual(caught.exception.token, "length")

    def test_float_steps(self):
        y = (C.c_int64 * 3)(-2561, 0, 10924)
        out = mv.scale_tensor(y, 3, 0x3F9C, f.BF16)
        self.assertEqual(list(out), [-2561 * 1.21875, 0.0, 10924 * 1.21875])
        with self.assertRaises(f.FormatError) as caught:
            mv.scale_tensor(y, 3, 0x7FC0, f.BF16)
        self.assertEqual(caught.exception.token, "scale_nonfinite")
        partials = (C.c_int64 * 4)(3, -5, 7, 11)
        words = (C.c_uint32 * 2)(0x3C00, 0x3800)  # 1.0 and 0.5, one per two partials
        self.assertEqual(list(mv.scale_groups(partials, 1, 4, words, 2, f.F16, 2)), [3 - 5 + 0.5 * (7 + 11)])
        exact = mv.exact_groups_f16(partials, 1, 4, words, 2, 2)
        self.assertEqual(exact[0], (3 - 5) * 2 ** 24 + (7 + 11) * 2 ** 23)
        self.assertEqual(mv.exact_mismatches(exact, 24, mv.scale_groups(partials, 1, 4, words, 2, f.F16, 2), 1), (0, -1))


class CommittedReportTest(unittest.TestCase):
    """What docs/ternary-check.md says about the real layer, read from the committed report."""

    def setUp(self):
        self.report = json.loads(mv.REPORT.read_text())

    def test_deterministic_content(self):
        text = mv.REPORT.read_text()
        for word in ("generated", "seconds", "timestamp", "duration"):
            self.assertNotIn(f'"{word}"', text)
        self.assertEqual(self.report["compiler"]["commit"], (ROOT / "native" / "compiler.lock").read_text().strip())
        self.assertEqual(self.report["activations"]["first"], [117, 13, 18, -28, -91, -95, 2, 42])

    def test_accumulators_identical_where_the_tensor_is_stored(self):
        bitnet = [t for t in self.report["tensors"] if t["model"] == "BitNet b1.58 2B4T"]
        bonsai = [t for t in self.report["tensors"] if t["model"] == "Ternary Bonsai 2 27B"]
        self.assertEqual(len(bitnet), 2)
        self.assertEqual(len(bonsai), 1)
        for tensor in bitnet:
            same = tensor["comparisons"][0]
            self.assertEqual((same["a"], same["b"]), ("hf_packed", "I2_S"))
            for key in ("trits", "accumulator_rows", "partials"):
                self.assertEqual(same[key], {"differ": 0, "first": -1})
            formats = tensor["formats"]
            self.assertEqual(formats["hf_packed"]["accumulators"], formats["I2_S"]["accumulators"])
            self.assertEqual(same["float_step_rows"]["differ"], tensor["tensor"]["shape"][0])
        for tensor in bonsai:
            self.assertEqual(tensor["tensor"]["shape"], [5120, 17408])
            self.assertEqual(sorted(tensor["formats"]), ["PQ2_0", "PTQ1_0", "Q2_0", "mlx_2bit"])
            for comparison in tensor["comparisons"]:
                for key in ("partials", "accumulator_rows", "float_step_rows", "exact_rows"):
                    self.assertEqual(comparison[key], {"differ": 0, "first": -1}, comparison)
            for label, entry in tensor["formats"].items():
                self.assertEqual(entry["float_step"]["rows_where_f64_is_not_exact"], {"differ": 0, "first": -1})
                if label != "mlx_2bit":
                    self.assertTrue(entry["prism_metadata"])
            self.assertEqual(tensor["formats"]["mlx_2bit"]["float_step"]["groups_where_bias_is_not_minus_scale"], 0)

    def test_numbers_quoted_in_the_docs(self):
        self.assertTrue(self.report["activations"]["sha256_le"].startswith("1987f310"))
        quoted = {"model.layers.0.self_attn.q_proj.weight": (10924, 102400, 1.21875, 1.2188547849655151),
                  "model.layers.0.mlp.down_proj.weight": (18753, 276480, 2.15625, 2.163161277770996)}
        for tensor in self.report["tensors"][:2]:
            top, partials, bf16, f32 = quoted[tensor["tensor"]["hf"]]
            packed, i2s = tensor["formats"]["hf_packed"], tensor["formats"]["I2_S"]
            self.assertEqual((packed["accumulators"]["max_abs"], packed["partials"]["count"]), (top, partials))
            self.assertEqual((packed["float_step"]["scale"]["value"], i2s["float_step"]["scale"]["value"]), (bf16, f32))
        bonsai = self.report["tensors"][2]
        for entry in bonsai["formats"].values():
            self.assertEqual((entry["accumulators"]["max_abs"], entry["partials"]["count"]), (36778, 1392640))
        self.assertEqual(bonsai["formats"]["mlx_2bit"]["float_step"]["scale"]["count"], 696320)

    def test_derived_absmean_differs_exactly_by_the_trit_mismatches(self):
        expected = {"model.layers.0.self_attn.q_proj.weight": (79719, 2529, 2560, 2534),
                    "model.layers.0.mlp.down_proj.weight": (101673, 2549, 2560, 2550)}
        for tensor in self.report["tensors"][:2]:
            trits, rows_differ, rows, touched = expected[tensor["tensor"]["hf"]]
            self.assertEqual(tensor["comparisons"][1]["difference_tensor"]["rows_with_nonzero"], touched)
            derived = tensor["comparisons"][1]
            self.assertEqual((derived["a"], derived["b"]), ("hf_packed", "bf16_absmean"))
            self.assertEqual(derived["trits"]["differ"], trits)
            self.assertEqual(derived["difference_tensor"]["nonzero"], trits)
            self.assertEqual(derived["accumulator_rows"]["differ"], rows_differ)
            self.assertLessEqual(rows_differ, derived["difference_tensor"]["rows_with_nonzero"])
            self.assertLessEqual(derived["difference_tensor"]["rows_with_nonzero"], rows)
            self.assertEqual(derived["difference_tensor"]["rows_where_y_a_minus_y_b_is_not_D_x"],
                             {"differ": 0, "first": -1})
            self.assertTrue(tensor["formats"]["bf16_absmean"]["derived"])


class RecomputeFromCacheTest(unittest.TestCase):
    """Recomputes the whole report from build/fixtures; skipped when the cache lacks a range."""

    def setUp(self):
        self._offline = os.environ.get(fx.OFFLINE_ENV)
        os.environ[fx.OFFLINE_ENV] = "1"
        tc.BITNET._remotes.clear()
        tc.BONSAI._remotes.clear()

    def tearDown(self):
        if self._offline is None:
            os.environ.pop(fx.OFFLINE_ENV, None)
        else:
            os.environ[fx.OFFLINE_ENV] = self._offline
        tc.BITNET._remotes.clear()
        tc.BONSAI._remotes.clear()

    def test_report_reproduces_byte_for_byte(self):
        seen = set()
        read, prefix = fx.Remote.read, fx.Remote.prefix

        def reading(remote, begin, end):
            seen.add((remote.key, begin, end))
            return read(remote, begin, end)

        def prefixing(remote, needed):
            for chunk in remote.entry.prefix:
                seen.add((remote.key, chunk["begin"], chunk["end"]))
                if chunk["end"] >= needed:
                    break
            return prefix(remote, needed)
        try:
            with mock.patch.object(fx.Remote, "read", reading), mock.patch.object(fx.Remote, "prefix", prefixing):
                text = mv.dumps(mv.run())
        except fx.FixtureError as error:
            self.skipTest(f"fixture cache incomplete (python3 tools/fetch-fixtures.py): {error}")
        self.assertEqual(text, mv.REPORT.read_text())
        # The manifest tags exactly the ranges the report reads with its consumer.
        manifest = fx.Manifest(fx.MANIFEST)
        tagged = {(entry.key, begin, end) for entry in manifest.entries.values()
                  for (begin, end), record in entry.ranges.items() if "layer0_matvec" in record["used_by"]}
        self.assertEqual(seen, tagged)
        self.assertEqual(len(seen), 36)

    def test_bonsai_gguf_files_declare_a_hadamard_rotation(self):
        for label in ("PTQ1_0", "PQ2_0", "Q2_0"):
            remote = tc.BONSAI[label]
            try:
                prefix = remote.prefix(remote.entry.prefix_end)
            except fx.FixtureError as error:
                self.skipTest(f"fixture cache incomplete: {error}")
            self.assertIn(b"prism.hadamard.transform", prefix, label)
            self.assertIn(b"normalized-sylvester-walsh-hadamard", prefix, label)


if __name__ == "__main__":
    unittest.main()
