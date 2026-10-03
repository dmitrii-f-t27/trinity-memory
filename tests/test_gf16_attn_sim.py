"""The gf16-attn-v1 controller RTL (t27/rtl/gf16_attn.t27) against the
attention oracle: the tiny two-head model replays causal 1, 2 and 8 position
runs plus result-only mode in iverilog, every stage value bit for bit.

Skips when the pinned t27 compiler or iverilog is not available locally; the
CI workflow builds the compiler and runs the same simulation on both
platforms.
"""
import os
import shutil
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import gf16_wide_build as base  # noqa: E402

HAS_COMPILER = (base.ROOT / "build/compiler/target/release/t27c").is_file()
HAS_IVERILOG = shutil.which("iverilog") is not None


@unittest.skipUnless(HAS_COMPILER and HAS_IVERILOG,
                     "pinned t27 compiler and iverilog required")
class AttentionRtlTest(unittest.TestCase):
    def _simulate(self, xs, result=False):
        import tempfile
        from tools import gf16_attn_build as build
        from tools import gf16_attn_vectors as vectors
        simulator = os.environ.get("TRINITY_ATTN_SIMULATOR", "iverilog")
        with tempfile.TemporaryDirectory() as work:
            return build.simulate(Path(work), vectors.tiny_model(7), xs,
                                  simulator=simulator, result=result)

    def test_single_position_replay(self):
        out = self._simulate([[3, -5, 7, 1, -2, 4, 0, -8]])
        self.assertEqual(out["positions"], 1)
        self.assertEqual(out["profile"], "gf16-attn-v1")

    def test_two_positions_rope_and_softmax(self):
        # position 1 exercises the real rotate_half and a two-entry softmax
        out = self._simulate([[3, -5, 7, 1, -2, 4, 0, -8], [1, 2, 3, 4, 5, 6, 7, 8]])
        self.assertEqual(out["positions"], 2)

    def test_eight_positions_kv_cache(self):
        import random
        rng = random.Random(3)
        xs = [[rng.randrange(-128, 128) for _ in range(8)] for _ in range(8)]
        out = self._simulate(xs)
        self.assertEqual(out["positions"], 8)
        self.assertLess(out["clock_split"]["report_wait"], out["clock_split"]["total"])

    def test_result_only_mode(self):
        import random
        rng = random.Random(3)
        xs = [[rng.randrange(-128, 128) for _ in range(8)] for _ in range(8)]
        out = self._simulate(xs, result=True)
        self.assertEqual(out["stage_values"], 64)
        self.assertLess(out["clock_split"]["report_wait"], 512)

    def test_nondefault_run_id_and_mismatched_capture_rejected(self):
        import tempfile
        from tools import attn_reference as ar
        from tools import gf16_attn_build as build
        from tools import gf16_attn_vectors as vectors
        model=vectors.tiny_model(7)
        xs=[[v << ar.Q for v in (3,-5,7,1,-2,4,0,-8)]]
        tables=ar.rope_tables(1,ar.ROPE_THETA,4)[1]
        stages,_=ar.fpga_q16(model,xs,tables)
        with tempfile.TemporaryDirectory() as tmp:
            work=Path(tmp)
            inp=vectors.write_inputs(work,model,xs,tables,run=37)
            command=build.compile_sim(work,build.generate(work)[0],(8,4,2,4),
                                      os.environ.get('TRINITY_ATTN_SIMULATOR','iverilog'))
            capture=work/'capture.txt'
            base.run([*command,'+input='+str(inp),'+output='+str(capture)],timeout=60)
            raw=capture.read_bytes()
            self.assertEqual(vectors.validate(raw,stages,run=37)['run'],37)
            with self.assertRaises(ValueError):vectors.validate(raw,stages,run=1)
            # Completion and start must both carry the requested run ID.
            mixed=raw.replace(b'd00000025',b'd00000001',1)
            with self.assertRaises(ValueError):vectors.validate(mixed,stages,run=37)
            mixed=raw.replace(b'z00000025',b'z00000001',1)
            with self.assertRaises(ValueError):vectors.validate(mixed,stages,run=37)


if __name__ == "__main__":
    unittest.main()
