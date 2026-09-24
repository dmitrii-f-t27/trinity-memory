"""Ternary Check Live (issues #48-#51) on synthetic GGUF headers and a local
HTTP server standing in for the Hub: no network."""
import hashlib
import http.server
import json
import os
import re
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from trinity_memory import live

ROOT = Path(__file__).resolve().parent.parent
REVISION = "0123456789abcdef0123456789abcdef01234567"


def _key(name, vtype, payload):
    raw = name.encode()
    return struct.pack("<Q", len(raw)) + raw + struct.pack("<I", vtype) + payload


def _string(value):
    raw = value.encode()
    return struct.pack("<Q", len(raw)) + raw


def _gguf(records, keys=(), arch="qwen35", alignment=32):
    """A GGUF v3 header: `records` are (name, dims, type, offset); keys are
    (name, vtype, payload bytes). No architecture key when arch is None."""
    keys = ([("general.architecture", 8, _string(arch))] if arch else []) + list(keys)
    out = bytearray(struct.pack("<IIQQ", 0x46554747, 3, len(records), len(keys)))
    for name, vtype, payload in keys:
        out += _key(name, vtype, payload)
    for name, dims, ggml_type, offset in records:
        out += _string(name) + struct.pack("<I", len(dims))
        out += b"".join(struct.pack("<Q", d) for d in dims) + struct.pack("<IQ", ggml_type, offset)
    data_start = (len(out) + alignment - 1) // alignment * alignment
    return bytes(out) + bytes(data_start - len(out))


