"""Full generated GF16 FFN, independent integer oracle and transport checks."""
import ctypes
from pathlib import Path
import random
import tempfile
import unittest
from tools import gf16_ffn_reference as ref, gf16_ffn_build as build
from tools import gf16_ffn_vectors as vectors, gf16_wide_reference as wide


def model(h=8, i=6, o=4, seed=111):
    rng=random.Random(seed)
    return {'shapes':{'gate':[i,h],'up':[i,h],'down':[o,i]},
            'gate':[rng.choice([-1,0,1]) for _ in range(i*h)],
            'up':[rng.choice([-1,0,1]) for _ in range(i*h)],
            'down':[rng.choice([-1,0,1]) for _ in range(o*i)],
            'w_post':[ref.ONE]*h,'w_sub':[ref.ONE]*i,
            'scales':dict.fromkeys(('gate','up','down'),ref.ONE)}


class Oracle(unittest.TestCase):
    def test_lattice_and_quantization(self):
        self.assertEqual(ref.fixed(1),1)
        self.assertEqual(ref.fixed(0x8001),-1)
        self.assertEqual(ref.fixed(ref.ONE),1<<39)
        self.assertLess(ref.fixed(0x7dff),1<<71)
        self.assertEqual(ref.actquant([0,0x8000,1,0x8001])[1],[0]*4)
        q,c,m=ref.actquant([0x7dff,0xfdff,0,0x8000])
        self.assertEqual(c,[127,-127,0,0]);self.assertEqual(q,[0x7dff,0xfdff,0,0])
        # A large positive prefix that would overflow signed64 Q39 cancels.
        y,d=ref.linear([1,1,-1,-1],1,4,[0x7dff]*4,ref.ONE)
        self.assertEqual((y,d),([0],[0]))

    def test_invalid_and_overflow(self):
        for word in (-1,65536,0x7e00,0xffff):
            with self.assertRaises(ValueError):ref.actquant([word])
        with self.assertRaises(ValueError):ref.actquant([])
        with self.assertRaises(ValueError):ref.linear([3],1,1,[ref.ONE],ref.ONE)
        with self.assertRaises(ValueError):ref.linear([1,1],1,2,[0x7dff]*2,ref.ONE)

    def test_trace_rejects_corruption(self):
        m=model();r=ref.evaluate(m,[0]*8)
        lines=[('G',8,6),*vectors.expected_lines(r),('c',1,100),('k',0,20),('k',1,30),('z',1,0)]
        raw=''.join(f'{t}{i:08x}{v:010x}\n' for t,i,v in lines).encode()
        self.assertTrue(vectors.validate(raw,r)['pass'])
        for bad in (raw[:-1],raw+raw[-20:],raw.replace(b'h000000000000000000',b'h000000000000000001'),
                    raw.replace(b'G000000080000000006',b'F000000080000000006')):
            with self.assertRaises(ValueError):vectors.validate(bad,r)

    def test_phase_protocol_rejects_bad_totals(self):
        r=ref.evaluate(model(),[0]*8)
        tail=[('c',1,100),('k',0,20),('k',1,30),('k',2,20)]
        tail += [('t',i,10 if i<7 else 30) for i in range(8)]
        tail += [('z',1,0)]
        def raw(items):
            return ''.join(f'{t}{i:08x}{v:010x}\n' for t,i,v in vectors.expected_lines(r)+items).encode()
        self.assertEqual(sum(vectors.validate(raw(tail),r)['phase_clocks'].values()),100)
        for index,value in ((0,99),(1,90),(3,51),(4,9),(11,31)):
            bad=tail[:];tag,i,_=bad[index];bad[index]=(tag,i,value)
            with self.subTest(index=index),self.assertRaises(ValueError):vectors.validate(raw(bad),r)

    def test_board_payload_preflight(self):
        import hashlib
        import json
        # Avoid generating 53M Python weight integers: transport only needs
        # correctly sized immutable payloads for this host preflight test.

        r={'profile':ref.PROFILE,'stages':{s:[0]*n for s,n in zip('hguasy',[2560,6912,6912,6912,6912,2560])},
           'actquant':{'h':[0]*2560,'s':[0]*6912},'codes':{'h':[0]*2560,'s':[0]*6912}}
        bases=dict(doorbell=64,scales=80,x=4096,post=8192,sub=12288,gate=65536,up=393216,down=720896)
        counts=dict(doorbell=1,scales=3,x=2560,post=2560,sub=6912,gate=276480,up=276480,down=276480)
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);manifest={}
            for name,base in bases.items():
                data=((1 | (vectors.MAGIC << 32)).to_bytes(16,'little') if name=='doorbell' else bytes(16*counts[name]))
                (folder/(name+'.bin')).write_bytes(data)
                manifest[name]={'word_address':base,'byte_address':base*16,'bytes':len(data),
                                'file':name+'.bin','sha256':hashlib.sha256(data).hexdigest()}
            vectors.validate_board_inputs(folder,manifest,r)
            for region,key,value in [('x','byte_address',0),('gate','bytes',16),('scales','file','../scales.bin'),
                                     ('x','sha256','0'*64),('sub','word_address',0)]:
                bad=json.loads(json.dumps(manifest));bad[region][key]=value
                with self.subTest(region=region,key=key),self.assertRaises(ValueError):
                    vectors.validate_board_inputs(folder,bad,r)
            for run in (0,2,2**32):
                with self.assertRaises(ValueError):vectors.validate_board_inputs(folder,manifest,r,run)
            r['trace']='result'
            with self.assertRaises(ValueError):vectors.validate_board_inputs(folder,manifest,r)
            data=(1 | (vectors.MAGIC<<32) | (1<<64)).to_bytes(16,'little')
            (folder/'doorbell.bin').write_bytes(data)
            manifest['doorbell']['sha256']=hashlib.sha256(data).hexdigest()
            vectors.validate_board_inputs(folder,manifest,r)
            r['trace']='unexpected'
            with self.assertRaises(ValueError):vectors.validate_board_inputs(folder,manifest,r)
            r['trace']='result'
            r['profile']='q16'
            with self.assertRaises(ValueError):vectors.validate_board_inputs(folder,manifest,r)
            r['profile']=ref.PROFILE
            bad=(0x7e00).to_bytes(16,'little')+bytes(32)
            (folder/'scales.bin').write_bytes(bad);manifest['scales']['sha256']=hashlib.sha256(bad).hexdigest()
            with self.assertRaises(ValueError):vectors.validate_board_inputs(folder,manifest,r)


