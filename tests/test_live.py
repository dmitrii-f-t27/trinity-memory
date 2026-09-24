"""Ternary Check Live (issues #48-#51) on synthetic GGUF headers and a local
HTTP server standing in for the Hub: no network."""
import http.server
import json
import re
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
    (name, vtype, payload bytes)."""
    keys = [("general.architecture", 8, _string(arch))] + list(keys)
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


def _hadamard_keys(names, block=1024):
    array = struct.pack("<IQ", 8, len(names)) + b"".join(_string(n) for n in names)
    return [("prism.hadamard.version", 4, struct.pack("<I", 1)),
            ("prism.hadamard.block_size", 4, struct.pack("<I", block)),
            ("prism.hadamard.transform", 8, _string("normalized-sylvester-walsh-hadamard")),
            ("prism.hadamard.axis", 8, _string("input-last-dimension")),
            ("prism.hadamard.sign_mode", 8, _string("identity")),
            ("prism.hadamard.weight_names", 9, array)]


class CheckHeaderTest(unittest.TestCase):
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
        self.assertEqual(record["problems"]["extent"], {"count": 1, "examples": ["blk.0.a.weight"], "fits": {"PQ2_0": 1}})
        self.assertEqual(record["problems"]["bounds"]["fits"], {"PQ2_0": 1})
        self.assertEqual(record["layouts"], {})

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

    def test_big_endian_zeros_and_truncation(self):
        record = live.check_header(bytes.fromhex("474755460000000300000000000000000000000000000000"), 10 ** 6)
        self.assertEqual((record["verdict"], record["runtimes"]["llama.cpp"]["status"]), ("refused", "endian"))
        record = live.check_header(bytes(64), 10 ** 6)
        self.assertEqual(record["runtimes"]["bitnet.cpp"]["status"], "magic")
        header, size = _file([("a", 42, 64)], 18)
        self.assertEqual(live.check_header(header[:30], size)["verdict"], "undecided")

    def test_unknown_architecture(self):
        header, size = _file([("a", 42, 64)], 18, arch="no-such-arch")
        record = live.check_header(header, size)
        self.assertEqual(record["runtimes"]["llama.cpp"], {"verdict": "refuses", "status": "arch"})

    def test_status_tokens_match_the_registry(self):
        owners = (ROOT / "specs" / "formats" / "OWNERS.md").read_text()
        rows = {int(status): token for token, status in re.findall(r"^\| `([a-z_]+)` \| (-\d+) \|", owners, re.M)}
        for status, token in live.TOKENS.items():
            self.assertEqual(rows.get(status), token, status)


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
        old = live.FIRST_READ
        live.FIRST_READ = 64
        try:
            with tempfile.TemporaryDirectory() as tmp:
                hub = FakeHub(data)
                self.assertEqual(live.read_header(hub, "a/b", REVISION, "m-Q2_0.gguf", size, Path(tmp)), header)
                self.assertEqual(hub.reads[0], (0, 64))
                for (_, e0), (b1, _) in zip(hub.reads, hub.reads[1:]):
                    self.assertEqual(e0, b1)
                self.assertLessEqual(hub.reads[-1][1], 2 * len(header))
                again = FakeHub(data)
                self.assertEqual(live.read_header(again, "a/b", REVISION, "m-Q2_0.gguf", size, Path(tmp)), header)
                self.assertEqual(again.reads, [])
                path = live.header_path(Path(tmp), "a/b", REVISION, "m-Q2_0.gguf")
                path.write_bytes(header[:50])                          # a truncated cache entry is read again
                third = FakeHub(data)
                self.assertEqual(live.read_header(third, "a/b", REVISION, "m-Q2_0.gguf", size, Path(tmp)), header)
                self.assertTrue(third.reads)
        finally:
            live.FIRST_READ = old

    def test_a_refusal_before_the_records_keeps_what_was_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = FakeHub(bytes(4096))
            got = live.read_header(hub, "a/b", REVISION, "zeros.gguf", 4096, Path(tmp))
            self.assertEqual(live.check_header(got, 4096)["runtimes"]["llama.cpp"]["status"], "magic")
            self.assertTrue(live.header_path(Path(tmp), "a/b", REVISION, "zeros.gguf").is_file())

    def test_cache_keys(self):
        cache = Path("/c")
        self.assertNotEqual(live.header_path(cache, "q/m", REVISION, "a/b.gguf"),
                            live.header_path(cache, "q/m", REVISION, "a--b.gguf"))
        for revision in ("../../x", None, "main"):
            with self.assertRaises(live.LiveError):
                live.header_path(cache, "q/m", revision, "a.gguf")
        with self.assertRaises(live.LiveError):
            live.header_path(cache, "../x", REVISION, "a.gguf")


class DiscoveryTest(unittest.TestCase):
    def test_names_files_and_threshold(self):
        class Hub:
            def search(self, query):
                return [{"id": "prism-ml/Ternary-Bonsai-2-27B-gguf", "downloads": 5},
                        {"id": "someone/Llama-3-8B-GGUF", "downloads": 10 ** 6},
                        {"id": "x/Bonsai-2-27B-PQ2_0-GGUF", "downloads": 500},
                        {"id": "y/model-q2_0", "downloads": 200},
                        {"id": "z/fooq2_0bar", "downloads": 900},
                        {"id": "w/IQ2_0-thing", "downloads": 900}]

            def gguf_models(self, pages):
                return [{"id": "unsloth/Big-GGUF", "downloads": 9000,
                         "siblings": [{"rfilename": "Big-TQ1_0.gguf"}, {"rfilename": "Big-Q4_K_M.gguf"}]},
                        {"id": "other/Plain-GGUF", "downloads": 9000, "siblings": [{"rfilename": "Plain-IQ2_XXS.gguf"}]}]
        found = live.discover(Hub(), ("q",), min_downloads=100)
        self.assertEqual(found, {"x/Bonsai-2-27B-PQ2_0-GGUF": 500, "y/model-q2_0": 200, "unsloth/Big-GGUF": 9000})

    def test_file_selection(self):
        info = {"siblings": [
            {"rfilename": "Ternary-Bonsai-27B-Q2_g64.gguf", "size": 7, "lfs": {"sha256": "a"}},
            {"rfilename": "Ternary-Bonsai-27B-PQ2_0.gguf", "size": 6, "lfs": {"sha256": "b"}},
            {"rfilename": "Ternary-Bonsai-27B-F16.gguf", "size": 50},
            {"rfilename": "mmproj-PQ2_0.gguf", "size": 1},
            {"rfilename": "Flora-7B.gguf", "size": 1},
            {"rfilename": "README.md", "size": 1}]}
        files, total = live.gguf_files(info)
        self.assertEqual([name for name, _, _ in files], ["Ternary-Bonsai-27B-PQ2_0.gguf", "Ternary-Bonsai-27B-Q2_g64.gguf"])
        self.assertEqual(total, 4)
        untagged = {"siblings": [{"rfilename": "big.gguf", "size": 9}, {"rfilename": "model.gguf", "size": 3},
                                 {"rfilename": "model-F16.gguf", "size": 1},
                                 {"rfilename": "doctors/x.lora.gguf", "size": 1}]}
        self.assertEqual(live.gguf_files(untagged)[0], [("big.gguf", 9, None), ("model.gguf", 3, None)])
        floats = {"siblings": [{"rfilename": "m-BF16.gguf", "size": 9}, {"rfilename": "m-f32.gguf", "size": 12}]}
        self.assertEqual(live.gguf_files(floats)[0], [("m-BF16.gguf", 9, None)])

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
        self.mode, self.requests, self.limited = "ok", [], False
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.requests.append((self.path, self.headers.get("Range"), self.headers.get("Authorization")))
                if outer.mode == "rate-limit-once" and not outer.limited:
                    outer.limited = True
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
                        self.send_header("Content-Range", f"bytes {shown[0]}-{shown[1]}/{len(BLOB)}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
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
        self.server.mode = "wrong-range"
        with self.assertRaises(live.LiveError):
            self.hub.read("r/m", REVISION, "f.gguf", 10, 20, len(BLOB))
        self.server.mode = "rate-limit-once"
        self.assertEqual(self.hub.read("r/m", REVISION, "f.gguf", 0, 4, len(BLOB)), BLOB[:4])
        self.assertEqual(self.hub.api("models/r/m")["sha"], REVISION)

    def test_offline_and_foreign_urls(self):
        hub = live.Hub(endpoint=self.server.url, offline=True)
        with self.assertRaises(live.LiveError):
            hub.api("models/r/m")
        with self.assertRaises(live.LiveError):
            self.hub._open("https://example.com/api/x")


class ScanTest(unittest.TestCase):
    def test_scan_dedupe_errors_and_recheck(self):
        header, size = _file([("blk.0.a.weight", 42, 128), ("blk.0.b.weight", 42, 128)], 34)
        data = header + bytes(size - len(header))

        class Hub:
            def model(self, repo):
                if repo == "bad/repo":
                    raise live.LiveError("HTTP 500")
                if repo == "gated/repo":
                    return {"sha": REVISION, "gated": "manual"}
                return {"sha": REVISION, "siblings": [{"rfilename": "m-Q2_0.gguf", "size": size, "lfs": {"sha256": "s"}}]}

            def read(self, repo, revision, filename, begin, end, size):
                return data[begin:end]

        with tempfile.TemporaryDirectory() as tmp:
            saved = []
            entries = live.scan(Hub(), {"a/one": 10, "b/two": 5, "bad/repo": 3, "gated/repo": 2}, Path(tmp),
                                save=lambda done: saved.append(len(done)))
            self.assertEqual(saved, [1, 2, 3, 4])
            first, second, bad, gated = entries
            self.assertEqual(first["files"][0]["verdict"], "refused")
            self.assertEqual(second["files"][0]["same_as"], {"repo": "a/one", "file": "m-Q2_0.gguf"})
            self.assertEqual(second["files"][0]["runtimes"], first["files"][0]["runtimes"])
            self.assertEqual((bad["state"], gated["state"]), ("unavailable", "gated"))
            report = live.report(entries, {"queries": []}, "2026-09-24T00:00:00+00:00")
            self.assertEqual(report["summary"]["verdicts"], {"refused": 2})
            self.assertEqual(report["summary"]["repositories_with_refused_files"], 2)
            again = live.recheck(json.loads(json.dumps(report)), Path(tmp))
            self.assertEqual(again[0]["files"][0], entries[0]["files"][0])
            self.assertEqual(again[1]["files"][0]["same_as"], {"repo": "a/one", "file": "m-Q2_0.gguf"})


if __name__ == "__main__":
    unittest.main()
