#!/usr/bin/env python3
"""Build and check the release assets of one commit (issue #36). I/O only.

  python3 tools/build-release-assets.py --commit SHA --linux-wheel PATH \\
      (--ci-run-json FILE | --no-ci) [--out DIR] [--deployment-target 13.3] \\
      [--gates] [--skip-sdist-rebuild] [--python PY]

Run it on macOS arm64 from a checkout of this repository at SHA, with T27_ROOT
set to a checkout of gHashTag/t27 at native/compiler.lock, and a Python that
has setuptools>=68 and wheel>=0.43 (the wheel and sdist are built without
build isolation and without an index, so nothing is downloaded).

1. Refuses a commit that is not HEAD (full 40-hex SHA), a tree with tracked
   changes or untracked files (ignored files such as build/ are allowed), and
   an output directory that is not empty.
2. Linux wheel: PATH is the wheel of the CI run on the same commit (artifact
   native-python-3.12 of .github/workflows/ci.yml). Checked: the name matches
   what ternary-check/resolve-runtime.sh looks for, the dist-info version, the
   native manifest (schema, version, compiler pin, every file's sha256), ELF
   x86-64 binaries, and every trinity_memory/*.py equal to this commit's.
   --ci-run-json (from `gh run view RUN --json ...`, see below) must describe
   a completed, successful run of that same commit, every job successful.
3. With --gates: runs the four gates (sh tools/test-t27.sh; python -m unittest
   discover -s tests; sh tools/check-specs.sh; sh tools/fetch-upstream.sh then
   OFFLINE=1 make ternary-check-verify) and records their exit status.
4. macOS wheel: MACOSX_DEPLOYMENT_TARGET (default 13.3: native/float.cpp
   needs std::to_chars, available from macOS 13.3, and GitHub's macos-14 and
   macos-15 runners are newer) for sh tools/build-t27.sh and pip wheel. Every
   Mach-O file in the wheel must be arm64 with minos (otool -l:
   LC_BUILD_VERSION or LC_VERSION_MIN_MACOSX) at or below the target, the
   wheel's platform tag too; the same package checks as the Linux wheel; then
   tests/native/test_installed_wheel.py --rtl installs it outside the checkout.
5. sdist through the setuptools PEP 517 backend. Every tracked file under the
   release paths (CHANGELOG.md, LICENSE, NOTICE, fixtures/, schemas/, specs/,
   conformance/, ternary-check/, the Ternary Check reports, t27/, native/,
   tools/, tests/ and the build files) must be in it with this commit's bytes.
   Unless --skip-sdist-rebuild: the sdist is unpacked, built into a wheel from
   scratch (setup.py runs tools/build-t27.sh) and that wheel is installed and
   tested the same way.
6. Copies reports/ternary-check.json and reports/ternary-check.html, writes
   validation.json (versions, commit and tree, compiler, host, deployment
   target, every check above, gate and CI results, report digests), then
   SHA256SUMS over every asset ("<sha256>  <name>", sorted by name), and
   verifies it (and with `shasum -a 256 -c` when available).
7. Refuses to finish if HEAD or the tracked tree changed during the run.

CI run JSON, for example:
  gh run view RUN_ID --repo dmitrii-f-t27/trinity-memory \\
      --json databaseId,url,headSha,event,status,conclusion,workflowName,jobs > ci-run.json
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
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
import time
import zipfile

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = "trinity.release-validation.v1"
DIST = "trinity_ternary_memory"
LINUX_TAG = re.compile(r"-py3-none-(manylinux[0-9_]*_x86_64|linux_x86_64)\.whl$")
MACOS_TAG = re.compile(r"-py3-none-macosx_(\d+)_(\d+)_arm64\.whl$")
NATIVE = {"linux": ("libtrinity_memory_t27.so", "trinity-memory-t27"),
          "macos": ("libtrinity_memory_t27.dylib", "trinity-memory-t27")}
RUNTIME_FILES = ("codecs.wasm", "formats.wasm", "compiler.revision", "compiler.sha256")
# Tracked paths the source distribution must carry byte for byte.
SDIST_PATHS = ("CHANGELOG.md", "LICENSE", "NOTICE", "README.md", "README.ru.md", "MANIFEST.in", "Makefile",
               "pyproject.toml", "setup.py", "fixtures/", "schemas/", "specs/", "conformance/", "ternary-check/",
               "reports/ternary-check.json", "reports/ternary-check.html", "reports/ternary-check/", "t27/",
               "native/", "tools/", "tests/", "trinity_memory/", "rtl/", "scripts/", "docs/", "examples/",
               ".trinity/")
REPORTS = ("reports/ternary-check.json", "reports/ternary-check.html")
MACHO_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}


class ReleaseError(RuntimeError):
    """A check failed; the message says which."""


def log(message: str) -> None:
    print(f"[release] {message}", file=sys.stderr, flush=True)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if result.returncode:
        raise ReleaseError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def run(command, *, cwd: Path, env=None, log_path: Path | None = None) -> float:
    """Runs a command, its output to log_path (or inherited); fails on a nonzero exit."""
    started = time.monotonic()
    log(f"$ {' '.join(map(str, command))}")
    if log_path is None:
        result = subprocess.run(list(map(str, command)), cwd=cwd, env=env)
    else:
        with open(log_path, "wb") as sink:
            result = subprocess.run(list(map(str, command)), cwd=cwd, env=env, stdout=sink,
                                    stderr=subprocess.STDOUT)
    if result.returncode:
        tail = ""
        if log_path is not None:
            tail = "\n" + "\n".join(log_path.read_text(errors="replace").splitlines()[-40:])
        raise ReleaseError(f"{' '.join(map(str, command))} exited {result.returncode}{tail}")
    return round(time.monotonic() - started, 1)


# 1. Preflight -------------------------------------------------------------

def preflight(root: Path, commit: str) -> str:
    """Refuses anything but a clean tree at exactly `commit`; returns its tree SHA."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ReleaseError(f"--commit must be a full 40-hex commit SHA, got {commit!r}")
    head = git(root, "rev-parse", "HEAD").strip()
    if head != commit:
        raise ReleaseError(f"HEAD is {head}, not the release commit {commit}; check out the commit first")
    status = git(root, "status", "--porcelain", "--untracked-files=all")
    if status.strip():
        raise ReleaseError("the working tree is not clean (tracked changes or untracked files):\n" + status)
    return git(root, "rev-parse", "HEAD^{tree}").strip()


