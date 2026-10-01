"""Independent integer arithmetic, generated helpers and clocked wide-norm RTL."""
import ctypes
import os
from pathlib import Path
import random
import shutil
import tempfile
import unittest
from tools import gf16_wide_reference as ref, gf16_reference as gf
from tools import gf16_wide_build as build


def cases():
    rng=random.Random(109)
    finite=[g for g in range(65536) if ref.finite(g)]
    rows=[([0]*8,[0,0x8000]*4,[0x3e00]*8),
          ([1,0x200,0x3e00,0x7dff,0xbe00],[1,0x8001,0xbe00,0x7dff,0x3e00],[0x3e00]*5),
          ([0x7dff]*8,[0x7dff]+[0]*7,[0x7dff]*8),
          ([1]*3,[1,0x8001,0],[0x7dff,1,0x8000])]
    for n in (1,2,7,31,73):
        rows.append(tuple([rng.choice(finite) for _ in range(n)] for _ in range(3)))
    return rows


class Oracle(unittest.TestCase):
    def test_bounds_and_epsilon(self):
        for gate,up,weight in cases()+[([0x7dff]*6912,[0x7dff]*6912,[0x3e00]*6912)]:
            r=ref.row(gate,up,weight)
            self.assertLess(r['sum_squares'],1<<95)
            self.assertGreater(r['root'],0)
        self.assertEqual(ref.product(1,1),gf.f32_bits(2**-117))
        self.assertLess(gf.f32_value(ref.product(0x7dff,0x7dff)),2**96)
        self.assertEqual(ref.row([0],[0x8000],[0x3e00])['output'],[0x8000])

    def test_invalid_inputs(self):
        for bad in (-1,65536,0x7e00,0xffff):
            with self.assertRaises(ValueError):ref.product(bad,0)
        with self.assertRaises(ValueError):ref.row([],[],[])
        with self.assertRaises(ValueError):ref.normalize([0],[0,1])
        with self.assertRaises(ValueError):ref.normalize([0x7f800000],[0])

    def test_ratio_nearest_even_and_sticky(self):
        self.assertEqual(ref.encode_ratio(1,1<<40),0)
        self.assertEqual(ref.encode_ratio(3,1<<40),2)
        self.assertEqual(ref.encode_ratio(1025,1<<39),1024)
        self.assertEqual(ref.encode_ratio(4101,1<<41),1025)


