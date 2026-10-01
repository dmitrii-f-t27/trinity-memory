"""A slow XADC call must not suspend UART draining or hide capture failures."""
import importlib.util
from pathlib import Path
import queue
import sys
import threading
import unittest

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


if __name__=='__main__': unittest.main()
