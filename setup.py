"""Platform-wheel assembly for generated t27 artifacts; no fallback backend."""
from hashlib import sha256
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys

from setuptools import Distribution, setup
from setuptools.command.build_py import build_py
from wheel.bdist_wheel import bdist_wheel

ROOT = Path(__file__).resolve().parent


def native_artifacts():
    directory = ROOT / "build" / "t27"
    library = "libtrinity_memory_t27." + ("dylib" if sys.platform == "darwin" else "so")
    required = [directory / library, directory / "trinity-memory-t27", directory / "compiler.revision",
                directory / "compiler.sha256", directory / "SHA256SUMS",
                directory / "rtl/resources/trinity_dot_stream.v"]
    if not all(path.is_file() for path in required):
        if not os.environ.get("T27_ROOT"):
            raise RuntimeError("Native artifacts are missing. Build tools/build-t27.sh with the pinned T27_ROOT, or install a platform wheel. Python fallback is not provided.")
        subprocess.run(["sh", "tools/build-t27.sh"], cwd=ROOT, check=True)
    pin = (ROOT / "native/compiler.lock").read_text().strip()
    if (directory / "compiler.revision").read_text().strip() != pin:
        raise RuntimeError("Native artifact compiler revision does not match native/compiler.lock")
    # Audit all source and generated output entries, so a wheel cannot silently
    # package an old binary after a t27/native source edit.
    entries = {}
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        name = name.lstrip("*")
        path = ROOT / name
        if not path.is_file() or sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"Native build provenance is stale at {name}; rebuild tools/build-t27.sh")
        entries[name] = digest
    for source in [*ROOT.glob("t27/*.t27"), *ROOT.glob("t27/rtl/*.t27"),
                   *ROOT.glob("native/*.c"), *ROOT.glob("native/*.cpp"), *ROOT.glob("native/*.h")]:
        if source.relative_to(ROOT).as_posix() not in entries:
            raise RuntimeError(f"Native source was added after the build: {source.name}; rebuild")
    return directory, library, pin


class NativeBuild(build_py):
    def run(self):
        directory, library, pin = native_artifacts()
        super().run()
        target = Path(self.build_lib) / "trinity_memory" / "_native_runtime"
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        for name in (library, "trinity-memory-t27", "compiler.revision", "compiler.sha256", "codecs.wasm"):
            shutil.copy2(directory / name, target / name)
        shutil.copytree(directory / "rtl/resources", target / "rtl/resources")
        records = {path.relative_to(target).as_posix(): sha256(path.read_bytes()).hexdigest()
                   for path in sorted(target.rglob("*")) if path.is_file()}
        manifest = {"schema": "trinity.native-wheel.v1", "compiler_revision": pin,
                    "version": "0.3.0", "files": records}
        (target / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")


class PlatformDistribution(Distribution):
    def has_ext_modules(self):
        return True


class PlatformWheel(bdist_wheel):
    def finalize_options(self):
        super().finalize_options()
        self.root_is_pure = False

    def get_tag(self):
        _, _, platform = super().get_tag()
        if sys.platform == "darwin":
            artifacts = ROOT / "build/t27"
            architectures = []
            for name in ("libtrinity_memory_t27.dylib", "trinity-memory-t27"):
                output = subprocess.check_output(["/usr/bin/lipo", "-archs", str(artifacts/name)], text=True)
                architectures.append(set(output.split()))
            if architectures[0] != architectures[1]:
                raise RuntimeError("Native CLI/library architectures differ")
            selected = architectures[0]
            if selected == {"arm64", "x86_64"}: architecture = "universal2"
            elif selected in ({"arm64"}, {"x86_64"}): architecture = next(iter(selected))
            else: raise RuntimeError(f"Unsupported native Mach-O architectures: {selected}")
            platform = re.sub(r"(universal2|arm64|x86_64)$", architecture, platform)
        return "py3", "none", platform


setup(cmdclass={"build_py": NativeBuild, "bdist_wheel": PlatformWheel},
      distclass=PlatformDistribution)
