"""The composite Action ternary-check/action.yml, off GitHub.

Its two steps are the scripts ternary-check/resolve-runtime.sh and
ternary-check/run.sh; this test runs them with the environment the runner
gives a composite step. runtime "release" is exercised against a local
release directory (file:// URLs) holding a stand-in wheel and its
SHA256SUMS, so no network is used; the real v0.4.0 assets are checked after
publication. .github/workflows/ci.yml runs the Action itself.
"""
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / "ternary-check"
LIBRARY = ROOT / "build" / "t27" / ("libtrinity_memory_t27.dylib" if sys.platform == "darwin"
                                    else "libtrinity_memory_t27.so")
HOST = {("Darwin", "arm64"): ("macOS", "ARM64", "macosx_13_0_arm64"),
        ("Linux", "x86_64"): ("Linux", "X64", "linux_x86_64")}.get((platform.system(), platform.machine()))


def environment(tmp: Path, **extra):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GITHUB_")}
    env.pop("PYTHONPATH", None)
    env.update(RUNNER_TEMP=str(tmp), GITHUB_OUTPUT=str(tmp / "output"), GITHUB_STEP_SUMMARY=str(tmp / "step.md"),
               PYTHON=sys.executable)
    env.update(extra)
    return env


def outputs(tmp: Path) -> dict:
    path = tmp / "output"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return dict(line.split("=", 1) for line in lines)


def bash(script: str, env, cwd=ROOT):
    return subprocess.run(["bash", str(ACTION / script)], env=env, cwd=cwd, capture_output=True, text=True)


class ActionDefinition(unittest.TestCase):
    def test_inputs_and_outputs_are_wired(self):
        text = (ACTION / "action.yml").read_text(encoding="utf-8")
        declared_inputs = set(re.findall(r"^  ([a-z-]+):\n    description:", text.split("\noutputs:")[0], re.M))
        self.assertEqual(declared_inputs, {"decoder", "vectors", "formats", "report", "summary", "fail-on", "timeout",
                                           "runtime", "release-url", "python"})
        self.assertEqual(set(re.findall(r"\$\{\{ inputs\.([a-z-]+) \}\}", text)), declared_inputs)
        run_outputs = set(re.findall(r"\$\{\{ steps\.run\.outputs\.([a-z]+) \}\}", text))
        self.assertEqual(run_outputs, {"report", "summary", "passed", "cases", "failures", "mismatches", "silent"})
        self.assertIn("using: composite", text)
        self.assertEqual(text.count("shell: bash"), text.count("- id:"))
        # Inputs reach the scripts through env, never through the script text.
        for line in text.splitlines():
            if line.strip().startswith("run:"):
                self.assertNotIn("${{", line)


