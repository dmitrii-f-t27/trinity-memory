"""GF16 contract: independent oracle -> generated C -> RTL -> mapped RTL."""
import ctypes
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import gf16_reference as ref

COMPILER = Path(os.environ.get('T27_ROOT', str(ROOT / 'build/compiler'))) / 'target/release/t27c'
SOURCE = ROOT / 't27/rtl/gf16_codec.t27'


def run(args, **kwargs):
    result = subprocess.run(args, text=True, capture_output=True, **kwargs)
    if result.returncode:
        raise AssertionError(f'{args[0]} failed:\n{result.stdout}\n{result.stderr}')
    return result.stdout


def encode_inputs():
    cases = {ref.decode(g) for g in range(65536)}
    # Every boundary, both signs: immediate binary32 predecessor, exact tie,
    # immediate successor. Includes zero/subnormal and exponent transitions.
    for lo in range(0x7dff):
        mid = ref.f32_bits((ref.POSITIVE[lo] + ref.POSITIVE[lo + 1]) / 2)
        for bits in (mid - 1, mid, mid + 1):
            cases.update((bits, bits | 0x80000000))
    mid = ref.f32_bits(ref.OVERFLOW_MIDPOINT)
    for bits in (mid - 1, mid, mid + 1):
        cases.update((bits, bits | 0x80000000))
    # Every BF16 bit pattern, including NaNs and exponents outside GF16 range.
    cases.update(b << 16 for b in range(65536))
    cases.update((0x7f800001, 0xff800001, 0x7fffffff, 0xffffffff))
    rng = random.Random(103)
    cases.update(rng.getrandbits(32) for _ in range(8192))
    return sorted(cases)


class Oracle(unittest.TestCase):
    def test_known_values(self):
        self.assertEqual(ref.decode(0x3e00), 0x3f800000)  # one
        self.assertEqual(ref.decode(0x3c00), 0x3f000000)  # half
        self.assertEqual(ref.decode(1), ref.f32_bits(2**-39))
        self.assertEqual(ref.decode(0x200), ref.f32_bits(2**-30))
        self.assertEqual(ref.decode(0x7dff), ref.f32_bits(2**32 - 2**22))
        self.assertEqual(ref.encode(ref.f32_bits(2**-40)), 0)
        self.assertEqual(ref.encode(ref.f32_bits(-2**-40)), 0x8000)
        self.assertEqual(ref.encode(ref.f32_bits(3 * 2**-40)), 2)
        self.assertEqual(ref.encode(ref.f32_bits(ref.OVERFLOW_MIDPOINT)), 0x7e00)


