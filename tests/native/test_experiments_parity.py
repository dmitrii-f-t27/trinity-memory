"""Differential checks for executable t27 experiments and safe report rendering.

The frozen Python package is an oracle only; native entry points use real TCP.
CPU timing values are deliberately not compared across runtimes.
"""
import copy
import ctypes as C
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/reference"))
from trinity_memory_reference import benchmark, conformance, edge, rtl_compute

LIBRARY = Path(os.environ.get("TRINITY_EXPERIMENT_LIBRARY", ROOT / "build/t27" / ("libtrinity_memory_t27.dylib" if sys.platform == "darwin" else "libtrinity_memory_t27.so")))
lib = C.CDLL(str(LIBRARY))
PTR = C.POINTER(C.c_uint8)
for name, args in {
    "tm_exp_edge": [C.c_bool,C.c_int64,PTR,C.c_size_t],
    "tm_exp_conformance": [PTR,C.c_size_t,C.c_bool,C.c_int64,PTR,C.c_size_t],
    "tm_exp_benchmark": [C.c_size_t,C.c_size_t,C.c_int64,PTR,C.c_size_t],
    "tm_exp_signal_model": [C.c_int32,PTR,C.c_size_t],
    "tm_exp_fixtures_json": [PTR,C.c_size_t],
    "tm_report_edge_html": [PTR,C.c_size_t,PTR,C.c_size_t],
    "tm_report_benchmark_html": [PTR,C.c_size_t,PTR,C.c_size_t],
}.items():
    fn=getattr(lib,name);fn.argtypes=args;fn.restype=C.c_int64


def byte_array(data):
    return (C.c_uint8 * len(data)).from_buffer_copy(data)


def invoke(name,*args,capacity=1048576):
    out=(C.c_uint8*capacity)()
    size=getattr(lib,name)(*args,out,capacity)
    if size < 0:
        raise ValueError(f"{name}: {size}")
    return bytes(out[:size])


def render(name,report):
    raw=json.dumps(report,ensure_ascii=False,allow_nan=False).encode()
    return invoke(name,byte_array(raw),len(raw)).decode()


