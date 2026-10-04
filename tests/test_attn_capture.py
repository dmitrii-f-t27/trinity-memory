"""Attention runner uses the same real-CLI fault injection as FFN."""
import importlib.util
import hashlib
import tempfile
from pathlib import Path
import unittest
from tests import test_ffn_capture as common
ROOT=common.ROOT

spec=importlib.util.spec_from_file_location('attn_capture',ROOT/'tools/fpga-attn-run.py')
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)


class AttentionBaudCleanup(common.BaudCleanup):
    runner=runner
    region_names=('doorbell','scales','w_in','w_sub','x','rope','q','k','v','o')
    attention=True


class AttentionDescriptors(unittest.TestCase):
    def test_pair_changes_only_run_and_trace_mode(self):
        ref={'profile':'gf16-attn-v1','run':41,'positions':8,'trace':'full','stages':{}}
        payload=(41 | (0x41545431<<32) | (8<<64)).to_bytes(16,'little')
        first,second=runner.trace_runs(ref,payload,True)
        self.assertEqual(first,('',ref,41,payload))
        name,following,run,descriptor=second
        self.assertEqual((name,run,following['trace']),('result-only',42,'result'))
        self.assertEqual(int.from_bytes(descriptor,'little'),42 | (0x41545431<<32) | (8<<64) | (1<<96))
        self.assertEqual(following['stages'],ref['stages'])
        self.assertEqual(len(runner.trace_runs(following,descriptor,False)),1)
        with self.assertRaises(ValueError):runner.trace_runs(following,descriptor,True)

    def test_bad_descriptors_rejected_even_without_pairing(self):
        ref={'profile':'gf16-attn-v1','run':41,'positions':2,'trace':'full'}
        payload=(41 | (0x41545431<<32) | (2<<64)).to_bytes(16,'little')
        for key,value in [('profile','gf16-ffn-v1'),('run',0),('run',2**32),
                          ('run',42),('positions',1),('positions',0),('positions',9),
                          ('trace','result'),('trace','unknown')]:
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):
                runner.trace_runs({**ref,key:value},payload,False)
        with self.assertRaises(ValueError):runner.trace_runs(ref,bytes(16),False)

    def test_board_manifest_rejects_hash_valid_wrong_doorbell(self):
        # Valid real-board geometry with inert payloads. Hashes alone cannot
        # establish that the descriptor selects the reference's run and mode.
        regions={'doorbell':(64,1),'scales':(80,5),'w_in':(4096,2560),
                 'w_sub':(8192,2560),'x':(32768,5120),'rope':(321536,256),
                 'q':(65536,102400),'k':(167936,25600),'v':(193536,25600),
                 'o':(219136,102400)}
        stages={'r':[0]*5120,'k':[0]*1280,'sc':[0]*60}
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);manifest={}
            for name,(base,count) in regions.items():
                data=bytes(16*count);(folder/(name+'.bin')).write_bytes(data)
                manifest[name]={'file':name+'.bin','word_address':base,'byte_address':16*base,
                                'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
            def descriptor(run=41,positions=2,result=False):
                data=(run | (0x41545431<<32) | (positions<<64) | (int(result)<<96)).to_bytes(16,'little')
                (folder/'doorbell.bin').write_bytes(data)
                manifest['doorbell']['sha256']=hashlib.sha256(data).hexdigest()
            descriptor()
            runner.av.validate_board_inputs(folder,manifest,stages,41,2)
            for change in ({'run':42},{'positions':1},{'result':True}):
                descriptor(**change)
                with self.assertRaisesRegex(ValueError,'doorbell differs'):
                    runner.av.validate_board_inputs(folder,manifest,stages,41,2)
            runner.av.validate_board_inputs(folder,manifest,stages,41,2,result_only=True)
