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
            self.assertIsNone(row["consumer_stalls_per_run"])
            self.assertIsNone(row["consumer_stalls_percent"])
            self.assertEqual(row["wait_stalls_per_run"], 20)
            self.assertEqual(row["wait_stalls_percent"], 10)
            self.assertEqual(row["issue_hold_cycles_percent"], 75)
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
        self.assertAlmostEqual(dense["wait_stalls_percent"], 0.246482, places=5)
        self.assertAlmostEqual(dense["issue_hold_cycles_percent"], 99.944, places=2)
        self.assertGreater(baseline["wait_stalls_percent"], 0.30)
        self.assertLess(baseline["wait_stalls_percent"], 0.31)
        self.assertIsNone(dense["consumer_stalls_percent"])
        self.assertIsNone(baseline["consumer_stalls_percent"])
        self.assertEqual(dense["arithmetic_weights_per_s"], 4_800_000_000)
        self.assertEqual(baseline["arithmetic_weights_per_s"], 3_840_000_000)
        self.assertNotEqual(dense["cycles_mean"], tool.load("measure_d5")["runs"][0]["cycles"])

    def test_golden_uart_lines_recover_actual_consumer_stalls(self):
        gold = tool.build()["measurement"]["counter_correction"]["golden_runs"]
        self.assertEqual(gold["dense5"]["consumer_stalls"], 19)
        self.assertEqual(gold["baseline2"]["consumer_stalls"], 65)
        self.assertEqual(gold["dense5"]["wait_stalls"], 81932)
        self.assertEqual(gold["dense5"]["issue_hold_cycles"], 33218427)

    def test_summary_rejects_missing_duplicate_and_invalid_counters(self):
        lines = tool.load("golden_d5")["summary_lines"]
        bad_cases = [lines[:-1], lines + [lines[0]],
                     [line if line[0] != "c" else ["c", 1, 0] for line in lines],
                     [line if line[0] != "n" else ["n", 0, -1] for line in lines]]
        for bad in bad_cases:
            with self.subTest(lines=bad), self.assertRaises(ValueError):
                tool.decode_run_summary(bad)

    def test_v2_replays_raw_lines_and_v1_does_not_invent_missing_counters(self):
        lines = tool.load("golden_d5")["summary_lines"]
        fresh = {"schema": "trinity.fpga-ddr3-matvec-measure.v2",
                 "runs": [{"summary_lines": lines, "consumer_stalls": 123456}]}
        self.assertEqual(tool.corrected_runs(fresh)[0]["consumer_stalls"], 19)
        old = tool.load("measure_d5")
        corrected = tool.corrected_runs(old)
        self.assertIsNone(corrected[0]["consumer_stalls"])
        self.assertEqual(old["runs"][0]["consumer_stalls"], 81931)

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
