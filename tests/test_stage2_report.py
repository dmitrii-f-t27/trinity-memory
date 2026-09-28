"""The published stage-2 numbers must follow every captured run, not literals."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("stage2_report", ROOT / "tools/stage2-report.py")
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


class Stage2ReportTest(unittest.TestCase):
    def test_means_and_percentages_use_all_runs_and_ignore_stale_summary(self):
        original_load = tool.load

        def load(name):
            data = copy.deepcopy(original_load(name))
            if name.startswith("measure_"):
                data["runs"] = [
                    {"cycles": 100, "weights_per_s": 40, "consumer_stalls": 10,
                     "wait_stalls": 50, "command_stalls": 2},
                    {"cycles": 300, "weights_per_s": 20, "consumer_stalls": 30,
                     "wait_stalls": 250, "command_stalls": 6},
                ]
                # Retain the original summary: it must not control the result.
            return data

        with patch.object(tool, "load", side_effect=load):
            report = tool.build()
        for row in report["measurement"]["rows"]:
            self.assertEqual(row["runs"], 2)
            self.assertEqual(row["cycles_mean"], 200)
            self.assertEqual(row["consumer_stalls_per_run"], 20)
            self.assertEqual(row["consumer_stalls_percent"], 10)
            self.assertEqual(row["wait_stalls_percent"], 75)
            self.assertEqual(row["command_stalls_percent"], 2)
            self.assertEqual(row["weights_per_s_mean"], 30)
            self.assertEqual(row["weights_per_s_sd"], 14.1)
        html = tool.render(report)
        self.assertIn("10.0000%", html)
        self.assertIn("75.0000%", html)
        self.assertNotIn("99.98%", html)
        self.assertNotRegex(html, r"__[A-Z0-9_]+__")

    def test_committed_captures_have_correct_stall_percentages(self):
        report = tool.build()
        dense, baseline = report["measurement"]["rows"]
        self.assertEqual(dense["runs"], 12)
        self.assertAlmostEqual(dense["consumer_stalls_percent"], 0.246482, places=5)
        self.assertAlmostEqual(dense["wait_stalls_percent"], 99.944, places=2)
        self.assertGreater(baseline["consumer_stalls_percent"], 0.30)
        self.assertLess(baseline["consumer_stalls_percent"], 0.31)
        self.assertNotEqual(dense["cycles_mean"], tool.load("measure_d5")["runs"][0]["cycles"])

    def test_check_accepts_published_report(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(tool.check(tool.build()))

    def test_check_rejects_stale_json_html_and_documentation(self):
        report = tool.build()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            doc = out / "hardware.md"
            valid = {"stage2.json": json.dumps(report), "index.html": tool.render(report),
                     "hardware.md": tool.DOC_START + "\n" + tool.measurement_markdown(report) + tool.DOC_END}
            with patch.object(tool, "OUT", out), patch.object(tool, "DOC", doc):
                for corrupt in valid:
                    with self.subTest(file=corrupt):
                        for name, text in valid.items():
                            (out / name).write_text(text)
                        if corrupt == "stage2.json":
                            changed = copy.deepcopy(report)
                            changed["measurement"]["rows"][1]["weights_per_s_mean"] += 50
                            (out / corrupt).write_text(json.dumps(changed))
                        else:
                            (out / corrupt).write_text(valid[corrupt].replace("99.944", "99.123"))
                        with contextlib.redirect_stdout(io.StringIO()):
                            self.assertFalse(tool.check(report))

    def test_document_update_preserves_unrelated_sections(self):
        report = tool.build()
        text = "before\n" + tool.DOC_START + "old numbers" + tool.DOC_END + "\nafter"
        changed = tool.update_document(text, report)
        self.assertTrue(changed.startswith("before\n"))
        self.assertTrue(changed.endswith("\nafter"))
        self.assertNotIn("old numbers", changed)
        self.assertEqual(tool.update_document(changed, report), changed)


if __name__ == "__main__":
    unittest.main()
