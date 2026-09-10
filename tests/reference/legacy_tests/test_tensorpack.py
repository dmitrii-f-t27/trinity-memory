from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import json
import math
import struct
import unittest
from dataclasses import replace
from unittest.mock import patch
import zlib

from trinity_memory_reference.codecs import CODECS, pack
from trinity_memory_reference.container import encode_file
from trinity_memory_reference.tensorpack import (
    HEADER_BYTES, MAGIC, MAX_DIMENSION, MAX_METADATA_BYTES, MAX_PAYLOAD_BYTES,
    MAX_TENSORS, MAX_TOTAL_TRITS, Tensor, TensorPackError, decode_tensors,
    encode_tensors, inspect_tensorpack,
)


def frame(metadata, payload=b"", tensor_count=None):
    """Independent framing fixture, deliberately bypassing production encoder."""
    if isinstance(metadata, dict):
        if tensor_count is None:
            tensor_count = len(metadata["tensors"])
        metadata = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode()
    prefix = struct.pack("<4sBBHIIQ", b"TTPK", 1, 0, 0, len(metadata), tensor_count or 0, len(payload))
    return prefix + struct.pack("<II", zlib.crc32(metadata, zlib.crc32(prefix)),
                                zlib.crc32(payload)) + metadata + payload


def descriptor(name="weights", values=(1, 0, -1), codec="dense5"):
    blob = encode_file(values, codec)
    entry = {"name": name, "shape": [len(values)], "codec": codec, "scales": [1.0],
             "scale_axis": None, "axes": [], "offset": 0, "length": len(blob)}
    return entry, blob


def inner_crc(blob):
    """Reseal an intentionally malformed TMEM with independent CRC assembly."""
    return blob[:20] + struct.pack("<I", zlib.crc32(blob[24:], zlib.crc32(blob[:20]))) + blob[24:]


