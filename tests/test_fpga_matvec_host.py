"""Host-side shape and capture validation without opening a serial port."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("matvec_host", ROOT / "tools/fpga-matvec-run.py")
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


class MatvecHostValidation(unittest.TestCase):
    def test_unaligned_or_truncated_weight_payload_is_rejected(self):
        for cols, rows, count in ((81, 1, 81), (80, 1, 79), (80, 0, 0), (0, 1, 0)):
            with self.subTest(cols=cols, rows=rows, count=count), self.assertRaises(ValueError):
                host.weight_stream([0] * count, cols, rows, host.model.FMT_D5)

    def test_rows_outside_the_golden_tensor_are_rejected_before_fetch(self):
        for tensor in host.TENSORS:
            for rows in (0, -1, 2561):
                with self.subTest(tensor=tensor, rows=rows), self.assertRaises(ValueError):
                    host.chunk_reference(rows, tensor)

    def test_wrong_row_ids_and_missing_summary_are_saved_as_failed_captures(self):
        summary = [("d", 1, 0), ("c", 32, 200), ("o", 4, 0), ("w", 1, 1),
                   ("n", 0, 0), ("u", 160, 0), ("z", 1, 0)]
        for lines, passes in (([("y", 0, 0)] + summary, True),
                              ([("y", 9, 0)] + summary, False),
                              ([("y", 0, 0)], False)):
            with self.subTest(lines=lines), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "capture.json"
                argv = ["fpga-matvec-run.py", "--synthetic", "1", "--rows", "1",
                        "--work", tmp, "--output", str(output)]
                with patch.object(sys, "argv", argv), patch.object(host, "loader_load"), \
                     patch.object(host, "synthetic_chunk", return_value=([0] * 2560, [0] * 2560, [0], 2560)), \
                     patch.object(host, "doorbell_and_capture", return_value=lines), patch("builtins.print"):
                    self.assertEqual(host.main(), 0 if passes else 1)
                result = json.loads(output.read_text())
                self.assertEqual(result["bit_exact"], passes)
                self.assertEqual(result["lines"], [list(line) for line in lines])


if __name__ == "__main__":
    unittest.main()
