"""Replay conformance/memory_edge_demo.json against the executable Edge Demo.

The native signal model must equal the golden containers, the native fixtures
must equal the spec fixtures, the native edge report (and the committed
reports/t27/edge.json) must carry the predicted labels, accumulators, RTL rows
and limitations, scoring rules must hold through the Bridge, and the CLI must
agree with the adapter except for wall-clock fields.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from trinity_memory.bridge import BridgeClient, BridgeServer
from trinity_memory.edge import LABELS, TEMPLATES, classify, fixture_cases, run_edge_demo, signal_model
from trinity_memory.tensorpack import Tensor, decode_tensors, encode_tensors

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = json.loads((ROOT / "conformance" / "memory_edge_demo.json").read_text(encoding="utf-8"))
CLI = ROOT / "build" / "t27" / "trinity-memory-t27"
TOOLS = all(shutil.which(tool) for tool in ("iverilog", "vvp"))


def by_kind(kind):
    return [vector for vector in DOCUMENT["vectors"] if vector["kind"] == kind]


def strip_timing(value, fields):
    if isinstance(value, dict):
        return {key: strip_timing(item, fields) for key, item in value.items() if key not in fields}
    if isinstance(value, list):
        return [strip_timing(item, fields) for item in value]
    return value


class SpecEdgeDemoConformance(unittest.TestCase):
    def check_report(self, report, rtl):
        constants = DOCUMENT["constants"]["report"]
        self.assertEqual(report["schema"], constants["schema"])
        self.assertTrue(report["passed"])
        self.assertEqual(report["evidence"], constants["evidence"] if rtl else constants["evidence"][:1])
        self.assertFalse(report["physical_device_tested"])
        self.assertEqual(report["runtime"], constants["runtime"])
        self.assertEqual(report["labels"], list(LABELS))
        self.assertEqual(report["fixture_count"], constants["fixture_count"])
        self.assertEqual(report["model"], DOCUMENT["constants"]["model"]["description"])
        self.assertEqual(report["limitations"], constants["limitations"])
        models = {vector["codec"]: vector for vector in by_kind("model")}
        self.assertEqual([mode["codec"] for mode in report["modes"]], DOCUMENT["constants"]["experiment"]["modes"])
        for mode in report["modes"]:
            self.assertEqual(list(mode), constants["mode_fields"])
            model = models[mode["codec"]]
            self.assertEqual(mode["container_bytes"], model["container_bytes"])
            self.assertEqual(mode["raw_weight_payload_bytes"], model["raw_weight_payload_bytes"])
            self.assertEqual(mode["container_sha256"], model["container_sha256"])
            self.assertTrue(mode["roundtrip_exact"])
            self.assertEqual(mode["capabilities"]["backend"], "emulator")
            self.assertEqual(len(mode["cases"]), 6)
            for case, fixture in zip(mode["cases"], by_kind("fixture")):
                self.assertEqual(list(case), constants["case_fields"])
                for key in ("name", "samples", "expected", "label", "ambiguous", "accumulators", "scores"):
                    self.assertEqual(case[key], fixture[key], f"{mode['codec']}/{fixture['name']}/{key}")
                self.assertEqual(case["reference"], fixture["accumulators"])
                self.assertEqual(case["backend"], "emulator")
                self.assertTrue(case["passed"])
                rows = [row for row in by_kind("rtl_row") if row["codec"] == mode["codec"] and row["fixture"] == fixture["name"]]
                self.assertEqual(len(rows), 3)
                if rtl:
                    self.assertEqual(len(case["rtl"]), 3)
                    for witness, row in zip(case["rtl"], rows):
                        for key in ("seed", "result", "error", "weight_count", "groups", "encoded_weight_bits", "codec"):
                            self.assertEqual(witness[key], row[key], f"{row['fixture']}/{row['row']}/{key}")
                        self.assertEqual(witness["evidence"], "rtl-simulation")
                        self.assertEqual(witness["accumulator_bits"], 32)
                else:
                    self.assertEqual(case["rtl"], [])

    def test_templates_fixtures_and_models_match_the_native_demo(self):
        self.assertEqual(DOCUMENT["constants"]["model"]["templates"], [list(row) for row in TEMPLATES])
        self.assertEqual(DOCUMENT["constants"]["model"]["labels"], list(LABELS))
        native = fixture_cases()
        self.assertEqual([(f["name"], f["samples"], f["expected"]) for f in native],
                         [(f["name"], f["samples"], f["expected"]) for f in by_kind("fixture")])
        for vector in by_kind("model"):
            data = signal_model(vector["codec"])
            self.assertEqual(data.hex(), vector["ttpk_hex"], vector["codec"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), vector["container_sha256"])
            tensors = decode_tensors(data)
            self.assertEqual(tensors, [Tensor("signal_templates", (3, 12), tuple(v for row in TEMPLATES for v in row),
                                              vector["codec"], (1.0,), None, ("class", "sample"))])

    def test_native_report_and_committed_report_follow_the_spec(self):
        self.check_report(run_edge_demo(rtl=TOOLS, seed=27), TOOLS)
        committed = json.loads((ROOT / "reports" / "t27" / "edge.json").read_text(encoding="utf-8"))
        self.check_report(committed, "rtl-simulation" in committed["evidence"])

    def test_scoring_rules_through_the_bridge(self):
        with BridgeServer() as server:
            client = BridgeClient(server.url)
            handle = client.upload(signal_model())
            for vector in by_kind("scoring"):
                if "model" in vector:
                    model = vector["model"]
                    blob = encode_tensors([Tensor("signal_templates", tuple(model["shape"]), tuple(model["values"]),
                                                  scales=tuple(model["scales"]), scale_axis=model["scale_axis"])])
                    target = client.upload(blob)
                else:
                    target = handle
                if "samples" not in vector:
                    continue
                if "error" in vector:
                    with self.assertRaisesRegex(ValueError, vector["error"]):
                        classify(client, target, vector["samples"])
                    continue
                result = classify(client, target, vector["samples"])
                self.assertEqual(result["label"], vector["label"], vector["id"])
                self.assertEqual(result["ambiguous"], vector["ambiguous"], vector["id"])
                self.assertEqual(result["accumulators"], vector["accumulators"], vector["id"])
                if "scores" in vector:
                    self.assertEqual(result["scores"], vector["scores"], vector["id"])
            for fixture in by_kind("fixture"):
                result = classify(client, handle, fixture["samples"])
                self.assertEqual((result["label"], result["accumulators"]), (fixture["label"], fixture["accumulators"]))

    @unittest.skipUnless(CLI.is_file() and os.access(CLI, os.X_OK) and TOOLS, "native CLI or Icarus missing")
    def test_cli_agrees_with_the_adapter_except_wall_clock(self):
        with tempfile.TemporaryDirectory(prefix="trinity-spec-edge-") as directory:
            output = Path(directory) / "edge.json"
            html = Path(directory) / "edge.html"
            # The CLI parser accepts --seed only for benchmark (its usage text says otherwise); the default seed is 27.
            result = subprocess.run([str(CLI), "edge-demo", "--rtl", "--output", str(output), "--html", str(html)],
                                    capture_output=True, text=True, timeout=600)
            self.assertEqual(result.returncode, 0, result.stderr)
            cli_report = json.loads(output.read_text(encoding="utf-8"))
            page = html.read_text(encoding="utf-8")
        self.check_report(cli_report, True)
        timing = DOCUMENT["constants"]["report"]["timing_fields"]
        self.assertEqual(strip_timing(cli_report, timing), strip_timing(run_edge_demo(rtl=True, seed=27), timing))
        for fixture in by_kind("fixture"):
            self.assertIn(fixture["name"], page)

    def test_generator_output_is_committed(self):
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "generate-spec-vectors.py"), "--check"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
