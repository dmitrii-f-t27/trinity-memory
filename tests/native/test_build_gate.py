"""Run the production build gate against a source the parser misinterprets."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.environ.get("T27_ROOT"), "T27_ROOT required for compiler gate")
class BuildGate(unittest.TestCase):
    def test_lexer_discard_fails_before_codegen(self):
        with tempfile.TemporaryDirectory(prefix="trinity-lexer-gate-") as directory:
            checkout = Path(directory)
            for name in ("tools", "native", "t27"):
                (checkout / name).mkdir()
            shutil.copy2(ROOT / "tools/build-t27.sh", checkout / "tools/build-t27.sh")
            shutil.copy2(ROOT / "native/compiler.lock", checkout / "native/compiler.lock")
            source = checkout / "t27/codecs.t27"
            source.write_text(
                "module Review { fn sign() -> i32 { return \u22121; } }\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["T27_ROOT"] = str(Path(environment["T27_ROOT"]).resolve())
            compiler = Path(environment["T27_ROOT"]) / "target/release/t27c"
            parser = subprocess.run(
                [str(compiler), "parse-complete", "--show", str(source)],
                capture_output=True, text=True, check=True,
            )
            # This fixture demonstrates why the parser's gate is insufficient.
            self.assertIn("nothing discarded", parser.stdout)
            result = subprocess.run(
                ["sh", str(checkout / "tools/build-t27.sh")],
                cwd=checkout, env=environment, capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Source lexer discarded characters", result.stderr)
            self.assertRegex(result.stderr, r"3\s+TOTAL across 1 spec\(s\)")
            self.assertFalse((checkout / "build/t27/codecs.h").exists())


if __name__ == "__main__":
    unittest.main()
