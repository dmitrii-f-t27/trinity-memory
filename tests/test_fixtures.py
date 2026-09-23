"""Fixture manifest, cache and range fetching against a local HTTP server (no network)."""
import contextlib
import hashlib
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from trinity_memory import fixtures as fx

ROOT = Path(__file__).resolve().parent.parent
REPO, REVISION, NAME = "test-org/test-model", "0123456789abcdef0123456789abcdef01234567", "model.bin"
SIZE = (2 << 20) + 4096
BLOB = bytes((i * 7 + (i >> 11)) & 255 for i in range(SIZE))


def _load_tool(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch_fixtures = _load_tool("fetch_fixtures", "tools/fetch-fixtures.py")


def _range(kind, begin, end, **fields):
    record = {"file": NAME, "kind": kind, **fields, "begin": begin, "end": end,
              "sha256": hashlib.sha256(BLOB[begin:end]).hexdigest()}
    return record


def _manifest_data():
    return {
        "schema": fx.SCHEMA,
        "models": [{
            "repo": REPO, "revision": REVISION, "license": "Apache-2.0",
            "files": [{"name": NAME, "size": SIZE}],
            "ranges": [
                _range("prefix", 0, 1 << 20),
                _range("prefix", 1 << 20, 2 << 20),
                _range("tensor", 2 << 20, (2 << 20) + 1024, tensor="t", dtype="U8", shape=[1024]),
                _range("trailer", (2 << 20) + 1024, (2 << 20) + 1056, tensor="t", dtype="U8", shape=[1024],
                       tensor_offset=1024),
            ],
        }],
    }


class Server:
    """Serves BLOB at /<repo>/resolve/<revision>/<name> with byte ranges.
    `mode` selects a fault: ignore-range, short, rate-limit-once, not-found."""

    def __init__(self):
        self.mode, self.requests, self.limited = "ok", [], False
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.requests.append((self.path, self.headers.get("Range"), self.headers.get("Authorization")))
                if self.path != f"/{REPO}/resolve/{REVISION}/{NAME}" or outer.mode == "not-found":
                    self.send_error(404)
                    return
                if outer.mode == "rate-limit-once" and not outer.limited:
                    outer.limited = True
                    self.send_response(429)
                    self.send_header("RateLimit", '"resolvers";r=0;t=0')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                first, last = (int(x) for x in self.headers["Range"].split("=")[1].split("-"))
                body = BLOB[first: last + 1]
                if outer.mode == "ignore-range":
                    body = BLOB
                    self.send_response(200)
                else:
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {first}-{last}/{SIZE}")
                if outer.mode == "short":
                    body = body[:-1]
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(0.05,), daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class FixtureTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.manifest_path = self.root / "manifest.json"
        self.manifest_path.write_text(json.dumps(_manifest_data()))
        self.cache = self.root / "cache"
        self.directory = self.cache / REPO.replace("/", "--") / REVISION / NAME
        patches = [mock.patch.dict(os.environ, {"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"}),
                   mock.patch.object(fx.time, "sleep", lambda seconds: None),
                   mock.patch.object(fx, "LOCK", self.root / "manifest.lock.json")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop(fx.OFFLINE_ENV, None)

    def tool(self, *args, endpoint=None):
        argv = ["--manifest", str(self.manifest_path), "--cache", str(self.cache), *args]
        if endpoint:
            argv += ["--endpoint", endpoint]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = fetch_fixtures.main(argv)
        return status, out.getvalue(), err.getvalue()

    def remote(self, endpoint="http://127.0.0.1:9", offline=None):
        return fx.Remote(fx.Manifest(self.manifest_path).file(REPO, NAME), cache=self.cache,
                         endpoint=endpoint, offline=offline)


class FetchToolTest(FixtureTestCase):
    def test_fetch_then_offline_verify(self):
        with Server() as server:
            status, out, err = self.tool(endpoint=server.endpoint)
            self.assertEqual(status, 0, err)
            self.assertIn("fetched 4", out)
            self.assertTrue(all(auth is None for _, _, auth in server.requests))
            self.assertEqual(sorted(r for _, r, _ in server.requests),
                             sorted(["bytes=0-1048575", "bytes=1048576-2097151",
                                     f"bytes={2 << 20}-{(2 << 20) + 1023}",
                                     f"bytes={(2 << 20) + 1024}-{(2 << 20) + 1055}"]))
        self.assertEqual((self.directory / "prefix.bin").read_bytes(), BLOB[: 2 << 20])
        self.assertEqual((self.directory / f"{2 << 20}-{(2 << 20) + 1024}.bin").read_bytes(),
                         BLOB[2 << 20: (2 << 20) + 1024])
        status, out, err = self.tool("--offline")
        self.assertEqual(status, 0, err)
        self.assertIn("verified in cache 4", out)

    def test_offline_reports_missing_and_corrupt_ranges(self):
        status, _, err = self.tool("--offline")
        self.assertEqual(status, 1)
        self.assertIn("missing", err)
        with Server() as server:
            self.assertEqual(self.tool(endpoint=server.endpoint)[0], 0)
        path = self.directory / f"{2 << 20}-{(2 << 20) + 1024}.bin"
        data = path.read_bytes()
        path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        status, _, err = self.tool("--offline")
        self.assertEqual(status, 1)
        self.assertIn("differs from the manifest", err)
        with Server() as server:
            status, out, err = self.tool(endpoint=server.endpoint)
        self.assertEqual(status, 0, err)
        self.assertIn("fetched 1", out)
        self.assertEqual(path.read_bytes(), BLOB[2 << 20: (2 << 20) + 1024])

    def test_sha256_mismatch_fails_and_writes_nothing(self):
        data = _manifest_data()
        data["models"][0]["ranges"][2]["sha256"] = "0" * 64
        self.manifest_path.write_text(json.dumps(data))
        with Server() as server:
            status, _, err = self.tool(endpoint=server.endpoint)
        self.assertEqual(status, 1)
        self.assertIn("fetched sha256", err)
        self.assertFalse((self.directory / f"{2 << 20}-{(2 << 20) + 1024}.bin").exists())

    def test_server_faults_fail(self):
        for mode, message in (("ignore-range", "ignored the range request"), ("short", "expected"),
                              ("not-found", "HTTP 404")):
            with self.subTest(mode=mode), Server() as server:
                server.mode = mode
                status, _, err = self.tool(endpoint=server.endpoint)
                self.assertEqual(status, 1)
                self.assertIn(message, err)

    def test_rate_limit_is_retried(self):
        with Server() as server:
            server.mode = "rate-limit-once"
            status, _, err = self.tool("--jobs", "1", endpoint=server.endpoint)
        self.assertEqual(status, 0, err)
        self.assertTrue(server.limited)

    def test_unknown_cache_file_fails_unless_not_strict(self):
        with Server() as server:
            self.assertEqual(self.tool(endpoint=server.endpoint)[0], 0)
        (self.directory / "5-9.bin").write_bytes(b"xxxx")
        status, _, err = self.tool("--offline")
        self.assertEqual(status, 1)
        self.assertIn("5-9.bin: not listed", err)
        status, _, err = self.tool("--offline", "--no-strict")
        self.assertEqual(status, 0, err)

    def test_record_adds_one_range_in_order_and_writes_the_lock(self):
        begin, end = (2 << 20) + 2048, (2 << 20) + 2080
        with Server() as server:
            status, out, err = self.tool("--record", REPO, NAME, str(begin), str(end), "--kind", "tensor",
                                         "--tensor", "u", "--dtype", "U8", "--shape", "32",
                                         endpoint=server.endpoint)
            self.assertEqual(status, 0, err)
            self.assertEqual(self.tool("--record", REPO, NAME, str(begin), str(end), "--kind", "tensor",
                                       endpoint=server.endpoint)[0], 1)
        manifest = fx.Manifest(self.manifest_path)
        self.assertEqual([r["begin"] for r in manifest.data["models"][0]["ranges"]][-1], begin)
        self.assertEqual(manifest.file(REPO, NAME).ranges[begin, end]["sha256"],
                         hashlib.sha256(BLOB[begin:end]).hexdigest())
        self.assertEqual(self.manifest_path.read_text(), manifest.dumps())
        self.assertEqual(json.loads(fx.LOCK.read_text()), manifest.lock())
        self.assertEqual((self.directory / f"{begin}-{end}.bin").read_bytes(), BLOB[begin:end])


class RemoteTest(FixtureTestCase):
    def test_unknown_range_is_an_error_and_the_manifest_is_unchanged(self):
        before = self.manifest_path.read_text()
        with Server() as server:
            remote = self.remote(server.endpoint)
            with self.assertRaisesRegex(fx.FixtureError, "--record"):
                remote.read(2 << 20, (2 << 20) + 512)
            with self.assertRaisesRegex(fx.FixtureError, "not in fixtures/manifest.json"):
                remote.read_within(SIZE - 8, SIZE)
            self.assertEqual(server.requests, [])
        self.assertEqual(self.manifest_path.read_text(), before)

    def test_reads_fetch_verify_and_slice(self):
        with Server() as server:
            remote = self.remote(server.endpoint)
            self.assertEqual(remote.read(2 << 20, (2 << 20) + 1024), BLOB[2 << 20: (2 << 20) + 1024])
            self.assertEqual(remote.read_within((2 << 20) + 1030, (2 << 20) + 1040),
                             BLOB[(2 << 20) + 1030: (2 << 20) + 1040])
            self.assertEqual(remote.read_within(8, 100), BLOB[8:100])
            self.assertEqual(len(remote.prefix(1)), 1 << 20)
            self.assertEqual(remote.prefix((1 << 20) + 1), BLOB[: 2 << 20])
            with self.assertRaisesRegex(fx.FixtureError, "header bytes needed"):
                remote.prefix((2 << 20) + 1)
            fetched = len(server.requests)
        offline = self.remote(offline=True)
        self.assertEqual(offline.read((2 << 20) + 1024, (2 << 20) + 1056), BLOB[(2 << 20) + 1024: (2 << 20) + 1056])
        self.assertEqual(fetched, 4)

    def test_cached_ranges_and_prefix_are_rehashed(self):
        with Server() as server:
            remote = self.remote(server.endpoint)
            remote.prefix(2 << 20)
            remote.read(2 << 20, (2 << 20) + 1024)
        prefix = self.directory / "prefix.bin"
        data = bytearray(prefix.read_bytes())
        data[(1 << 20) + 5] ^= 1
        prefix.write_bytes(bytes(data))
        offline = self.remote(offline=True)
        self.assertEqual(offline.prefix(10), BLOB[: 1 << 20])
        with self.assertRaisesRegex(fx.FixtureError, "differs from the manifest"):
            offline.prefix((1 << 20) + 10)
        path = self.directory / f"{2 << 20}-{(2 << 20) + 1024}.bin"
        path.write_bytes(bytes(1024))
        with self.assertRaisesRegex(fx.FixtureError, "differs from the manifest"):
            offline.read(2 << 20, (2 << 20) + 1024)

    def test_offline_never_fetches(self):
        with mock.patch.dict(os.environ, {fx.OFFLINE_ENV: "1"}):
            remote = self.remote()
            with self.assertRaisesRegex(fx.FixtureError, "fetching is off"):
                remote.read(2 << 20, (2 << 20) + 1024)

    def test_manifest_validation(self):
        cases = [
            (lambda d: d.update(schema="other"), "schema"),
            (lambda d: d["models"][0].update(revision="main"), "40-hex"),
            (lambda d: d["models"][0]["ranges"][2].update(end=SIZE + 1), "outside the file"),
            (lambda d: d["models"][0]["ranges"][2].update(kind="blob"), "kind"),
            (lambda d: d["models"][0]["ranges"][2].pop("tensor"), "must name its tensor"),
            (lambda d: d["models"][0]["ranges"][2].update(extra=1), "unknown fields"),
            (lambda d: d["models"][0]["ranges"].append(dict(d["models"][0]["ranges"][2])), "listed twice"),
            (lambda d: d["models"][0]["ranges"].pop(0), "contiguous"),
        ]
        for change, message in cases:
            with self.subTest(message=message):
                data = _manifest_data()
                change(data)
                self.manifest_path.write_text(json.dumps(data))
                with self.assertRaisesRegex(fx.FixtureError, message):
                    fx.Manifest(self.manifest_path)


class CommittedManifestTest(unittest.TestCase):
    def setUp(self):
        self.manifest = fx.Manifest(fx.MANIFEST)

    def test_text_is_canonical_and_the_lock_is_generated_from_it(self):
        self.assertEqual(fx.MANIFEST.read_text(), self.manifest.dumps())
        self.assertEqual(fx.LOCK.read_text(), self.manifest.lock_text(),
                         "fixtures/manifest.lock.json is stale: python3 tools/fetch-fixtures.py --write-lock")

    def test_models_are_pinned_and_licensed(self):
        consumers = set(self.manifest.data["consumers"])
        for model in self.manifest.data["models"]:
            self.assertIn(model["license"], ("MIT", "Apache-2.0"))
            for record in model["ranges"]:
                self.assertTrue(record["used_by"])
                self.assertLessEqual(set(record["used_by"]), consumers)
                if record["kind"] != "prefix":
                    self.assertTrue("dtype" in record or "ggml_type" in record, record)
                    self.assertIn("shape", record)

    def test_consumers_read_revisions_from_the_manifest(self):
        from trinity_memory import ternary_check
        audit = _load_tool("bitnet_audit", "tools/bitnet_audit.py")
        for remote in [*ternary_check.BITNET.values(), *ternary_check.BONSAI.values()]:
            entry = self.manifest.file(remote.repo, remote.filename)
            self.assertEqual((remote.revision, remote.size), (entry.revision, entry.size))
        for key in audit.FILES:
            source = audit.Source(key, offline=True)
            self.assertEqual(source.revision, self.manifest.file(source.repo, source.filename).revision)

    def test_every_i2s_tensor_has_its_trailer_range(self):
        gguf = self.manifest.file("microsoft/bitnet-b1.58-2B-4T-gguf", "ggml-model-i2_s.gguf")
        trailers = [r for r in gguf.ranges.values() if r["kind"] == "trailer"]
        self.assertEqual(len(trailers), 210)
        self.assertTrue(all(r["end"] - r["begin"] == 32 for r in trailers))
        scales = [r for r in self.manifest.file("microsoft/bitnet-b1.58-2B-4T", "model.safetensors").ranges.values()
                  if r["kind"] == "scale"]
        self.assertEqual(len(scales), 210)


if __name__ == "__main__":
    unittest.main()
