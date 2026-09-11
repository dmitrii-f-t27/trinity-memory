"""Replay conformance/memory_tensorpack.json through the TensorPack implementation.

Golden containers must byte-match the native encoder and decode back through
the Python adapters; every rejected container must be refused by both readers.
When the native CLI is built, the same containers are checked through
tensor-inspect / tensor-unpack, and the nested TMEM of a tensor is shown to be
exactly the CLI's pack of its values (file -> memory -> restore is exact).
"""
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

from trinity_memory.tensorpack import (
    HEADER_BYTES, MAGIC, MAX_AXIS_BYTES, MAX_DIMENSION, MAX_METADATA_BYTES, MAX_NAME_BYTES, MAX_PAYLOAD_BYTES,
    MAX_RANK, MAX_TENSORS, MAX_TOTAL_TRITS, VERSION, Tensor, TensorPackError, decode_tensors, encode_tensors,
    inspect_tensorpack,
)
from trinity_memory.container import decode_file, encode_file

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = json.loads((ROOT / "conformance" / "memory_tensorpack.json").read_text(encoding="utf-8"))
CLI = ROOT / "build" / "t27" / "trinity-memory-t27"
DESCRIPTOR_KEYS = ["axes", "codec", "length", "name", "offset", "scale_axis", "scales", "shape"]


def to_tensor(item):
    return Tensor(item["name"], tuple(item["shape"]), tuple(item["values"]), item["codec"],
                  tuple(item["scales"]), item["scale_axis"], tuple(item["axes"]))


def by_kind(kind):
    return [vector for vector in DOCUMENT["vectors"] if vector["kind"] == kind]


def subset(test, expected, actual, path):
    if isinstance(expected, dict):
        test.assertIsInstance(actual, dict, path)
        for key, value in expected.items():
            test.assertIn(key, actual, path)
            subset(test, value, actual[key], f"{path}.{key}")
    elif isinstance(expected, list):
        test.assertEqual(len(expected), len(actual), path)
        for index, (item, other) in enumerate(zip(expected, actual)):
            subset(test, item, other, f"{path}[{index}]")
    else:
        test.assertEqual(expected, actual, path)


