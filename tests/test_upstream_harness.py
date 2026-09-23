"""Offline checks of the llama.cpp issue 15193 harness plumbing.

The harness itself (tests/upstream/run-llamacpp-15193.sh) needs the network
and the generated t27 headers; these tests only check the lock file, that
the extractor refuses sources that differ from it, and that the driver states
each variant's kernel path and checks build provenance and Rosetta AVX2.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "tests" / "upstream" / "llama.cpp.lock.json"
SPEC = importlib.util.spec_from_file_location("extract_llamacpp_tq", ROOT / "tests" / "upstream" / "extract_llamacpp_tq.py")
extract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(extract)


class LockFile(unittest.TestCase):
    def setUp(self):
        self.lock = json.loads(LOCK.read_text())

    def test_single_pin(self):
        self.assertEqual(self.lock["repo"], "ggml-org/llama.cpp")
        self.assertEqual(self.lock["commit"], "e6ab7c1a41054a888ada952eab4c886444c2f5ad")
        self.assertEqual(self.lock["directory"], "llama.cpp-" + self.lock["commit"][:8])

    def test_files_and_ranges(self):
        for path, meta in self.lock["files"].items():
            self.assertRegex(meta["sha256"], r"^[0-9a-f]{64}$", path)
            self.assertRegex(meta["git_blob_sha"], r"^[0-9a-f]{40}$", path)
        for item in self.lock["extracts"]:
            self.assertIn(item["file"], self.lock["files"])
            self.assertIn(item["guard"], extract.GUARDS)
            self.assertLessEqual(item["first"], item["last"])
            self.assertTrue(item["first_line"] and item["last_line"])

    def test_no_llamacpp_source_is_committed(self):
        for path in (p for p in (ROOT / "tests" / "upstream").iterdir() if p.is_file()):
            self.assertNotRegex(path.read_text(errors="replace"), re.compile(r"^void ggml_vec_dot_tq1_0_q8_K\(", re.M), path.name)


class Driver(unittest.TestCase):
    def setUp(self):
        self.script = (ROOT / "tests" / "upstream" / "run-llamacpp-15193.sh").read_text()

    def test_every_named_kernel_variant_states_its_expectation(self):
        variants = re.findall(r"^\s*(?:variants=\")?((?:arm64|x86_64)-(?:dotprod|int16|avx2|generic))\|([^|]*)\|", self.script, re.M)
        self.assertEqual(sorted({name for name, _ in variants}),
                         ["arm64-dotprod", "arm64-int16", "x86_64-avx2", "x86_64-generic"])
        expected = {"arm64-dotprod": "-DHARNESS_EXPECT_DOTPROD=1", "arm64-int16": "-DHARNESS_EXPECT_DOTPROD=0",
                    "x86_64-avx2": "-DHARNESS_EXPECT_AVX2=1", "x86_64-generic": "-DHARNESS_EXPECT_AVX2=0"}
        for name, flags in variants:
            self.assertIn(expected[name], flags.split(), name)
        header = (ROOT / "tests" / "upstream" / "llamacpp_harness.h").read_text()
        self.assertIn("#if defined(HARNESS_EXPECT_DOTPROD)", header)
        self.assertIn("#if defined(HARNESS_EXPECT_AVX2)", header)

    def test_checks_build_provenance_and_rosetta_avx2(self):
        self.assertIn("build/t27/SHA256SUMS", self.script)
        self.assertIn("tests/upstream/avx2_probe.c", self.script)
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertNotIn("macos-14", workflow)


class Extractor(unittest.TestCase):
    def _fixture(self, directory: Path, body: str):
        source = directory / "src" / "a.c"
        source.parent.mkdir(parents=True)
        source.write_text(body)
        return {"repo": "example/upstream", "commit": "0" * 40, "license": "test", "directory": str(directory),
                "files": {"src/a.c": {"sha256": hashlib.sha256(body.encode()).hexdigest(), "git_blob_sha": "0" * 40}},
                "extracts": [{"file": "src/a.c", "first": 2, "last": 4, "guard": "arm",
                              "first_line": "int f(void) {", "last_line": "}"}]}

    def test_copies_verified_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            lock = self._fixture(tmp, "// header\nint f(void) {\n    return 1;\n}\n")
            (tmp / "lock.json").write_text(json.dumps(lock))
            extract.extract(tmp / "lock.json", tmp / "out.c")
            text = (tmp / "out.c").read_text()
            self.assertIn("#if defined(__ARM_NEON)\nint f(void) {\n    return 1;\n}\n#endif\n", text)

    def test_rejects_shifted_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            lock = self._fixture(tmp, "// header\n// moved\nint f(void) {\n    return 1;\n}\n")
            (tmp / "lock.json").write_text(json.dumps(lock))
            with self.assertRaises(SystemExit) as caught:
                extract.extract(tmp / "lock.json", tmp / "out.c")
            self.assertIn("expected a line starting with", str(caught.exception))
            self.assertFalse((tmp / "out.c").exists())

    def test_rejects_changed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            lock = self._fixture(tmp, "// header\nint f(void) {\n    return 1;\n}\n")
            (tmp / "src" / "a.c").write_text("// header\nint f(void) {\n    return 2;\n}\n")
            (tmp / "lock.json").write_text(json.dumps(lock))
            with self.assertRaises(SystemExit) as caught:
                extract.extract(tmp / "lock.json", tmp / "out.c")
            self.assertIn("differs from the lock", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