def project_version(root: Path) -> str:
    match = re.search(r'^version = "([^"]+)"$', (root / "pyproject.toml").read_text(), re.M)
    if not match:
        raise ReleaseError("pyproject.toml has no version")
    version = match.group(1)
    init = re.search(r'^__version__="([^"]+)"$', (root / "trinity_memory/__init__.py").read_text(), re.M)
    if not init or init.group(1) != version:
        raise ReleaseError(f"trinity_memory/__init__.py does not say {version}")
    return version


# 2. Wheels ------------------------------------------------------------------

def is_elf_x86_64(data: bytes) -> bool:
    # ELF magic, 64-bit class, little endian, e_machine EM_X86_64 (62).
    return len(data) >= 20 and data[:4] == b"\x7fELF" and data[4] == 2 and data[5] == 1 \
        and int.from_bytes(data[18:20], "little") == 62


def check_wheel(wheel: Path, root: Path, version: str, pin: str, kind: str) -> dict:
    """Package checks shared by both wheels; `kind` is "linux" or "macos"."""
    pattern = LINUX_TAG if kind == "linux" else MACOS_TAG
    if not (wheel.name.startswith(f"{DIST}-{version}-") and pattern.search(wheel.name)):
        raise ReleaseError(f"{wheel.name}: not a {kind} wheel name of {DIST} {version}")
    runtime = "trinity_memory/_native_runtime/"
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        dist_info = f"{DIST}-{version}.dist-info/"
        metadata = archive.read(dist_info + "METADATA").decode()
        if not re.search(rf"^Version: {re.escape(version)}$", metadata, re.M):
            raise ReleaseError(f"{wheel.name}: METADATA does not say Version: {version}")
        tags = re.findall(r"^Tag: (\S+)$", archive.read(dist_info + "WHEEL").decode(), re.M)
        if len(tags) != 1 or not wheel.name.endswith(f"-{tags[0]}.whl"):
            raise ReleaseError(f"{wheel.name}: WHEEL tags {tags} do not match the file name")
        manifest = json.loads(archive.read(runtime + "manifest.json"))
        if manifest.get("schema") != "trinity.native-wheel.v1" or manifest.get("version") != version:
            raise ReleaseError(f"{wheel.name}: native manifest is {manifest.get('schema')} "
                               f"version {manifest.get('version')}, expected {version}")
        if manifest.get("compiler_revision") != pin:
            raise ReleaseError(f"{wheel.name}: compiler {manifest.get('compiler_revision')} is not the pin {pin}")
        files = manifest.get("files") or {}
        for name in (*NATIVE[kind], *RUNTIME_FILES):
            if name not in files:
                raise ReleaseError(f"{wheel.name}: native manifest lacks {name}")
        for name, digest in files.items():
            member = runtime + name
            if member not in names or sha256_bytes(archive.read(member)) != digest:
                raise ReleaseError(f"{wheel.name}: {member} does not match its manifest sha256")
        if archive.read(runtime + "compiler.revision").decode().strip() != pin:
            raise ReleaseError(f"{wheel.name}: compiler.revision is not the pin")
        if kind == "linux":
            for name in NATIVE["linux"]:
                if not is_elf_x86_64(archive.read(runtime + name)[:64]):
                    raise ReleaseError(f"{wheel.name}: {name} is not an ELF x86-64 file")
        for name in ("codecs.wasm", "formats.wasm"):
            if archive.read(runtime + name)[:8] != b"\0asm\1\0\0\0":
                raise ReleaseError(f"{wheel.name}: {name} is not WebAssembly")
        sources = {path.relative_to(root).as_posix() for path in (root / "trinity_memory").glob("*.py")}
        packaged = {name for name in names if re.fullmatch(r"trinity_memory/[^/]+\.py", name)}
        if packaged != sources:
            raise ReleaseError(f"{wheel.name}: Python modules differ from this commit: "
                               f"missing {sorted(sources - packaged)}, extra {sorted(packaged - sources)}")
        for name in sorted(sources):
            if archive.read(name) != (root / name).read_bytes():
                raise ReleaseError(f"{wheel.name}: {name} differs from this commit")
    return {"name": wheel.name, "sha256": sha256_file(wheel), "bytes": wheel.stat().st_size,
            "tag": tags[0], "native_files": len(files), "python_modules_equal_commit": len(sources),
            "compiler_revision": pin}