class TensorPackTests(unittest.TestCase):
    def assert_rejected(self, data):
        for reader in (decode_tensors, inspect_tensorpack):
            with self.assertRaises(TensorPackError):
                reader(data)

    def test_independent_scalar_fixture_and_header_layout(self):
        # A one-value baseline TMEM is independently specified below: trit -1
        # occupies lane 10, remaining lanes encode zero.
        inner_prefix = struct.pack("<4sBBBBQI", b"TMEM", 1, 0, 0, 0, 1, 1)
        blob = inner_prefix + struct.pack("<I", zlib.crc32(b"\x02", zlib.crc32(inner_prefix))) + b"\x02"
        entry = {"name": "scalar", "shape": [], "codec": "baseline2", "scales": [0.25],
                 "scale_axis": None, "axes": [], "offset": 0, "length": 25}
        data = frame({"order": "C", "tensors": [entry]}, blob)
        self.assertEqual(HEADER_BYTES, 32)
        self.assertEqual(MAGIC, b"TTPK")
        self.assertEqual(decode_tensors(data), [Tensor("scalar", (), (-1,), "baseline2", (0.25,))])

    def test_all_codecs_partial_groups_and_determinism(self):
        values = tuple(-1 if i % 8 == 0 else 0 for i in range(23))
        for codec in CODECS:
            tensors = [Tensor(codec, (23,), values, codec)]
            with self.subTest(codec=codec):
                data = encode_tensors(tensors)
                self.assertEqual(decode_tensors(data), tensors)
                self.assertEqual(encode_tensors(decode_tensors(data)), data)
                self.assertEqual(encode_tensors(tensors), data)

    def test_mixed_tensor_metadata_values_and_sizes(self):
        tensors = [Tensor("matrix.α", (2, 3), (-1, 0, 1, 1, 0, -1), "dense22",
                          (0.1, 0.75), 0, ("output", "input")),
                   Tensor("cube", (1, 2, 2), (0, 0, 0, -1), "sparse41",
                          (math.nextafter(1.0, 2.0),), None, ("batch", "row", "column")),
                   Tensor("scalar", (), (1,))]
        data = encode_tensors(tensors)
        decoded = decode_tensors(bytearray(data))
        self.assertEqual(decoded, tensors)
        self.assertEqual(decoded[0].scales[0].hex(), tensors[0].scales[0].hex())
        info = inspect_tensorpack(data)
        self.assertEqual(info["format"], "TensorPack")
        self.assertEqual(info["order"], "C")
        self.assertEqual(info["tensor_count"], 3)
        self.assertEqual(info["total_count"], 11)
        self.assertEqual(info["header_bytes"] + info["metadata_bytes"] + info["payload_bytes"], len(data))
        self.assertEqual(sum(t["length"] for t in info["tensors"]), info["payload_bytes"])
        self.assertEqual(sum(t["payload_bytes"] + 24 for t in info["tensors"]), info["payload_bytes"])
        self.assertTrue(info["validated"])
        with self.assertRaises(AttributeError):
            decoded[0].name = "changed"

    def test_empty_pack_is_valid(self):
        self.assertEqual(decode_tensors(encode_tensors([])), [])
        self.assertEqual(inspect_tensorpack(encode_tensors([]))["total_count"], 0)

    def test_encoder_rejects_invalid_metadata_and_values(self):
        base = Tensor("weights", (2,), (0, 1))
        variants = [replace(base, name=name) for name in ("", "x\n", "x\0", "\ud800", "x" * 257, 1)]
        variants += [replace(base, shape=shape) for shape in ([2], (0,), (-1,), (True,), (2.0,),
                                                             (MAX_DIMENSION + 1,), (1,) * 17, (3,))]
        variants += [replace(base, scales=scales) for scales in ((), [1.0], (True,), (1,), (0.0,),
                                                                 (-0.0,), (-1.0,), (float("nan"),),
                                                                 (float("inf"),), (1.0, 2.0))]
        variants += [replace(base, scale_axis=axis) for axis in (True, -1, 1, 0.0, "0")]
        variants += [replace(base, axes=axes) for axes in (["x"], ("",), ("x", "y"), ("x" * 65,), (True,))]
        variants += [replace(base, codec=codec) for codec in (True, [], "unknown")]
        variants += [replace(base, values=values) for values in ([0, 1], (0,), (0, True), (0, 1.0), (0, 2))]
        variants += [Tensor("x", (2, 1), (0, 1), axes=("x", "x")),
                     Tensor("x", (), (1,), scale_axis=0),
                     Tensor("x", (4,), (1, 1, 0, 0), "sparse41")]
        for tensor in variants:
            with self.subTest(tensor=repr(tensor)), self.assertRaises(TensorPackError):
                encode_tensors([tensor])
        for values in (None, "x", iter([base]), [42], [base, base]):
            with self.assertRaises(TensorPackError):
                encode_tensors(values)

    def test_every_single_bit_corruption_and_truncation_rejected(self):
        data = encode_tensors([Tensor("w", (3,), (-1, 0, 1))])
        for index in range(len(data)):
            for bit in range(8):
                altered = bytearray(data)
                altered[index] ^= 1 << bit
                with self.assertRaises(TensorPackError):
                    decode_tensors(altered)
        for length in range(len(data)):
            self.assert_rejected(data[:length])
        self.assert_rejected(data + b"\0")
        self.assert_rejected("TTPK")
        self.assert_rejected(None)

    def test_valid_crc_cannot_hide_invalid_json_or_duplicate_keys(self):
        entry, payload = descriptor()
        metadata = json.dumps({"order": "C", "tensors": [entry]}).encode()
        malformed = [b"\xff", b"\xef\xbb\xbf{}", b"null", b"[]", b"{}", b'{"order":"C","tensors":[],}',
                     b'{"order":"C","order":"C","tensors":[]}',
                     metadata.replace(b'"name": "weights"', b'"name":"weights","name":"other"'),
                     metadata.replace(b"[1.0]", b"[NaN]"), metadata.replace(b"[1.0]", b"[Infinity]"),
                     metadata.replace(b"[1.0]", b"[1e999]"),
                     metadata.replace(b"[3]", b"[999999999999999999999999999999999]"),
                     b"[" * 10000 + b"0" + b"]" * 10000,
                     b'{"order":"C","tensors":[]} extra']
        for raw in malformed:
            with self.subTest(raw=raw[:100]):
                self.assert_rejected(frame(raw, payload, 1))

    def test_valid_crc_cannot_hide_invalid_schema(self):
        entry, payload = descriptor()
        for metadata in ({"order": "F", "tensors": [entry]},
                         {"order": "C", "tensors": {}, "future": 1},
                         {"order": "C", "tensors": [entry], "future": 1},
                         {"order": "C", "tensors": [None]}):
            self.assert_rejected(frame(metadata, payload))
        changes = {"shape": [None, "3", [True], [0], [-1], [3.0], [4], [MAX_TOTAL_TRITS + 1]],
                   "axes": [None, "x", ["x", "y"], [True]], "scales": [None, [1], [False], [], [0.0], [-1.0]],
                   "scale_axis": [True, -1, 1, 0.0], "codec": [None, "baseline2", "unknown"],
                   "name": [None, "", "\ud800"], "offset": [True, -1, 1, 0.0],
                   "length": [True, 0, entry["length"] - 1, entry["length"] + 1, float(entry["length"])]}
        for field, values in changes.items():
            for value in values:
                altered = {**entry, field: value}
                # Escaped JSON permits malformed surrogate strings to reach the decoder.
                raw = json.dumps({"order": "C", "tensors": [altered]}).encode()
                with self.subTest(field=field, value=value):
                    self.assert_rejected(frame(raw, payload, 1))
        for field in entry:
            altered = dict(entry)
            del altered[field]
            self.assert_rejected(frame({"order": "C", "tensors": [altered]}, payload))
        self.assert_rejected(frame({"order": "C", "tensors": [{**entry, "future": 1}]}, payload))

    def test_duplicate_names_offsets_gaps_reordering_and_tails_rejected(self):
        first, payload = descriptor()
        second = {**first, "name": "other", "offset": len(payload)}
        valid = {"order": "C", "tensors": [first, second]}
        self.assertEqual(len(decode_tensors(frame(valid, payload * 2))), 2)
        for change in ({"name": "weights"}, {"offset": 0}, {"offset": len(payload) + 1}, {"offset": -1}):
            self.assert_rejected(frame({"order": "C", "tensors": [first, {**second, **change}]}, payload * 2))
        self.assert_rejected(frame({"order": "C", "tensors": [second, first]}, payload * 2))
        self.assert_rejected(frame(valid, payload * 2, 1))
        self.assert_rejected(frame({"order": "C", "tensors": [first]}, payload + b"\0"))
        self.assert_rejected(frame({"order": "C", "tensors": []}, payload))

    def test_inner_crc_reserved_codes_and_padding_are_checked(self):
        for codec in CODECS:
            entry, blob = descriptor(values=(0,), codec=codec)
            bad_values = [0] * CODECS[codec].group_size
            bad_values[-1] = 1
            bad_padding = inner_crc(blob[:24] + pack(bad_values, codec))
            with self.subTest(codec=codec):
                self.assert_rejected(frame({"order": "C", "tensors": [entry]}, bad_padding))
            if CODECS[codec].group_bits % 8:
                unused_bits = inner_crc(blob[:-1] + bytes([blob[-1] | 0x80]))
                self.assert_rejected(frame({"order": "C", "tensors": [entry]}, unused_bits))
        entry, blob = descriptor(values=(0,) * 5)
        self.assert_rejected(frame({"order": "C", "tensors": [entry]}, inner_crc(blob[:-1] + b"\xff")))
        # The outer CRC is correct, but the nested checksum is wrong.
        self.assert_rejected(frame({"order": "C", "tensors": [entry]}, blob[:20] + b"\0" * 4 + blob[24:]))
        bad_count = inner_crc(blob[:8] + struct.pack("<Q", 1) + blob[16:])
        self.assert_rejected(frame({"order": "C", "tensors": [entry]}, bad_count))
        bad_flags = inner_crc(blob[:6] + b"\x01" + blob[7:])
        self.assert_rejected(frame({"order": "C", "tensors": [entry]}, bad_flags))

    def test_resource_limits_before_nested_decode(self):
        for meta_size, count, size in ((MAX_METADATA_BYTES + 1, 0, 0), (0, MAX_TENSORS + 1, 0),
                                       (0, 0, MAX_PAYLOAD_BYTES + 1)):
            self.assert_rejected(struct.pack("<4sBBHIIQII", b"TTPK", 1, 0, 0, meta_size, count, size, 0, 0))
        base = Tensor("w", (2,), (0, 1))
        data = encode_tensors([base, replace(base, name="other")])
        with patch("trinity_memory_reference.tensorpack.MAX_TOTAL_TRITS", 3):
            with self.assertRaises(TensorPackError):
                encode_tensors([base, replace(base, name="other")])
            with patch("trinity_memory_reference.tensorpack.decode_file", side_effect=AssertionError("must not decode")):
                self.assert_rejected(data)
        with patch("trinity_memory_reference.tensorpack.MAX_METADATA_BYTES", 32):
            with self.assertRaises(TensorPackError):
                encode_tensors([base])
        with patch("trinity_memory_reference.tensorpack.MAX_PAYLOAD_BYTES", 24):
            with self.assertRaises(TensorPackError):
                encode_tensors([base])
        with self.assertRaises(TensorPackError):
            encode_tensors([base] * (MAX_TENSORS + 1))

    def test_callers_can_lower_but_not_disable_the_decode_limit(self):
        data = encode_tensors([Tensor("w", (2,), (0, 1))])
        for reader in (decode_tensors, inspect_tensorpack):
            for limit in (0, -1, True, 1.0, "2", None):
                with self.assertRaises(TensorPackError):
                    reader(data, max_total_trits=limit)
            with patch("trinity_memory_reference.tensorpack.decode_file", side_effect=AssertionError("must not decode")):
                with self.assertRaisesRegex(TensorPackError, "decoded trit limit"):
                    reader(data, max_total_trits=1)
                with patch("trinity_memory_reference.tensorpack.MAX_TOTAL_TRITS", 1):
                    with self.assertRaisesRegex(TensorPackError, "decoded trit limit"):
                        reader(data, max_total_trits=MAX_TOTAL_TRITS * 2)
            reader(data, max_total_trits=2)


if __name__ == "__main__":
    unittest.main()
