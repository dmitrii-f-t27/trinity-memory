"""tools/ddr3-weights-per-second.py (issue #65): the committed summary is recomputed from the
committed captures of #62's build of record; a changed figure fails the check; a load whose
calibration did not hold, a failed run and a transcript that does not give its record's counters
are excluded (and the last two reported as problems); consumer (B) rows from records of
tools/fpga-matvec-capture.py: the statistic, the exclusions, the ratio and the verdict."""
from __future__ import annotations

import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import shutil
import statistics
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / "reports/fpga/ddr3-weights-per-second-2026-09-25.json"
READER = ROOT / "reports/fpga/ddr3-reader-2026-09-24-42b6f5a9-x16-seed11"
RUNS = {"load1": 515, "load2": 516, "load3": 518}


def load_tool():
    spec = importlib.util.spec_from_file_location("ddr3_weights_per_second", ROOT / "tools/ddr3-weights-per-second.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_gz(path: Path):
    return json.loads(gzip.decompress(path.read_bytes()))


def write_gz(path: Path, record) -> None:
    path.write_bytes(gzip.compress(json.dumps(record, indent=1).encode(), mtime=0))


class WeightsPerSecond(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def work(self) -> Path:
        work = Path(tempfile.mkdtemp(prefix="trinity-wps-"))
        self.addCleanup(shutil.rmtree, work)
        return work

    def run_tool(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = self.tool.main([str(a) for a in argv])
        return code, out.getvalue()

    def summarize(self, *argv):
        out = self.work() / "summary.json"
        code, text = self.run_tool(*argv, "--output", out)
        return code, json.loads(out.read_text()), text

    def reader_copy(self) -> Path:
        target = self.work() / READER.name
        shutil.copytree(READER, target)
        return target

    def edit_transcript(self, load: Path, change, update_sha: bool) -> None:
        """Change the load's transcript (uart.tsv.gz), with or without its record's sha256."""
        path = load / "uart.tsv.gz"
        lines = gzip.decompress(path.read_bytes()).decode().splitlines(keepends=True)
        change(lines)
        data = "".join(lines).encode()
        path.write_bytes(gzip.compress(data, mtime=0))
        if update_sha:
            record = read_gz(load / "capture.json.gz")
            record["uart_transcript"]["sha256"] = hashlib.sha256(data).hexdigest()
            write_gz(load / "capture.json.gz", record)

    # --- the committed summary --------------------------------------------------------------

    def test_the_committed_summary_is_reproduced(self):
        code, text = self.run_tool("--check", SUMMARY)
        self.assertEqual(code, 0, text)
        self.assertIn("reproduced", text)

    def test_the_committed_figures(self):
        summary = json.loads(SUMMARY.read_text())
        self.assertEqual(summary["inputs"], {"delivery": [str(READER.relative_to(ROOT))], "workload": []})
        (row,) = summary["rows"]
        self.assertEqual(row["consumer"], "A")
        self.assertEqual([load["runs"] for load in row["loads"]], list(RUNS.values()))
        self.assertTrue(all(load["calibration_held"] and load["transcript"]["equals_record"]
                            and load["transcript"]["counters_decoded_again_equal"] for load in row["loads"]))
        d5, b2 = row["formats"]["dense5"], row["formats"]["baseline2"]
        self.assertEqual((d5["used"], b2["used"], d5["excluded"], b2["excluded"]), (774, 775, [], []))
        self.assertEqual((d5["logical_trits"], b2["logical_trits"]), ([17_694_720], [17_694_720]))
        self.assertEqual((d5["words"], b2["words"]), ([221_184], [276_480]))
        # The same trits in both formats, so the ratio is the ratio of the cycles: 1.25 by arithmetic
        # when both read at the same words per clock; below the ceiling in the medians.
        ratio = row["ratio_dense5_baseline2"]
        self.assertLess(abs(ratio["of_medians"] - 1.25), 1e-4)
        self.assertLessEqual(ratio["of_medians"], 1.25)
        self.assertLess(ratio["bracket"][0], ratio["of_medians"])
        self.assertLess(ratio["of_medians"], ratio["bracket"][1])
        self.assertEqual(row["memory_bound"]["verdict"], True)
        self.assertIn("by construction", row["memory_bound"]["basis"])
        self.assertTrue(row["every_run_passed"])
        self.assertEqual(summary["problems"], [])
        self.assertEqual(len(summary["not_measured"]), 2)   # consumer (B) and consumer (A) on the #64 chunk

    def test_a_changed_figure_fails_the_check(self):
        summary = json.loads(SUMMARY.read_text())
        summary["rows"][0]["formats"]["dense5"]["weights_per_s"]["median"] += 1
        changed = self.work() / "changed.json"
        changed.write_text(json.dumps(summary))
        code, text = self.run_tool("--check", changed)
        self.assertEqual(code, 1)
        self.assertIn("DIFFERS", text)

    # --- consumer (A): what is excluded and what is a problem ----------------------------------

    def test_a_load_whose_calibration_did_not_hold_is_excluded(self):
        reader = self.reader_copy()

        def return_to_idle(lines):
            # The load's last status line now reports one return to IDLE since reset.
            i = max(k for k, line in enumerate(lines) if line.split("\t")[-1].startswith("S"))
            t_s, when, line = lines[i].rstrip("\n").split("\t")
            a = int(line[1:9], 16) + 1
            lines[i] = f"{t_s}\t{when}\tS{a:08x}{line[9:]}\n"
        self.edit_transcript(reader / "load2", return_to_idle, update_sha=True)
        record = read_gz(reader / "load2/capture.json.gz")
        record["decoded"]["checks"]["no_return_to_idle"] = False          # as the capture tool decodes it
        write_gz(reader / "load2/capture.json.gz", record)
        code, summary, text = self.summarize("--delivery", reader)
        self.assertEqual(code, 0, text)
        (row,) = summary["rows"]
        excluded = row["formats"]["dense5"]["excluded"] + row["formats"]["baseline2"]["excluded"]
        self.assertEqual(len(excluded), RUNS["load2"])
        self.assertTrue(all(e["where"] == "load2" and "calibration" in e["excluded"] for e in excluded))
        self.assertEqual(row["formats"]["dense5"]["used"] + row["formats"]["baseline2"]["used"],
                         RUNS["load1"] + RUNS["load3"])
        self.assertFalse(row["loads"][1]["calibration_held"])

    def test_a_record_that_its_transcript_contradicts_is_a_problem(self):
        reader = self.reader_copy()
        record = read_gz(reader / "load2/capture.json.gz")
        record["decoded"]["checks"]["no_return_to_idle"] = False          # the transcript says it held
        write_gz(reader / "load2/capture.json.gz", record)
        code, summary, text = self.summarize("--delivery", reader)
        self.assertEqual(code, 1)
        self.assertTrue(any("calibration" in p for p in summary["problems"]), summary["problems"])

    def test_a_failed_run_is_excluded(self):
        reader = self.reader_copy()
        record = read_gz(reader / "load3/capture.json.gz")
        run = record["decoded"]["reader"]["runs"][7]
        run["pass"] = False
        run["checks"]["equals_host_model"] = False
        write_gz(reader / "load3/capture.json.gz", record)
        code, summary, text = self.summarize("--delivery", reader)
        self.assertEqual(code, 0, text)
        (row,) = summary["rows"]
        name = run["format_name"]
        self.assertEqual(row["formats"][name]["excluded"],
                         [{"where": "load3", "run": 7, "excluded": "failed: equals_host_model"}])
        self.assertFalse(row["every_run_passed"])

    def test_a_transcript_that_differs_from_its_record(self):
        def slower(lines):
            # One read's cycles (line c, field b) one clock more than the record says.
            i = next(k for k, line in enumerate(lines) if line.split("\t")[-1].startswith("c"))
            t_s, when, line = lines[i].rstrip("\n").split("\t")
            lines[i] = f"{t_s}\t{when}\t{line[:9]}{int(line[9:], 16) + 1:010x}\n"
        for update_sha, problem in ((False, "sha256"), (True, "counters")):
            with self.subTest(update_sha=update_sha):
                reader = self.reader_copy()
                self.edit_transcript(reader / "load1", slower, update_sha=update_sha)
                code, summary, _ = self.summarize("--delivery", reader)
                self.assertEqual(code, 1)
                self.assertTrue(any(problem in p for p in summary["problems"]), summary["problems"])
                (row,) = summary["rows"]
                excluded = row["formats"]["dense5"]["excluded"] + row["formats"]["baseline2"]["excluded"]
                self.assertEqual(len(excluded), RUNS["load1"])
                self.assertTrue(all(e["where"] == "load1" for e in excluded))

    # --- consumer (B) ---------------------------------------------------------------------------

    def matvec_record(self, work: Path, cycles: dict, *, calib=None, faults=None, stalls=None, rx=b"YZ"):
        """A record as tools/fpga-matvec-capture.py writes it (the fields the summary reads)."""
        calib, faults, stalls = calib or {}, faults or {}, stalls or {}
        words = {"dense5": 320 * 32, "baseline2": 320 * 40}
        formats = {}
        for name, values in cycles.items():
            runs = []
            for i, c in enumerate(values):
                status, equal = faults.get((name, i), (0, True))
                runs.append({"status": status, "equal_reference": equal, "checksum_ok": True,
                             "z": {"status": status, "rows": 320, "words": words[name], "cycles": c,
                                   "idle_clocks": c - words[name], "latency": 40, "invalid_codes": 0,
                                   "stray_words": 0, "consumer_stalls": stalls.get((name, i), 0), "checksum": 0}})
            formats[name] = {"calib_held": calib.get(name, True), "runs": runs}
        (work / "capture.json.rx.bin.gz").write_bytes(gzip.compress(rx, mtime=0))
        record = {"schema": "trinity.fpga-ddr-capture.v1", "started_utc": "2026-09-25T00:00:00.000Z",
                  "chunk": {"tensor": "q_proj", "rows": [0, 320], "cols": 2560, "reference_full_sha256": "ab",
                            "reference_matches_report": True},
                  "rx": {"file": "capture.json.rx.bin.gz", "bytes": len(rx), "sha256": hashlib.sha256(b"YZ").hexdigest()},
                  "formats": formats}
        path = work / "capture.json"
        path.write_text(json.dumps(record))
        return path

    def test_workload_rows(self):
        work = self.work()
        d5 = [10_400, 10_390, 10_410, 10_405]
        b2 = [13_000, 12_990, 13_010, 13_020]
        path = self.matvec_record(work, {"dense5": d5, "baseline2": b2}, faults={("dense5", 3): (2, False)})
        code, summary, text = self.summarize("--workload", path)
        self.assertEqual(code, 0, text)
        (row,) = summary["rows"]
        self.assertEqual(row["consumer"], "B")
        f = self.tool.F_CTRL_HZ
        self.assertEqual(row["formats"]["dense5"]["excluded"],
                         [{"where": "dense5", "run": 3, "excluded": "status 2, accumulators equal False, checksum True"}])
        self.assertEqual(row["formats"]["dense5"]["weights_per_s"]["median"],
                         self.tool.sig(statistics.median(320 * 2560 * f / c for c in d5[:3])))
        self.assertEqual(row["formats"]["baseline2"]["weights_per_s"]["n"], 4)
        self.assertEqual(row["ratio_dense5_baseline2"]["of_medians"],
                         self.tool.sig(row["formats"]["dense5"]["weights_per_s"]["median"]
                                       / row["formats"]["baseline2"]["weights_per_s"]["median"]))
        self.assertEqual(row["formats"]["dense5"]["idle_clocks"], {"min": 10_390 - 10_240, "max": 10_410 - 10_240})
        self.assertTrue(row["memory_bound"]["verdict"])
        self.assertFalse(row["every_run_passed"])
        self.assertEqual(len(summary["not_measured"]), 1)                 # only consumer (A) on the chunk

    def test_workload_exclusions_and_verdict(self):
        cycles = {"dense5": [10_400, 10_401], "baseline2": [13_000, 13_001]}
        # A format after which the calibration had dropped: its runs are excluded and no ratio is given.
        code, summary, _ = self.summarize("--workload", self.matvec_record(self.work(), cycles,
                                                                            calib={"baseline2": False}))
        (row,) = summary["rows"]
        self.assertEqual(row["formats"]["baseline2"]["used"], 0)
        self.assertIsNone(row["ratio_dense5_baseline2"])
        self.assertFalse(row["memory_bound"]["verdict"])                  # not shown for both formats
        # A consumer stall in a run used: not memory-bound.
        code, summary, _ = self.summarize("--workload", self.matvec_record(self.work(), cycles,
                                                                            stalls={("baseline2", 1): 3}))
        self.assertFalse(summary["rows"][0]["memory_bound"]["verdict"])
        self.assertEqual(summary["rows"][0]["memory_bound"]["consumer_stalls_max"], 3)
        # Received bytes that are not the record's: a problem, and no run is used.
        code, summary, _ = self.summarize("--workload", self.matvec_record(self.work(), cycles, rx=b"other"))
        self.assertEqual(code, 1)
        self.assertEqual(summary["rows"][0]["formats"]["dense5"]["used"], 0)


if __name__ == "__main__":
    unittest.main()
