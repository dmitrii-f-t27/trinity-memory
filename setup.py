"""Include canonical RTL sources in wheels without keeping a second source copy."""
from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithRTL(build_py):
    def run(self):
        super().run()
        root = Path(__file__).resolve().parent
        for name in ("trinity_dot_stream.v", "tb_dot_stream.v",
                     "generated/ternary_dense5_decoder.v", "ternary_baseline5_decoder.v"):
            destination = Path(self.build_lib) / "trinity_memory" / "rtl" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / "rtl" / name, destination)


setup(cmdclass={"build_py": BuildWithRTL})
