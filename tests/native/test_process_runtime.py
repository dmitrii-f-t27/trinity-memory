"""Exercise actual subprocess isolation, argv, failure, timeout and cleanup."""
import ctypes as C
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / 'build/t27' / ('libtrinity_memory_t27.dylib' if sys.platform == 'darwin' else 'libtrinity_memory_t27.so')

class ProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = C.CDLL(str(LIB))
        L = cls.lib
        L.tm_process_new.restype = C.c_void_p
        L.tm_process_free.argtypes = [C.c_void_p]
        L.tm_process_reset.argtypes = [C.c_void_p]
        L.tm_process_arg.argtypes = [C.c_void_p, C.c_void_p, C.c_size_t]
        L.tm_process_write.argtypes = [C.c_void_p, C.c_char_p, C.c_void_p, C.c_size_t]
        L.tm_process_buffer.argtypes = [C.c_void_p, C.c_size_t]
        L.tm_process_buffer.restype = C.c_void_p
        L.tm_process_run.argtypes = [C.c_void_p, C.c_char_p, C.c_void_p, C.c_size_t, C.c_int32]
        L.tm_process_run.restype = C.c_int64
        L.tm_process_resource.argtypes = [C.c_char_p, C.c_void_p, C.c_size_t]
        L.tm_process_resource.restype = C.c_int64

    def setUp(self):
        self.context = self.lib.tm_process_new()
        self.assertTrue(self.context)
    def tearDown(self):
        if self.context:
            self.lib.tm_process_free(self.context)
    def run_process(self, args, capacity=4096, timeout=1000):
        self.lib.tm_process_reset(self.context)
        for arg in args[1:]:
            raw = arg.encode()
            self.assertEqual(self.lib.tm_process_arg(self.context, raw, len(raw)), 0)
        output = C.create_string_buffer(capacity)
        n = self.lib.tm_process_run(self.context, args[0].encode(), output, capacity, timeout)
        return n, output.raw[:max(0, n)]

    def test_arguments_files_and_owned_cleanup(self):
        text = b'literal $HOME `echo unsafe`; newline\n\x00bytes'
        self.assertEqual(self.lib.tm_process_write(self.context, b'input.bin', text, len(text)), 0)
        n, output = self.run_process([shutil.which('cat'), 'input.bin'])
        self.assertEqual((n, output), (len(text), text))
        n, output = self.run_process([shutil.which('pwd')])
        directory = Path(output.decode().strip())
        self.assertTrue((directory / 'input.bin').is_file())
        self.assertEqual(directory.parent, Path('/private/tmp') if sys.platform == 'darwin' else Path('/tmp'))
        self.assertEqual(self.lib.tm_process_arg(self.context, b'a\x00b', 3), -1)
        for name in (b'../escape', b'/tmp/escape', b'.', b'..', b''):
            self.assertEqual(self.lib.tm_process_write(self.context, name, text, len(text)), -1)
        allocation = self.lib.tm_process_buffer(self.context, 127)
        self.assertEqual(C.string_at(allocation, 127), bytes(127))
        self.lib.tm_process_free(self.context); self.context = None
        self.assertFalse(directory.exists())

    def test_timeout_nonzero_missing_and_bounded_output(self):
        start = time.monotonic()
        n, _ = self.run_process([shutil.which('sleep'), '5'], timeout=30)
        self.assertLess(n, 0)
        self.assertLess(time.monotonic() - start, 1)
        self.assertLess(self.run_process([shutil.which('false')])[0], 0)
        self.assertLess(self.run_process(['/nonexistent/trinity-tool'])[0], 0)
        self.assertLess(self.run_process([shutil.which('printf'), '%s', 'too long'], capacity=2)[0], 0)
        self.assertEqual(self.run_process([shutil.which('printf'), '%s', 'exact'], capacity=5), (5, b'exact'))

    def test_resource_resolution(self):
        buffer = C.create_string_buffer(4096)
        n = self.lib.tm_process_resource(b'trinity_dot_stream.v', buffer, len(buffer))
        self.assertGreater(n, 0)
        path = Path(buffer.raw[:n].decode())
        self.assertIn('Generated from t27', path.read_text())
        self.assertLess(self.lib.tm_process_resource(b'../../etc/passwd', buffer, len(buffer)), 0)
        self.assertLess(self.lib.tm_process_resource(b'missing.v', buffer, len(buffer)), 0)
        self.assertLess(self.lib.tm_process_resource(b'trinity_dot_stream.v', buffer, 2), 0)

if __name__ == '__main__':
    unittest.main()
