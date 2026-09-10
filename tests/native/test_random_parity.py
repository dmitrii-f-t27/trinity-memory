"""CPython random.Random oracle for the generated native MT19937 kernels.

Python is test infrastructure only. Override TRINITY_RANDOM_LIBRARY to test an
isolated generated module; otherwise use the project's compiled native library.
"""
import ctypes as C
import os
from pathlib import Path
import random
import struct
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
LIB = os.environ.get('TRINITY_RANDOM_LIBRARY') or ROOT / 'build/t27' / (
    'libtrinity_memory_t27.dylib' if sys.platform == 'darwin' else 'libtrinity_memory_t27.so')
lib = C.CDLL(str(LIB))

class TMRandom(C.Structure):
    _fields_ = [('mt', C.c_uint32 * 624), ('index', C.c_size_t)]

P = C.POINTER(TMRandom)
P32, P64 = C.POINTER(C.c_uint32), C.POINTER(C.c_int64)
for name, args, result in (
    ('init', [P, C.c_uint64], None), ('init_i64', [P, C.c_int64], None),
    ('seed_words', [P, P32, C.c_size_t], C.c_int32),
    ('u32', [P], C.c_uint32), ('bits', [P, C.c_uint32], C.c_uint64),
    ('below', [P, C.c_uint32], C.c_uint32), ('below64', [P, C.c_uint64], C.c_uint64),
    ('random', [P], C.c_double), ('choice', [P, P64, C.c_uint32, P64], C.c_int32),
    ('randint', [P, C.c_int64, C.c_int64, P64], C.c_int32),
    ('range', [P, C.c_int64, C.c_int64, C.c_int64, P64], C.c_int32),
    ('sample_scratch', [C.c_uint32, C.c_uint32], C.c_int64),
    ('sample', [P, C.c_uint32, C.c_uint32, P32, C.c_size_t, P32, C.c_size_t], C.c_int32),
):
    f = getattr(lib, 'tm_random_' + name); f.argtypes = args; f.restype = result

