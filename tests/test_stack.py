"""Independent cross-component acceptance checks for the five-direction stack."""

from pathlib import Path
import contextlib
import io
import json
import tempfile
import unittest

from trinity_memory.bridge import BridgeClient, BridgeServer
from trinity_memory.conformance import run_conformance
from trinity_memory.edge import classify, run_edge_demo, signal_model
from trinity_memory.cli import main
from trinity_memory.tensorpack import Tensor, encode_tensors


class StackTests(unittest.TestCase):
    def test_reference_vectors_and_corruption_over_http(self):
        report = run_conformance(Path(__file__).resolve().parents[1] / "examples/conformance.json")
        self.assertTrue(report["passed"])
        self.assertEqual(report["positive_checks"], 46)
        self.assertEqual(report["corrupt_rejections"], 6)
        self.assertEqual(report["rtl_checks"], 0)

    def test_edge_demo_compares_two_encodings(self):
        report = run_edge_demo()
        self.assertEqual(report["fixture_count"], 6)
        self.assertFalse(report["physical_device_tested"])
        modes = report["modes"]
        self.assertEqual([m["raw_weight_payload_bytes"] for m in modes], [8, 9])
        self.assertEqual([c["accumulators"] for c in modes[0]["cases"]],
                         [c["accumulators"] for c in modes[1]["cases"]])

    def test_constant_signal_is_ambiguous(self):
        with BridgeServer() as server:
            client = BridgeClient(server.url)
            handle = client.upload(signal_model())
            result = classify(client, handle, [17] * 12)
            self.assertTrue(result["ambiguous"])
            self.assertIsNone(result["label"])
            self.assertEqual(result["accumulators"], [0, 0, 0])

    def test_scaled_classifier_and_overflow(self):
        with BridgeServer() as server:
            client = BridgeClient(server.url)
            for scales, expected in (((1.0, 2.0, 0.5), "falling"), ((1e308, 1e308, 1.0), None)):
                blob = encode_tensors([Tensor("signal_templates", (3, 1), (1, 1, -1),
                                             scales=scales, scale_axis=0)])
                handle = client.upload(blob)
                if expected is None:
                    with self.assertRaisesRegex(ValueError, "finite"):
                        classify(client, handle, [3])
                else:
                    self.assertEqual(classify(client, handle, [3])["label"], expected)

    def test_invalid_fixture_and_duplicate_json_fail_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / "input.json", root / "output.bin"
            output.write_bytes(b"keep")
            for document in ([], {}, {"schema": "trinity.conformance.v1", "vectors": []}):
                source.write_text(json.dumps(document))
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["conformance", "--vectors", str(source), "--output", str(output)]), 2)
                self.assertEqual(output.read_bytes(), b"keep")
            source.write_text('[{"name":"w","shape":[1],"shape":[2],"values":[1,-1]}]')
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["tensor-pack", str(source), str(output)]), 2)
            self.assertEqual(output.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
