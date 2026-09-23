"""tools/build-release-assets.py without building a release.

The Linux wheel comes from CI, so here it is a stand-in: a zip with the real
dist-info layout, this checkout's trinity_memory/*.py, a native manifest and
files whose first bytes are an ELF x86-64 header. The package checks must
accept it and refuse each single defect. The preflight is run against a
temporary git repository; SHA256SUMS writing and verification, the sdist
member check, the otool parser and, on macOS with a C compiler, the minos
check of a real Mach-O file are tested directly, as are the CI run and
artifact checks (hand-made `gh` JSON), the gate order and unittest counts
(with `run` replaced), the stale build directories and the rule that a macOS
tag must not be below any minos. The macOS wheel build itself runs only in a
real release or a --no-ci dry run (see the tool's docstring).
"""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("build_release_assets", ROOT / "tools" / "build-release-assets.py")
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)

VERSION = tool.project_version(ROOT)
PIN = (ROOT / "native" / "compiler.lock").read_text().strip()
ELF_X86_64 = b"\x7fELF\x02\x01\x01" + bytes(11) + (62).to_bytes(2, "little") + bytes(44)


def fake_linux_wheel(directory: Path, *, version=VERSION, pin=PIN, manifest_version=None, tamper=None,
                     name=None, elf=ELF_X86_64, drop=None, source_edit=None, stray=None) -> Path:
    """A stand-in for the CI Linux wheel; each keyword introduces one defect."""
    runtime = "trinity_memory/_native_runtime/"
    files = {"libtrinity_memory_t27.so": elf + b"library", "trinity-memory-t27": elf + b"cli",
             "codecs.wasm": b"\0asm\1\0\0\0codecs", "formats.wasm": b"\0asm\1\0\0\0formats",
             "compiler.revision": (pin + "\n").encode(), "compiler.sha256": b"0" * 64 + b"  -\n"}
    if drop:
        files.pop(drop)
    manifest = {"schema": "trinity.native-wheel.v1", "compiler_revision": pin,
                "version": manifest_version or version,
                "files": {key: hashlib.sha256(value).hexdigest() for key, value in files.items()}}
    if tamper:
        files[tamper] = files[tamper] + b"changed"
    tag = "py3-none-linux_x86_64"
    path = directory / (name or f"{tool.DIST}-{version}-{tag}.whl")
    dist_info = f"{tool.DIST}-{version}.dist-info/"
    with zipfile.ZipFile(path, "w") as archive:
        for module in sorted((ROOT / "trinity_memory").glob("*.py")):
            data = module.read_bytes()
            if source_edit == module.name:
                data += b"\n# edited\n"
            archive.writestr(f"trinity_memory/{module.name}", data)
        for key, value in files.items():
            archive.writestr(runtime + key, value)
        archive.writestr(runtime + "manifest.json", json.dumps(manifest))
        if stray:
            archive.writestr(stray, b"left over from an earlier build")
        archive.writestr(dist_info + "METADATA", f"Metadata-Version: 2.4\nName: trinity-ternary-memory\nVersion: {version}\n")
        archive.writestr(dist_info + "WHEEL", f"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: {tag}\n")
    return path


class LinuxWheelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="release-wheel-"))
        self.addCleanup(shutil.rmtree, self.tmp)

    def check(self, wheel):
        return tool.check_wheel(wheel, ROOT, VERSION, PIN, "linux")

    def test_stand_in_passes(self):
        facts = self.check(fake_linux_wheel(self.tmp))
        self.assertEqual(facts["tag"], "py3-none-linux_x86_64")
        self.assertEqual(facts["compiler_revision"], PIN)
        self.assertEqual(facts["python_modules_equal_commit"], len(list((ROOT / "trinity_memory").glob("*.py"))))

    def test_each_defect_is_refused(self):
        cases = {
            "manifest version": dict(manifest_version="0.3.0"),
            "digest": dict(tamper="formats.wasm"),
            "not ELF": dict(elf=b"\xcf\xfa\xed\xfe" + bytes(60)),
            "ELF of another machine": dict(elf=ELF_X86_64[:18] + (183).to_bytes(2, "little") + ELF_X86_64[20:]),
            "missing formats.wasm": dict(drop="formats.wasm"),
            "edited module": dict(source_edit="ternary_check_run.py"),
            "compiler pin": dict(pin="0" * 40),
            "platform": dict(name=f"{tool.DIST}-{VERSION}-py3-none-linux_aarch64.whl"),
            "stale subpackage": dict(stray="trinity_memory/zz_stale/evil.py"),
            "stale data file": dict(stray="trinity_memory/stale.txt"),
            "file outside the manifest": dict(stray="trinity_memory/_native_runtime/extra.so"),
            "other top-level package": dict(stray="strayp/__init__.py"),
        }
        for label, defect in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as directory:
                wheel = fake_linux_wheel(Path(directory), **defect)
                with self.assertRaises(tool.ReleaseError):
                    self.check(wheel)

    def test_resolve_runtime_finds_the_names_this_tool_accepts(self):
        # ternary-check/resolve-runtime.sh selects the wheel from SHA256SUMS by these patterns.
        text = (ROOT / "ternary-check" / "resolve-runtime.sh").read_text()
        self.assertIn("-py3-none-(manylinux[0-9_]*_x86_64|linux_x86_64)\\.whl$", text)
        self.assertIn("-py3-none-macosx_[0-9]+_[0-9]+_arm64\\.whl$", text)
        self.assertRegex(f"{tool.DIST}-{VERSION}-py3-none-macosx_14_0_arm64.whl", tool.MACOS_TAG)


def ci_run_json(directory: Path, **changes) -> Path:
    """A `gh run view --json ...` of a successful push run of ci.yml; keywords change fields."""
    run = {"databaseId": 42, "url": "https://github.com/dmitrii-f-t27/trinity-memory/actions/runs/42",
           "headSha": "a" * 40, "event": "push", "status": "completed", "conclusion": "success",
           "workflowName": "Executable t27 stack",
           "jobs": [{"name": name, "conclusion": "success"} for name in sorted(tool.CI_JOBS)]}
    run.update(changes)
    path = directory / "ci-run.json"
    path.write_text(json.dumps(run))
    return path


class CiRunTest(unittest.TestCase):
    def test_ci_yml_push_run_with_every_job(self):
        with tempfile.TemporaryDirectory() as directory:
            facts = tool.check_ci_run(ci_run_json(Path(directory)), "a" * 40)
        self.assertEqual((facts["workflow"], facts["event"], facts["run_id"]), ("Executable t27 stack", "push", 42))
        self.assertEqual({job["name"] for job in facts["jobs"]}, tool.CI_JOBS)

    def test_ci_jobs_are_the_jobs_of_ci_yml(self):
        # Job names: the job ids of ci.yml, with the matrix value in parentheses.
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        ids = set(re.findall(r"^  ([a-z0-9-]+):$", text.split("\njobs:\n", 1)[1], re.M))
        self.assertEqual({name.split(" (")[0] for name in tool.CI_JOBS}, ids)

    def test_other_runs_are_refused(self):
        one_job = [{"name": "smoke (ubuntu-latest)", "conclusion": "success"}]
        cases = {
            "another workflow": dict(workflowName="Ternary Check release smoke", event="workflow_dispatch",
                                     jobs=one_job),
            "weekly workflow": dict(workflowName="Ternary Check weekly", event="schedule"),
            "pull request event": dict(event="pull_request"),
            "another commit": dict(headSha="b" * 40),
            "failed": dict(conclusion="failure"),
            "missing job": dict(jobs=[{"name": name, "conclusion": "success"} for name in sorted(tool.CI_JOBS)][1:]),
            "extra job": dict(jobs=[{"name": name, "conclusion": "success"} for name in sorted(tool.CI_JOBS)]
                              + [{"name": "extra", "conclusion": "success"}]),
            "skipped job": dict(jobs=[{"name": name, "conclusion": "skipped" if name == "sdk" else "success"}
                                      for name in sorted(tool.CI_JOBS)]),
        }
        for label, change in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(tool.ReleaseError):
                    tool.check_ci_run(ci_run_json(Path(directory), **change), "a" * 40)


