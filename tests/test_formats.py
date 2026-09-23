"""Python bindings of t27/formats.t27 on synthetic containers (no network)."""
import json
import struct
import unittest

from trinity_memory import formats as f


def _gguf(tensors, keys=()):
    out = bytearray(struct.pack("<IIQQ", 0x46554747, 3, len(tensors), len(keys)))
    for key, value in keys:
        out += struct.pack("<Q", len(key)) + key.encode() + struct.pack("<I", 4) + struct.pack("<I", value)
    for name, dims, ggml_type, offset in tensors:
        out += struct.pack("<Q", len(name)) + name.encode() + struct.pack("<I", len(dims))
        out += b"".join(struct.pack("<Q", d) for d in dims) + struct.pack("<IQ", ggml_type, offset)
    return bytes(out)


class FormatsBindingTest(unittest.TestCase):
    def test_gguf_lookup_and_listing(self):
        header = _gguf([("blk.0.ffn_down.weight", [256, 2], 35, 0), ("blk.0.attn_q.weight", [128, 4], 36, 160)],
                       keys=[("general.alignment", 32)])
        status, info = f.gguf_find(header, "blk.0.attn_q.weight")
        self.assertEqual(status, 0)
        self.assertEqual((info.tensor_type, info.shape, info.offset), (36, [128, 4], 160))
        self.assertEqual(info.data_start, (len(header) + 31) // 32 * 32)
        self.assertEqual(f.gguf_tensor_bytes(info), 512 // 4 + 32)
        self.assertEqual([name for name, _ in f.gguf_names(header)], ["blk.0.ffn_down.weight", "blk.0.attn_q.weight"])
        status, info = f.gguf_find(header[:40], "blk.0.attn_q.weight")
        self.assertEqual(status, f.TRUNCATED)
        self.assertGreater(info.needed, 40)

    def test_safetensors_lookup(self):
        meta = {"w": {"dtype": "U8", "shape": [2, 3], "data_offsets": [0, 6]}}
        header = json.dumps(meta).encode()
        blob = struct.pack("<Q", len(header)) + header + bytes(6)
        status, info, dtype = f.safetensors_find(blob, "w")
        self.assertEqual((status, dtype, info.shape, info.begin, info.end), (0, "U8", [2, 3], 8 + len(header), 14 + len(header)))
        self.assertEqual(f.safetensors_find(blob, "missing")[0], f.NOT_FOUND)

    def test_decoders_round_trip_worked_examples(self):
        values, scales, outside = f.decode_blocks(f.Q2_0, bytes([0x00, 0x34, 0x1B, 0xE4, 0x06]) + bytes(13), 64)
        self.assertEqual((list(values[:8]), scales, outside), ([2, 1, 0, -1, -1, 0, 1, 2], [0x3400], 2))
        i2s = bytes([0x92]) + bytes(31) + struct.pack("<f", 1.0) + bytes(28)
        values, scale, outside = f.decode_i2s(i2s, 128)
        self.assertEqual((values[0], values[32], values[64], values[96], f.f32_value(scale), outside), (1, 0, -1, 1, 1.0, 0))
        values, outside = f.decode_hf_packed(bytes([0x86]), 4, 1)
        self.assertEqual(list(values[:4]), [1, 0, -1, 1])
        values, outside = f.decode_linear2(bytes([0xDB]), 1, 4, 1, 2)
        self.assertEqual(list(values[:4]), [1, 0, -1, 1])
        with self.assertRaises(f.FormatError):
            f.decode_i2s(i2s[:-1], 128)
        self.assertEqual(f.i2s_trailer_nonzero(i2s[:-1] + b"\x01", 128), 1)

    def test_absmean_and_scales(self):
        values, boundary, mean, s = f.absmean_bf16(bytes([0x80, 0x3F, 0x00, 0xBF, 0x80, 0x3E, 0x00, 0x00]), 4)
        self.assertEqual((list(values[:4]), boundary, mean), ([1, -1, 1, 0], 0, 0.4375))
        total, first = f.compare_scales([0x3C00, 0x3400], 2, f.F16, 128, [0x3F80], 1, f.BF16, 256, 256)
        self.assertEqual((total, first), (128, 128))


if __name__ == "__main__":
    unittest.main()