class Generated(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not build.COMPILER.is_file() and 'T27_ROOT' not in os.environ:
            raise unittest.SkipTest('generated wide kernel runs in native CI')
        for tool in (str(build.COMPILER),'iverilog','vvp','cc','yosys'):
            if not shutil.which(tool):raise RuntimeError('required tool missing: '+tool)
        cls.temp=tempfile.TemporaryDirectory(prefix='trinity-gf16-wide-');cls.addClassCleanup(cls.temp.cleanup)
        cls.work=Path(cls.temp.name)
        cls.rtl,cls.lib=build.generate(cls.work)
        for name in ('product','multiply'):
            f=getattr(cls.lib,name);f.argtypes=[ctypes.c_uint32]*2;f.restype=ctypes.c_uint32
        cls.lib.quantize.argtypes=[ctypes.c_uint32,ctypes.c_int32];cls.lib.quantize.restype=ctypes.c_uint64
        cls.lib.unit_round.argtypes=[ctypes.c_uint64]*5+[ctypes.c_uint32];cls.lib.unit_round.restype=ctypes.c_uint32

    def test_helper_arithmetic(self):
        rng=random.Random(109)
        # All GF16 words as one operand, then independent random pairings.
        pairs=[(g,0x3e00) for g in range(65536)]+[(rng.randrange(65536),rng.randrange(65536)) for _ in range(100000)]
        for g,u in pairs:
            if not ref.finite(g) or not ref.finite(u):continue
            self.assertEqual(self.lib.product(g,u),ref.product(g,u),(g,u))
            self.assertEqual(self.lib.multiply(g,u),ref.multiply(g,u),(g,u))
        mask=(1<<64)-1
        for g,u,w in cases():
            r=ref.row(g,u,w);den=r['root']
            for p,m,unit in zip(r['product'],r['magnitudes'],r['unit']):
                self.assertEqual(self.lib.quantize(p,r['block_exponent']),m)
                q,rem=divmod(m<<71,den)
                self.assertEqual(self.lib.unit_round(q,rem&mask,rem>>64,den&mask,den>>64,(p>>16)&0x8000),unit)
        # Exercise ties with nonzero division remainder (no double rounding).
        for q in (0,1,511,512,1023,1024,1025,(1<<40)+1024):
            for rem in (0,1,2,3):
                self.assertEqual(self.lib.unit_round(q,rem,0,4,0,0),ref.encode_ratio(q*4+rem,4<<39))

    def test_clocked_rows_and_protocol(self):
        print(build.simulate(self.work,self.rtl,cases()).strip())

    def test_synthesized_rows_and_protocol(self):
        mapped,_=build.synthesize(self.work,self.rtl)
        print(build.simulate(self.work/'mapped',[mapped],cases()[:3]).strip())

    def test_generated_rtl_arithmetic(self):
        rng=random.Random(110)
        pairs=[(g,0x7dff) for g in range(65536)]
        pairs += [(rng.randrange(65536),rng.randrange(65536)) for _ in range(100000)]
        pairs=[(a,b) for a,b in pairs if ref.finite(a) and ref.finite(b)]
        memory=self.work/'arithmetic.mem'
        memory.write_text(''.join(f'{a:08x}{b:08x}{ref.product(a,b):08x}{ref.multiply(a,b):08x}\n' for a,b in pairs))
        tb=self.work/'arithmetic_tb.v'
        tb.write_text(f'''module arithmetic_tb;
reg [127:0] probes[0:{len(pairs)-1}]; integer i;
TrinityGf16WideNormT27 dut(.clk(1'b0),.rst_n(1'b0),.en(1'b0),.gate_in(32'd0),.up_in(32'd0));
initial begin
  $readmemh("{memory}",probes);
  for(i=0;i<{len(pairs)};i=i+1) begin
    if(dut.product(probes[i][127:96],probes[i][95:64])!==probes[i][63:32] ||
       dut.multiply(probes[i][127:96],probes[i][95:64])!==probes[i][31:0])
       $fatal(1,"arithmetic pair %d",i);
  end
  $display("PASS arithmetic pairs={len(pairs)}");$finish;
end
endmodule''')
        exe=self.work/'arithmetic.vvp'
        build.run(['iverilog','-g2012','-s','arithmetic_tb','-o',exe,self.rtl[0],tb])
        print(build.run(['vvp',exe],timeout=120).strip())

    def test_helper_c_sanitizers(self):
        harness=self.work/'sanitize.c'
        harness.write_text('''#include "helpers.c"
#include <assert.h>
int main(void) {
    unsigned state=109;
    for(unsigned i=0;i<300000;i++) {
        state=1664525u*state+1013904223u; unsigned a=state&65535;
        state=1664525u*state+1013904223u; unsigned b=state&65535;
        if(finite(a)&&finite(b)) { unsigned p=product(a,b); (void)multiply(a,b);
            int e=(int)((p>>23)&255)-167; if(e< -54)e=-54;
            assert(quantize(p,e)<(1ull<<41)); (void)eps_low(e);(void)eps_high(e);
        }
    }
    for(int e=-54;e<=55;e++){(void)eps_low(e);(void)eps_high(e);}
    return 0;
}
''')
        exe=self.work/'sanitize'
        build.run(['cc','-std=c11','-O1','-g','-Wno-parentheses-equality','-fsanitize=address,undefined',
                   '-fno-sanitize-recover=all',harness,'-o',exe])
        build.run([exe],timeout=60)


if __name__=='__main__':unittest.main()
