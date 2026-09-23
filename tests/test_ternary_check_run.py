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
import stat
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

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


def forwarding(directory: Path, name: str, after: str) -> str:
    """A decoder that runs the reference decoder, then applies `after` to a call that exited 0."""
    body = ("import subprocess\n"
            f"status = subprocess.call([{sys.executable!r}, '-m', 'trinity_memory', 'ternary-check', *sys.argv[1:]])\n"
            "if status == 0 and sys.argv[1] in ('decode', 'encode'):\n"
            + textwrap.indent(textwrap.dedent(after).strip() + "\n", "    ")
            + "sys.exit(status)\n")
    return script(directory, name, body)


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
                         {"source": "listing", "decode": list(tc.FORMATS), "encode": list(tc.FORMATS),
                          "unrecognized": []})
        self.assertEqual(s["idle_formats"], [])
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
        self.assertEqual(strict["decoder_formats"], {"source": "listing", "decode": ["TQ2_0"], "encode": [],
                                                     "unrecognized": []})
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


def reference_after_listing(directory: Path, name: str, listing: str) -> str:
    """A decoder whose `formats` prints `listing` and which forwards every other call to the reference."""
    return script(directory, name, f"""
        if sys.argv[1] == "formats":
            sys.stdout.write({listing!r})
            sys.exit(0)
        os.execvp({sys.executable!r}, [{sys.executable!r}, "-m", "trinity_memory", "ternary-check", *sys.argv[1:]])
        """)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class WrongOutputsWithRightValues(unittest.TestCase):
    """Decoders whose values are right but whose scale words, flags or encoded bytes are not."""
    FORMATS = ["TQ2_0", "I2_S", "MLX2", "ONNX2"]

    def check(self, report, op, field):
        self.assertFalse(report["summary"]["passed"])
        wrong = [c for c in report["cases"] if c["op"] == op and c["expect"] == op]
        self.assertTrue(wrong)
        for case in wrong:
            self.assertEqual(case["outcome"], "mismatch", case)
            self.assertTrue(case["fails"], case)
            if op == "decode":
                self.assertEqual(case["values"], {"differ": 0}, case)
        for case in report["cases"]:
            if case not in wrong:
                self.assertFalse(case["fails"], case)
        return wrong

    def test_wrong_scale_words_are_a_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            decoder = forwarding(Path(tmp), "bad_scales.py", """
                if sys.argv[1] == "decode":
                    data = bytearray(open(sys.argv[6], "rb").read())
                    data[0] ^= 1
                    open(sys.argv[6], "wb").write(bytes(data))
                """)
            report, summary = run(decoder, formats=self.FORMATS)
        for case in self.check(report, "decode", "scales"):
            self.assertEqual((case["scales"]["first"], case["scales"]["differ"] > 0), (0, True), case)
            self.assertEqual(case["scales"]["actual"], case["scales"]["expected"] ^ 1, case)
            self.assertEqual(case["flags"], "equal", case)
        self.assertIn("scales: ", summary)

    def test_wrong_reported_flags_are_a_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Reports scale_zero 1 where the reference reports nothing, and nothing where it reports a flag.
            decoder = forwarding(Path(tmp), "bad_flags.py", """
                if sys.argv[1] == "decode":
                    lines = open("flags").read().split("\\n") if os.path.exists("flags") else []
                    counts = [int(line.split()[1]) for line in lines if line.strip()]
                    open("flags", "w").write("" if any(counts) else "scale_zero 1\\n")
                """)
            report, summary = run(decoder, formats=self.FORMATS)
        for case in self.check(report, "decode", "flags"):
            self.assertEqual(case["scales"], {"differ": 0}, case)
            self.assertEqual(case["flags"], "differ", case)
            self.assertNotEqual(case["flags_reported"], case["flags_expected"], case)
        self.assertIn("flags: expected", summary)

    def test_wrong_encoded_bytes_are_a_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            decoder = forwarding(Path(tmp), "bad_encode.py", """
                if sys.argv[1] == "encode":
                    data = bytearray(open(sys.argv[6], "rb").read())
                    data[-1] ^= 1
                    open(sys.argv[6], "wb").write(bytes(data))
                """)
            report, summary = run(decoder, formats=self.FORMATS)
        for case in self.check(report, "encode", "output"):
            self.assertEqual(case["output"]["differ"], 1, case)
            self.assertEqual(case["output"]["first"], case["output"]["actual_bytes"] - 1, case)
        self.assertIn("output: 1 differ", summary)


