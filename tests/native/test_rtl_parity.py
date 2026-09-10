"""Native packet parity and actual Icarus RTL execution, no simulator substitute.

Legacy Python is the independent oracle/test fixture driver only. Native RTL
resources are resolved by the OS adapter; all runner/protocol logic is .t27.
"""
import ctypes as C
import os
from pathlib import Path
import random
import sys
import unittest
import tempfile
from unittest.mock import patch

ROOT=Path(os.environ.get("TRINITY_SOURCE_ROOT") or Path(__file__).resolve().parents[2])
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/"tests/reference"))
LIB=os.environ.get('TRINITY_RTL_LIBRARY') or ROOT/'build/t27'/(
    'libtrinity_memory_t27.dylib' if sys.platform=='darwin' else 'libtrinity_memory_t27.so')
lib=C.CDLL(str(LIB))
from trinity_memory_reference.rtl_compute import _packets,run_rtl_dot

class Expected(C.Structure):
    _fields_=[('result',C.c_int64),('error',C.c_bool)]
class Observed(C.Structure):
    _fields_=[('index',C.c_uint64),('result',C.c_int64),('error',C.c_bool),('cycles',C.c_uint64)]
class Summary(C.Structure):
    _fields_=[(key,C.c_uint64) for key in ['cycles','groups','input_stalls','output_stalls','source_bubbles','resets']]+[('result_count',C.c_size_t)]
class Witness(C.Structure):
    _fields_=[('result',C.c_int64),('error',C.c_bool)]+[(key,C.c_uint64) for key in ['cycles','groups','input_stalls','output_stalls','source_bubbles','resets','result_cycle']]+[('weight_count',C.c_size_t),('encoded_weight_bits',C.c_uint64)]
P32,P64=C.POINTER(C.c_int32),C.POINTER(C.c_uint64)
lib.tm_rtl_prepare_dot.argtypes=[C.c_int32,P32,P32,C.c_size_t,P64,C.c_size_t,C.POINTER(Expected)]
lib.tm_rtl_prepare_dot.restype=C.c_int64
lib.tm_rtl_run_dot.argtypes=[P32,P32,C.c_size_t,C.c_int32,C.c_int64,C.POINTER(Witness)]
lib.tm_rtl_run_dot.restype=C.c_int32
lib.tm_rtl_run_frames.argtypes=[P64,C.c_size_t,C.POINTER(Expected),C.c_size_t,C.c_int32,C.c_int64,C.c_uint32,C.POINTER(Observed),C.c_size_t,C.POINTER(Summary)]
lib.tm_rtl_run_frames.restype=C.c_int32

def a32(values):return (C.c_int32*len(values))(*values)

class NativeRTLParity(unittest.TestCase):
    def test_packets_and_scoreboard(self):
        rng=random.Random(27)
        for count in list(range(81))+[127,257,1024,65539]:
            weights=[rng.choice((-1,0,1)) for _ in range(count)]
            activations=[rng.randint(-128,127) for _ in range(count)]
            for codec,name in [(0,'baseline2'),(1,'dense5')]:
                wanted=_packets(weights,activations,name)
                output=(C.c_uint64*len(wanted))();expected=Expected()
                result=lib.tm_rtl_prepare_dot(codec,a32(weights),a32(activations),count,output,len(output),C.byref(expected))
                self.assertEqual(result,len(wanted));self.assertEqual(list(output),wanted)
                self.assertEqual(expected.result,sum(w*a for w,a in zip(weights,activations)));self.assertFalse(expected.error)

    def test_actual_dot_simulations(self):
        rng=random.Random(728)
        fixtures=[([],[]),([-1],[-128]),([1],[127]),([-1]*5,[-128]*5),
                  ([1,-1,0,-1,1,1],[-128,-128,127,127,127,-128]),
                  ([rng.choice((-1,0,1)) for _ in range(517)],[rng.randrange(-128,128) for _ in range(517)])]
        for weights,activations in fixtures:
            for codec,name in [(0,'baseline2'),(1,'dense5')]:
                with self.subTest(codec=name,count=len(weights)):
                    witness=Witness()
                    status=lib.tm_rtl_run_dot(a32(weights),a32(activations),len(weights),codec,2026,C.byref(witness))
                    self.assertEqual(status,0,'actual native RTL simulation failed')
                    reference=run_rtl_dot(weights,activations,name,2026)
                    for key,_ in Witness._fields_:self.assertEqual(getattr(witness,key),reference[key],key)

    def test_actual_adversarial_frames_and_narrow_overflow(self):
        from scripts import test_dot_rtl as oracle
        original=oracle._run_packets
        def native_runner(packets,expected,codec,seed=27,acc_width=32):
            results=(Observed*len(expected))();summary=Summary()
            expected_c=(Expected*len(expected))(*(Expected(value,error) for value,error in expected))
            packet_c=(C.c_uint64*len(packets))(*packets)
            status=lib.tm_rtl_run_frames(packet_c,len(packets),expected_c,len(expected),
                1 if codec=='dense5' else 0,seed,acc_width,results,len(results),C.byref(summary))
            self.assertEqual(status,0,'actual native adversarial RTL simulation failed')
            reference=original(packets,expected,codec,seed,acc_width)
            report={key:getattr(summary,key) for key,_ in Summary._fields_ if key!='result_count'}
            report['results']=[{key:getattr(row,key) for key,_ in Observed._fields_} for row in results]
            for key,value in report.items():self.assertEqual(value,reference[key],key)
            return report
        try:
            oracle._run_packets=native_runner
            for codec in ['dense5','baseline2']:
                for seed in [27,307]:
                    report=oracle.protocol_suite(codec,seed)
                    self.assertEqual(report['checked_results'],57);self.assertEqual(report['rejected_frames'],7)
                report=oracle.overflow_suite(codec)
                self.assertEqual(report['checked_results'],6);self.assertEqual(report['rejected_frames'],3)
        finally:oracle._run_packets=original

    def test_missing_resources_or_tools_never_fallback(self):
        with tempfile.TemporaryDirectory() as empty:
            witness=Witness();witness.result=123
            with patch.dict(os.environ,{'TRINITY_T27_RTL_ROOT':empty}):
                self.assertEqual(lib.tm_rtl_run_dot(a32([1]),a32([2]),1,1,27,C.byref(witness)),-86)
                self.assertEqual(witness.result,123)
            with patch.dict(os.environ,{'PATH':empty}):
                self.assertEqual(lib.tm_rtl_run_dot(a32([1]),a32([2]),1,1,27,C.byref(witness)),-85)
                self.assertEqual(witness.result,123)

    def test_invalid_seed_and_shapes_leave_witness(self):
        for seed in [-1,1<<32]:
            witness=Witness();witness.result=123
            self.assertEqual(lib.tm_rtl_run_dot(a32([1]),a32([2]),1,1,seed,C.byref(witness)),-80)
            self.assertEqual(witness.result,123)
        for codec in [-1,2,3]:
            witness=Witness();self.assertEqual(lib.tm_rtl_run_dot(a32([1]),a32([2]),1,codec,27,C.byref(witness)),-80)

if __name__=='__main__':unittest.main()