def _file(tensors, block_bytes, keys=(), arch="qwen35"):
    """Every tensor is 1024 x 4 weights stored in blocks of `block_bytes` per
    `group` weights, back to back. Returns (header, file size)."""
    offset, records = 0, []
    for name, ggml_type, group in tensors:
        records.append((name, [1024, 4], ggml_type, offset))
        offset += 4 * (1024 // group) * block_bytes
        offset = (offset + 31) // 32 * 32
    header = _gguf(records, keys, arch)
    return header, len(header) + offset


def _split(count, names, arch="llama", declared=None):
    """The parts of a split model as gguf-split writes them: the keys in the
    first part, split.no, split.count and split.tensors.count in every part;
    one Q2_0 tensor of 1024 x 4 per part. Returns [(file name, header, size)]."""
    parts = []
    for no, name in enumerate(names):
        keys = [("split.no", 2, struct.pack("<H", no)), ("split.count", 2, struct.pack("<H", count)),
                ("split.tensors.count", 5, struct.pack("<i", len(names) if declared is None else declared))]
        header = _gguf([(name, [1024, 4], 42, 0)], keys, arch if no == 0 else None)
        parts.append((f"m-Q2_0-{no + 1:05d}-of-{count:05d}.gguf", header, len(header) + 1152))
    return parts


def _hadamard_keys(names, block=1024):
    array = struct.pack("<IQ", 8, len(names)) + b"".join(_string(n) for n in names)
    return [("prism.hadamard.version", 4, struct.pack("<I", 1)),
            ("prism.hadamard.block_size", 4, struct.pack("<I", block)),
            ("prism.hadamard.transform", 8, _string("normalized-sylvester-walsh-hadamard")),
            ("prism.hadamard.axis", 8, _string("input-last-dimension")),
            ("prism.hadamard.sign_mode", 8, _string("identity")),
            ("prism.hadamard.weight_names", 9, array)]


class CheckModelTest(unittest.TestCase):
    def test_legacy_q2_0_layout_is_refused_everywhere_and_diagnosed(self):
        # Type 42 is Q2_0 with 64-weight groups (18 bytes); the bytes follow the
        # 128-weight, 34-byte layout (PrismML-Eng/llama.cpp#167).
        header, size = _file([("blk.0.a.weight", 42, 128), ("blk.0.b.weight", 42, 128)], 34)
        record = live.check_header(header, size)
        self.assertEqual((record["verdict"], record["native"]), ("refused", "llama.cpp"))
        self.assertEqual(record["runtimes"]["llama.cpp"],
                         {"verdict": "refuses", "status": "offsets", "record": "blk.0.b.weight",
                          "expected": 1152, "found": 1088})
        self.assertEqual(record["runtimes"]["prismml"]["status"], "offsets")
        self.assertEqual(record["problems"], {"extent": {"count": 2, "examples": ["blk.0.a.weight", "blk.0.b.weight"],
                                                         "fits": {"PQ2_0": 2}}})
        self.assertEqual((record["layouts"], record["ggml_types"], record["tensors"]), ({}, [[42, 2]], 2))

    def test_fork_ids_run_only_in_the_fork(self):
        header, size = _file([("blk.0.ffn_down.weight", 142, 128), ("blk.0.ffn_up.weight", 142, 128)], 34)
        record = live.check_header(header, size)
        self.assertEqual((record["verdict"], record["native"], record["layouts"]), ("ok", "prismml", {"PQ2_0": 2}))
        self.assertEqual(record["runtimes"]["llama.cpp"],
                         {"verdict": "refuses", "status": "type", "record": "blk.0.ffn_down.weight"})
        self.assertEqual(record["runtimes"]["prismml"], {"verdict": "accepts"})
        self.assertEqual(record["hadamard"], {"present": False, "status": "ok"})

    def test_a_declared_rotation_is_ignored_outside_the_fork(self):
        header, size = _file([("blk.0.attn_q.weight", 1, 1)], 2, keys=_hadamard_keys(["blk.0.attn_q.weight"]))
        record = live.check_header(header, size)
        self.assertEqual(record["runtimes"]["prismml"], {"verdict": "accepts"})
        self.assertEqual(record["runtimes"]["llama.cpp"], {"verdict": "ignores_rotation"})
        self.assertEqual(record["hadamard"]["status"], "ok")
        self.assertEqual((record["verdict"], record["ternary_tensors"]), ("no_ternary_layout", 0))

    def test_invalid_rotation_is_refused_by_the_fork(self):
        keys = _hadamard_keys(["blk.0.attn_q.weight"], block=12)
        header, size = _file([("blk.0.attn_q.weight", 1, 1)], 2, keys=keys)
        record = live.check_header(header, size)
        self.assertEqual(record["runtimes"]["prismml"], {"verdict": "refuses", "status": "hadamard_block", "entry": 0})
        self.assertEqual(record["hadamard"]["status"], "hadamard_block")

    def test_row_not_whole_blocks(self):
        header = _gguf([("blk.0.ffn_down.weight", [1320, 5120], 35, 0)], arch="qwen2")
        record = live.check_header(header, len(header) + 5120 * 330)
        self.assertEqual(record["runtimes"]["llama.cpp"],
                         {"verdict": "refuses", "status": "row", "record": "blk.0.ffn_down.weight"})
        self.assertEqual(record["problems"]["row"]["count"], 1)

    def test_endian_magic_short_and_truncated(self):
        record = live.check_header(bytes.fromhex("474755460000000300000000000000000000000000000000"), 10 ** 6)
        self.assertEqual((record["verdict"], record["runtimes"]["llama.cpp"]["status"]), ("other_runtime", "endian"))
        record = live.check_header(bytes(64), 10 ** 6)
        self.assertEqual(record["runtimes"]["bitnet.cpp"]["status"], "magic")
        header, size = _file([("a", 42, 64)], 18)
        # A header cut short of the file's end asks for more bytes: no verdict;
        # a file that ends inside its header is refused by the reader.
        self.assertEqual(live.check_header(header[:30], size)["verdict"], "undecided")
        self.assertEqual(live.check_header(header[:30], size)["runtimes"]["llama.cpp"],
                         {"verdict": "no_verdict", "status": "truncated"})
        short = live.check_header(header[:30], 30)
        self.assertEqual((short["verdict"], short["runtimes"]["llama.cpp"]["status"]), ("refused", "short"))

    def test_unknown_architecture_or_type_is_other_software(self):
        header, size = _file([("a", 42, 64)], 18, arch="no-such-arch")
        record = live.check_header(header, size)
        self.assertEqual(record["runtimes"]["llama.cpp"], {"verdict": "refuses", "status": "arch"})
        self.assertEqual((record["verdict"], record["native"]), ("other_runtime", "other"))
        header = _gguf([("a", [256, 4], 66, 0)])
        record = live.check_header(header, len(header) + 4096)
        self.assertEqual((record["verdict"], record["runtimes"]["prismml"]["status"]), ("other_runtime", "type"))
        header, size = _file([("a", 42, 64)], 18, arch="clip")
        record = live.check_header(header, size)
        self.assertEqual((record["verdict"], record["runtimes"]["llama.cpp"]["status"]), ("refused", "arch"))

    def test_status_tokens_match_the_registry(self):
        owners = (ROOT / "specs" / "formats" / "OWNERS.md").read_text()
        rows = {int(status): token for token, status in re.findall(r"^\| `([a-z_]+)` \| (-\d+) \|", owners, re.M)}
        for status, token in live.TOKENS.items():
            self.assertEqual(rows.get(status), token, status)


class SplitModelTest(unittest.TestCase):
    def test_one_verdict_for_the_parts_together(self):
        parts = _split(3, ["blk.0.w", "blk.1.w", "blk.2.w"])
        record = live.check_model(parts)
        self.assertEqual((record["verdict"], record["native"], record["tensors"], record["ternary_tensors"]),
                         ("ok", "llama.cpp", 3, 3))
        self.assertEqual(record["runtimes"]["llama.cpp"], {"verdict": "accepts"})
        self.assertEqual(record["layouts"], {"Q2_0": 3})

    def test_a_missing_part_or_a_duplicate_is_refused(self):
        parts = _split(3, ["blk.0.w", "blk.1.w", "blk.2.w"])
        record = live.check_model(parts[:2])
        self.assertEqual(record["runtimes"]["llama.cpp"],
                         {"verdict": "refuses", "status": "split", "file": 2, "expected": 3, "found": 0})
        self.assertEqual(record["verdict"], "refused")
        duplicate = _split(2, ["blk.0.w", "blk.0.w"])
        self.assertEqual(live.check_model(duplicate)["runtimes"]["llama.cpp"],
                         {"verdict": "refuses", "status": "name", "file": duplicate[1][0], "record": "blk.0.w"})
        wrong_count = _split(2, ["a", "b"], declared=5)
        self.assertEqual(live.check_model(wrong_count)["runtimes"]["llama.cpp"]["status"], "split")

    def test_a_later_part_alone_is_not_other_software(self):
        later = _split(2, ["a", "b"])[1:]
        record = live.check_model(later)
        self.assertEqual((record["verdict"], record["native"]), ("refused", "llama.cpp"))
        self.assertEqual(record["runtimes"]["llama.cpp"]["status"], "split")

    def test_models_group_the_parts_of_split_files(self):
        info = {"siblings": [
            {"rfilename": "Q2_0/m-Q2_0-00002-of-00002.gguf", "size": 5, "lfs": {"sha256": "b" * 64}},
            {"rfilename": "Q2_0/m-Q2_0-00001-of-00002.gguf", "size": 7, "lfs": {"sha256": "a" * 64}},
            {"rfilename": "m-TQ1_0.gguf", "size": 3, "lfs": {"sha256": "c" * 64}},
            {"rfilename": "m-Q4_K_M-00001-of-00003.gguf", "size": 3}]}
        models, files, total = live.gguf_models(info)
        self.assertEqual((files, total), (4, 3))
        self.assertEqual([[name for name, _, _ in model] for model in models],
                         [["Q2_0/m-Q2_0-00001-of-00002.gguf", "Q2_0/m-Q2_0-00002-of-00002.gguf"], ["m-TQ1_0.gguf"]])


class RuntimeTablesTest(unittest.TestCase):
    def test_generated_module_is_current_and_pins_agree(self):
        done = subprocess.run([sys.executable, str(ROOT / "tools" / "generate-runtime-tables.py"), "--check"],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        lock = json.loads((ROOT / "specs" / "formats" / "upstream.lock.json").read_text())["upstreams"]
        pins = {"llama_cpp": lock["llama.cpp"], "prismml": lock["prismml"], "bitnet_cpp": lock["bitnet.cpp-llama.cpp"]}
        for name, pin in pins.items():
            spec = json.loads((ROOT / "specs" / "runtimes" / f"{name}.json").read_text())
            self.assertEqual((spec["repo"], spec["commit"]), (pin["repo"], pin["commit"]), name)
            self.assertNotIn("(unknown)", spec["archs"])
            self.assertNotIn("gptj", spec["archs"])


class FakeHub:
    """Serves one file from memory and counts range reads."""

    def __init__(self, data: bytes):
        self.data, self.reads = data, []

    def read(self, repo, revision, filename, begin, end, size):
        self.reads.append((begin, end))
        return self.data[begin:end]


class ReadHeaderTest(unittest.TestCase):
    def test_grows_until_complete_caches_and_revalidates(self):
        header, size = _file([(f"blk.{i}.w", 42, 64) for i in range(40)], 18)
        data = header + bytes(size - len(header))
        sha = hashlib.sha256(data).hexdigest()
        old = live.FIRST_READ
        live.FIRST_READ = 64
        try:
            with tempfile.TemporaryDirectory() as tmp:
                hub = FakeHub(data)
                self.assertEqual(live.read_header(hub, "a/b", REVISION, "m-Q2_0.gguf", size, sha, Path(tmp)), header)
                self.assertEqual(hub.reads[0], (0, 64))
                for (_, e0), (b1, _) in zip(hub.reads, hub.reads[1:]):
                    self.assertEqual(e0, b1)
                self.assertLessEqual(hub.reads[-1][1], 2 * len(header))
                again = FakeHub(data)
                self.assertEqual(live.read_header(again, "a/b", REVISION, "m-Q2_0.gguf", size, sha, Path(tmp)), header)
                self.assertEqual(again.reads, [])
                path = live.header_path(Path(tmp), "a/b", REVISION, "m-Q2_0.gguf", sha)
                path.write_bytes(header[:50])                          # a truncated cache entry is read again
                third = FakeHub(data)
                self.assertEqual(live.read_header(third, "a/b", REVISION, "m-Q2_0.gguf", size, sha, Path(tmp)), header)
                self.assertTrue(third.reads)
        finally:
            live.FIRST_READ = old

    def test_a_refusal_before_the_records_keeps_what_was_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = FakeHub(bytes(4096))
            got = live.read_header(hub, "a/b", REVISION, "zeros.gguf", 4096, None, Path(tmp))
            self.assertEqual(live.check_header(got, 4096)["runtimes"]["llama.cpp"]["status"], "magic")
            self.assertTrue(live.header_path(Path(tmp), "a/b", REVISION, "zeros.gguf").is_file())

    def test_a_file_that_ends_inside_its_header_is_a_verdict(self):
        header, _ = _file([(f"blk.{i}.w", 42, 64) for i in range(8)], 18)
        hub = FakeHub(header[:100])
        got = live.read_header(hub, "a/b", REVISION, "cut.gguf", 100, None, None)
        self.assertEqual(live.check_header(got, 100)["runtimes"]["llama.cpp"], {"verdict": "refuses", "status": "short"})
        self.assertEqual(live.read_header(FakeHub(b""), "a/b", REVISION, "empty.gguf", 0, None, None), b"")

    def test_cache_keys(self):
        cache = Path("/c")
        self.assertNotEqual(live.header_path(cache, "q/m", REVISION, "a/b.gguf"),
                            live.header_path(cache, "q/m", REVISION, "a--b.gguf"))
        # names that differ only in case must not share an entry on a case-insensitive disk
        self.assertNotEqual(str(live.header_path(cache, "q/m", REVISION, "M.gguf")).lower(),
                            str(live.header_path(cache, "q/m", REVISION, "m.gguf")).lower())
        self.assertEqual(live.header_path(cache, "q/m", REVISION, "a.gguf", "ab" * 32),
                         live.header_path(cache, "other/repo", None, "b.gguf", "ab" * 32))
        for revision in ("../../x", None, "main"):
            with self.assertRaises(live.LiveError):
                live.header_path(cache, "q/m", revision, "a.gguf")
        with self.assertRaises(live.LiveError):
            live.header_path(cache, "../x", REVISION, "a.gguf")
        with self.assertRaises(live.LiveError):
            live.header_path(cache, "q/m", REVISION, "a.gguf", "../x")


class DiscoveryTest(unittest.TestCase):
    def test_names_files_threshold_and_failures(self):
        class Hub:
            def search(self, query):
                if query == "broken":
                    raise live.LiveError("HTTP 500")
                return [{"id": "prism-ml/Ternary-Bonsai-2-27B-gguf", "downloads": 5},
                        {"id": "someone/Llama-3-8B-GGUF", "downloads": 10 ** 6},
                        {"id": "x/Bonsai-2-27B-PQ2_0-GGUF", "downloads": 500},
                        {"id": "y/model-q2_0", "downloads": 200},
                        {"id": "z/fooq2_0bar", "downloads": 900},
                        {"id": "w/IQ2_0-thing", "downloads": 900},
                        {"id": "v/TriLM_3.9B-GGUF", "downloads": 300}]

            def gguf_models(self, pages):
                return [{"id": "unsloth/Big-GGUF", "downloads": 9000,
                         "siblings": [{"rfilename": "Big-TQ1_0.GGUF"}, {"rfilename": "Big-Q4_K_M.gguf"}]},
                        {"id": "other/Plain-GGUF", "downloads": 9000, "siblings": [{"rfilename": "Plain-IQ2_XXS.gguf"}]},
                        {"id": "odd/NoSiblings", "downloads": 9000}]
        found, seen = live.discover(Hub(), ("q", "broken"), min_downloads=100)
        self.assertEqual(found, {"x/Bonsai-2-27B-PQ2_0-GGUF": 500, "y/model-q2_0": 200, "v/TriLM_3.9B-GGUF": 300,
                                 "unsloth/Big-GGUF": 9000})
        self.assertEqual((seen["hits"], seen["gguf_repositories"]), ({"q": 7}, 3))
        self.assertEqual(seen["errors"], [{"query": "broken", "error": "HTTP 500"}])

    def test_file_selection(self):
        info = {"siblings": [
            {"rfilename": "Ternary-Bonsai-27B-Q2_g64.gguf", "size": 7, "lfs": {"sha256": "a" * 64}},
            {"rfilename": "Ternary-Bonsai-27B-PQ2_0.gguf", "size": 6, "lfs": {"sha256": "b" * 64}},
            {"rfilename": "Ternary-Bonsai-27B-F16.gguf", "size": 50},
            {"rfilename": "mmproj-PQ2_0.gguf", "size": 1},
            {"rfilename": "Flora-7B.gguf", "size": 1},
            {"rfilename": "README.md", "size": 1}]}
        models, files, total = live.gguf_models(info)
        self.assertEqual([model[0][0] for model in models], ["Ternary-Bonsai-27B-PQ2_0.gguf", "Ternary-Bonsai-27B-Q2_g64.gguf"])
        self.assertEqual((files, total), (4, 4))
        untagged = {"siblings": [{"rfilename": "big.gguf", "size": 9}, {"rfilename": "model.gguf", "size": 3},
                                 {"rfilename": "model-F16.gguf", "size": 1}, {"rfilename": "model-fp32.gguf", "size": 1},
                                 {"rfilename": "doctors/x.lora.gguf", "size": 1},
                                 {"rfilename": "x-lora_merged.gguf", "size": 2}]}
        self.assertEqual([model[0] for model in live.gguf_models(untagged)[0]],
                         [("big.gguf", 9, None), ("model.gguf", 3, None), ("x-lora_merged.gguf", 2, None)])
        floats = {"siblings": [{"rfilename": "m-BF16.gguf", "size": 9}, {"rfilename": "m-f32.gguf", "size": 12}]}
        self.assertEqual(live.gguf_models(floats)[0], [[("m-BF16.gguf", 9, None)]])

    def test_next_link(self):
        hub = live.Hub(endpoint="https://huggingface.co")
        base = "https://huggingface.co/api/models"
        self.assertEqual(hub._next('<https://huggingface.co/api/models?cursor=a>; rel="next"', base), base + "?cursor=a")
        self.assertEqual(hub._next('</api/models?cursor=b>; rel=next', base), base + "?cursor=b")
        self.assertIsNone(hub._next('<file:///etc/passwd>; rel="next"', base))
        self.assertIsNone(hub._next("", base))


BLOB = bytes(range(256)) * 16


class Server:
    """Serves BLOB at /r/m/resolve/<rev>/f.gguf with byte ranges, or a fault."""

    def __init__(self):
        self.mode, self.requests, self.faulted = "ok", [], False
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.requests.append((self.path, self.headers.get("Range"), self.headers.get("Authorization")))
                if outer.mode == "rate-limit-once" and not outer.faulted:
                    outer.faulted = True
                    self.send_response(429)
                    self.send_header("Retry-After", "0")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path.startswith("/api/"):
                    body = json.dumps({"sha": REVISION}).encode()
                    self.send_response(200)
                else:
                    first, last = (int(x) for x in self.headers["Range"].split("=")[1].split("-"))
                    body = BLOB[first: last + 1]
                    if outer.mode == "ignore-range":
                        body = BLOB
                        self.send_response(200)
                    else:
                        self.send_response(206)
                        shown = (first + 1, last + 1) if outer.mode == "wrong-range" else (first, last)
                        total = len(BLOB) + 1 if outer.mode == "wrong-total" else len(BLOB)
                        if outer.mode != "no-range":
                            self.send_header("Content-Range", f"bytes {shown[0]}-{shown[1]}/{total}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    if outer.mode == "cut-once" and not outer.faulted:
                        outer.faulted = True
                        self.wfile.write(body[: len(body) // 2])
                        self.wfile.flush()
                        self.connection.close()
                        return
                    self.wfile.write(body)
                except ConnectionError:
                    pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class HubTest(unittest.TestCase):
    def setUp(self):
        self.server = Server()
        self.hub = live.Hub(endpoint=self.server.url, interval=0, attempts=3, offline=False)

    def tearDown(self):
        self.server.close()

    def test_range_reads_and_faults(self):
        self.assertEqual(self.hub.read("r/m", REVISION, "f.gguf", 10, 20, len(BLOB)), BLOB[10:20])
        self.assertTrue(all(auth is None for _, _, auth in self.server.requests))
        self.server.mode = "ignore-range"
        with self.assertRaises(live.LiveError):
            self.hub.read("r/m", REVISION, "f.gguf", 10, 20, len(BLOB))
        self.assertEqual(self.hub.read("r/m", REVISION, "f.gguf", 0, len(BLOB), len(BLOB)), BLOB)  # the whole file
        for mode in ("wrong-range", "wrong-total", "no-range"):
            self.server.mode = mode
            with self.assertRaises(live.LiveError):
                self.hub.read("r/m", REVISION, "f.gguf", 10, 20, len(BLOB))
        self.server.mode = "rate-limit-once"
        self.assertEqual(self.hub.read("r/m", REVISION, "f.gguf", 0, 4, len(BLOB)), BLOB[:4])
        self.assertEqual(self.hub.api("models/r/m")["sha"], REVISION)

    def test_a_body_cut_short_is_read_again(self):
        self.server.mode = "cut-once"
        before = self.hub.requests
        self.assertEqual(self.hub.read("r/m", REVISION, "f.gguf", 0, 1000, len(BLOB)), BLOB[:1000])
        self.assertEqual(self.hub.requests - before, 2)

    def test_offline_and_foreign_urls(self):
        hub = live.Hub(endpoint=self.server.url, offline=True)
        with self.assertRaises(live.LiveError):
            hub.api("models/r/m")
        with self.assertRaises(live.LiveError):
            self.hub._fetch("https://example.com/api/x")


class ScanTest(unittest.TestCase):
    def test_scan_dedupe_errors_log_and_recheck(self):
        header, size = _file([("blk.0.a.weight", 42, 128), ("blk.0.b.weight", 42, 128)], 34)
        data = header + bytes(size - len(header))
        split = _split(2, ["blk.0.w", "blk.1.w"])
        files = {name: header + bytes(part_size - len(header)) for name, header, part_size in split}
        files["m-Q2_0.gguf"] = data
        sha = "5" * 64

        class Hub:
            def model(self, repo):
                if repo == "bad/repo":
                    raise live.LiveError("HTTP 500")
                if repo == "gated/repo":
                    return {"sha": REVISION, "gated": "manual"}
                siblings = [{"rfilename": "m-Q2_0.gguf", "size": size, "lfs": {"sha256": sha}}]
                if repo == "s/split":
                    siblings = [{"rfilename": name, "size": part_size} for name, _, part_size in split]
                return {"sha": REVISION, "siblings": siblings}

            def read(self, repo, revision, filename, begin, end, size):
                return files[filename][begin:end]

        with tempfile.TemporaryDirectory() as tmp:
            saved, logged = [], []
            entries = live.scan(Hub(), {"a/one": 10, "b/two": 5, "bad/repo": 3, "gated/repo": 2, "s/split": 1},
                                Path(tmp), log=logged.append, save=lambda done: saved.append(len(done)))
            self.assertEqual(saved, [1, 2, 3, 4, 5])
            first, second, bad, gated, split_entry = entries
            self.assertEqual(first["models"][0]["verdict"], "refused")
            self.assertEqual(first["models"][0]["header_sha256"], [hashlib.sha256(header).hexdigest()])
            self.assertEqual(second["models"][0]["same_as"], {"repo": "a/one", "file": "m-Q2_0.gguf"})
            self.assertEqual(second["models"][0]["runtimes"], first["models"][0]["runtimes"])
            self.assertEqual((bad["state"], gated["state"]), ("unavailable", "gated"))
            model = split_entry["models"][0]
            self.assertEqual((model["verdict"], model["split"], len(model["files"])), ("ok", 2, 2))
            # the log carries counts, never a repository or a verdict
            self.assertEqual(logged, ["[5/5] repositories, 3 models"])
            report = live.report(entries, {"queries": []}, "2026-09-24T00:00:00+00:00")
            self.assertEqual(report["summary"]["verdicts"], {"ok": 1, "refused": 2})
            self.assertEqual((report["summary"]["repositories_with_refused_models"], report["summary"]["split_models"]),
                             (2, 1))
            again = live.recheck(json.loads(json.dumps(report)), Path(tmp))
            self.assertEqual(again[0]["models"][0], entries[0]["models"][0])
            self.assertEqual(again[1]["models"][0]["same_as"], {"repo": "a/one", "file": "m-Q2_0.gguf"})
            self.assertEqual(again[4]["models"][0]["verdict"], "ok")
            # a cached header that no longer matches its recorded hash is not used
            live.header_path(Path(tmp), "a/one", REVISION, "m-Q2_0.gguf", sha).write_bytes(header[:40])
            again = live.recheck(json.loads(json.dumps(report)), Path(tmp))
            self.assertEqual(again[0]["models"][0]["recheck"], "kept")


class ReplayTest(unittest.TestCase):
    def _binary(self, folder: Path, name: str, body: str) -> Path:
        path = folder / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return path

    def test_crashes_bytes_and_timeouts_are_recorded(self):
        header, size = _file([("blk.0.a.weight", 42, 64)], 18)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            sha = "7" * 64
            path = live.header_path(tmp / "headers", "r/m", REVISION, "m.gguf", sha)
            path.parent.mkdir(parents=True)
            path.write_bytes(header)
            entries = [{"repo": "r/m", "revision": REVISION, "models": [
                {"file": "m.gguf", "verdict": "ok", "files": [{"file": "m.gguf", "size": size, "lfs_sha256": sha}]}]}]
            binaries = {"llama.cpp": self._binary(tmp, "accepts", "echo ACCEPTED tensors=1"),
                        "prismml": self._binary(tmp, "noise", "printf 'gguf_init: \\377\\376 bad\\n' >&2; exit 2"),
                        "bitnet.cpp": self._binary(tmp, "crash", "kill -ABRT $$")}
            counts = live.replay(entries, tmp / "headers", binaries)
            self.assertEqual((counts["files"], counts["agree"], counts["disagree"], counts["crashes"]), (1, 1, 2, 1))
            upstream = entries[0]["models"][0]["files"][0]["upstream"]
            self.assertEqual(upstream["llama.cpp"], {"accepted": True, "agrees": True})
            self.assertIn("�", upstream["prismml"]["message"])
            self.assertEqual((upstream["bitnet.cpp"]["accepted"], upstream["bitnet.cpp"]["signal"]), (False, 6))
            self.assertEqual(counts["runtimes"], ["bitnet.cpp", "llama.cpp", "prismml"])


if __name__ == "__main__":
    unittest.main()