def parse_minos(otool_text: str) -> list[str]:
    """macOS minimum versions in `otool -l` output: LC_BUILD_VERSION minos, LC_VERSION_MIN_MACOSX version."""
    found, command = [], None
    for line in otool_text.splitlines():
        words = line.split()
        if len(words) >= 2 and words[0] == "cmd":
            command = words[1]
        elif len(words) >= 2 and command == "LC_BUILD_VERSION" and words[0] == "minos":
            found.append(words[1])
        elif len(words) >= 2 and command == "LC_VERSION_MIN_MACOSX" and words[0] == "version":
            found.append(words[1])
    return found


def version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def macho_facts(path: Path) -> dict:
    """Architectures (lipo) and minimum macOS versions (otool) of one Mach-O file."""
    listing = subprocess.run(["otool", "-arch", "all", "-l", str(path)], capture_output=True, text=True)
    if listing.returncode:
        raise ReleaseError(f"otool -l {path.name} failed: {listing.stderr.strip()}")
    archs = subprocess.run(["lipo", "-archs", str(path)], capture_output=True, text=True)
    if archs.returncode:
        raise ReleaseError(f"lipo -archs {path.name} failed: {archs.stderr.strip()}")
    return {"minos": parse_minos(listing.stdout), "archs": archs.stdout.split()}


