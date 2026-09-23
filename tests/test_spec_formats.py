"""Replay conformance/formats_*.json through the Python bindings of t27/formats.t27.

The vectors are generated from the upstream loops restated in specs/formats/
(tools/generate-spec-vectors.py). tests/native_spec_formats.c compares them with
the specs and the generated C, tests/spec_formats_wasm_replay.mjs with the WASM
build; this test checks that trinity_memory.formats, which only moves bytes,
returns the same values, statuses and flags.
"""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

from trinity_memory import formats as f

ROOT = Path(__file__).resolve().parents[1]
FAMILIES = ("llama_cpp", "prismml", "bitnet_cpp", "hf_bitnet", "mlx", "onnx")
KIND = {"F16": f.F16, "BF16": f.BF16, "F32": f.F32}


def load(family):
    return json.loads((ROOT / "conformance" / f"formats_{family}.json").read_text(encoding="utf-8"))


def values_of(hex_text):
    return [b - 256 if b > 127 else b for b in bytes.fromhex(hex_text)]


def kind(vector):
    """The reader a vector exercises: its kind, or `reader` for kind "reject"."""
    return vector["reader"] if vector["kind"] == "reject" else vector["kind"]


def status_of(call):
    try:
        return call(), 0
    except f.FormatError as error:
        return None, error.status