class LinuxArtifactTest(unittest.TestCase):
    """The Linux wheel is tied to the CI run through the artifact digest GitHub lists."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="release-artifact-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.wheel = fake_linux_wheel(self.tmp)
        self.zip = self.tmp / "native-python-3.12.zip"
        with zipfile.ZipFile(self.zip, "w") as archive:
            archive.writestr("installed-wheel.json", "{}")
            archive.write(self.wheel, f"wheels/{self.wheel.name}")
        self.run_facts = tool.check_ci_run(ci_run_json(self.tmp), "a" * 40)

    def artifacts(self, **changes) -> Path:
        item = {"id": 7, "name": "native-python-3.12", "expired": False,
                "digest": "sha256:" + hashlib.sha256(self.zip.read_bytes()).hexdigest(),
                "workflow_run": {"id": 42, "head_sha": "a" * 40}}
        item.update(changes)
        path = self.tmp / "artifacts.json"
        path.write_text(json.dumps({"total_count": 2, "artifacts": [
            item, {"id": 8, "name": "native-python-3.14", "digest": "sha256:" + "0" * 64,
                   "workflow_run": {"id": 42, "head_sha": "a" * 40}}]}))
        return path

    def test_digest_and_wheel_match(self):
        facts = tool.check_linux_artifact(self.artifacts(), self.zip, self.wheel, self.run_facts)
        self.assertEqual((facts["id"], facts["member"], facts["run_id"]), (7, f"wheels/{self.wheel.name}", 42))

    def test_refusals(self):
        cases = {"digest": dict(digest="sha256:" + "1" * 64),
                 "another run": dict(workflow_run={"id": 43, "head_sha": "a" * 40}),
                 "another commit": dict(workflow_run={"id": 42, "head_sha": "b" * 40}),
                 "expired": dict(expired=True)}
        for label, change in cases.items():
            with self.subTest(label), self.assertRaises(tool.ReleaseError):
                tool.check_linux_artifact(self.artifacts(**change), self.zip, self.wheel, self.run_facts)
        other = fake_linux_wheel(Path(tempfile.mkdtemp(dir=self.tmp)), source_edit="ternary_check_run.py")
        with self.assertRaisesRegex(tool.ReleaseError, "differs"):
            tool.check_linux_artifact(self.artifacts(), self.zip, other, self.run_facts)


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="release-git-"))
        self.addCleanup(shutil.rmtree, self.repo)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        for command in (["init", "-q"], ["config", "commit.gpgsign", "false"]):
            subprocess.run(["git", "-C", str(self.repo), *command], check=True, env=env)
        (self.repo / ".gitignore").write_text("build/\n")
        (self.repo / "file.txt").write_text("one\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True, env=env)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", "one"], check=True, env=env)
        self.head = tool.git(self.repo, "rev-parse", "HEAD").strip()

    def test_clean_tree_at_the_commit(self):
        (self.repo / "build").mkdir()
        (self.repo / "build" / "ignored.bin").write_bytes(b"x")
        self.assertEqual(tool.preflight(self.repo, self.head), tool.git(self.repo, "rev-parse", "HEAD^{tree}").strip())

    def test_refusals(self):
        with self.assertRaisesRegex(tool.ReleaseError, "full 40-hex"):
            tool.preflight(self.repo, self.head[:12])
        with self.assertRaisesRegex(tool.ReleaseError, "not the release commit"):
            tool.preflight(self.repo, "0" * 40)
        (self.repo / "untracked.txt").write_text("new\n")
        with self.assertRaisesRegex(tool.ReleaseError, "not clean"):
            tool.preflight(self.repo, self.head)
        (self.repo / "untracked.txt").unlink()
        (self.repo / "file.txt").write_text("two\n")
        with self.assertRaisesRegex(tool.ReleaseError, "not clean"):
            tool.preflight(self.repo, self.head)

    def test_main_refuses_before_writing(self):
        out = self.repo / "out"
        with tempfile.TemporaryDirectory() as directory:
            wheel = fake_linux_wheel(Path(directory))
            status = tool.main(["--commit", "0" * 40, "--linux-wheel", str(wheel), "--no-ci", "--out", str(out),
                                "--root", str(ROOT)])
        self.assertEqual(status, 1)
        self.assertFalse(out.exists())


class GatesTest(unittest.TestCase):
    def test_order_environment_and_counts(self):
        calls = []

        def fake_run(command, *, cwd, env=None, log_path=None):
            calls.append((command, env))
            if "unittest" in command:
                log_path.write_text("test_a (tests.test_x.X.test_a) ... ok\n"
                                    "test_b (tests.test_x.X.test_b)\nA docstring. ... skipped 'no node'\n"
                                    "extracted 1 ranges\n\n----\nRan 2 tests in 0.1s\n\nOK (skipped=1)\n")
            else:
                log_path.write_text("ok\n")
            return 0.1

        original = tool.run
        tool.run = fake_run
        try:
            with tempfile.TemporaryDirectory() as directory:
                results = tool.run_gates(ROOT, sys.executable, {}, Path(directory))
                self.assertEqual(sorted(p.name for p in Path(directory).iterdir()),
                                 [f"gate-{i}.log" for i in range(5)])
        finally:
            tool.run = original
        labels = [result["command"] for result in results]
        # The unittest gate runs after ternary-check-verify has written build/upstream/matrix.
        self.assertEqual(labels[-1], tool.UNITTEST_GATE)
        self.assertLess(labels.index("OFFLINE=1 make ternary-check-verify"), labels.index(tool.UNITTEST_GATE))
        self.assertEqual(calls[-1][1]["TRINITY_REQUIRE_CACHED"], "1")
        self.assertEqual((results[-1]["tests_run"], results[-1]["skipped"], results[-1]["skipped_tests"]),
                         (2, 1, [{"test": "test_b (tests.test_x.X.test_b)", "reason": "no node"}]))

    def test_counts(self):
        self.assertEqual(tool.unittest_counts("Ran 3 tests in 1s\n\nOK (skipped=2)\n")["skipped"], 2)
        with self.assertRaises(tool.ReleaseError):
            tool.unittest_counts("no summary\n")
        self.assertEqual(tool.unittest_counts("Ran 247 tests in 64.2s\n\nOK\n"),
                         {"tests_run": 247, "skipped": 0, "skipped_tests": []})


class StaleBuildTest(unittest.TestCase):
    def test_platform_lib_directories_are_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("build/lib", "build/lib.macosx-10.15-universal2-cpython-314",
                         "build/lib.macosx-26.0-arm64-cpython-314", "build/bdist.macosx-14.0-arm64", "build/t27"):
                (root / name).mkdir(parents=True)
            self.assertEqual([path.name for path in tool.stale_build_dirs(root)],
                             ["bdist.macosx-14.0-arm64", "lib", "lib.macosx-10.15-universal2-cpython-314",
                              "lib.macosx-26.0-arm64-cpython-314"])


class ChecksumTest(unittest.TestCase):
    def test_write_verify_and_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            (out / "b.whl").write_bytes(b"wheel")
            (out / "a.json").write_bytes(b"{}")
            tool.write_checksums(out)
            lines = (out / "SHA256SUMS").read_text().splitlines()
            self.assertEqual([line.split("  ")[1] for line in lines], ["a.json", "b.whl"])
            self.assertEqual(lines[0], hashlib.sha256(b"{}").hexdigest() + "  a.json")
            self.assertEqual(tool.verify_checksums(out), 2)
            (out / "b.whl").write_bytes(b"wheel!")
            with self.assertRaises(tool.ReleaseError):
                tool.verify_checksums(out)
            (out / "b.whl").write_bytes(b"wheel")
            (out / "c.txt").write_bytes(b"unlisted")
            with self.assertRaisesRegex(tool.ReleaseError, "lists"):
                tool.verify_checksums(out)


class SdistTest(unittest.TestCase):
    def test_members_must_carry_the_commit_bytes(self):
        required = {"CHANGELOG.md": hashlib.sha256(b"log").hexdigest(),
                    "reports/ternary-check.json": hashlib.sha256(b"{}").hexdigest()}
        with tempfile.TemporaryDirectory() as directory:
            def sdist(members):
                path = Path(directory) / f"{tool.DIST}-{VERSION}.tar.gz"
                with tarfile.open(path, "w:gz") as archive:
                    top = tarfile.TarInfo(f"{tool.DIST}-{VERSION}")
                    top.type = tarfile.DIRTYPE
                    archive.addfile(top)
                    for name, data in {"PKG-INFO": b"Version", **members}.items():
                        info = tarfile.TarInfo(f"{tool.DIST}-{VERSION}/{name}")
                        info.size = len(data)
                        archive.addfile(info, io.BytesIO(data))
                return path
            facts = tool.check_sdist(sdist({"CHANGELOG.md": b"log", "reports/ternary-check.json": b"{}"}), VERSION,
                                     required)
            self.assertEqual(facts["tracked_files_checked"], 2)
            with self.assertRaisesRegex(tool.ReleaseError, "missing"):
                tool.check_sdist(sdist({"CHANGELOG.md": b"log"}), VERSION, required)
            with self.assertRaisesRegex(tool.ReleaseError, "outside"):
                tool.check_sdist(sdist({"../CHANGELOG.md": b"log"}), VERSION, required)
            with self.assertRaisesRegex(tool.ReleaseError, "differing"):
                tool.check_sdist(sdist({"CHANGELOG.md": b"old", "reports/ternary-check.json": b"{}"}), VERSION,
                                 required)

    @unittest.skipUnless((ROOT / ".git").exists(), "needs a git checkout (the list comes from git ls-files)")
    def test_required_files_include_the_release_paths(self):
        required = tool.sdist_required(ROOT)
        for name in ("CHANGELOG.md", "LICENSE", "NOTICE", "fixtures/manifest.json", "schemas/ternary-check.v1.schema.json",
                     "reports/ternary-check.json", "reports/ternary-check.html", "ternary-check/action.yml",
                     "ternary-check/CONTRACT.md", "conformance/formats_onnx.json", "specs/formats/upstream.lock.json"):
            self.assertIn(name, required)
        self.assertTrue(any(name.startswith("reports/ternary-check/repro/") for name in required))


class MachOTest(unittest.TestCase):
    def test_parse_minos(self):
        text = ("Load command 9\n      cmd LC_BUILD_VERSION\n  cmdsize 32\n platform 1\n    minos 13.3\n"
                "      sdk 26.0\nLoad command 10\n      cmd LC_VERSION_MIN_MACOSX\n  cmdsize 16\n  version 11.0\n"
                "      sdk 12.0\nLoad command 11\n      cmd LC_SOURCE_VERSION\n  version 0.0\n")
        self.assertEqual(tool.parse_minos(text), ["13.3", "11.0"])
        self.assertLess(tool.version_tuple("13.3"), tool.version_tuple("14.0"))
        self.assertGreater(tool.version_tuple("13.10"), tool.version_tuple("13.3"))

    @unittest.skipUnless(platform.system() == "Darwin" and platform.machine() == "arm64" and shutil.which("cc")
                         and shutil.which("otool") and shutil.which("lipo"), "needs macOS arm64 with cc, otool, lipo")
    def test_minos_of_a_real_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            (tree / "main.c").write_text("int main(void) { return 0; }\n")
            subprocess.run(["cc", "-arch", "arm64", "-mmacosx-version-min=13.3", "main.c", "-o", "program"],
                           cwd=tree, check=True, capture_output=True)
            facts = tool.check_macho(tree, "13.3")
            self.assertEqual([(fact["file"], fact["minos"], fact["archs"]) for fact in facts],
                             [("program", ["13.3"], ["arm64"])])
            with self.assertRaisesRegex(tool.ReleaseError, "above the deployment target"):
                tool.check_macho(tree, "13.0")
            # Built for 13.3, a wheel would be tagged macosx_13_0: pip on 13.0-13.2 would install
            # a binary that does not load there, so the tag must be at or above every minos.
            with self.assertRaisesRegex(tool.ReleaseError, "needs 13.3"):
                tool.check_tag_covers_minos(f"{tool.DIST}-{VERSION}-py3-none-macosx_13_0_arm64.whl", facts)
            self.assertEqual(tool.check_tag_covers_minos(f"{tool.DIST}-{VERSION}-py3-none-macosx_14_0_arm64.whl",
                                                         facts), "14.0")

    def test_tag_must_cover_minos(self):
        facts = [{"file": "lib.dylib", "minos": ["14.0"]}, {"file": "cli", "minos": ["13.3", "14.0"]}]
        self.assertEqual(tool.check_tag_covers_minos(f"{tool.DIST}-{VERSION}-py3-none-macosx_14_0_arm64.whl", facts),
                         "14.0")
        for tag in ("macosx_13_0_arm64", "macosx_11_0_arm64"):
            with self.subTest(tag), self.assertRaises(tool.ReleaseError):
                tool.check_tag_covers_minos(f"{tool.DIST}-{VERSION}-py3-none-{tag}.whl", facts)


if __name__ == "__main__":
    unittest.main()