def check_macho(tree: Path, target: str) -> list[dict]:
    """Every Mach-O file under `tree` must be arm64 only, with every minos at or below `target`."""
    facts = []
    for path in sorted(p for p in tree.rglob("*") if p.is_file()):
        with open(path, "rb") as handle:
            if handle.read(4) not in MACHO_MAGIC:
                continue
        fact = {"file": path.relative_to(tree).as_posix(), **macho_facts(path)}
        if fact["archs"] != ["arm64"]:
            raise ReleaseError(f"{fact['file']}: architectures {fact['archs']}, expected arm64 only")
        if not fact["minos"]:
            raise ReleaseError(f"{fact['file']}: no LC_BUILD_VERSION or LC_VERSION_MIN_MACOSX")
        high = [v for v in fact["minos"] if version_tuple(v) > version_tuple(target)]
        if high:
            raise ReleaseError(f"{fact['file']}: minos {high} is above the deployment target {target}")
        facts.append(fact)
    if not facts:
        raise ReleaseError("no Mach-O file found in the macOS wheel")
    return facts


def build_macos_wheel(root: Path, work: Path, python: str, env: dict, target: str) -> Path:
    for stale in [root / "build/lib", *root.glob("build/bdist.*")]:
        shutil.rmtree(stale, ignore_errors=True)
    run(["sh", "tools/build-t27.sh"], cwd=root, env=env, log_path=work / "build-t27-macos.log")
    wheels = work / "macos-wheel"
    wheels.mkdir()
    run([python, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--no-index", ".",
         "--wheel-dir", wheels], cwd=root, env=env, log_path=work / "pip-wheel-macos.log")
    built = sorted(wheels.glob("*.whl"))
    if len(built) != 1:
        raise ReleaseError(f"expected one wheel, pip wrote {[p.name for p in built]}")
    match = MACOS_TAG.search(built[0].name)
    if not match:
        raise ReleaseError(f"{built[0].name} is not a macOS arm64 wheel")
    if (int(match.group(1)), int(match.group(2))) > version_tuple(target)[:2]:
        raise ReleaseError(f"{built[0].name}: platform tag above the deployment target {target}")
    return built[0]


def installed_wheel_test(root: Path, wheel: Path, python: str, env: dict, output: Path) -> dict:
    run([python, "tests/native/test_installed_wheel.py", "--wheel", wheel, "--rtl", "--output", output],
        cwd=root, env=env, log_path=output.with_suffix(".log"))
    return json.loads(output.read_text())


# 5. Source distribution -------------------------------------------------------

def sdist_required(root: Path) -> dict[str, str]:
    """Tracked files the sdist must carry, with this commit's sha256."""
    tracked = git(root, "ls-files", "-z").split("\0")
    selected = [name for name in tracked if name and any(
        name == path or (path.endswith("/") and name.startswith(path)) for path in SDIST_PATHS)]
    return {name: sha256_file(root / name) for name in selected}