class Generated(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not build.base.COMPILER.is_file():
            raise unittest.SkipTest('pinned compiler required in native CI')
        cls.temp=tempfile.TemporaryDirectory(prefix='trinity-gf16-ffn-');cls.addClassCleanup(cls.temp.cleanup)
        cls.work=Path(cls.temp.name);cls.rtl,cls.lib=build.generate(cls.work)
        cls.lib.fixed0.argtypes=cls.lib.fixed1.argtypes=[ctypes.c_uint32]
        cls.lib.fixed0.restype=cls.lib.fixed1.restype=ctypes.c_uint64
        cls.lib.pack39.argtypes=[ctypes.c_uint64]*6+[ctypes.c_uint32]
        cls.lib.pack39.restype=ctypes.c_uint32

    def test_fixed_lattice_and_rational_rounding(self):
        mask=(1<<64)-1
        for word in range(65536):
            if not wide.finite(word):continue
            v=abs(ref.fixed(word))
            self.assertEqual(self.lib.fixed0(word),v&mask)
            self.assertEqual(self.lib.fixed1(word),v>>64)
            self.assertEqual(self.lib.pack39(v&mask,v>>64,0,0,1,0,word&0x8000),word)
        rng=random.Random(111)
        for _ in range(10000):
            q=rng.getrandbits(rng.randrange(0,85));d=rng.getrandbits(80) or 1;r=rng.randrange(d)
            self.assertEqual(self.lib.pack39(q&mask,q>>64,r&mask,r>>64,d&mask,d>>64,0),
                             wide.encode_ratio(q*d+r,d<<39))

    def test_clocked_full_pipeline(self):
        for h,i,o in ((1,1,1),(8,6,4),(65,67,3)):
            m=model(h,i,o);rng=random.Random(h)
            x=[rng.choice([0,1,0x8001,ref.ONE,ref.ONE|0x8000,0x7dff,0xfdff,ref.ONE-512]) for _ in range(h)]
            r=ref.evaluate(m,x)
            got=build.simulate(self.work/f'shape-{h}',self.rtl,m,x,r)
            self.assertTrue(got['pass']);print(h,got['clock_split'])

    def test_backpressure_accounting_and_zero(self):
        m=model();x=[0,0x8000]*4;r=ref.evaluate(m,x)
        fast=build.simulate(self.work/'zero-fast',self.rtl,m,x,r)
        slow=build.simulate(self.work/'zero-slow',self.rtl,m,x,r,mem_base=15,out_base=40)
        self.assertGreater(slow['clock_split']['total'],fast['clock_split']['total'])
        self.assertGreater(slow['clock_split']['controller_other'],0)
        self.assertGreater(slow['clock_split']['memory_wait'],fast['clock_split']['memory_wait'])
        self.assertGreater(slow['clock_split']['report_wait'],fast['clock_split']['report_wait'])

    def test_large_signed_prefix_cancellation(self):
        # Q39 terms reach bit 70, so both carry and sign-extension matter.
        # Large prefixes cancel exactly before the single GF16 dot rounding.
        m=model(64,4,2)
        m['w_post']=[0x7dff]*64
        m['gate']=[1]*32+[-1]*32 + [-1]*32+[1]*32 + [0]*64 + [1,-1]*32
        m['up']=m['gate'][:]
        x=[ref.ONE]*64;r=ref.evaluate(m,x)
        self.assertEqual(r['stages']['g'],[0]*4)
        for mode in ('full','result'):
            self.assertTrue(build.simulate(self.work/('cancel-'+mode),self.rtl,m,x,r,trace=mode)['pass'])

    def test_result_only_and_phase_partition(self):
        m=model(65,67,3);x=[ref.ONE if i%3 else (ref.ONE|0x8000) for i in range(65)]
        r=ref.evaluate(m,x)
        full=build.simulate(self.work/'trace-full',self.rtl,m,x,r,out_base=200)
        result=build.simulate(self.work/'trace-result',self.rtl,m,x,r,out_base=200,trace='result')
        self.assertEqual(result['stage_values'],3)
        self.assertEqual(result['actquant_values'],0)
        self.assertEqual(result['trace'],'result')
        self.assertLess(result['clock_split']['total'],full['clock_split']['total'])
        self.assertLess(result['clock_split']['report_wait'],full['clock_split']['report_wait'])
        for record in (full,result):
            self.assertEqual(sum(record['phase_clocks'].values()),record['clock_split']['total'])
            self.assertEqual(record['projection_loop_clocks'],5*(2*65*67+3*67))
        raw=(self.work/'trace-result'/'capture.txt').read_bytes()
        expected={**r,'trace':'result'}
        for bad in (raw.replace(b't00000000',b't00000001'),
                    raw.replace(b'k00000002',b'k00000003'),
                    b''.join(line for line in raw.splitlines(keepends=True) if not line.startswith(b't')),
                    raw.replace(b'd000000010000000002',b'd000000010000000001')):
            with self.assertRaises(ValueError):vectors.validate(bad,expected)
        with self.assertRaises(ValueError):vectors.validate(raw,r)

    def test_faults_reset_and_second_vector(self):
        m=model();x=[ref.ONE,ref.ONE|0x8000]*4;r=ref.evaluate(m,x)
        probes=[({'CALIB_LOSS_AT':1000,'EXPECT_ERROR':2},None),
                ({'SPURIOUS_ACK_AT':1000,'EXPECT_ERROR':5},None),
                ({'EXPECT_ERROR':6},(4096,0x7e00)),
                ({'EXPECT_ERROR':6},(80,1<<100)),
                ({'EXPECT_ERROR':3},(65536,3)),
                ({'EXPECT_ERROR':10},(64,(2<<64)|(vectors.MAGIC<<32)|1))]
        for i,(params,patch) in enumerate(probes):
            work=self.work/f'fault-{i}';inp=vectors.write_inputs(work,m,x)
            if patch:
                with inp.open('a') as f:f.write(f'@{patch[0]:x}\n{patch[1]:032x}\n')
            command=build.compile_sim(work,self.rtl,(8,6,4),test_params=params)
            log=build.base.run([*command,'+input='+str(inp),'+output='+str(work/'capture.txt')],timeout=120)
            self.assertIn(f'PASS expected GF16 error {params["EXPECT_ERROR"]}',log)
        for params in ({'RESET_AT':1000},{'REPEATS':2}):
            work=self.work/next(iter(params));inp=vectors.write_inputs(work,m,x)
            command=build.compile_sim(work,self.rtl,(8,6,4),test_params=params)
            build.base.run([*command,'+input='+str(inp),'+output='+str(work/'capture.txt')],timeout=120)
            raw=(work/'capture.txt').read_bytes()
            if 'REPEATS' in params:
                offset=raw.index(b'd000000020000000001\n')
                self.assertTrue(vectors.validate(raw[:offset],r)['pass'])
                self.assertTrue(vectors.validate(raw[offset:],ref.evaluate(m,[0]*8),run=2)['pass'])
            else:self.assertTrue(vectors.validate(raw,r)['pass'])


if __name__=='__main__':unittest.main()
