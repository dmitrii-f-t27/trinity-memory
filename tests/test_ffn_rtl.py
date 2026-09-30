"""Issue #92: actual generated FFN RTL versus independent integer arithmetic."""
import math
import random
import subprocess
import tempfile
import unittest
import os
import sys
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPILER = ROOT / 'build/compiler/target/release/t27c'
MASK = (1 << 64) - 1
sys.path.insert(0, str(ROOT / 'tools'))
os.environ.setdefault('TRINITY_FIXTURES_OFFLINE', '1')
import ffn_reference as fr
import matvec_device_model as device
import ffn_vectors as vectors


def generated(spec, work):
    source = ROOT / 't27/rtl' / (spec + '.t27')
    result = subprocess.run([str(COMPILER), 'gen-verilog', str(source)],
                            text=True, capture_output=True, check=True)
    path = work / (spec + '.v')
    path.write_text(result.stdout, encoding='ascii')
    return path


@unittest.skipUnless(COMPILER.is_file() and shutil.which('iverilog'), 'pinned t27c and Icarus required')
class WideArithmetic(unittest.TestCase):
    def test_actual_sequential_engine(self):
        rng = random.Random(92)
        vectors = []
        for a, b in [(0, 0), (MASK, MASK), (1 << 63, 1 << 63), (MASK, 1),
                     (1, MASK)] + [(rng.getrandbits(64), rng.getrandbits(64)) for _ in range(80)]:
            vectors.append((1, a, b, a * b, 0, 0))
        for a, b in [(0, 1), ((1 << 192) - 1, 1), ((1 << 139) - 1, 6912),
                     (5, 2), (7, 2), (1 << 190, (1 << 96) - 1)] + [
                         (rng.getrandbits(192), rng.getrandbits(96) or 1) for _ in range(80)]:
            q, r = divmod(a, b)
            vectors.append((2, a, b, q, r, 0))
        for a in [0, 1, 2, 3, 4, (1 << 192) - 1, 1 << 190] + [
                rng.getrandbits(192) for _ in range(80)]:
            q = math.isqrt(a)
            vectors.append((3, a, 0, q, a - q*q, 0))
        vectors.append((2, 1, 0, 0, 0, 1))
        with tempfile.TemporaryDirectory(prefix='trinity-ffn-wide-') as temp:
            work = Path(temp)
            rtl = generated('ffn_wide', work)
            cases = []
            for i, (op, a, b, q, r, error) in enumerate(vectors):
                cases.append(f'''
                @(negedge clk); mode={op}; a0=64'h{a&MASK:x}; a1=64'h{(a>>64)&MASK:x}; a2=64'h{a>>128:x};
                b0=64'h{b&MASK:x}; b1=64'h{b>>64:x}; start=1;
                @(negedge clk); start=0;
                wait(done); #1;
                if ({{q2,q1,q0}} !== 192'h{q:x} || {{r1,r0}} !== 128'h{r:x} || fault !== 1'b{error})
                    $fatal(1,"case {i} op {op}: q=%h r=%h fault=%b",{{q2,q1,q0}},{{r1,r0}},fault);
                ''')
            tb = work / 'tb.v'
            tb.write_text('''module tb;
              reg clk=0; always #5 clk=~clk;
              reg rst_n=0, start=0;
              reg [31:0] mode=0;
              reg [63:0] a0=0,a1=0,a2=0,b0=0,b1=0;
              wire [63:0] q0,q1,q2,r0,r1; wire done,busy,fault;
              TrinityFfnWideT27 dut(.clk(clk),.rst_n(rst_n),.en(1'b1),.ready(),
                .start(start),.mode(mode),.a0(a0),.a1(a1),.a2(a2),.b0(b0),.b1(b1),
                .q0(q0),.q1(q1),.q2(q2),.r0(r0),.r1(r1),.done(done),.busy(busy),.fault(fault));
              initial begin repeat(2) @(negedge clk); rst_n=1;
              ''' + '\n'.join(cases) + '''
                $display("PASS wide arithmetic"); $finish;
              end
              initial begin #2000000; $fatal(1,"timeout"); end
            endmodule''')
            exe = work / 'test.vvp'
            subprocess.run(['iverilog', '-g2012', '-s', 'tb', '-o', str(exe), str(rtl), str(tb)],
                           capture_output=True, text=True, check=True)
            run = subprocess.run(['vvp', str(exe)], capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn('PASS wide arithmetic', run.stdout)


def write_vectors(work, model, x):
    regions = {64: [1 | (0x46464e31 << 32)],
               80: [fr.to_q(model['scales'][s]) & 0xffffffff for s in ('gate', 'up', 'down')],
               4096: [v & 0xffffffff for v in x],
               8192: [fr.to_q(v) & 0xffffffff for v in model['w_post']],
               12288: [fr.to_q(v) & 0xffffffff for v in model['w_sub']]}
    for stage, base in [('gate', 65536), ('up', 393216), ('down', 720896)]:
        rows, cols = model['shapes'][stage]
        words = []
        for row in range(rows):
            values = model[stage][row*cols:(row+1)*cols]
            values += [0] * (-cols % 64)
            for j in range(0, len(values), 64):
                lo, hi = device.encode_word(0, device.lane_word(values, j))
                words.append(lo | (hi << 64))
        regions[base] = words
    path = work / 'input.mem'
    with path.open('w') as out:
        for base, words in regions.items():
            out.write(f'@{base:x}\n')
            out.writelines(f'{word:032x}\n' for word in words)
    return path


def check_capture(test, path, expected, sats):
    text = path.read_text()
    lines = [(tag, int(a,16), int(b,16)) for tag,a,b in re.findall(r'([A-Za-z])([0-9a-fA-F]{8})([0-9a-fA-F]{10})',text)]
    for stage, values in expected.items():
        low = [(i,v) for tag,i,v in lines if tag==stage]
        high = [(i,v) for tag,i,v in lines if tag==('J' if stage=='a' else stage.upper())]
        test.assertEqual(low, [(i,v & 0xffffffff) for i,v in enumerate(values)], stage+' low')
        test.assertEqual(high, [(i,(v >> 32) & 0xffffffff) for i,v in enumerate(values)], stage+' high')
    test.assertEqual([(i,v) for tag,i,v in lines if tag=='t'],list(enumerate(sats.values())))
    test.assertEqual([(i,v) for tag,i,v in lines if tag=='z'],[(1,0)])
    test.assertEqual([(i,v) for tag,i,v in lines if tag=='d'],[(1,0)])
    total=[v for tag,i,v in lines if tag=='c']
    split=[(i,v) for tag,i,v in lines if tag=='k']
    test.assertEqual(len(total),1)
    test.assertEqual([i for i,_ in split],[0,1],'clock split lines')
    report,memory=split[0][1],split[1][1]
    test.assertGreater(report,0); test.assertGreater(memory,0)
    test.assertLessEqual(report+memory,total[0])
    return {'total':total[0],'report_wait':report,'memory_wait':memory,'compute':total[0]-report-memory}


@unittest.skipUnless(COMPILER.is_file() and shutil.which('iverilog'), 'pinned t27c and Icarus required')
class FfnPipeline(unittest.TestCase):
    def simulate(self, model, x, label, mem_base=2, out_base=1):
        expected, sats = fr.fpga_q16(model,x)
        with tempfile.TemporaryDirectory(prefix='trinity-ffn-'+label+'-') as temp:
            work=Path(temp)
            rtl=[generated(s,work) for s in ('ffn_wide','fpga_ffn')]
            inp=write_vectors(work,model,x)
            exe=work/'test.vvp'; output=work/'capture.txt'
            h=model['shapes']['gate'][1]; inner=model['shapes']['gate'][0]; out=model['shapes']['down'][0]
            subprocess.run(['iverilog','-g2012','-s','tb_ffn',f'-Ptb_ffn.H={h}',f'-Ptb_ffn.I={inner}',
                            f'-Ptb_ffn.O={out}',f'-Ptb_ffn.MEM_BASE={mem_base}',
                            f'-Ptb_ffn.OUT_BASE={out_base}','-o',str(exe),*map(str,rtl),str(ROOT/'rtl/t27/ffn.v'),
                            str(ROOT/'tests/tb_ffn.v')],text=True,capture_output=True,check=True)
            command=['vvp',str(exe)]
            if os.environ.get('TRINITY_FFN_SIMULATOR')=='verilator':
                subprocess.run(['verilator','--binary','--timing','--top-module','tb_ffn','-Wno-fatal','-j','4',
                                f'-GH={h}',f'-GI={inner}',f'-GO={out}',
                                f'-GMEM_BASE={mem_base}',f'-GOUT_BASE={out_base}','--Mdir',str(work/'obj_dir'),
                                *map(str,rtl),str(ROOT/'rtl/t27/ffn.v'),str(ROOT/'tests/tb_ffn.v')],
                               capture_output=True,text=True,check=True)
                command=[str(work/'obj_dir/Vtb_ffn')]
            run=subprocess.run([*command,'+input='+str(inp),'+output='+str(output)],
                               text=True,capture_output=True,timeout=180)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            return check_capture(self,output,expected,sats)

    def test_small_and_saturating_complete_layers(self):
        from tests.test_ffn_reference import DatapathTest
        model=DatapathTest()._tiny_model()
        for name,x in [('mixed',[v << 16 for v in (3,-5,7,1,-2,4,0,-8)]),('zero',[0]*8),('epsilon',[1]*8)]:
            with self.subTest(name=name): self.simulate(model,x,name)
        model['gate']=model['up']=[1]*48
        model['w_post']=[1.0]*8
        model['scales']={'gate':32767.0,'up':32767.0,'down':1.0}
        self.simulate(model,[1 << 16]*8,'positive-saturation')
        model['up']=[-1]*48
        self.simulate(model,[1 << 16]*8,'negative-saturation')

    def test_compute_clocks_do_not_depend_on_memory_or_report_latency(self):
        from tests.test_ffn_reference import DatapathTest
        model=DatapathTest()._tiny_model(); x=[v << 16 for v in (3,-5,7,1,-2,4,0,-8)]
        fast=self.simulate(model,x,'split-fast')
        slow=self.simulate(model,x,'split-slow',mem_base=9,out_base=13)
        self.assertEqual(fast['compute'],slow['compute'])
        self.assertGreater(slow['memory_wait'],fast['memory_wait'])
        self.assertGreater(slow['report_wait'],fast['report_wait'])
        self.assertGreater(slow['total'],fast['total'])

    def test_partial_words_and_signed_extremes(self):
        rng=random.Random(927)
        h,i,o=130,129,7
        model={'gate':[rng.choice((-1,0,1)) for _ in range(i*h)],
               'up':[rng.choice((-1,0,1)) for _ in range(i*h)],
               'down':[rng.choice((-1,0,1)) for _ in range(o*i)],
               'w_post':[rng.choice((-1.0,0.5,1.25)) for _ in range(h)],
               'w_sub':[rng.choice((-0.5,1.0,0.25)) for _ in range(i)],
               'scales':{'gate':0.125,'up':-0.0625,'down':0.25},
               'shapes':{'gate':[i,h],'up':[i,h],'down':[o,i]}}
        self.simulate(model,[rng.choice((-(1<<31),(1<<31)-1,-1,1,0)) for _ in range(h)],'extremes')


class CaptureValidation(unittest.TestCase):
    def test_doorbell_requires_a_valid_crc_and_load_reply(self):
        p=vectors.proto
        good=p.format_line('A',p.resp_word(1,True,0,p.CMD_LOAD,16),0)
        self.assertTrue(vectors.doorbell_acknowledged(b'h000000000000000000\n'+good))
        self.assertFalse(vectors.doorbell_acknowledged(good[:-2]+b'1\n'))
        self.assertFalse(vectors.doorbell_acknowledged(p.format_line('A',p.resp_word(2,True,0,p.CMD_LOAD,16),0)))

    def test_ffn_build_includes_uart_rx_constraints(self):
        with tempfile.TemporaryDirectory(prefix='trinity-ffn-xdc-') as temp:
            work=Path(temp); xdc=work/'tms_ddr3_loader_ax7203.xdc'
            subprocess.run(['make','-C',str(ROOT/'fpga/ax7203'),str(xdc),
                            'DDR3_APP=ffn','DDR3_BUILD='+str(work),
                            'DDR3_PLL_MULT=6','DDR3_DDR_DIV=5'],capture_output=True,text=True,check=True)
            text=xdc.read_text()
            self.assertIn((ROOT/'fpga/ax7203/ddr3/tms_ddr3_loader.xdc').read_text().strip(),text)
            self.assertIn('create_clock -period 16.667 -name clk_ctrl',text)

    def test_rejects_corruption_truncation_reordering_and_wrong_counts(self):
        expected={s:[-1] for s in vectors.STAGES}; sats={s:0 for s in vectors.STAGES}
        lines=['d000000010000000000\n']
        for s in vectors.STAGES:
            lines.extend([f'{s}0000000000ffffffff\n',f'{"J" if s=="a" else s.upper()}0000000000ffffffff\n'])
        lines+=['c000000010000000123\n']+[f't{i:08x}0000000000\n' for i in range(6)]+['z000000010000000000\n']
        raw=''.join(lines).encode()
        self.assertTrue(vectors.validate(raw,expected,sats)['pass'])
        self.assertNotIn('clock_split',vectors.validate(raw,expected,sats))
        split_lines=lines[:-8]+['c000000010000000123\n','k000000000000000020\n','k000000010000000003\n']+lines[-7:]
        split=''.join(split_lines).encode()
        self.assertEqual(vectors.validate(split,expected,sats)['clock_split'],
                         {'total':0x123,'report_wait':0x20,'memory_wait':3,'compute':0x100})
        for broken in [split.replace(b'k00000001',b'k00000000'),
                       split.replace(b'k000000010000000003\n',b''),
                       split.replace(b'k000000000000000020',b'k000000000000000200'),
                       b''.join(l.encode() for l in split_lines[:-9]+split_lines[-7:-6]+split_lines[-9:-7]+split_lines[-6:])]:
            with self.subTest(broken=broken[-80:]),self.assertRaises(ValueError):
                vectors.validate(broken,expected,sats)
        for broken in [raw[:-1],raw+b'garbage\n',raw.replace(b'h0000000000ffffffff',b'h0000000000fffffffe'),
                       ''.join(lines[:1]+lines[3:]+lines[1:3]).encode(),raw+lines[-1].encode()]:
            with self.subTest(broken=broken[:40]),self.assertRaises(ValueError):
                vectors.validate(broken,expected,sats)


if __name__ == '__main__':
    unittest.main()