def check_sdist(sdist: Path, version: str, required: dict[str, str]) -> dict:
    prefix = f"{DIST}-{version}/"
    if sdist.name != f"{DIST}-{version}.tar.gz":
        raise ReleaseError(f"unexpected sdist name {sdist.name}")
    members = {}
    with tarfile.open(sdist, "r:gz") as archive:
        for member in archive.getmembers():
            inside = member.name.startswith(prefix) and ".." not in Path(member.name).parts
            if member.name != prefix.rstrip("/") and not inside:
                raise ReleaseError(f"{sdist.name}: member {member.name} outside {prefix}")
            if member.isfile():
                members[member.name[len(prefix):]] = sha256_bytes(archive.extractfile(member).read())
    missing = sorted(name for name in required if name not in members)
    differ = sorted(name for name, digest in required.items() if name in members and members[name] != digest)
    if missing or differ:
        raise ReleaseError(f"{sdist.name}: missing {missing[:20]}{'...' if len(missing) > 20 else ''}, "
                           f"differing {differ[:20]}")
    if "PKG-INFO" not in members:
        raise ReleaseError(f"{sdist.name}: no PKG-INFO")
    return {"name": sdist.name, "files": len(members), "tracked_files_checked": len(required)}


def build_sdist(root: Path, out: Path, python: str, env: dict, work: Path) -> Path:
    code = "import sys; from setuptools import build_meta as b; print(b.build_sdist(sys.argv[1]))"
    run([python, "-c", code, out], cwd=root, env=env, log_path=work / "sdist.log")
    built = sorted(out.glob("*.tar.gz"))
    if len(built) != 1:
        raise ReleaseError(f"expected one sdist, found {[p.name for p in built]}")
    return built[0]


def rebuild_from_sdist(root: Path, sdist: Path, python: str, env: dict, work: Path) -> dict:
    """Unpacks the sdist, builds a wheel from scratch (setup.py runs tools/build-t27.sh), tests it."""
    unpacked = work / "sdist-rebuild"
    with tarfile.open(sdist, "r:gz") as archive:
        if hasattr(tarfile, "data_filter"):
            archive.extractall(unpacked, filter="data")
        else:  # Python before 3.12: the archive is the one this run just built
            archive.extractall(unpacked)
    source = next(unpacked.iterdir())
    wheels = work / "sdist-rebuild-wheel"
    wheels.mkdir()
    seconds = run([python, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--no-index", source,
                   "--wheel-dir", wheels], cwd=source, env=env, log_path=work / "sdist-rebuild.log")
    built = sorted(wheels.glob("*.whl"))
    if len(built) != 1:
        raise ReleaseError(f"sdist rebuild wrote {[p.name for p in built]}")
    test = installed_wheel_test(root, built[0], python, env, work / "sdist-rebuild-installed.json")
    return {"wheel": built[0].name, "sha256": sha256_file(built[0]), "seconds": seconds,
            "installed_test": test}


# 3. Gates, CI -----------------------------------------------------------------

def run_gates(root: Path, python: str, env: dict, work: Path) -> list[dict]:
    gates = [("sh tools/test-t27.sh", ["sh", "tools/test-t27.sh"], {}),
             ("python -m unittest discover -s tests", [python, "-m", "unittest", "discover", "-s", "tests"], {}),
             ("sh tools/check-specs.sh", ["sh", "tools/check-specs.sh"], {}),
             ("sh tools/fetch-upstream.sh", ["sh", "tools/fetch-upstream.sh"], {}),
             ("OFFLINE=1 make ternary-check-verify", ["make", "ternary-check-verify", "OFFLINE=1",
                                                        f"PYTHON={python}"], {"OFFLINE": "1"})]
    results = []
    for index, (label, command, extra) in enumerate(gates):
        seconds = run(command, cwd=root, env={**env, "PYTHON": python, **extra},
                      log_path=work / f"gate-{index}.log")
        results.append({"command": label, "exit": 0, "seconds": seconds})
    return results


def check_ci_run(path: Path, commit: str) -> dict:
    run_info = json.loads(path.read_text())
    if run_info.get("headSha") != commit:
        raise ReleaseError(f"CI run {run_info.get('url')} is for {run_info.get('headSha')}, not {commit}")
    if run_info.get("status") != "completed" or run_info.get("conclusion") != "success":
        raise ReleaseError(f"CI run {run_info.get('url')}: {run_info.get('status')} / {run_info.get('conclusion')}")
    jobs = [{"name": job.get("name"), "conclusion": job.get("conclusion")} for job in run_info.get("jobs") or []]
    failed = [job for job in jobs if job["conclusion"] != "success"]
    if not jobs or failed:
        raise ReleaseError(f"CI run {run_info.get('url')}: jobs not all successful: {failed or 'no jobs listed'}")
    return {"url": run_info.get("url"), "run_id": run_info.get("databaseId"), "workflow": run_info.get("workflowName"),
            "event": run_info.get("event"), "head_sha": commit, "conclusion": "success", "jobs": jobs}


