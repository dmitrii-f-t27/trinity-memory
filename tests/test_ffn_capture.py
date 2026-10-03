"""A slow XADC call must not suspend UART draining or hide capture failures."""
import importlib.util
import contextlib
import hashlib
import io
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
spec=importlib.util.spec_from_file_location('ffn_capture',ROOT/'tools/fpga-ffn-run.py')
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)


class Port:
    def __init__(self): self.parts=queue.Queue();self.drained=threading.Event()
    def reset_input_buffer(self): pass
    def write(self, data): self.sent=data
    def flush(self): pass
    def read(self, size):
        try: part=self.parts.get(timeout=0.005)
        except queue.Empty: return b''
        if isinstance(part,Exception): raise part
        if part.endswith(b'0000000000\n'): self.drained.set()
        return part


class Capture(unittest.TestCase):
    def test_drains_while_temperature_blocks_and_handles_split_completion(self):
        port=Port();raw=bytearray();body=b'h000000000000003e00\n'*10000
        def temperature():
            port.parts.put(body);port.parts.put(b'z000000');port.parts.put(b'010000000000\n')
            self.assertTrue(port.drained.wait(1),'UART stopped while checking XADC')
        runner.capture_stream(port,b'doorbell',raw,1,temperature,timeout=2)
        self.assertEqual(raw,body+b'z000000010000000000\n')
        self.assertEqual(port.sent,b'doorbell')

    def test_trace_pair_requires_full_profile_and_distinct_descriptor(self):
        ref={'profile':'gf16-ffn-v1','run':8,'stages':{'y':[1,2]}}
        payload=(8 | (0x47464631<<32)).to_bytes(16,'little')
        plans=runner.trace_runs(ref,payload,True)
        self.assertEqual(plans[0],('',ref,8,payload))
        name,following,run,descriptor=plans[1]
        self.assertEqual((name,run,following['trace']),('result-only',9,'result'))
        self.assertEqual(int.from_bytes(descriptor,'little'),9 | (0x47464631<<32) | (1<<64))
        self.assertEqual(following['stages'],ref['stages'])
        self.assertNotIn('trace',ref)
        for bad in ({**ref,'trace':'result'},{**ref,'run':2**32-1},
                    {**ref,'profile':'q16'},{**ref,'run':0}):
            with self.assertRaises(ValueError):runner.trace_runs(bad,payload,True)
        with self.assertRaises(ValueError):runner.trace_runs(ref,bytes(16),True)
        self.assertEqual(len(runner.trace_runs({'run':1},b'legacy',False)),1)

    def test_device_and_serial_errors_preserve_received_bytes(self):
        for failure in (b'E000000020000000001\n',OSError('disconnected')):
            port=Port();raw=bytearray();port.parts.put(b'prefix');port.parts.put(failure)
            with self.assertRaises((RuntimeError,OSError)):
                runner.capture_stream(port,b'x',raw,1,lambda:None,timeout=1)
            self.assertTrue(raw.startswith(b'prefix'))

    def test_timeout_and_temperature_error_stop_reader(self):
        for thermal in (False,True):
            def temperature():
                if thermal: raise ValueError('thermal limit')
            with self.assertRaises((TimeoutError,ValueError)):
                runner.capture_stream(Port(),b'x',bytearray(),1,temperature,timeout=0.01)
            self.assertFalse(any(t.name=='ffn-uart-reader' for t in threading.enumerate()))