class ARunThatChecksNothing(unittest.TestCase):
    """A run with no decoder call, or with a --formats name that ran no call, fails."""

    def test_filter_that_misses_the_listing(self):
        with tempfile.TemporaryDirectory() as tmp:
            decoder = reference_after_listing(Path(tmp), "only_tq2.py", "decode TQ2_0\n")
            report, summary = run(decoder, formats=["Q1_0"])
            never, _ = run(decoder, formats=["Q1_0"], fail_on="never")
        s = report["summary"]
        self.assertEqual((s["cases"], s["failures"], s["passed"]), (0, 0, False))
        self.assertEqual(s["idle_formats"], ["Q1_0"])
        self.assertEqual(s["not_run"]["unsupported"] + s["not_run"]["filtered"] + s["not_run"]["container"],
                         len(report["not_run"]))
        self.assertIn("## Ternary Check: FAIL", summary)
        self.assertIn("No decoder call ran", summary)
        self.assertFalse(never["summary"]["passed"])

    def test_misspelled_format_in_the_listing(self):
        with tempfile.TemporaryDirectory() as tmp:
            decoder = reference_after_listing(Path(tmp), "typo.py", "decode TQ1_0\ndecode TQ2-0\n")
            report, summary = run(decoder, formats=["TQ1_0", "TQ2_0"])
        s = report["summary"]
        self.assertGreater(s["cases"], 0)
        self.assertEqual(s["failures"], 0)
        self.assertFalse(s["passed"])
        self.assertEqual(s["idle_formats"], ["TQ2_0"])
        self.assertEqual(report["decoder_formats"]["unrecognized"], ["decode TQ2-0"])
        self.assertIn("ran no call: TQ2_0", summary)
        self.assertIn("`decode TQ2-0`", summary)

    def test_vectors_without_payload_vectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            document = json.loads((ROOT / "conformance" / "formats_llama_cpp.json").read_text(encoding="utf-8"))
            document["vectors"] = [v for v in document["vectors"]
                                   if (v["reader"] if v["kind"] == "reject" else v["kind"]) in runner.CONTAINER_KINDS]
            self.assertTrue(document["vectors"])
            path = Path(tmp) / "formats_containers.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            report, summary = run(OWN, vectors=[path])
        self.assertEqual(report["summary"]["cases"], 0)
        self.assertEqual(report["summary"]["not_run"], {"container": len(document["vectors"])})
        self.assertFalse(report["summary"]["passed"])
        self.assertIn("No decoder call ran", summary)