# 6. Checksums -------------------------------------------------------------------

def write_checksums(out: Path) -> Path:
    names = sorted(p.name for p in out.iterdir() if p.is_file() and p.name != "SHA256SUMS")
    if not names:
        raise ReleaseError(f"{out} holds no assets")
    lines = "".join(f"{sha256_file(out / name)}  {name}\n" for name in names)
    target = out / "SHA256SUMS"
    target.write_text(lines)
    return target


def verify_checksums(out: Path) -> int:
    listed = {}
    for line in (out / "SHA256SUMS").read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (\S+)", line)
        if not match:
            raise ReleaseError(f"SHA256SUMS: malformed line {line!r}")
        listed[match.group(2)] = match.group(1)
    present = {p.name for p in out.iterdir() if p.is_file() and p.name != "SHA256SUMS"}
    if set(listed) != present:
        raise ReleaseError(f"SHA256SUMS lists {sorted(listed)}, the directory holds {sorted(present)}")
    for name, digest in listed.items():
        if sha256_file(out / name) != digest:
            raise ReleaseError(f"SHA256SUMS: {name} does not match")
    if shutil.which("shasum"):
        result = subprocess.run(["shasum", "-a", "256", "-c", "SHA256SUMS"], cwd=out, capture_output=True, text=True)
        if result.returncode:
            raise ReleaseError(f"shasum -a 256 -c SHA256SUMS failed:\n{result.stdout}{result.stderr}")
    return len(listed)


# Main --------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--commit", required=True, help="full SHA of the release commit; must be HEAD")
    parser.add_argument("--linux-wheel", required=True, type=Path, help="the Linux wheel from CI for this commit")
    ci = parser.add_mutually_exclusive_group(required=True)
    ci.add_argument("--ci-run-json", type=Path, help="`gh run view --json ...` of the CI run of this commit")
    ci.add_argument("--no-ci", action="store_true", help="dry run without CI evidence (not for publishing)")
    parser.add_argument("--out", type=Path, help="output directory (default build/release-VERSION); must be empty")
    parser.add_argument("--deployment-target", default="13.3", help="MACOSX_DEPLOYMENT_TARGET (default 13.3)")
    parser.add_argument("--gates", action="store_true", help="run the four gates first and record them")
    parser.add_argument("--skip-sdist-rebuild", action="store_true", help="do not rebuild a wheel from the sdist")
    parser.add_argument("--python", default=sys.executable, help="Python with setuptools and wheel")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        return build(args)
    except ReleaseError as error:
        print(f"build-release-assets: {error}", file=sys.stderr)
        return 1