class BaudCleanup(unittest.TestCase):
    runner=runner
    region_names=('doorbell','gate','up','down','scales','post','sub','x')
    attention=False

    def exercise_main(self, failure=None):
        """Run the real CLI orchestration with stateful, fault-injected devices."""
        runner=self.runner
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); vectors=root/'vectors'; vectors.mkdir()
            out=root/'capture'; manifest={}
            for i,name in enumerate(self.region_names):
                data=bytes([i])*16; (vectors/(name+'.bin')).write_bytes(data)
                manifest[name]={'file':name+'.bin','bytes':len(data),
                                'sha256':hashlib.sha256(data).hexdigest(),'byte_address':i*16}
            if self.attention:
                descriptor=(1 | (0x41545431<<32) | (1<<64)).to_bytes(16,'little')
                (vectors/'doorbell.bin').write_bytes(descriptor)
            (vectors/'inputs.json').write_text(json.dumps(manifest))
            (vectors/'reference.json').write_text(json.dumps(
                {'profile':'gf16-attn-v1' if self.attention else 'gf16-ffn-v1',
                 'run':1,'expected':{},'saturations':{},'f64_error':{},'positions':1,'stages':{}}))
            boot=root/'boot.json'; boot.write_text(json.dumps(
                {'checks':{'gf16_attn_header' if self.attention else 'ffn_header':True},'bitstream_sha256':'test-hash','run':{'dna':'TEST-DNA'}}))
            device=SimpleNamespace(baud=115200,links=[],capture_open=False)
            outer=self

            class Link:
                def __init__(self, port, baud, now):
                    self.baud=baud; self.rx=bytearray(); self.closed=False
                    device.links.append(self)
                def close(self): self.closed=True

            class Loader:
                def __init__(self, link, settings, rng): self.link=link
                def status(self):
                    if self.link.baud!=device.baud:return None
                    return {'baud_div':round(60000000/device.baud),'calib':1<<24}

            def change(loader,args,before):
                outer.assertEqual(loader.link.baud,device.baud)
                if args.baud==460800 and failure=='switch-before':raise OSError('switch-before')
                device.baud=args.baud; loader.link.baud=args.baud
                if args.baud==460800 and failure=='switch-after':raise OSError('switch-after')
                return {'switched':True}

            class Child:
                def __init__(self, cmd, **kwargs):
                    path=Path(cmd[cmd.index('--output')+1]); name=path.stem
                    outer.assertEqual(device.baud,921600)
                    if failure=='interrupt' and name=='qualification':raise KeyboardInterrupt()
                    self.returncode=int(failure==name)
                    path.write_text(json.dumps({'pass':failure!='receipt','checks':{'readback':True}}))
                def poll(self): return self.returncode

            class Serial:
                def __init__(self, port, baud, **kwargs):
                    if failure=='open':raise OSError('open')
                    outer.assertEqual(baud,device.baud)
                def __enter__(self): device.capture_open=True; return self
                def __exit__(self,*args): device.capture_open=False

            def capture(*args,**kwargs):
                if failure=='capture':raise TimeoutError('capture')
                args[2].extend(b'captured bytes')

            def validate(*args,**kwargs):
                if failure=='validate':raise ValueError('validate')
                return {'pass':True}

            fake=SimpleNamespace(Link=Link,Loader=Loader,back_to_default=change)
            argv=['fpga-ffn-run.py','--vectors',str(vectors),'--boot',str(boot),
                  '--output',str(out),'--port','fake','--cable','fake']
            with contextlib.ExitStack() as stack:
                for target,attr,value in (
                    (sys,'argv',argv),
                    (runner.importlib.util,'spec_from_file_location',mock.Mock(return_value=
                        SimpleNamespace(loader=SimpleNamespace(exec_module=lambda m:None)))),
                    (runner.importlib.util,'module_from_spec',mock.Mock(return_value=fake)),
                    (runner.subprocess,'check_output',mock.Mock(return_value='TEST-DNA')),
                    (runner.subprocess,'run',mock.Mock(return_value=
                        SimpleNamespace(stdout='{"temp":35}',stderr='',returncode=0))),
                    (runner.subprocess,'Popen',Child),
                    (runner,'capture_stream',capture),
                    (runner.fv,'doorbell_acknowledged',lambda raw:True),
                    (runner.av if self.attention else runner.fv,'validate',validate)):
                    stack.enter_context(mock.patch.object(target,attr,value))
                if self.attention:
                    stack.enter_context(mock.patch.object(runner.av,'validate_board_inputs'))
                stack.enter_context(mock.patch.dict(sys.modules,{'serial':SimpleNamespace(Serial=Serial)}))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                if failure:
                    kind=KeyboardInterrupt if failure=='interrupt' else Exception
                    with self.assertRaises(kind):runner.main()
                    self.assertFalse((out/'result.json').exists())
                else:
                    self.assertEqual(runner.main(),0)
                    self.assertTrue(json.loads((out/'result.json').read_text())['pass'])
            self.assertEqual(device.baud,115200)
            self.assertTrue(all(link.closed for link in device.links))
            self.assertFalse(device.capture_open)
            receipts=list(out.glob('uart-restored*.json'))
            self.assertTrue(any(json.loads(p.read_text()).get('after',{}).get('baud_div')==521
                                for p in receipts))
            if failure in ('capture','validate'):
                self.assertTrue((out/'capture.txt').exists())

    def test_success_and_early_failures_restore_baud_and_close_ports(self):
        for failure in (None,'qualification',
                        *('load-'+name for name in self.region_names if name!='doorbell'),
                        'receipt','switch-before','switch-after',
                        'open','capture','validate','interrupt'):
            with self.subTest(failure=failure):self.exercise_main(failure)

    def test_failed_initial_switch_recovers_both_possible_endpoints(self):
        for switched in (False,True):
            with self.subTest(switched=switched):
                device=[115200]; primary=OSError('lost confirmation'); attempts=[]
                def rate(before,after,label):
                    attempts.append((before,after,label))
                    if label=='uart-fast':
                        if switched:device[0]=after
                        raise primary
                    if before!=device[0]:raise OSError('wrong baud')
                    device[0]=after
                with self.assertRaises(OSError) as caught:
                    with runner.restoring_baud(rate) as switch:
                        switch(115200,921600,'uart-fast')
                self.assertIs(caught.exception,primary)
                self.assertEqual(device[0],115200)
                self.assertEqual([x[0] for x in attempts[1:]],
                                 [921600] if switched else [921600,115200])

    def test_cleanup_failure_is_fatal_and_does_not_replace_primary_failure(self):
        def rate(*args):raise OSError('disconnected')
        with self.assertRaisesRegex(RuntimeError,'baud is unconfirmed'):
            with runner.restoring_baud(rate):pass
        primary=ValueError('readback mismatch'); stderr=io.StringIO()
        with contextlib.redirect_stderr(stderr),self.assertRaises(ValueError) as caught:
            with runner.restoring_baud(rate):raise primary
        self.assertIs(caught.exception,primary)
        self.assertIn('UART restoration failed',stderr.getvalue())


if __name__=='__main__': unittest.main()