class DecoderProcess(unittest.TestCase):
    def test_relative_paths_in_the_decoder_command(self):
        saved = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                Path("decode.py").write_text("", encoding="utf-8")
                Path("sub").mkdir()
                Path("sub", "dec").write_text("", encoding="utf-8")
                self.assertEqual(runner.decoder_command("python3 decode.py sub ./sub/dec -x missing.py"),
                                 ["python3", os.path.abspath("decode.py"), "sub", os.path.abspath("sub/dec"), "-x",
                                  "missing.py"])
                # A bare first word goes through PATH, as in a shell.
                self.assertEqual(runner.decoder_command("decode.py"), ["decode.py"])
                self.assertEqual(runner.decoder_command("./decode.py")[0], os.path.abspath("decode.py"))
            finally:
                os.chdir(saved)
        # From the repository root, the module words of the reference decoder stay as they are
        # (ternary-check/ is a directory there, not a file).
        os.chdir(ROOT)
        try:
            self.assertEqual(runner.decoder_command("python3 -m trinity_memory ternary-check"),
                             ["python3", "-m", "trinity_memory", "ternary-check"])
        finally:
            os.chdir(saved)

    def test_relative_pythonpath_entries_become_absolute(self):
        env = runner.child_environment({"PYTHONPATH": os.pathsep.join([".", "/abs", "lib"]), "OTHER": "."})
        self.assertEqual(env["PYTHONPATH"].split(os.pathsep), [os.path.abspath("."), "/abs", os.path.abspath("lib")])
        self.assertEqual(env["OTHER"], ".")
        self.assertNotIn("PYTHONPATH", runner.child_environment({}))

    def test_each_call_starts_in_an_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Exits 3 (unclassified) unless its working directory is empty, then uses it as
            # scratch space before running the reference decoder there.
            decoder = script(Path(tmp), "probe.py", f"""
                if os.listdir("."):
                    sys.exit(3)
                for name in ("values.bin", "scales.bin", "input.bin", "zero-points.bin"):
                    open(name, "wb").write(b"scratch")
                os.execvp({sys.executable!r}, [{sys.executable!r}, "-m", "trinity_memory", "ternary-check",
                                               *sys.argv[1:]])
                """)
            report, summary = run(decoder, formats=["TQ2_0", "MLX2", "ONNX2"])
        self.assertTrue(report["summary"]["passed"], summary)
        self.assertEqual(report["decoder_formats"]["source"], "listing")
        self.assertEqual({c["op"] for c in report["cases"]}, {"decode", "encode"})

    def test_time_limit_stops_the_whole_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pids = tmp / "pids"
            pids.mkdir()
            wrapper = tmp / "slow.sh"
            # A wrapper that does not exec: its child outlives it unless the group is killed.
            wrapper.write_text('#!/bin/sh\nif [ "$1" = formats ]; then echo "decode HF_PACKED"; exit 0; fi\n'
                               'sleep 30 </dev/null >/dev/null 2>&1 &\necho $! > "$TERNARY_TEST_PIDS/$$"\nwait\n',
                               encoding="ascii")
            wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
            started = time.monotonic()
            with mock.patch.dict(os.environ, {"TERNARY_TEST_PIDS": str(pids)}):
                report, _ = run(str(wrapper), timeout=1.5, formats=["HF_PACKED"], jobs=4)
            self.assertLess(time.monotonic() - started, 25)
            self.assertEqual({c["outcome"] for c in report["cases"]}, {"crashed"})
            children = [int(p.read_text(encoding="ascii")) for p in pids.iterdir()]
        self.assertEqual(len(children), report["summary"]["cases"])
        deadline = time.monotonic() + 10
        remaining = children
        while remaining and time.monotonic() < deadline:
            time.sleep(0.1)
            remaining = [pid for pid in children if alive(pid)]
        self.assertEqual(remaining, [])


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
            # A run that checks nothing exits 1 and says so.
            only_tq2 = reference_after_listing(Path(tmp), "only_tq2.py", "decode TQ2_0\n")
            result = self.call("run", "--decoder", only_tq2, "--formats", "Q1_0", "--report", str(report))
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("No decoder call ran", result.stdout)
            self.assertFalse(json.loads(report.read_text(encoding="utf-8"))["summary"]["passed"])

    def test_documented_decoder_commands(self):
        # CONTRACT.md: `python3 decode.py`, a bare file name in the directory the runner starts in.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "decode.py").write_text(
                f"import os, sys\nos.execvp({sys.executable!r}, [{sys.executable!r}, '-m', 'trinity_memory', "
                "'ternary-check', *sys.argv[1:]])\n", encoding="utf-8")
            result = self.call("run", "--decoder", f"{sys.executable} decode.py", "--formats", "TQ2_0", cwd=tmp)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Decoder formats: listing", result.stdout)
        # README.md: from a checkout with PYTHONPATH=. and the reference decoder.
        env = dict(os.environ, PYTHONPATH=".")
        result = subprocess.run([sys.executable, "-m", "trinity_memory", "ternary-check", "run", "--decoder",
                                 f"{sys.executable} -m trinity_memory ternary-check", "--formats", "TQ2_0"],
                                capture_output=True, text=True, env=env, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("## Ternary Check: PASS", result.stdout)

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
            # tk_encode refuses a VALUES file whose size is not COUNT.
            for size in (63, 65):
                (Path(tmp) / "short.bin").write_bytes(bytes(size))
                result = self.call("encode", "Q2_0", "64", "short.bin", "s.bin", "refused.bin", cwd=tmp)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual((Path(tmp) / "error").read_text(encoding="ascii"), "length\n")
                self.assertFalse((Path(tmp) / "refused.bin").exists())
                (Path(tmp) / "error").unlink()
            result = self.call("encode", "Q3_K", "64", "short.bin", "s.bin", "refused.bin", cwd=tmp)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual((Path(tmp) / "error").read_text(encoding="ascii"), "format\n")

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
