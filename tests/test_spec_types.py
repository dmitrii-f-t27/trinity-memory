"""Replay conformance/memory_types.json through the public Python adapters.

The vectors are generated from specs/memory/types.t27 without the native
implementation (tools/generate-spec-vectors.py). This test checks that the
executable stack reproduces them and that the committed constants match the
adapter tables, with independent arithmetic where a formula is stated.
"""
import itertools
import json
import struct
import unittest
import zlib
from pathlib import Path

from trinity_memory import CODECS, CodecError, decode_file, encode_file, inspect_file, pack, unpack
from trinity_memory.codecs import payload_size
from trinity_memory.container import HEADER_BYTES, MAGIC, VERSION

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = json.loads((ROOT / "conformance" / "memory_types.json").read_text(encoding="utf-8"))
VECTORS = {vector["id"]: vector for vector in DOCUMENT["vectors"]}
LANE = {0: 0, 1: 1, -1: 2}


def by_kind(kind):
    return [vector for vector in DOCUMENT["vectors"] if vector["kind"] == kind]


class SpecTypesConformance(unittest.TestCase):
    def test_document_identity(self):
        self.assertEqual(DOCUMENT["module"], "TrinityMemoryTypes")
        self.assertEqual(DOCUMENT["spec_path"], "specs/memory/types.t27")
        self.assertTrue((ROOT / DOCUMENT["spec_path"]).is_file())
        self.assertGreaterEqual(len(DOCUMENT["vectors"]), 20)
        self.assertEqual(len(VECTORS), len(DOCUMENT["vectors"]), "vector ids must be unique")

    def test_codec_constants_match_adapter_tables(self):
        constants = DOCUMENT["constants"]["codecs"]
        self.assertEqual(set(constants), set(CODECS))
        for name, codec in CODECS.items():
            entry = constants[name]
            self.assertEqual(entry["id"], codec.id, name)
            self.assertEqual(entry["group_trits"], codec.group_size, name)
            self.assertEqual(entry["group_bits"], codec.group_bits, name)
            self.assertEqual(entry["max_nonzero"], codec.max_nonzero, name)
            if codec.max_nonzero is None:
                self.assertEqual(entry["valid_words"], 3 ** codec.group_size, name)
                self.assertLessEqual(3 ** codec.group_size, 1 << codec.group_bits, name)
            else:
                states = sum(len(list(itertools.combinations(range(codec.group_size), i))) * 2 ** i
                             for i in range(codec.max_nonzero + 1))
                self.assertEqual(entry["valid_words"], states, name)
        self.assertEqual(DOCUMENT["constants"]["payload_bytes_65536"],
                         {name: payload_size(65536, name) for name in CODECS})
        self.assertEqual(DOCUMENT["constants"]["dense5"]["zero_group_code"], pack([0] * 5)[0])

    def test_header_constants_match_container_module(self):
        tmem = DOCUMENT["constants"]["tmem"]
        self.assertEqual(tmem["magic"].encode(), MAGIC)
        self.assertEqual(tmem["version"], VERSION)
        self.assertEqual(tmem["header_bytes"], HEADER_BYTES)
        self.assertEqual(struct.calcsize("<4sBBBBQI") + 4, tmem["header_bytes"])
        crc = DOCUMENT["constants"]["crc32"]
        self.assertEqual(int(crc["check_value"], 16), zlib.crc32(b"123456789"))
        self.assertEqual(int(crc["poly"], 16), 0xEDB88320)

    def test_group_vectors_roundtrip_and_match_independent_words(self):
        for vector in by_kind("group"):
            codec, trits = vector["codec"], vector["trits"]
            payload = pack(trits, codec)
            self.assertEqual(payload.hex(), vector["payload_hex"], vector["id"])
            self.assertEqual(unpack(payload, len(trits), codec), trits, vector["id"])
            self.assertEqual(len(payload), payload_size(len(trits), codec), vector["id"])
            group = CODECS[codec].group_size
            if codec == "baseline2" and len(trits) == group:
                self.assertEqual(payload[0], sum(LANE[t] << (2 * i) for i, t in enumerate(trits)), vector["id"])
            elif codec.startswith("dense") and len(trits) == group:
                word = sum((t + 1) * 3 ** i for i, t in enumerate(trits))
                self.assertEqual(int.from_bytes(payload, "little"), word, vector["id"])
            elif codec == "sparse41":
                nonzero = [(i, t) for i, t in enumerate(trits) if t]
                expected = 0 if not nonzero else 1 + 2 * nonzero[0][0] + (nonzero[0][1] == 1)
                self.assertEqual(payload[0], expected, vector["id"])

    def test_container_vectors_frame_and_checksum(self):
        for vector in by_kind("container"):
            codec, trits = vector["codec"], vector["trits"]
            data = encode_file(trits, codec)
            self.assertEqual(data.hex(), vector["tmem_hex"], vector["id"])
            magic, version, codec_id, r0, r1, count, payload_length = struct.unpack("<4sBBBBQI", data[:20])
            self.assertEqual((magic, version, r0, r1), (MAGIC, VERSION, 0, 0), vector["id"])
            self.assertEqual(codec_id, CODECS[codec].id, vector["id"])
            self.assertEqual(count, len(trits), vector["id"])
            self.assertEqual(payload_length, len(data) - HEADER_BYTES, vector["id"])
            self.assertEqual(struct.unpack("<I", data[20:24])[0], zlib.crc32(data[:20] + data[24:]), vector["id"])
            self.assertEqual(data[HEADER_BYTES:], pack(trits, codec), vector["id"])
            self.assertEqual(decode_file(data), trits, vector["id"])
            self.assertEqual(inspect_file(data)["codec"], codec, vector["id"])

    def test_invalid_words_are_rejected(self):
        for vector in by_kind("invalid_word"):
            payload = bytes.fromhex(vector["payload_hex"])
            with self.assertRaises(CodecError, msg=vector["id"]):
                unpack(payload, vector["count"], vector["codec"])

    def test_invalid_sparsity_is_rejected_without_pruning(self):
        for vector in by_kind("invalid_sparsity"):
            with self.assertRaises(CodecError, msg=vector["id"]):
                pack(vector["trits"], vector["codec"])

    def test_generator_output_is_committed(self):
        import subprocess
        import sys
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "generate-spec-vectors.py"), "--check"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
