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

    def test_multi_group_gqa_scores(self):
        # heads 4 over kv_heads 2: heads 2,3 must read the second kv group.
        # A signed-comparison bug in the row maximum only fires when a row's
        # true maximum is negative, which the one-kv-head fixtures never hit.
        import random
        from tools import gf16_attn_build as build
        from tools import gf16_attn_vectors as vectors
        import tempfile
        rng = random.Random(21)
        dims = {"hidden": 32, "heads": 4, "kv_heads": 2, "head_dim": 8,
                "kv_dim": 16, "group": 2, "pairs": 4}
        model = {
            "q": [rng.choice((-1, 0, 1)) for _ in range(32 * 32)],
            "k": [rng.choice((-1, 0, 1)) for _ in range(16 * 32)],
            "v": [rng.choice((-1, 0, 1)) for _ in range(16 * 32)],
            "o": [rng.choice((-1, 0, 1)) for _ in range(32 * 32)],
            "dims": dims,
            "w_in": [rng.uniform(-2, 2) for _ in range(32)],
            "w_sub": [rng.uniform(-2, 2) for _ in range(32)],
            "scales": {"q": 0.05, "k": 0.07, "v": 0.11, "o": 0.03},
            "packed_sha256": {},
        }
        xs = [[rng.randrange(-128, 128) for _ in range(32)] for _ in range(2)]
        with tempfile.TemporaryDirectory() as work:
            out = build.simulate(Path(work), model, xs)
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

    def test_repeated_run_replaces_kv_cache_without_reset(self):
        import tempfile
        from tools import attn_reference as ar
        from tools import gf16_attn_build as build
        from tools import gf16_attn_vectors as vectors
        model=vectors.tiny_model(7)
        xs=[[v << ar.Q for v in x] for x in ((3,-5,7,1,-2,4,0,-8),(1,2,3,4,5,6,7,8))]
        tables=ar.rope_tables(2,ar.ROPE_THETA,4)[1]
        stages,_=ar.fpga_q16(model,xs,tables)
        zero,_=ar.fpga_q16(model,[[0]*8 for _ in range(2)],tables)
        with tempfile.TemporaryDirectory() as tmp:
            work=Path(tmp); inp=vectors.write_inputs(work,model,xs,tables,run=37)
            command=build.compile_sim(work,build.generate(work)[0],(8,4,2,4,1),
                    os.environ.get('TRINITY_ATTN_SIMULATOR','iverilog'),test_params={'REPEATS':2})
            capture=work/'capture.txt'
            base.run([*command,'+input='+str(inp),'+output='+str(capture)],timeout=120)
            first,end,second=capture.read_bytes().partition(b'z000000250000000000\n')
            self.assertTrue(end)
            self.assertTrue(vectors.validate(first+end,stages,run=37,positions=2)['pass'])
            self.assertTrue(vectors.validate(second,zero,run=38,positions=2)['pass'])

    def test_shared_multiplier_modulo_and_signed_patterns(self):
        import tempfile
        import random
        from tools import gf16_attn_build as build
        rng=random.Random(115)
        pairs=[(0,0),(0,2**64-1),(2**64-1,2**64-1),(2**63,2),
               (2**63,2**63),(2**64-65536,131072),(2**32-1,2**32-1)]
        pairs += [(rng.getrandbits(64),rng.getrandbits(64)) for _ in range(32)]
        with tempfile.TemporaryDirectory() as tmp:
            work=Path(tmp);rtl=build.generate(work)[0]
            calls='\n'.join(f"check_product(64'h{a:016x},64'h{b:016x},64'h{(a*b)&(2**64-1):016x},64'd{(a+b)>>64});"
                            for a,b in pairs)
            tb=work/'tb_mul.v';tb.write_text('''`timescale 1ns/1ps
module tb_mul;
reg clk=0,rst_n=0; always #5 clk=~clk;
TrinityGf16AttnT27 dut(.clk(clk),.rst_n(rst_n),.en(1'b1),.calib(1'b1),
 .stall(1'b0),.ack(1'b0),.rdata_lo(64'd0),.rdata_hi(64'd0),
 .hidden(32'd8),.kv(32'd4),.heads(32'd2),.kv_heads(32'd1),.head_dim(32'd4),
 .line_idle(1'b1),.s_go(1'b0),.s_tag(32'd0),.s_a(32'd0),.s_b(64'd0));
task check_product(input [63:0] a,b,wanted,wanted_carry);
integer clocks;
begin
 if(dut.carry(a,b)!==wanted_carry)
  $fatal(1,"carry %h + %h got %h want %h",a,b,dut.carry(a,b),wanted_carry);
 @(negedge clk);dut.mul_a=a;dut.mul_b=b;dut.mul_return=999;dut.state=400;
 clocks=0;
 while(dut.state!=999 && clocks<67) begin @(negedge clk);#1;clocks=clocks+1;end
 if(dut.state!=999 || dut.mul_product!==wanted)
  $fatal(1,"product %h * %h got %h want %h",a,b,dut.mul_product,wanted);
end
endtask
initial begin
 repeat(3) @(negedge clk);rst_n=1;
'''+calls+'''
 $display("PASS exact shared multiplier");$finish;
end
endmodule
''')
            exe=work/'mul.vvp'
            base.run(['iverilog','-g2012','-s','tb_mul','-o',exe,*rtl,tb],timeout=60)
            self.assertIn('PASS exact shared multiplier',base.run(['vvp',exe],timeout=60))


if __name__ == "__main__":
    unittest.main()
