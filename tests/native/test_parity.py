"""Migration oracle tests; Python is test infrastructure, not native runtime."""
import ctypes as C
from itertools import product
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/reference"))
from trinity_memory_reference.codecs import CODECS, CodecError, pack, unpack
from trinity_memory_reference.container import encode_file, decode_file

LIB = ROOT / 'build/t27' / ('libtrinity_memory_t27.dylib' if sys.platform == 'darwin' else 'libtrinity_memory_t27.so')
lib = C.CDLL(str(LIB))
P8, P32 = C.POINTER(C.c_uint8), C.POINTER(C.c_int32)
lib.tm_encode.argtypes = [C.c_int32, P32, C.c_size_t, P8, C.c_size_t]
lib.tm_encode.restype = C.c_int64
lib.tm_decode.argtypes = [C.c_int32, P8, C.c_size_t, C.c_size_t, P32, C.c_size_t]
lib.tm_decode.restype = C.c_int32
lib.tm_pack.argtypes = lib.tm_encode.argtypes
lib.tm_pack.restype = C.c_int64
lib.tm_unpack.argtypes = [P8, C.c_size_t, P32, C.c_size_t]
lib.tm_unpack.restype = C.c_int64
lib.tm_payload_size.argtypes = [C.c_int32, C.c_uint64]
lib.tm_payload_size.restype = C.c_int64


def octets(data):
    return (C.c_uint8 * len(data)).from_buffer_copy(data)


class NativeParity(unittest.TestCase):
    def check_vector(self, codec, values):
        original = encode_file(values, codec.name)
        source = (C.c_int32 * len(values))(*values)
        out = (C.c_uint8 * len(original))()
        size = lib.tm_pack(codec.id, source, len(values), out, len(out))
        self.assertEqual(size, len(original))
        self.assertEqual(bytes(out), original)
        payload = pack(values, codec.name)
        self.assertEqual(lib.tm_payload_size(codec.id, len(values)), len(payload))
        self.assertEqual(lib.tm_encode(codec.id, source, len(values), out, len(out)), len(payload))
        self.assertEqual(bytes(out[:len(payload)]), payload)
        restored = (C.c_int32 * len(values))()
        self.assertEqual(lib.tm_unpack(octets(original), len(original), restored, len(restored)), len(values))
        self.assertEqual(list(restored), list(values))

    def test_every_small_byte_code_and_canonical_padding(self):
        for name in ('baseline2', 'dense5', 'sparse41', 'sparse82'):
            codec = CODECS[name]
            for count in range(1, codec.group_size + 1):
                for word in range(256):
                    data = bytes([word])
                    out = (C.c_int32 * count)(*([777] * count))
                    status = lib.tm_decode(codec.id, octets(data), 1, count, out, count)
                    try:
                        expected = unpack(data, count, name)
                    except CodecError:
                        self.assertLess(status, 0, (name, count, word))
                        self.assertEqual(list(out), [777] * count)
                    else:
                        self.assertEqual(status, 0, (name, count, word))
                        self.assertEqual(list(out), expected)

    def test_seeded_wire_parity_all_six_codecs(self):
        rng = random.Random(27)
        for codec in CODECS.values():
            for count in list(range(80)) + [127, 257, 1024, 65536]:
                values = [rng.choice((-1, 0, 1)) for _ in range(count)]
                if codec.max_nonzero is not None:
                    values = [0] * count
                    for start in range(0, count, codec.group_size):
                        width = min(count - start, codec.group_size)
                        for lane in rng.sample(range(width), rng.randrange(min(width, codec.max_nonzero) + 1)):
                            values[start + lane] = rng.choice((-1, 1))
                self.check_vector(codec, values)

    def test_every_dense5_input(self):
        for values in product((-1, 0, 1), repeat=5):
            self.check_vector(CODECS['dense5'], values)

    def test_corruptions_and_resealed_headers_match_reference(self):
        for codec in CODECS.values():
            original = encode_file([0] * 23, codec.name)
            mutations = [original[:i] for i in range(len(original))] + [original + b'\0']
            for pos in range(len(original)):
                for reseal in (False, True):
                    value = bytearray(original)
                    value[pos] ^= 0x80
                    if reseal:
                        crc = zlib.crc32(value[24:], zlib.crc32(value[:20]))
                        value[20:24] = crc.to_bytes(4, 'little')
                    mutations.append(bytes(value))
            for data in mutations:
                out = (C.c_int32 * 100)(*([777] * 100))
                status = lib.tm_unpack(octets(data), len(data), out, 100)
                try:
                    expected = decode_file(data)
                except CodecError:
                    self.assertLess(status, 0)
                    self.assertEqual(list(out), [777] * 100)
                else:
                    self.assertEqual(status, len(expected))
                    self.assertEqual(list(out[:status]), expected)


class NativeCLI(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.binary = ROOT / 'build/t27/trinity-memory-t27'

    def run_cli(self, *args, success=True):
        result = subprocess.run([str(self.binary), *map(str, args)], cwd=self.directory,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode == 0, success, result.stderr)
        return result

    def test_pack_unpack_outside_checkout(self):
        for name in CODECS:
            source, packed, restored = (self.directory / x for x in ('input.json', 'data.tmem', 'out.json'))
            values = [-1, 0, 0, 0, 0, 0, 0, 1, 0]
            source.write_text(json.dumps(values))
            self.run_cli('pack', source, packed, '--codec', name)
            self.assertEqual(packed.read_bytes(), encode_file(values, name))
            info = json.loads(self.run_cli('inspect', packed).stdout)
            self.assertTrue(info['validated'])
            self.assertEqual(info['count'], len(values))
            self.run_cli('unpack', packed, restored)
            self.assertEqual(json.loads(restored.read_text()), values)

    def test_bad_json_leaves_existing_output_untouched(self):
        source, output = self.directory / 'bad.json', self.directory / 'existing.tmem'
        for invalid in ('', 'null', '{}', '[true]', '[1.0]', '[2]', '[01]', '[+1]', '[--1]',
                        '[1,]', '[,1]', '[1 0]', '[1] 0', '[1e0]', '[NaN]', '[1', '[[1]]'):
            source.write_text(invalid)
            output.write_bytes(b'preserve')
            self.run_cli('pack', source, output, success=False)
            self.assertEqual(output.read_bytes(), b'preserve')

    def test_empty_negative_zero_whitespace_and_malformed_tmem(self):
        source, packed = self.directory / 'input.json', self.directory / 'data.tmem'
        for text, values in [('[]', []), (' \r\n[ -0, 0, -1, 1 ]\t', [0, 0, -1, 1])]:
            source.write_text(text)
            self.run_cli('pack', source, packed)
            self.assertEqual(packed.read_bytes(), encode_file(values))
        packed.write_bytes(packed.read_bytes() + b'\0')
        self.run_cli('inspect', packed, success=False)


if __name__ == '__main__':
    unittest.main()