class SpecFormatsConformance(unittest.TestCase):
    def test_documents_and_status_classes(self):
        for family in FAMILIES:
            document = load(family)
            self.assertEqual(document["spec_path"], f"specs/formats/{family}.t27")
            self.assertTrue((ROOT / document["spec_path"]).is_file())
            constants = document["constants"]
            self.assertEqual(constants["errors"], {token: status for status, token in f.TOKENS.items()})
            self.assertEqual(constants["flags"], {token: slot for slot, token in enumerate(f.FLAGS)})
            ids = constants["format_ids"]
            self.assertEqual((ids["TQ1_0"], ids["PTQ1_0"], ids["I2_S"], ids["LINEAR2"], ids["ONNX2"]),
                             (f.TQ1_0, f.PTQ1_0, f.I2_S, f.LINEAR2, f.ONNX2))
            ids_seen = [vector["id"] for vector in document["vectors"]]
            self.assertEqual(len(ids_seen), len(set(ids_seen)))
            lock = json.loads((ROOT / "specs" / "formats" / "upstream.lock.json").read_text(encoding="utf-8"))
            for key, upstream in document["upstream"].items():
                self.assertEqual(upstream["commit"], lock["upstreams"][key]["commit"])
                self.assertEqual(upstream["files"], sorted(lock["upstreams"][key]["files"]))

    def test_negative_vectors_carry_classes_and_upstream_views(self):
        cite = re.compile(r"^[A-Za-z0-9_./-]+:[0-9]+(-[0-9]+)?$")
        rejects = 0
        for family in FAMILIES:
            document = load(family)
            for v in document["vectors"]:
                self.assertEqual(v["kind"] == "reject", "error_class" in v, v["id"])
                negative = any(key in v for key in ("error_class", "flag_class", "silent_class"))
                self.assertEqual(negative, "silent_output" in v, v["id"])
                if "error_class" in v:
                    rejects += 1
                    status = document["constants"]["errors"][v["error_class"]]
                    self.assertIn(status, [v["expect"].get(key) for key in
                                           ("status", "check", "scale_status", "affine_status")], v["id"])
                for entry in v.get("silent_output", []):
                    self.assertIn(entry["behaviour"], ("decodes", "rejects", "not_applicable", "unknown"), v["id"])
                    self.assertEqual(entry["behaviour"] == "unknown", "UNKNOWN" in entry["cite"], v["id"])
                    self.assertTrue(entry["note"], v["id"])
                    for line in entry["cite"]:
                        if line != "UNKNOWN":
                            self.assertRegex(line, cite)
                            self.assertIn(line.split(":")[0], document["upstream"][entry["upstream"]]["files"], v["id"])
        self.assertGreater(rejects, 0)

    def test_block_vectors(self):
        for family in ("llama_cpp", "prismml"):
            ids = load(family)["constants"]["format_ids"]
            for v in load(family)["vectors"]:
                if kind(v) == "block":
                    data, fmt = bytes.fromhex(v["data_hex"]), ids[v["format"]]
                    result, status = status_of(lambda: f.decode_blocks(fmt, data, v["count"]))
                    if status:
                        self.assertEqual(status, v["expect"]["status"], v["id"])
                        continue
                    values, words, outside = result
                    self.assertEqual(outside, v["expect"]["status"], v["id"])
                    self.assertEqual(list(values[:v["count"]]), values_of(v["expect"]["values_hex"]), v["id"])
                    self.assertEqual(words, v["expect"]["scale_words"], v["id"])
                    self.assertEqual(f.block_flags(fmt, data, v["count"]), v["expect"]["flags"], v["id"])
                    if v["encode"]:
                        self.assertEqual(f.encode_blocks(fmt, list(values[:v["count"]]), words), data, v["id"])
                elif kind(v) == "block_encode":
                    _, status = status_of(lambda: f.encode_blocks(ids[v["format"]], values_of(v["values_hex"]),
                                                                  v["scale_words"]))
                    self.assertEqual(status, v["expect"]["status"], v["id"])

    def test_gguf_vectors(self):
        for v in load("llama_cpp")["vectors"]:
            if kind(v) != "gguf":
                continue
            status, info = f.gguf_find(bytes.fromhex(v["gguf_hex"]), v["tensor"])
            self.assertEqual(status, v["expect"]["find"], v["id"])
            self.assertEqual((info.tensor_type, info.prism, info.bitnet, info.offset, info.next_offset, info.has_next,
                              info.prev_end, info.has_prev, info.data_start),
                             (v["expect"]["ggml_type"], v["expect"]["prism"], v["expect"]["bitnet"],
                              v["expect"]["offset"], v["expect"]["next_offset"], v["expect"]["has_next"],
                              v["expect"]["prev_end"], v["expect"]["has_prev"], v["expect"]["data_start"]), v["id"])
            result, status = status_of(lambda: f.gguf_check(info, v["file_size"]))
            self.assertEqual(result if status == 0 else status, v["expect"]["check"], v["id"])

    def test_safetensors_vectors(self):
        seen = 0
        for v in load("hf_bitnet")["vectors"]:
            if kind(v) != "safetensors":
                continue
            seen += 1
            status, info, dtype = f.safetensors_find(bytes.fromhex(v["safetensors_hex"]), v["tensor"])
            e = v["expect"]
            self.assertEqual((status, info.begin, info.end, info.has_next, info.next_begin, info.has_prev, info.prev_end),
                             (e["find"], e["begin"], e["end"], e["has_next"], e["next_begin"], e["has_prev"],
                              e["prev_end"]), v["id"])
            if status != 0:
                self.assertEqual(e["check"], status, v["id"])
                continue
            self.assertEqual((dtype, info.shape, info.dtype_bits), (e["dtype"], e["shape"], e["dtype_bits"]), v["id"])
            result, status = status_of(lambda: f.safetensors_check(info, v["file_size"]))
            self.assertEqual(result if status == 0 else status, e["check"], v["id"])
        self.assertGreater(seen, 0)

    def test_i2s_vectors(self):
        for v in load("bitnet_cpp")["vectors"]:
            if kind(v) == "i2s":
                data = bytes.fromhex(v["data_hex"])
                result, status = status_of(lambda: f.decode_i2s(data, v["count"]))
                if status:
                    self.assertEqual(status, v["expect"]["status"], v["id"])
                    continue
                values, scale, outside = result
                self.assertEqual((outside, scale), (v["expect"]["status"], v["expect"]["scale_word"]), v["id"])
                self.assertEqual(list(values[:v["count"]]), values_of(v["expect"]["values_hex"]), v["id"])
                self.assertEqual(f.i2s_flags(data, v["count"]), v["expect"]["flags"], v["id"])
                if v.get("encode"):
                    self.assertEqual(f.encode_i2s(list(values[:v["count"]]), scale), data, v["id"])
            elif kind(v) == "i2s_encode":
                _, status = status_of(lambda: f.encode_i2s(values_of(v["values_hex"]), v["scale_word"]))
                self.assertEqual(status, v["expect"]["status"], v["id"])

    def test_hf_vectors(self):
        for v in load("hf_bitnet")["vectors"]:
            if kind(v) == "hf_packed":
                data = bytes.fromhex(v["data_hex"])
                result, status = status_of(lambda: f.decode_hf_packed(data, v["rows"], v["cols"]))
                if status:
                    self.assertEqual(status, v["expect"]["status"], v["id"])
                    continue
                values, outside = result
                _, scale_status = status_of(lambda: f.scales_check([v["scale_word"]], KIND[v["scale_kind"]]))
                self.assertEqual((outside, scale_status), (v["expect"]["status"], v["expect"]["scale_status"]), v["id"])
                n = v["rows"] * v["cols"]
                self.assertEqual(list(values[:n]), values_of(v["expect"]["values_hex"]), v["id"])
                if v.get("encode"):
                    self.assertEqual(f.encode_hf_packed(list(values[:n]), v["rows"], v["cols"]), data, v["id"])
            elif kind(v) == "hf_encode":
                _, status = status_of(lambda: f.encode_hf_packed(values_of(v["values_hex"]), v["rows"], v["cols"]))
                self.assertEqual(status, v["expect"]["status"], v["id"])

    def test_mlx_vectors(self):
        for v in load("mlx")["vectors"]:
            if kind(v) == "mlx":
                data = bytes.fromhex(v["data_hex"])
                result, status = status_of(lambda: f.decode_mlx2(data, v["rows"], v["cols"], v["group"]))
                if status:
                    self.assertEqual(status, v["expect"]["status"], v["id"])
                    continue
                values, outside = result
                self.assertEqual(outside, v["expect"]["status"], v["id"])
                flags, affine = status_of(lambda: f.affine_check(v["scale_words"], v["bias_words"], KIND[v["scale_kind"]]))
                self.assertEqual(affine, v["expect"]["affine_status"], v["id"])
                if affine == 0:
                    flags["outside_ternary"] = outside
                    self.assertEqual(flags, v["expect"]["flags"], v["id"])
                n = v["rows"] * v["cols"]
                self.assertEqual(list(values[:n]), values_of(v["expect"]["values_hex"]), v["id"])
                if v.get("encode"):
                    self.assertEqual(f.encode_mlx2(list(values[:n]), v["rows"], v["cols"], v["group"]), data, v["id"])
            elif kind(v) == "mlx_encode":
                _, status = status_of(lambda: f.encode_mlx2(values_of(v["values_hex"]), v["rows"], v["cols"], v["group"]))
                self.assertEqual(status, v["expect"]["status"], v["id"])

    def test_onnx_vectors(self):
        for v in load("onnx")["vectors"]:
            zp = bytes.fromhex(v.get("zero_points_hex", ""))
            if kind(v) == "onnx":
                data = bytes.fromhex(v["data_hex"])
                result, status = status_of(lambda: f.decode_onnx2(data, v["n"], v["k"], v["block_size"], zp))
                if status:
                    self.assertEqual(status, v["expect"]["status"], v["id"])
                    continue
                values, outside = result
                flags, scale_status = status_of(lambda: f.scales_check(v["scale_words"], KIND[v["scale_kind"]]))
                self.assertEqual((outside, scale_status), (v["expect"]["status"], v["expect"]["scale_status"]), v["id"])
                padding = f.onnx2_padding_nonzero(data, v["n"], v["k"], v["block_size"])
                self.assertEqual(padding, v["expect"]["flags"]["padding_nonzero"], v["id"])
                n = v["n"] * v["k"]
                self.assertEqual(list(values[:n]), values_of(v["expect"]["values_hex"]), v["id"])
                if v.get("encode"):
                    self.assertEqual(f.encode_onnx2(list(values[:n]), v["n"], v["k"], v["block_size"], zp), data, v["id"])
            elif kind(v) == "onnx_encode":
                _, status = status_of(lambda: f.encode_onnx2(values_of(v["values_hex"]), v["n"], v["k"],
                                                             v["block_size"], zp))
                self.assertEqual(status, v["expect"]["status"], v["id"])

    def test_generator_output_is_committed(self):
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "generate-spec-vectors.py"), "--check"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