class ExperimentsParity(unittest.TestCase):
    def test_models_and_fixed_fixtures(self):
        for codec,name in enumerate(("baseline2","dense5","dense17","dense22")):
            self.assertEqual(invoke("tm_exp_signal_model",codec),edge.signal_model(name))
        self.assertEqual(json.loads(invoke("tm_exp_fixtures_json")),edge.fixture_cases())
        with self.assertRaises(ValueError):invoke("tm_exp_signal_model",4)

    def test_benchmark_non_timing_fields(self):
        for count,seed in ((1,27),(4,0),(5,-27),(17,42),(127,27),(257,2**63-1)):
            actual=json.loads(invoke("tm_exp_benchmark",count,2,seed))
            expected=benchmark.run_benchmark(count,2,seed)
            for key in ("schema_version","seed","repeats","weights_per_dataset"):
                self.assertEqual(actual[key],expected[key])
            self.assertEqual(actual['environment']['runtime'],'native-t27')
            self.assertIsNone(actual['environment']['python'])
            for got,want in zip(actual['datasets'],expected['datasets']):
                self.assertEqual(set(got),set(want))
                self.assertEqual(got['name'],want['name'])
                self.assertEqual(got['count'],want['count'])
                self.assertEqual(got['zero_fraction'],want['zero_fraction'])
                self.assertAlmostEqual(got['entropy_bpw'],want['entropy_bpw'],places=14)
                self.assertEqual(len(got['results']),len(want['results']))
                for gr,wr in zip(got['results'],want['results']):
                    self.assertEqual(set(gr),set(wr))
                    for key in gr:
                        if key in ('encode_ms','decode_ms'):
                            self.assertTrue(math.isfinite(gr[key]) and gr[key]>=0)
                        else:self.assertEqual(gr[key],wr[key],key)

    def test_conformance_entire_software_report(self):
        source=(ROOT/'examples/conformance.json').read_bytes()
        for seed in (27,-7):
            actual=json.loads(invoke('tm_exp_conformance',byte_array(source),len(source),False,seed))
            expected=conformance.run_conformance(ROOT/'examples/conformance.json',seed=seed)
            self.assertEqual(actual.pop('runtime'),'native-t27')
            self.assertEqual(actual,expected)

    def test_conformance_rejections(self):
        baseline=json.loads((ROOT/'examples/conformance.json').read_bytes())
        cases=[]
        for field,value in [('weights',[2]),('activations',[128]),('dot',True),('name',''),('dense5_hex','GG')]:
            item=copy.deepcopy(baseline);item['vectors'][0][field]=value;cases.append(item)
        item=copy.deepcopy(baseline);item['vectors'].append(item['vectors'][0]);cases.append(item)
        item=copy.deepcopy(baseline);item['vectors'][0]['unknown']=0;cases.append(item)
        cases += [{'schema':'trinity.conformance.v1','vectors':[]},{}]
        for case in cases:
            raw=json.dumps(case).encode()
            with self.assertRaises(ValueError):invoke('tm_exp_conformance',byte_array(raw),len(raw),False,27)

    def test_edge_reports_and_safe_html(self):
        actual=json.loads(invoke('tm_exp_edge',False,27))
        expected=edge.run_edge_demo(seed=27)
        for key in ('schema','passed','evidence','physical_device_tested','seed','model','labels','fixture_count'):
            self.assertEqual(actual[key],expected[key])
        for got,want in zip(actual['modes'],expected['modes']):
            for key in ('codec','container_bytes','raw_weight_payload_bytes','container_sha256','roundtrip_exact'):
                self.assertEqual(got[key],want[key])
            self.assertGreaterEqual(got['upload_read_wall_ns'],0)
            self.assertEqual(got['capabilities']['backend'],'emulator')
            for gc,wc in zip(got['cases'],want['cases']):
                self.assertEqual({k:v for k,v in gc.items() if k!='rpc_wall_ns'}, {k:v for k,v in wc.items() if k!='rpc_wall_ns'})
        unsafe='<script>alert("x")</script>&\'😀'
        actual['modes'][0]['cases'][0]['name']=unsafe
        actual['limitations'].append(unsafe)
        html=render('tm_report_edge_html',actual)
        self.assertNotIn(unsafe,html)
        self.assertIn('&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;&amp;&#x27;😀',html)
        self.assertIn('RTL not run',html)

    def test_benchmark_validation_and_embedding(self):
        report=json.loads(invoke('tm_exp_benchmark',17,1,27))
        unsafe='</script><img src=x onerror=alert(1)>&\u2028\u2029😀'
        report['datasets'][0]['name']=unsafe
        html=render('tm_report_benchmark_html',report)
        self.assertNotIn(unsafe,html)
        embedded=re.search(r'<script type="application/json" id="benchmark-data">(.*?)</script>',html,re.S).group(1)
        self.assertEqual(json.loads(embedded),report)
        self.assertIn('\\u003c/script\\u003e',embedded)
        self.assertIn('\\u2028\\u2029',embedded)
        self.assertIn('WebAssembly.instantiate',html)
        self.assertNotIn('digits.reduce',html)
        self.assertIn('!== 1n',html)
        invalid=[]
        for field in ('count','zero_fraction','entropy_bpw'):
            for value in (-1,True,'0',None):
                obj=copy.deepcopy(report);obj['datasets'][0][field]=value;invalid.append(obj)
        obj=copy.deepcopy(report);obj['datasets'][0]['zero_fraction']=1.1;invalid.append(obj)
        for field in ('group_size','group_bits','payload_bytes','container_bytes','payload_bpw','container_bpw','encode_ms','decode_ms','ideal_payload_ratio_vs_baseline'):
            for value in (-1,True,'0',None):
                obj=copy.deepcopy(report);obj['datasets'][0]['results'][0][field]=value;invalid.append(obj)
        obj=copy.deepcopy(report);obj['datasets'][0]['results'][0]['roundtrip']=1;invalid.append(obj)
        invalid.extend([{}, {'schema_version':1,'datasets':[]}])
        for obj in invalid:
            with self.assertRaises(ValueError):render('tm_report_benchmark_html',obj)
        for raw in (b'{"schema_version":1,"schema_version":1}',b'{"schema_version":NaN}'):
            with self.assertRaises(ValueError):invoke('tm_report_benchmark_html',byte_array(raw),len(raw))

    @unittest.skipUnless(shutil.which('iverilog') and shutil.which('vvp'),'Icarus unavailable')
    def test_edge_and_conformance_actual_rtl(self):
        # The public run must execute each requested witness, not attach canned metrics.
        actual=json.loads(invoke('tm_exp_edge',True,27))
        for mode in actual['modes']:
            for case in mode['cases']:
                self.assertEqual(len(case['rtl']),3)
                for row,witness in enumerate(case['rtl']):
                    expected=rtl_compute.run_rtl_dot(list(edge.TEMPLATES[row]),case['samples'],codec='dense5' if mode['codec']=='dense5' else 'baseline5',seed=27+row)
                    self.assertEqual(witness,expected)
        source=(ROOT/'examples/conformance.json').read_bytes()
        result=json.loads(invoke('tm_exp_conformance',byte_array(source),len(source),True,27))
        self.assertEqual(result['rtl_checks'],22)
        self.assertEqual(sum(x['rtl'] is not None for x in result['checks']),22)
        for check in result['checks']:
            if check['rtl']:
                self.assertEqual(check['rtl']['result'],check['expected_dot'])
                self.assertFalse(check['rtl']['error'])
                self.assertGreater(check['rtl']['cycles'],0)


if __name__=='__main__':unittest.main()
