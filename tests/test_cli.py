import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from trinity_memory.benchmark import run_benchmark
from trinity_memory.cli import main


class CliTests(unittest.TestCase):
    def test_pack_inspect_unpack_and_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, binary, output, memory = [root / p for p in ("in.json", "out.tmem", "out.json", "out.mem")]
            values = [-1, 0, 1, -1, 1, 0, 1]
            source.write_text(json.dumps(values))
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                self.assertEqual(main(["pack", str(source), str(binary)]), 0)
                self.assertEqual(main(["inspect", str(binary)]), 0)
                self.assertEqual(main(["unpack", str(binary), str(output)]), 0)
                self.assertEqual(main(["export-rtl", str(source), str(memory)]), 0)
            self.assertEqual(json.loads(output.read_text()), values)
            self.assertEqual(memory.read_text().splitlines()[0], "b7")
            self.assertIn('"validated": true', captured.getvalue())

    def test_error_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "bad.json", root / "bad.tmem"
            source.write_text("[1,1,1,1]")
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(main(["pack", str(source), str(output), "--codec", "sparse41"]), 2)
            self.assertFalse(output.exists())
            self.assertIn("at most 1 nonzeros", errors.getvalue())

    def test_benchmark_sizes_and_reproducible_data(self):
        report = run_benchmark(count=88, repeats=1)
        self.assertEqual(len(report["datasets"]), 3)
        for dataset in report["datasets"]:
            for row in dataset["results"]:
                self.assertTrue(row["roundtrip"])
                self.assertEqual(row["container_bytes"], row["payload_bytes"] + 24)
                self.assertEqual(row["payload_bpw"], 8 * row["payload_bytes"] / 88)
        dense = {r["codec"]: r for r in report["datasets"][0]["results"]}
        self.assertEqual(dense["baseline2"]["payload_bytes"], 22)
        self.assertEqual(dense["dense5"]["payload_bytes"], 18)
        self.assertEqual(dense["dense22"]["payload_bytes"], 18)
        self.assertEqual(report["datasets"][1]["zero_fraction"], 0.75)
        for count, repeats in ((0, 1), (1, 0), (-1, 3)):
            with self.assertRaises(ValueError):
                run_benchmark(count, repeats)


if __name__ == "__main__":
    unittest.main()
