"""Ternary Check Live (issue #48) on synthetic GGUF headers: no network."""
import struct
import tempfile
import unittest
from pathlib import Path

from trinity_memory import live


def _gguf(tensors, keys=(), alignment=32):
    """A GGUF v3 header whose keys are u32 values; tensors are (name, dims, type, offset)."""
    keys = [("general.alignment", alignment)] + list(keys)
    out = bytearray(struct.pack("<IIQQ", 0x46554747, 3, len(tensors), len(keys)))
    for key, value in keys:
        out += struct.pack("<Q", len(key)) + key.encode() + struct.pack("<I", 4) + struct.pack("<I", value)
    for name, dims, ggml_type, offset in tensors:
        out += struct.pack("<Q", len(name)) + name.encode() + struct.pack("<I", len(dims))
        out += b"".join(struct.pack("<Q", d) for d in dims) + struct.pack("<IQ", ggml_type, offset)
    data_start = (len(out) + alignment - 1) // alignment * alignment
    return bytes(out) + bytes(data_start - len(out))


def _file(tensors, block_bytes, keys=()):
    """Header plus the size a file needs when every tensor is 1024 x 4 weights
    stored in blocks of `block_bytes` per `group` weights, packed back to back."""
    offset, records = 0, []
    for name, ggml_type, group in tensors:
        records.append((name, [1024, 4], ggml_type, offset))
        offset += 4 * (1024 // group) * block_bytes
        offset = (offset + 31) // 32 * 32
    header = _gguf(records, keys)
    return header, len(header) + offset


class FakeHub:
    """Serves one file from memory and counts range reads."""

    def __init__(self, data: bytes):
        self.data, self.reads = data, []

    def read(self, repo, revision, filename, begin, end):
        self.reads.append((begin, end))
        return self.data[begin:end]


class CheckHeaderTest(unittest.TestCase):
    def test_pq2_0_with_prism_namespace_passes(self):
        header, size = _file([("blk.0.ffn_down.weight", 142, 128), ("blk.0.ffn_up.weight", 142, 128)], 34,
                             keys=[("prism.quant.version", 1)])
        record = live.check_header(header, size)
        self.assertEqual(record["verdict"], "ok")
        self.assertEqual(record["layouts"], {"PQ2_0": 2})
        self.assertEqual(record["namespace"], {"prism": True, "bitnet": False})
        self.assertEqual(record["ggml_types"], {"142": 2})
        self.assertEqual(record["hadamard"], {"present": False, "status": "ok"})

    def test_hadamard_version_without_the_other_keys_is_rejected(self):
        header, size = _file([("blk.0.ffn_down.weight", 142, 128)], 34, keys=[("prism.hadamard.version", 1)])
        record = live.check_header(header, size)
        self.assertEqual(record["verdict"], "rejected")
        self.assertEqual(record["hadamard"]["status"], "hadamard_missing")
        self.assertEqual(record["rejections"]["hadamard_missing"]["count"], 1)
        self.assertEqual(record["layouts"], {"PQ2_0": 1})

    def test_fork_ids_without_the_fork_marker_are_read_as_the_forks_layouts(self):
        header, size = _file([("blk.0.ffn_down.weight", 143, 128)], 28)
        record = live.check_header(header, size)
        self.assertEqual((record["verdict"], record["prism_unmarked"], record["ternary_tensors"]), ("ok", 1, 1))
        self.assertEqual(record["layouts"], {"PTQ1_0": 1})

    def test_a_gap_left_between_records_is_refused_like_gguf_cpp(self):
        # Group-64 bytes (18 per 64) under the group-128 id 142: nothing
        # overlaps, but the first record does not end where the next begins.
        header, size = _file([("blk.0.a.weight", 142, 64), ("blk.0.b.weight", 142, 64)], 18,
                             keys=[("prism.quant.version", 1)])
        record = live.check_header(header, size)
        self.assertEqual(record["verdict"], "rejected")
        self.assertEqual(record["rejections"]["offsets"], {"count": 1, "examples": ["blk.0.a.weight"], "fits": {"Q2_0": 1}})
        self.assertEqual(record["layouts"], {"PQ2_0": 1})

    def test_q2_0_declared_with_group_128_bytes_is_rejected_on_extent(self):
        # Type 42 is Q2_0 with 64-weight groups (18 bytes); the bytes follow the
        # 128-weight, 34-byte layout, as in PrismML-Eng/llama.cpp#167.
        header, size = _file([("blk.0.a.weight", 42, 128), ("blk.0.b.weight", 42, 128)], 34)
        record = live.check_header(header, size)
        self.assertEqual(record["verdict"], "rejected")
        self.assertEqual(record["rejections"]["extent"]["count"], 2)
        self.assertEqual(record["rejections"]["extent"]["fits"], {"PQ2_0": 2})
        self.assertEqual(record["layouts"], {})

    def test_q2_0_group_64_passes(self):
        header, size = _file([("blk.0.a.weight", 42, 64), ("blk.0.b.weight", 42, 64)], 18)
        self.assertEqual(live.check_header(header, size)["layouts"], {"Q2_0": 2})

    def test_non_ternary_file(self):
        header, size = _file([("blk.0.a.weight", 8, 32)], 34)
        record = live.check_header(header, size)
        self.assertEqual((record["verdict"], record["ternary_tensors"], record["ggml_types"]),
                         ("no_ternary_layout", 0, {"8": 1}))

    def test_bad_magic_is_a_container_verdict(self):
        header, size = _file([("blk.0.a.weight", 42, 64)], 18)
        record = live.check_header(b"GGUX" + header[4:], size)
        self.assertEqual((record["verdict"], record["container"]), ("container", "container"))


class ReadHeaderTest(unittest.TestCase):
    def test_grows_until_the_reader_stops_asking_and_caches(self):
        header, size = _file([(f"blk.{i}.w", 42, 64) for i in range(40)], 18)
        data = header + bytes(size - len(header))
        hub = FakeHub(data)
        old = live.FIRST_READ
        live.FIRST_READ = 64
        try:
            with tempfile.TemporaryDirectory() as tmp:
                got = live.read_header(hub, "a/b", "0" * 40, "m-Q2_0.gguf", size, Path(tmp))
                self.assertEqual(got, header)
                self.assertGreater(len(hub.reads), 1)
                self.assertEqual(hub.reads[0], (0, 64))
                for (b0, e0), (b1, _) in zip(hub.reads, hub.reads[1:]):
                    self.assertEqual(e0, b1)
                again = FakeHub(data)
                self.assertEqual(live.read_header(again, "a/b", "0" * 40, "m-Q2_0.gguf", size, Path(tmp)), header)
                self.assertEqual(again.reads, [])
        finally:
            live.FIRST_READ = old


class DiscoveryTest(unittest.TestCase):
    def test_name_filter_and_threshold(self):
        class Hub:
            def search(self, query):
                return [{"id": "prism-ml/Ternary-Bonsai-2-27B-gguf", "downloads": 5},
                        {"id": "someone/Llama-3-8B-GGUF", "downloads": 10 ** 6},
                        {"id": "x/Bonsai-2-27B-PQ2_0-GGUF", "downloads": 500},
                        {"id": "y/model-q2_0", "downloads": 200},
                        {"id": "z/fooq2_0bar", "downloads": 900}]
        found = live.discover(Hub(), ("q",), min_downloads=100)
        self.assertEqual(found, {"x/Bonsai-2-27B-PQ2_0-GGUF": 500, "y/model-q2_0": 200})

    def test_file_selection(self):
        info = {"siblings": [
            {"rfilename": "Ternary-Bonsai-27B-Q2_g64.gguf", "size": 7, "lfs": {"sha256": "a"}},
            {"rfilename": "Ternary-Bonsai-27B-PQ2_0.gguf", "size": 6, "lfs": {"sha256": "b"}},
            {"rfilename": "Ternary-Bonsai-27B-F16.gguf", "size": 50},
            {"rfilename": "mmproj-PQ2_0.gguf", "size": 1},
            {"rfilename": "README.md", "size": 1}]}
        self.assertEqual([name for name, _, _ in live.gguf_files(info)],
                         ["Ternary-Bonsai-27B-PQ2_0.gguf", "Ternary-Bonsai-27B-Q2_g64.gguf"])
        untagged = {"siblings": [{"rfilename": "big.gguf", "size": 9}, {"rfilename": "model.gguf", "size": 3},
                                 {"rfilename": "model-F16.gguf", "size": 1},
                                 {"rfilename": "doctors/x.lora.gguf", "size": 1}]}
        self.assertEqual(live.gguf_files(untagged), [("big.gguf", 9, None), ("model.gguf", 3, None)])
        floats = {"siblings": [{"rfilename": "m-BF16.gguf", "size": 9}, {"rfilename": "m-f32.gguf", "size": 12}]}
        self.assertEqual(live.gguf_files(floats), [("m-BF16.gguf", 9, None)])

    def test_next_link(self):
        header = '<https://huggingface.co/api/models?cursor=abc>; rel="next"'
        self.assertEqual(live._next_link(header), "https://huggingface.co/api/models?cursor=abc")
        self.assertIsNone(live._next_link(""))


if __name__ == "__main__":
    unittest.main()