def build(args) -> int:
    root = args.root.resolve()
    tree = preflight(root, args.commit)
    version = project_version(root)
    pin = (root / "native/compiler.lock").read_text().strip()
    out = (args.out or root / "build" / f"release-{version}").resolve()
    if out.exists() and any(out.iterdir()):
        raise ReleaseError(f"{out} is not empty")
    linux_source = args.linux_wheel.resolve()
    if not linux_source.is_file():
        raise ReleaseError(f"no Linux wheel at {linux_source}")
    ci_run = check_ci_run(args.ci_run_json, args.commit) if args.ci_run_json else None
    if (platform.system(), platform.machine()) != ("Darwin", "arm64"):
        raise ReleaseError("the macOS arm64 wheel is built here: run this on macOS arm64")
    if not os.environ.get("T27_ROOT"):
        raise ReleaseError("set T27_ROOT to a checkout of gHashTag/t27 at native/compiler.lock")
    target = args.deployment_target
    version_tuple(target)
    linux = check_wheel(linux_source, root, version, pin, "linux")
    log(f"Linux wheel {linux['name']}: package checks pass")

    env = {**os.environ, "MACOSX_DEPLOYMENT_TARGET": target}
    env.pop("PYTHONPATH", None)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="trinity-release-") as scratch:
        work = Path(scratch)
        gates = run_gates(root, args.python, env, work) if args.gates else None
        macos_built = build_macos_wheel(root, work, args.python, env, target)
        macos = check_wheel(macos_built, root, version, pin, "macos")
        unpacked = work / "macos-unpacked"
        with zipfile.ZipFile(macos_built) as archive:
            archive.extractall(unpacked)
        macos["macho"] = check_macho(unpacked, target)
        macos["installed_test"] = installed_wheel_test(root, macos_built, args.python, env,
                                                       work / "installed-macos-wheel.json")
        log(f"macOS wheel {macos['name']}: {len(macos['macho'])} Mach-O files at or below {target}, installed test passed")

        sdist = build_sdist(root, work / "sdist", args.python, env, work)
        sdist_facts = check_sdist(sdist, version, sdist_required(root))
        sdist_facts["sha256"] = sha256_file(sdist)
        sdist_facts["rebuild"] = None if args.skip_sdist_rebuild else rebuild_from_sdist(
            root, sdist, args.python, env, work)
        log(f"sdist {sdist.name}: {sdist_facts['tracked_files_checked']} tracked files present with this commit's bytes")

        shutil.copy2(linux_source, out / linux_source.name)
        shutil.copy2(macos_built, out / macos_built.name)
        shutil.copy2(sdist, out / sdist.name)
        reports = {}
        for name in REPORTS:
            data = (root / name).read_bytes()
            if sha256_bytes(data) != sha256_bytes(subprocess.run(
                    ["git", "-C", str(root), "show", f"{args.commit}:{name}"], capture_output=True, check=True).stdout):
                raise ReleaseError(f"{name} differs from the commit")
            (out / Path(name).name).write_bytes(data)
            reports[Path(name).name] = {"source": name, "sha256": sha256_bytes(data), "bytes": len(data)}
        summary = json.loads((root / REPORTS[0]).read_text())["summary"]
        reports["ternary-check.json"]["summary"] = {"cells": summary["cells"], "status": summary["status"],
                                                    "provenance": summary["provenance"]}

        compiler_sha = (root / "build/t27/compiler.sha256").read_text().split()[0]
        validation = {
            "schema": SCHEMA,
            "version": version,
            "tag": f"v{version}",
            "source_commit": args.commit,
            "source_tree": tree,
            "compiler": {"repo": "gHashTag/t27", "revision": pin, "t27c_sha256": compiler_sha},
            "built_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "host": {"platform": platform.platform(), "macos": platform.mac_ver()[0], "machine": platform.machine(),
                     "python": platform.python_version()},
            "macos_deployment_target": target,
            "linux_wheel": {**linux, "source": "CI artifact native-python-3.12 of the run in ci"},
            "macos_wheel": macos,
            "sdist": sdist_facts,
            "reports": reports,
            "gates": gates,
            "ci": ci_run,
            "ci_verified": ci_run is not None,
        }
        (out / "validation.json").write_text(json.dumps(validation, indent=2, sort_keys=False) + "\n")
    write_checksums(out)
    count = verify_checksums(out)
    if git(root, "rev-parse", "HEAD").strip() != args.commit or \
            git(root, "status", "--porcelain", "--untracked-files=all").strip():
        raise ReleaseError("HEAD or the tracked tree changed during the build; discard the output")
    log(f"{count} assets in {out}, SHA256SUMS verified")
    for name in sorted(p.name for p in out.iterdir()):
        print(out / name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