class SpecTensorPackConformance(unittest.TestCase):
    def test_document_and_limits(self):
        self.assertEqual(DOCUMENT["module"], "TrinityMemoryTensorPackSpec")
        self.assertTrue((ROOT / DOCUMENT["spec_path"]).is_file())
        limits = DOCUMENT["constants"]["limits"]
        self.assertEqual((limits["tensors"], limits["metadata_bytes"], limits["payload_bytes"], limits["total_trits"]),
                         (MAX_TENSORS, MAX_METADATA_BYTES, MAX_PAYLOAD_BYTES, MAX_TOTAL_TRITS))
        self.assertEqual((limits["rank"], limits["dimension"], limits["name_bytes"], limits["axis_bytes"]),
                         (MAX_RANK, MAX_DIMENSION, MAX_NAME_BYTES, MAX_AXIS_BYTES))
        header = DOCUMENT["constants"]["header"]
        self.assertEqual((header["magic"].encode(), header["version"], header["header_bytes"]), (MAGIC, VERSION, HEADER_BYTES))
        self.assertEqual(len({vector["id"] for vector in DOCUMENT["vectors"]}), len(DOCUMENT["vectors"]))

    def test_golden_packs_match_the_native_encoder_and_decode_back(self):
        for vector in by_kind("pack"):
            with self.subTest(vector=vector["id"]):
                tensors = [to_tensor(item) for item in vector["tensors"]]
                data = encode_tensors(tensors)
                self.assertEqual(data.hex(), vector["ttpk_hex"])
                self.assertEqual(decode_tensors(data), tensors)
                self.assertEqual(encode_tensors(decode_tensors(data)), data)
                subset(self, vector["inspect"], inspect_tensorpack(data), vector["id"])
                magic, version, flags, reserved, metadata_length, count, payload_length = struct.unpack("<4sBBHIIQ", data[:24])
                self.assertEqual((magic, version, flags, reserved), (MAGIC, VERSION, 0, 0))
                self.assertEqual(count, len(tensors))
                self.assertEqual(len(data), HEADER_BYTES + metadata_length + payload_length)
                metadata_crc, payload_crc = struct.unpack("<II", data[24:32])
                self.assertEqual(metadata_crc, zlib.crc32(data[:24] + data[32:32 + metadata_length]))
                self.assertEqual(payload_crc, zlib.crc32(data[32 + metadata_length:]))
                metadata = data[32:32 + metadata_length].decode("utf-8")
                self.assertEqual(metadata, vector["metadata_json"])
                document = json.loads(metadata)
                self.assertEqual(list(document), ["order", "tensors"])
                self.assertEqual(document["order"], "C")
                offset = 0
                for descriptor, tensor in zip(document["tensors"], tensors):
                    self.assertEqual(list(descriptor), DESCRIPTOR_KEYS)
                    self.assertEqual(descriptor["offset"], offset)
                    nested = data[32 + metadata_length + offset:32 + metadata_length + offset + descriptor["length"]]
                    self.assertEqual(nested, encode_file(tensor.values, tensor.codec))
                    self.assertEqual(decode_file(nested), list(tensor.values))
                    offset += descriptor["length"]
                self.assertEqual(offset, payload_length)
                # The metadata is exactly Python's sorted, whitespace-free JSON of the descriptors.
                self.assertEqual(metadata, json.dumps(document, ensure_ascii=False, allow_nan=False,
                                                      separators=(",", ":"), sort_keys=True))

    def test_rejected_packs_are_refused_by_both_readers(self):
        for vector in by_kind("invalid_pack"):
            data = bytes.fromhex(vector["ttpk_hex"])
            for reader in (decode_tensors, inspect_tensorpack):
                with self.subTest(vector=vector["id"], reader=reader.__name__), self.assertRaises(TensorPackError):
                    reader(data)

    @unittest.skipUnless(CLI.is_file() and os.access(CLI, os.X_OK), "native CLI not built")
    def test_native_cli_agrees_on_every_container(self):
        with tempfile.TemporaryDirectory(prefix="trinity-spec-ttpk-") as directory:
            work = Path(directory)
            for vector in by_kind("pack"):
                path = work / f"{vector['id']}.ttpk"
                path.write_bytes(bytes.fromhex(vector["ttpk_hex"]))
                inspected = subprocess.run([str(CLI), "tensor-inspect", str(path)], capture_output=True, text=True)
                self.assertEqual(inspected.returncode, 0, vector["id"] + inspected.stderr)
                subset(self, vector["inspect"], json.loads(inspected.stdout), vector["id"])
                unpacked = work / f"{vector['id']}.json"
                result = subprocess.run([str(CLI), "tensor-unpack", str(path), str(unpacked)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, vector["id"] + result.stderr)
                restored = json.loads(unpacked.read_text())
                self.assertEqual([to_tensor(item) for item in restored], [to_tensor(item) for item in vector["tensors"]])
                repacked = work / f"{vector['id']}.repacked.ttpk"
                result = subprocess.run([str(CLI), "tensor-pack", str(unpacked), str(repacked)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, vector["id"] + result.stderr)
                self.assertEqual(repacked.read_bytes().hex(), vector["ttpk_hex"])
            for vector in by_kind("invalid_pack"):
                path = work / f"{vector['id']}.ttpk"
                path.write_bytes(bytes.fromhex(vector["ttpk_hex"]))
                result = subprocess.run([str(CLI), "tensor-inspect", str(path)], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0, vector["id"])

    @unittest.skipUnless(CLI.is_file() and os.access(CLI, os.X_OK), "native CLI not built")
    def test_nested_tmem_equals_the_cli_pack_of_the_same_values(self):
        vector = next(v for v in by_kind("pack") if v["id"] == "example_tensorpack_json")
        tensor = vector["tensors"][0]
        data = bytes.fromhex(vector["ttpk_hex"])
        metadata_length = int.from_bytes(data[8:12], "little")
        nested = data[32 + metadata_length:]
        with tempfile.TemporaryDirectory(prefix="trinity-spec-ttpk-") as directory:
            work = Path(directory)
            values = work / "values.json"
            values.write_text(json.dumps(tensor["values"]))
            packed = work / "values.tmem"
            result = subprocess.run([str(CLI), "pack", str(values), str(packed), "--codec", tensor["codec"]], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(packed.read_bytes(), nested)
            exported_from_values = work / "values.mem"
            result = subprocess.run([str(CLI), "export-rtl", str(values), str(exported_from_values), "--codec", tensor["codec"]], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            restored = work / "restored.json"
            result = subprocess.run([str(CLI), "unpack", str(packed), str(restored)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(restored.read_text()), tensor["values"])
            exported_from_restored = work / "restored.mem"
            result = subprocess.run([str(CLI), "export-rtl", str(restored), str(exported_from_restored), "--codec", tensor["codec"]], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(exported_from_values.read_bytes(), exported_from_restored.read_bytes())

    def test_generator_output_is_committed(self):
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "generate-spec-vectors.py"), "--check"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