class NativeRandomParity(unittest.TestCase):
    def seeded(self, seed):
        native = TMRandom()
        if -(1 << 63) <= seed < 0:
            lib.tm_random_init_i64(C.byref(native), seed)
        elif 0 <= seed < 1 << 64:
            lib.tm_random_init(C.byref(native), seed)
        else:
            absolute = abs(seed)
            words = []
            while absolute:
                words.append(absolute & 0xffffffff); absolute >>= 32
            words = words or [0]
            self.assertEqual(lib.tm_random_seed_words(C.byref(native), (C.c_uint32 * len(words))(*words), len(words)), 0)
        reference = random.Random(seed)
        self.assert_state(native, reference)
        return native, reference

    def assert_state(self, native, reference):
        state = reference.getstate()[1]
        self.assertEqual(tuple(native.mt), state[:-1])
        self.assertEqual(native.index, state[-1])

    def test_integer_seeds_and_twists(self):
        seeds = [0, 1, -1, 12, 27, -27, 2708, (1 << 32)-1, 1 << 32,
                 (1 << 64)-1, -(1 << 63), 1 << 64, -(1 << 128) + 31,
                 (1 << 20000) + (1 << 19937) + 27]
        for seed in seeds:
            with self.subTest(seed_bits=abs(seed).bit_length(), seed_sign=seed < 0):
                native, reference = self.seeded(seed)
                for _ in range(2500):
                    self.assertEqual(lib.tm_random_u32(C.byref(native)), reference.getrandbits(32))
                self.assert_state(native, reference)
        native, reference = self.seeded(27)
        self.assertEqual(lib.tm_random_seed_words(C.byref(native), (C.c_uint32 * 5)(27,0,0,0,0),5),0)
        self.assert_state(native, reference)

    def test_mixed_bits_rejection_and_floats(self):
        for seed in (0, 27, -27, (1 << 64)-1):
            native, reference = self.seeded(seed)
            for cycle in range(30):
                for bits in range(65):
                    self.assertEqual(lib.tm_random_bits(C.byref(native), bits), reference.getrandbits(bits))
                for n in [1,2,3,4,5,8,127,128,129,255,256,257,1 << 31,
                          (1 << 32)-1,1 << 32,(1 << 63)+1,(1 << 64)-1]:
                    self.assertEqual(lib.tm_random_below64(C.byref(native), n), reference._randbelow(n))
                self.assertEqual(struct.pack('d',lib.tm_random_random(C.byref(native))),struct.pack('d',reference.random()))
            self.assert_state(native, reference)

    def test_integer_ranges_choice_and_full_signed64(self):
        lo, hi = -(1 << 63), (1 << 63)-1
        intervals = [(-128,127),(0,0),(1,1),(-1,-1),(lo,hi),(lo,0),(-1,hi),(0,hi),(lo,lo),(hi,hi)]
        ranges = [(lo,hi,1),(hi,lo,-1),(0,lo,-1),(lo,0,1),(hi,lo,lo),
                  (lo,hi,hi),(-100,101,3),(100,-101,-3),(5,6,9),(5,4,-9)]
        output = C.c_int64()
        population = (C.c_int64 * 5)(lo,-1,0,3,hi)
        for seed in (0,27,-12):
            native, reference = self.seeded(seed)
            for _ in range(500):
                for a,b in intervals:
                    self.assertEqual(lib.tm_random_randint(C.byref(native),a,b,C.byref(output)),0)
                    self.assertEqual(output.value, reference.randint(a,b))
                for a,b,step in ranges:
                    self.assertEqual(lib.tm_random_range(C.byref(native),a,b,step,C.byref(output)),0)
                    self.assertEqual(output.value, reference.randrange(a,b,step))
                self.assertEqual(lib.tm_random_choice(C.byref(native),population,len(population),C.byref(output)),0)
                self.assertEqual(output.value, reference.choice(list(population)))
            self.assert_state(native, reference)

    def test_sample_pool_set_and_thresholds(self):
        native, reference = self.seeded(27)
        populations = list(range(0,35)) + [84,85,86,276,277,278,1044,1045,1046,10000,0xffffffff]
        for n in populations:
            ks = sorted({k for k in [0,1,2,5,6,20,21,22,85,86,n] if k <= n and k < 500})
            for k in ks:
                need = lib.tm_random_sample_scratch(n,k)
                out = (C.c_uint32 * k)(); scratch = (C.c_uint32 * need)()
                for _ in range(5):
                    self.assertEqual(lib.tm_random_sample(C.byref(native),n,k,out,k,scratch,need),0)
                    self.assertEqual(list(out),reference.sample(range(n),k))
        self.assert_state(native, reference)

    def test_complete_experiment_rng_call_order(self):
        native, reference = self.seeded(27)
        for count in [1,4,5,6,12,31,65]:
            self.assertEqual([lib.tm_random_below(C.byref(native),3)-1 for _ in range(count)],
                             [reference.choice((-1,0,1)) for _ in range(count)])
            self.assertEqual([lib.tm_random_below(C.byref(native),256)-128 for _ in range(count)],
                             [reference.randint(-128,127) for _ in range(count)])
        self.assert_state(native, reference)
        native, reference = self.seeded(27)
        count = 65539
        self.assertEqual([lib.tm_random_below(C.byref(native),3)-1 for _ in range(count)],
                         [reference.choice((-1,0,1)) for _ in range(count)])
        for group, maximum in [(4,1),(8,2)]:
            expected, actual = [0]*count, [0]*count
            for start in range(0,count,group):
                width = min(group,count-start); k = min(maximum,width)
                out=(C.c_uint32*k)();scratch=(C.c_uint32*width)()
                self.assertEqual(lib.tm_random_sample(C.byref(native),width,k,out,k,scratch,width),0)
                chosen = reference.sample(range(width),k)
                self.assertEqual(list(out),chosen)
                for index in out:actual[start+index] = -1 + 2*lib.tm_random_below(C.byref(native),2)
                for index in chosen:expected[start+index] = reference.choice((-1,1))
            self.assertEqual(actual,expected)
        self.assert_state(native,reference)

if __name__ == '__main__':
    unittest.main()
