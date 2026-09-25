"""tools/fpga-matvec-capture.py (issue #65, consumer (B)) against the protocol-4 model on the timed
fake line of tests/test_uart_loader.py: the chunk's rows uploaded and read back in both formats,
the activations, repeated M runs whose Y lines equal the reference, the Z counters, weights per
second from the cycles, the statistic, the ratio and the memory-bound verdict. The model is ideal
(one word per clock, no idle clock), so its figures are arithmetic: 80 or 64 trits per clock.
Needs the fixture cache (skipped without it, or failed with TRINITY_REQUIRE_CACHED=1)."""
from __future__ import annotations

import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
import bridge_link_protocol as blink  # noqa: E402
import uart_loader_protocol as proto  # noqa: E402

REQUIRE_CACHED = os.environ.get("TRINITY_REQUIRE_CACHED") == "1"


class MatvecCapture(unittest.TestCase):
    def setUp(self):
        import stage1_chunk
        try:
            self.chunk = stage1_chunk.chunk()
        except Exception as error:  # noqa: BLE001 - fixtures.CacheMiss or a missing cache directory
            if REQUIRE_CACHED:
                self.fail(f"TRINITY_REQUIRE_CACHED=1: {error}")
            self.skipTest(f"fixture cache: {error}")
        spec = importlib.util.spec_from_file_location("fpga_matvec_capture", ROOT / "tools/fpga-matvec-capture.py")
        self.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tool)

    def rig(self, **model_options):
        from test_uart_loader import FakeDevice, FakePort
        model = blink.MatvecDevice(store=proto.SparseStore(29), store_log2=29, build_id=0x5EED1063, default_div=25,
                                   proto=proto.PROTO_DDR3, **model_options)
        model.out.clear()
        device = FakeDevice(model, 25_000_000, 0.05)

        def opener(_name, baud):
            return FakePort(device, baud)
        return model, opener

    def capture(self, opener, *extra):
        saved = self.tool.UL.OPENER
        self.tool.UL.OPENER = opener
        work = tempfile.TemporaryDirectory(prefix="trinity-matvec-capture-")
        self.addCleanup(work.cleanup)
        out = Path(work.name) / "capture.json"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = self.tool.main(["--port", "fake", "--baud", "1000000", "--rows", "16", "--runs", "3",
                                       "--ack-timeout", "0.2", "--guard", "0.12", "--output", str(out), *extra])
        finally:
            self.tool.UL.OPENER = saved
        return code, json.loads(out.read_text()), out

    def test_both_formats_with_the_statistic(self):
        model, opener = self.rig()
        code, record, out = self.capture(opener)
        self.assertEqual(code, 0, record.get("stopped"))
        self.assertEqual(record["device"]["protocol"], 4)
        self.assertTrue(record["chunk"]["reference_matches_report"])
        f_ctrl = record["design_hz"]
        for name, fmt, wpr in (("dense5", 1, 32), ("baseline2", 0, 40)):
            entry = record["formats"][name]
            self.assertTrue(entry["upload"]["identical"], name)
            self.assertTrue(entry["all_runs_exact"], name)
            self.assertEqual(len(entry["runs"]), 3)
            for run in entry["runs"]:
                self.assertEqual((run["status"], run["z"]["words"], run["z"]["words_per_row"]), (0, 16 * wpr, wpr))
                self.assertTrue(run["checksum_ok"])
                self.assertEqual(run["mismatching_rows"], [])
            # The model takes a word per clock: cycles = words, so weights per second is the arithmetic peak.
            self.assertAlmostEqual(entry["weights_per_s"]["median"], 16 * 2560 * f_ctrl / (16 * wpr))
            self.assertEqual(entry["weights_per_s"]["n"], 3)
            self.assertEqual(entry["weights_per_s"]["sd"], 0.0)
            self.assertTrue(entry["calib_held"])
        self.assertAlmostEqual(record["ratio_dense5_baseline2"]["of_medians"], 1.25)
        self.assertTrue(record["memory_bound"])
        rx = gzip.decompress((out.parent / record["rx"]["file"]).read_bytes())
        self.assertEqual(hashlib.sha256(rx).hexdigest(), record["rx"]["sha256"])
        self.assertEqual(len(model.runs), 6)

    def test_a_wrong_accumulator_fails_the_run_and_the_exit_status(self):
        model, opener = self.rig(result_faults={5: 1})
        code, record, _ = self.capture(opener, "--formats", "dense5")
        self.assertEqual(code, 1)
        entry = record["formats"]["dense5"]
        self.assertFalse(entry["all_runs_exact"])
        self.assertEqual([r["mismatching_rows"] for r in entry["runs"]], [[5]] * 3)
        self.assertIsNone(entry["weights_per_s"])                     # no run counts


if __name__ == "__main__":
    unittest.main()
