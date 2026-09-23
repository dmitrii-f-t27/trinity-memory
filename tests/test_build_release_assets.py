"""tools/build-release-assets.py without building a release.

The Linux wheel comes from CI, so here it is a stand-in: a zip with the real
dist-info layout, this checkout's trinity_memory/*.py, a native manifest and
files whose first bytes are an ELF x86-64 header. The package checks must
accept it and refuse each single defect. The preflight is run against a
temporary git repository; SHA256SUMS writing and verification, the sdist
member check, the otool parser and, on macOS with a C compiler, the minos
check of a real Mach-O file are tested directly. The macOS wheel build itself
runs only in a real release (see the tool's docstring).
"""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
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
                     name=None, elf=ELF_X86_64, drop=None, source_edit=None) -> Path:
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
        self.assertRegex(f"{tool.DIST}-{VERSION}-py3-none-macosx_13_0_arm64.whl", tool.MACOS_TAG)


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


if __name__ == "__main__":
    unittest.main()
