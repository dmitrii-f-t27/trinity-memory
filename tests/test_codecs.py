import itertools
import math
import random
import struct
import unittest
import zlib

from trinity_memory import CODECS, CodecError, decode_file, encode_file, inspect_file, pack, unpack
from trinity_memory.codecs import SPARSE_STATES, payload_size
from trinity_memory.container import HEADER_BYTES, PREFIX
from trinity_memory.sparsity import entropy_bpw, project_topk, symmetric_entropy


class CodecTests(unittest.TestCase):
    def test_dense5_exhaustive_matches_independent_base3(self):
        for trits in itertools.product((-1, 0, 1), repeat=5):
            expected = sum((t + 1) * 3 ** i for i, t in enumerate(trits))
            self.assertEqual(pack(trits), bytes([expected]))
            self.assertEqual(unpack(bytes([expected]), 5), list(trits))

    def test_dense5_rejects_every_reserved_code(self):
        for code in range(243, 256):
            with self.assertRaises(CodecError):
                unpack(bytes([code]), 5)

    def test_baseline_exhaustive(self):
        lane = {0: 0, 1: 1, -1: 2}
        for trits in itertools.product((-1, 0, 1), repeat=4):
            word = sum(lane[t] << (2 * i) for i, t in enumerate(trits))
            self.assertEqual(pack(trits, "baseline2"), bytes([word]))
            self.assertEqual(unpack(bytes([word]), 4, "baseline2"), list(trits))
        for word in range(256):
            if any(((word >> (2 * i)) & 3) == 3 for i in range(4)):
                with self.assertRaises(CodecError):
                    unpack(bytes([word]), 4, "baseline2")

    def test_sparse_exhaustive_membership_and_codes(self):
        for codec, n, k, states in (("sparse41", 4, 1, 9), ("sparse82", 8, 2, 129)):
            seen = set()
            for trits in itertools.product((-1, 0, 1), repeat=n):
                if sum(t != 0 for t in trits) <= k:
                    payload = pack(trits, codec)
                    self.assertEqual(unpack(payload, n, codec), list(trits))
                    seen.add(payload)
                else:
                    with self.assertRaises(CodecError):
                        pack(trits, codec)
            self.assertEqual(len(seen), states)
            self.assertEqual(states, sum(math.comb(n, i) * 2 ** i for i in range(k + 1)))
            for word in range(states, 1 << CODECS[codec].group_bits):
                with self.assertRaises(CodecError):
                    unpack(bytes([word]), n, codec)

    def test_sparse41_rtl_contract(self):
        self.assertEqual(pack([0] * 4, "sparse41"), b"\0")
        for i in range(4):
            for t in (-1, 1):
                group = [0] * 4
                group[i] = t
                self.assertEqual(pack(group, "sparse41")[0], 1 + 2 * i + (t == 1))

    def test_dense17_and22_bit_contiguity(self):
        for name in ("dense17", "dense22"):
            c = CODECS[name]
            groups = [[-1] * c.group_size, [1] * c.group_size, [0] * c.group_size]
            words = [sum((t + 1) * 3 ** i for i, t in enumerate(g)) for g in groups]
            combined = sum(w << (i * c.group_bits) for i, w in enumerate(words))
            expected = combined.to_bytes((3 * c.group_bits + 7) // 8, "little")
            trits = sum(groups, [])
            self.assertEqual(pack(trits, name), expected)
            self.assertEqual(unpack(expected, len(trits), name), trits)

    def test_dense_reserved_big_words(self):
        for name in ("dense17", "dense22"):
            c = CODECS[name]
            for word in (3 ** c.group_size, (1 << c.group_bits) - 1):
                with self.assertRaises(CodecError):
                    unpack(word.to_bytes((c.group_bits + 7) // 8, "little"), c.group_size, name)

    def test_partial_groups_random_lengths_and_dot_product(self):
        rng = random.Random(2708)
        for name, c in CODECS.items():
            for count in list(range(72)) + [127, 255, 1024, 4097]:
                values = []
                while len(values) < count:
                    if c.max_nonzero is None:
                        group = [rng.choice((-1, 0, 1)) for _ in range(c.group_size)]
                    else:
                        group = list(rng.choice(SPARSE_STATES[name]))
                    values.extend(group)
                values = values[:count]
                payload = pack(values, name)
                decoded = unpack(payload, count, name)
                self.assertEqual(len(payload), payload_size(count, name))
                self.assertEqual(decoded, values)
                activation = [rng.randrange(-127, 128) for _ in values]
                self.assertEqual(sum(t * a for t, a in zip(values, activation)),
                                 sum(t * a for t, a in zip(decoded, activation)))

    def test_invalid_weights_counts_codecs(self):
        for value in (True, False, 0.0, 1.0, "1", None, 2, -2, float("nan")):
            with self.assertRaises(CodecError):
                pack([value])
        for count in (-1, True, 1.0):
            with self.assertRaises(CodecError):
                unpack(b"", count)
        with self.assertRaises(CodecError):
            pack([0], "missing")

    def test_canonical_padding(self):
        for name, c in CODECS.items():
            if c.group_size > 1:
                bad = [0] * c.group_size
                bad[-1] = 1
                with self.assertRaisesRegex(CodecError, "padded trits"):
                    unpack(pack(bad, name), 1, name)
            payload = pack([0], name)
            unused = len(payload) * 8 - c.group_bits
            if unused:
                bad = bytearray(payload)
                bad[-1] |= 128
                with self.assertRaisesRegex(CodecError, "unused high bits"):
                    unpack(bad, 1, name)

    def test_payload_exact_length(self):
        for name in CODECS:
            data = pack([0] * 33, name)
            for malformed in (data[:-1], data + b"\0"):
                with self.assertRaises(CodecError):
                    unpack(malformed, 33, name)


class ContainerTests(unittest.TestCase):
    @staticmethod
    def reseal(data):
        checksum = zlib.crc32(data[HEADER_BYTES:], zlib.crc32(data[:PREFIX.size]))
        data[PREFIX.size:HEADER_BYTES] = struct.pack("<I", checksum)
        return data

    def test_roundtrip_all_codecs_and_empty(self):
        self.assertEqual(HEADER_BYTES, 24)
        for name in CODECS:
            for values in ([], [0], [-1] + [0] * 21):
                data = encode_file(values, name)
                self.assertEqual(decode_file(data), values)
                info = inspect_file(data)
                self.assertEqual(info["count"], len(values))
                self.assertEqual(info["container_bytes"], len(data))
                self.assertTrue(info["validated"])

    def test_single_bit_corruption_every_byte_is_rejected(self):
        data = encode_file([-1, 0, 1] * 17)
        for i in range(len(data)):
            for bit in range(8):
                altered = bytearray(data)
                altered[i] ^= 1 << bit
                with self.assertRaises(CodecError):
                    decode_file(altered)

    def test_truncation_and_trailing_data(self):
        data = encode_file([1, 0, -1])
        for n in range(len(data)):
            with self.assertRaises(CodecError):
                decode_file(data[:n])
        with self.assertRaises(CodecError):
            decode_file(data + b"\0")

    def test_reserved_code_even_with_correct_crc(self):
        data = bytearray(encode_file([0] * 5))
        data[-1] = 255
        with self.assertRaisesRegex(CodecError, "invalid dense5 code"):
            decode_file(self.reseal(data))

    def test_noncanonical_padding_even_with_correct_crc(self):
        data = bytearray(encode_file([0]))
        data[-1] = pack([0, 0, 0, 0, 1])[0]
        with self.assertRaisesRegex(CodecError, "padded trits"):
            decode_file(self.reseal(data))


class SparsityTests(unittest.TestCase):
    def test_entropy_limits(self):
        self.assertEqual(entropy_bpw([]), 0)
        self.assertEqual(entropy_bpw([0] * 8), 0)
        self.assertAlmostEqual(entropy_bpw([-1, 0, 1]), math.log2(3))
        self.assertAlmostEqual(symmetric_entropy(1 / 3), math.log2(3))
        self.assertAlmostEqual(symmetric_entropy(0.75), 1.061278124459133)
        self.assertEqual(symmetric_entropy(0), 1)
        self.assertEqual(symmetric_entropy(1), 0)
        for p in (-0.1, 1.1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                symmetric_entropy(p)

    def test_projection_is_explicit_and_stable(self):
        original = [0.1, -0.9, 0.2, 0.0, -0.8, 0.8, 0.0, 0.0, 0.5]
        result = project_topk(original, 4, 1)
        self.assertEqual(result, [0, -1, 0, 0, -1, 0, 0, 0, 1])
        self.assertEqual(unpack(pack(result, "sparse41"), len(result), "sparse41"), result)
        self.assertEqual(original[0], 0.1)
        self.assertEqual(project_topk([1, -1, 0], 4, 0), [0, 0, 0])

    def test_invalid_projection(self):
        for n, k in ((0, 0), (4, -1), (4, 5), (True, 1)):
            with self.assertRaises(ValueError):
                project_topk([1], n, k)
        for weights in ([float("nan")], [float("inf")], [True], ["1"]):
            with self.assertRaises(CodecError):
                project_topk(weights, 4, 1)


if __name__ == "__main__":
    unittest.main()