class GeneratedCodec(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Required conformance gate: missing tools must not silently pass CI.
        for tool in (str(COMPILER), 'cc', 'iverilog', 'vvp', 'yosys'):
            if not shutil.which(tool):
                raise RuntimeError(f'GF16 conformance requires {tool}')
        pin = (ROOT / 'native/compiler.lock').read_text().strip()
        actual = run(['git', '-C', str(COMPILER.parents[2]), 'rev-parse', 'HEAD']).strip()
        if actual != pin:
            raise RuntimeError(f'GF16 compiler revision mismatch: {actual}, expected {pin}')
        cls.temp = tempfile.TemporaryDirectory(prefix='trinity-gf16-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.work = Path(cls.temp.name)
        cls.c = cls.work / 'codec.c'
        cls.rtl = cls.work / 'codec.v'
        for command, path in [('gen-c', cls.c), ('gen-verilog', cls.rtl)]:
            output = run([str(COMPILER), command, str(SOURCE)])
            if output != run([str(COMPILER), command, str(SOURCE)]):
                raise AssertionError(f'{command} is nondeterministic')
            path.write_text(output)
        lib = cls.work / 'codec.so'
        run(['cc', '-std=c11', '-O2', '-shared', '-fPIC', str(cls.c), '-o', str(lib)])
        cls.lib = ctypes.CDLL(str(lib))
        for name in ('encode', 'decode'):
            getattr(cls.lib, name).argtypes = [ctypes.c_uint32]
            getattr(cls.lib, name).restype = ctypes.c_uint32
        cls.inputs = encode_inputs()
        cls.expected = [ref.encode(bits) for bits in cls.inputs]

    def test_generated_c(self):
        for bits, expected in zip(self.inputs, self.expected):
            self.assertEqual(self.lib.encode(bits), expected, f'encode {bits:08x}')
        for bits in range(65536):
            decoded = self.lib.decode(bits)
            self.assertEqual(decoded, ref.decode(bits), f'decode {bits:04x}')
            expected = 0x7e01 if bits & 0x7e00 == 0x7e00 and bits & 511 else bits
            self.assertEqual(self.lib.encode(decoded), expected, f'round trip {bits:04x}')
        print(f'GF16 C: {len(self.inputs)} encode vectors; 65536 decode/round trips')

    def test_generated_c_sanitized(self):
        vectors = self.work / 'native.txt'
        vectors.write_text(''.join(f'{b:x} {e:x}\n' for b, e in zip(self.inputs, self.expected)))
        harness = self.work / 'sanitized.c'
        harness.write_text('''#include "codec.c"
#include <stdio.h>
#include <assert.h>
int main(int argc, char **argv) {
    (void)argc;
    FILE *f = fopen(argv[1], "r"); assert(f);
    unsigned bits, expected;
    while (fscanf(f, "%x %x", &bits, &expected) == 2) assert(encode(bits) == expected);
    fclose(f);
    for (unsigned g=0; g<65536; ++g) {
        unsigned want = ((g & 0x7e00) == 0x7e00 && (g & 511)) ? 0x7e01 : g;
        assert(encode(decode(g)) == want);
    }
    puts("PASS GF16 ASan/UBSan");
}
''')
        exe = self.work / 'sanitized'
        run(['cc', '-std=c11', '-O1', '-g', '-fsanitize=address,undefined',
             '-fno-sanitize-recover=all', str(harness), '-o', str(exe)])
        self.assertIn('PASS GF16 ASan/UBSan', run([str(exe), str(vectors)], timeout=60))

    def simulate(self, rtl, inputs, expected, suffix):
        work = self.work
        memory = work / f'{suffix}.mem'
        memory.write_text(''.join(f'{bits:08x}{exp:08x}{i % 65536:08x}{ref.decode(i % 65536):08x}\n'
                                  for i, (bits, exp) in enumerate(zip(inputs, expected))))
        # Every run cycles through all 65536 GF16 decodes repeatedly.
        tb = work / f'{suffix}_tb.v'
        tb.write_text(f'''`timescale 1ns / 1ps
module tb;
reg clk=0; always #5 clk=~clk;
reg rst_n=0, en=0;
reg [31:0] f32_in=0, gf16_in=0;
wire [31:0] gf16_out, f32_out;
wire ready;
reg [127:0] vectors [0:{len(inputs)-1}];
reg [31:0] old_g, old_f;
integer i;
TrinityGf16CodecT27 dut(.*);
initial begin
  $readmemh("{memory}", vectors);
  repeat(2) @(negedge clk);
  if (gf16_out !== 0 || f32_out !== 0) $fatal(1,"reset");
  rst_n=1; en=1;
  for (i=0; i<{len(inputs)}; i=i+1) begin
    f32_in=vectors[i][127:96]; gf16_in=vectors[i][63:32];
    @(negedge clk);
    if (gf16_out !== vectors[i][95:64] || f32_out !== vectors[i][31:0])
      $fatal(1,"case %d f32=%h GF16=%h expected=%h decode=%h expected=%h",
        i,f32_in,gf16_out,vectors[i][95:64],f32_out,vectors[i][31:0]);
  end
  old_g=gf16_out; old_f=f32_out; en=0; f32_in=32'h3f800000; gf16_in=32'h3e00;
  repeat(3) @(negedge clk);
  if (gf16_out !== old_g || f32_out !== old_f) $fatal(1,"enable hold");
  rst_n=0;
  @(negedge clk);
  if (gf16_out !== 0 || f32_out !== 0) $fatal(1,"reset while disabled");
  $display("PASS GF16 {suffix} {len(inputs)} vectors"); $finish;
end
initial begin #{len(inputs)*20+1000}; $fatal(1,"timeout"); end
endmodule
''')
        exe = work / f'{suffix}.vvp'
        run(['iverilog', '-g2012', '-s', 'tb', '-o', str(exe), str(rtl), str(tb)])
        output = run(['vvp', str(exe)], timeout=120)
        self.assertIn(f'PASS GF16 {suffix}', output)
        print(output.strip())

    def test_generated_rtl(self):
        self.simulate(self.rtl, self.inputs, self.expected, 'rtl')

    def test_synthesized_rtl(self):
        mapped = self.work / 'mapped.v'
        log = run(['yosys', '-Q', '-T', '-p',
                   f'read_verilog {self.rtl}; synth -top TrinityGf16CodecT27; '
                   f'check -assert; write_verilog -noattr {mapped}'], timeout=120)
        self.assertIn('Found and reported 0 problems', log)
        self.simulate(mapped, self.inputs, self.expected, 'mapped')


if __name__ == '__main__':
    unittest.main()