@unittest.skipUnless(LIBRARY.is_file(), "build the native library first: tools/build-t27.sh")
class Runtime(unittest.TestCase):
    def test_checkout_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            result = bash("resolve-runtime.sh", environment(tmp, RUNTIME="."))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(outputs(tmp)["pythonpath"], str(ROOT))
            result = bash("resolve-runtime.sh", environment(tmp, RUNTIME=str(tmp / "nowhere")))
            self.assertEqual(result.returncode, 1)
            self.assertIn("neither release nor a directory", result.stderr)
            result = bash("resolve-runtime.sh", environment(tmp, RUNTIME=str(tmp)))
            self.assertEqual(result.returncode, 1)
            self.assertIn("no trinity_memory package", result.stderr)

    @unittest.skipUnless(HOST, "release wheels exist for Linux x86_64 and macOS arm64 only")
    def test_release_runtime_checks_sha256sums(self):
        runner_os, runner_arch, tag = HOST
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            release = tmp / "releases" / "v9.9.9"
            release.mkdir(parents=True)
            wheel = release / f"trinity_ternary_memory-9.9.9-py3-none-{tag}.whl"
            other = release / "trinity_ternary_memory-9.9.9-py3-none-win_amd64.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                for source in sorted((ROOT / "trinity_memory").glob("*.py")):
                    archive.write(source, f"trinity_memory/{source.name}")
                archive.write(LIBRARY, f"trinity_memory/_native_runtime/{LIBRARY.name}")
            other.write_bytes(b"not for this platform")
            digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()  # noqa: E731
            (release / "SHA256SUMS").write_text(f"{digest(wheel)}  {wheel.name}\n{digest(other)}  {other.name}\n",
                                                encoding="utf-8")
            env = environment(tmp, RUNTIME="release", VERSION="9.9.9", RELEASE_URL=(tmp / "releases").as_uri(),
                              RUNNER_OS=runner_os, RUNNER_ARCH=runner_arch)
            result = bash("resolve-runtime.sh", env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"verified {wheel.name} ({digest(wheel)})", result.stdout)
            site = Path(outputs(tmp)["pythonpath"])
            self.assertTrue((site / "trinity_memory" / "_native_runtime" / LIBRARY.name).is_file())
            # The unpacked wheel runs the check without the checkout on PYTHONPATH.
            result = bash("run.sh", environment(tmp, TERNARY_CHECK_PYTHONPATH=str(site),
                                                DECODER=f"{sys.executable} -m trinity_memory ternary-check",
                                                VECTORS=str(ROOT / "conformance" / "formats_mlx.json"),
                                                REPORT=str(tmp / "mlx.json")), cwd=tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((tmp / "mlx.json").read_text(encoding="utf-8"))["vectors"][0]["file"],
                             "formats_mlx.json")

            wheel.write_bytes(wheel.read_bytes() + b"tampered")
            (tmp / "output").unlink()
            result = bash("resolve-runtime.sh", env)
            self.assertEqual(result.returncode, 1)
            self.assertIn("SHA256SUMS of v9.9.9 says", result.stderr)
            self.assertFalse((tmp / "output").exists())

            result = bash("resolve-runtime.sh", dict(env, RUNNER_OS="Windows", RUNNER_ARCH="X64"))
            self.assertEqual(result.returncode, 1)
            self.assertIn("Linux x86_64 and macOS arm64 only", result.stderr)
            result = bash("resolve-runtime.sh", dict(env, VERSION="9.9.8"))
            self.assertEqual(result.returncode, 1)
            self.assertIn("cannot download", result.stderr)


@unittest.skipUnless(LIBRARY.is_file(), "build the native library first: tools/build-t27.sh")
class Run(unittest.TestCase):
    def test_outputs_summary_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "step.md").write_text("earlier step\n", encoding="utf-8")
            base = dict(TERNARY_CHECK_PYTHONPATH=str(ROOT), FORMATS="PQ2_0,Q2_0")
            own = bash("run.sh", environment(tmp, **base, DECODER="python3 -m trinity_memory ternary-check",
                                             PYTHON="python3", REPORT=str(tmp / "own.json")))
            self.assertEqual(own.returncode, 0, own.stderr)
            first = outputs(tmp)
            self.assertEqual((first["passed"], first["failures"], first["report"]), ("true", "0", str(tmp / "own.json")))
            (tmp / "output").unlink()
            wrong = bash("run.sh", environment(tmp, **base, DECODER="tests/action/wrong_group_decoder.py",
                                               REPORT=str(tmp / "wrong.json"), FAIL_ON="mismatch"))
            self.assertEqual(wrong.returncode, 1, wrong.stderr)
            second = outputs(tmp)
            self.assertEqual(second["passed"], "false")
            self.assertGreater(int(second["mismatches"]), 0)
            step = (tmp / "step.md").read_text(encoding="utf-8")
            self.assertTrue(step.startswith("earlier step\n## Ternary Check: PASS"))
            self.assertIn("## Ternary Check: FAIL", step)
            check = subprocess.run([sys.executable, str(ROOT / "tests" / "action" / "check_reports.py"),
                                    str(tmp / "own.json"), str(tmp / "wrong.json")], capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)
            self.assertIn("value mismatches", check.stdout)
            reversed_check = subprocess.run([sys.executable, str(ROOT / "tests" / "action" / "check_reports.py"),
                                             str(tmp / "wrong.json"), str(tmp / "own.json")], capture_output=True)
            self.assertNotEqual(reversed_check.returncode, 0)


if __name__ == "__main__":
    unittest.main()
