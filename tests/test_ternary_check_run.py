"""The Ternary Check CLI contract (ternary-check/CONTRACT.md) end to end.

`trinity-memory ternary-check run` feeds every vector of
conformance/formats_*.json to a decoder process. The reference decoder
(`python3 -m trinity_memory ternary-check`, the t27 readers and writers) must
pass all of them; deliberately wrong decoders must be caught with the outcome
the contract names. Comparison and verdicts are the t27 functions of
t27/ternary_contract.t27; tests/native_ternary_contract.c tests those alone.
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from trinity_memory import ternary_check_run as runner
from trinity_memory import ternary_contract as tc

ROOT = Path(__file__).resolve().parents[1]
OWN = f"{sys.executable} -m trinity_memory ternary-check"
WRONG = str(ROOT / "tests" / "action" / "wrong_group_decoder.py")
FAMILIES = ("bitnet_cpp", "hf_bitnet", "llama_cpp", "mlx", "onnx", "prismml")


def documents():
    return [json.loads((ROOT / "conformance" / f"formats_{family}.json").read_text(encoding="utf-8"))
            for family in FAMILIES]


def expected_calls():
    """(decode and encode calls, rejections, container vectors) the vectors define."""
    calls = rejections = containers = 0
    for document in documents():
        for v in document["vectors"]:
            kind = v["reader"] if v["kind"] == "reject" else v["kind"]
            if kind in ("gguf", "safetensors"):
                containers += 1
                continue
            calls += 1
            rejections += "error_class" in v
            calls += "error_class" not in v and kind in runner.DECODE_KINDS and bool(v.get("encode"))
    return calls, rejections, containers


class Environment:
    def __enter__(self):
        self.saved = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = str(ROOT) + (os.pathsep + self.saved if self.saved else "")
        return self

    def __exit__(self, *exc):
        if self.saved is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = self.saved


def run(decoder, **options):
    with Environment():
        return runner.run(decoder, **options)


def script(directory: Path, name: str, body: str) -> str:
    path = directory / name
    path.write_text("import os, sys\n" + textwrap.dedent(body), encoding="utf-8")
    return f"{sys.executable} {path}"


class ReferenceDecoder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report, cls.summary = run(OWN)

    def test_passes_every_vector(self):
        calls, rejections, containers = expected_calls()
        s = self.report["summary"]
        self.assertTrue(s["passed"], self.summary)
        self.assertEqual(s["cases"], calls)
        self.assertEqual(s["failures"], 0)
        self.assertEqual(s["outcomes"]["rejected"], rejections)
        self.assertEqual(s["outcomes"]["match"], calls - rejections)
        self.assertEqual(s["not_run"], {"container": containers})
        self.assertEqual(self.report["decoder_formats"],
                         {"source": "listing", "decode": list(tc.FORMATS), "encode": list(tc.FORMATS)})
        for case in self.report["cases"]:
            self.assertFalse(case["fails"], case)
            if case["op"] == "decode" and case["expect"] == "decode":
                self.assertEqual((case["values"], case["scales"], case["flags"]), ({"differ": 0}, {"differ": 0},
                                                                                   "equal"), case)
            if case["expect"].startswith("reject:"):
                self.assertEqual(case["error"], case["expect"][len("reject:"):], case)
        self.assertIn("## Ternary Check: PASS", self.summary)

    def test_every_vector_is_accounted_for(self):
        seen = {(c["file"], c["id"]) for c in self.report["cases"]} | \
               {(c["file"], c["id"]) for c in self.report["not_run"]}
        for family, document in zip(FAMILIES, documents()):
            for v in document["vectors"]:
                self.assertIn((f"formats_{family}.json", v["id"]), seen)
        self.assertEqual([v["file"] for v in self.report["vectors"]], [f"formats_{f}.json" for f in FAMILIES])

    def test_report_is_reproducible(self):
        again, summary = run(OWN, jobs=1)
        self.assertEqual(json.dumps(runner.reproducible(again)), json.dumps(runner.reproducible(self.report)))
        self.assertEqual(summary, self.summary)
        self.assertEqual(self.report["schema"], "trinity.ternary-check-run.v1")
        self.assertIn("started_utc", self.report["run"])
        self.assertNotIn("started_utc", json.dumps(runner.reproducible(self.report)))

    def test_formats_filter(self):
        report, _ = run(OWN, formats=["I2_S", "MLX2"])
        self.assertTrue(report["summary"]["passed"])
        self.assertEqual({c["format"] for c in report["cases"]}, {"I2_S", "MLX2"})
        self.assertEqual(report["formats_filter"], ["I2_S", "MLX2"])
        self.assertGreater(report["summary"]["not_run"]["filtered"], 0)
        with self.assertRaises(runner.RunError):
            run(OWN, formats=["Q3_K"])


class WrongDecoders(unittest.TestCase):
    def test_group_128_read_as_group_64_is_a_mismatch(self):
        report, summary = run(WRONG)
        s = report["summary"]
        self.assertFalse(s["passed"])
        failing = [c for c in report["cases"] if c["fails"]]
        self.assertTrue(failing)
        self.assertEqual({c["format"] for c in failing}, {"PQ2_0"})
        mismatches = [c for c in failing if c["outcome"] == "mismatch"]
        self.assertTrue(mismatches)
        for case in mismatches:
            self.assertGreater(case["values"]["differ"], 0)
            self.assertGreaterEqual(case["values"]["first"], 0)
        self.assertIn("## Ternary Check: FAIL", summary)
        self.assertIn("pq2_0_random_two_blocks", summary)
        # Decoding the other nine formats is untouched.
        for case in report["cases"]:
            if case["format"] != "PQ2_0":
                self.assertFalse(case["fails"], case)

    def test_silent_decoder_and_fail_on_policies(self):
        with tempfile.TemporaryDirectory() as tmp:
            decoder = script(Path(tmp), "silent.py", """
                if sys.argv[1] == "formats":
                    print("decode TQ2_0")
                    sys.exit(0)
                for path in sys.argv[5:7]:
                    open(path, "wb").close()
                """)
            strict, _ = run(decoder)
            silent, summary = run(decoder, fail_on="silent")
            never, _ = run(decoder, fail_on="never")
        self.assertEqual({c["format"] for c in strict["cases"]}, {"TQ2_0"})
        self.assertEqual(strict["decoder_formats"], {"source": "listing", "decode": ["TQ2_0"], "encode": []})
        outcomes = {c["outcome"] for c in strict["cases"]}
        self.assertEqual(outcomes, {"mismatch", "silent"})
        self.assertFalse(strict["summary"]["passed"])
        self.assertFalse(silent["summary"]["passed"])
        self.assertEqual({c["outcome"] for c in silent["cases"] if c["fails"]}, {"silent"})
        self.assertEqual(silent["summary"]["failures"], silent["summary"]["outcomes"]["silent"])
        self.assertTrue(never["summary"]["passed"])
        self.assertIn("unsupported", strict["summary"]["not_run"])
        self.assertIn("`--fail-on silent`", summary)

    def test_classes_flags_and_crashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            # No `formats` command: decode is assumed for every format, encode for none.
            unflagged = script(Path(tmp), "unflagged.py", f"""
                import subprocess
                if sys.argv[1] == "formats":
                    sys.exit(3)
                status = subprocess.call([{sys.executable!r}, "-m", "trinity_memory", "ternary-check", *sys.argv[1:]])
                if os.path.exists("flags"):
                    os.remove("flags")
                sys.exit(status)
                """)
            report, _ = run(unflagged, formats=["TQ1_0", "I2_S"])
            self.assertEqual(report["decoder_formats"]["source"], "assumed")
            self.assertEqual({c["op"] for c in report["cases"]}, {"decode"})
            self.assertTrue(report["summary"]["passed"])
            flagged = [c for c in report["cases"] if c["class"].startswith("flag:")]
            self.assertTrue(flagged)
            self.assertEqual({c["outcome"] for c in flagged}, {"match_unflagged"})

            wrong_class = script(Path(tmp), "wrong_class.py", """
                if sys.argv[1] == "formats":
                    print("decode Q1_0")
                    sys.exit(0)
                open("error", "w").write("padding\\n" if "0034a503" in open(sys.argv[4], "rb").read().hex() else "nonsense")
                sys.exit(1)
                """)
            report, _ = run(wrong_class)
            outcomes = {c["outcome"] for c in report["cases"]}
            self.assertLessEqual(outcomes, {"unexpected_reject", "unclassified", "wrong_class"})
            self.assertIn("unclassified", outcomes)
            unrecognized = [c for c in report["cases"] if c.get("error") == "unrecognized"]
            self.assertTrue(unrecognized)

            crash = script(Path(tmp), "crash.py", """
                import signal
                if sys.argv[1] == "formats":
                    print("decode ONNX2")
                    sys.exit(0)
                os.kill(os.getpid(), signal.SIGKILL)
                """)
            report, summary = run(crash)
            self.assertEqual({c["outcome"] for c in report["cases"]}, {"crashed"})
            self.assertEqual({c["exit"] for c in report["cases"]}, {None})
            self.assertIn("stopped by a signal or the time limit", summary)

            slow = script(Path(tmp), "slow.py", """
                import time
                if sys.argv[1] == "formats":
                    print("decode HF_PACKED")
                    sys.exit(0)
                time.sleep(30)
                """)
            report, _ = run(slow, timeout=0.5, formats=["HF_PACKED"])
            self.assertEqual({c["outcome"] for c in report["cases"]}, {"crashed"})


class CommandLine(unittest.TestCase):
    def call(self, *arguments, cwd=None):
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        return subprocess.run([sys.executable, "-m", "trinity_memory", "ternary-check", *arguments],
                              capture_output=True, text=True, env=env, cwd=cwd)

    def test_run_writes_report_summary_and_exit_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            report, summary = Path(tmp) / "r" / "own.json", Path(tmp) / "summary.md"
            summary.write_text("before\n", encoding="utf-8")
            result = self.call("run", "--decoder", OWN, "--formats", "Q2_0,PQ2_0", "--report", str(report),
                               "--summary", str(summary))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(report.read_text(encoding="utf-8"))["summary"]["passed"])
            self.assertTrue(summary.read_text(encoding="utf-8").startswith("before\n## Ternary Check: PASS"))
            result = self.call("run", "--decoder", WRONG, "--formats", "PQ2_0", "--report", str(report))
            self.assertEqual(result.returncode, 1)
            result = self.call("run", "--decoder", WRONG, "--formats", "PQ2_0", "--fail-on", "never")
            self.assertEqual(result.returncode, 0)
            result = self.call("run", "--decoder", OWN, "--vectors", str(Path(tmp) / "missing.json"))
            self.assertEqual(result.returncode, 2)
            result = self.call("run", "--decoder", OWN, "--formats", "NOPE")
            self.assertEqual(result.returncode, 2)
            result = self.call("frobnicate")
            self.assertEqual(result.returncode, 2)

    def test_reference_decoder_refusal_writes_one_class_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "in.bin"
            data.write_bytes(bytes(17))
            result = self.call("decode", "Q2_0", "64", str(data), "v.bin", "s.bin", cwd=tmp)
            self.assertEqual(result.returncode, 1)
            self.assertEqual((Path(tmp) / "error").read_text(encoding="ascii"), "length\n")
            self.assertFalse((Path(tmp) / "v.bin").exists())
            result = self.call("decode", "Q3_K", "64", str(data), "v.bin", "s.bin", cwd=tmp)
            self.assertEqual(result.returncode, 1)
            self.assertEqual((Path(tmp) / "error").read_text(encoding="ascii"), "format\n")
            data.write_bytes(bytes([0x00, 0x3c]) + bytes([0x55]) * 16)  # scale 1.0, sixteen bytes of code 1 (0)
            (Path(tmp) / "error").unlink()
            result = self.call("decode", "Q2_0", "64", str(data), "v.bin", "s.bin", "future_key=1", cwd=tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((Path(tmp) / "v.bin").read_bytes(), bytes(64))
            self.assertEqual((Path(tmp) / "s.bin").read_bytes(), bytes([0x00, 0x3c]))
            self.assertEqual((Path(tmp) / "flags").read_bytes(), b"")
            self.assertFalse((Path(tmp) / "error").exists())
            result = self.call("decode", "Q2_0", "sixty-four", str(data), "v.bin", "s.bin", cwd=tmp)
            self.assertEqual(result.returncode, 2)
            result = self.call("encode", "Q2_0", "64", "v.bin", "s.bin", "out.bin", cwd=tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((Path(tmp) / "out.bin").read_bytes(), data.read_bytes())

    def test_formats_listing(self):
        result = self.call("formats")
        self.assertEqual(result.returncode, 0)
        lines = result.stdout.splitlines()
        self.assertEqual(lines, [f"{op} {name}" for op in ("decode", "encode") for name in tc.FORMATS])


class Tokens(unittest.TestCase):
    def test_contract_tokens_are_the_vector_constants(self):
        for document in documents():
            constants = document["constants"]
            for token, status in constants["errors"].items():
                self.assertEqual(tc.error_token(status), token)
                self.assertEqual(tc.error_status(token.encode() + b"\n"), status)
            self.assertEqual({token: slot for slot, token in enumerate(tc.flag_tokens())}, constants["flags"])
            for name, fid in constants["format_ids"].items():
                expected = "MLX2" if name == "LINEAR2" else name
                self.assertEqual(tc.format_name(fid), expected)


if __name__ == "__main__":
    unittest.main()
