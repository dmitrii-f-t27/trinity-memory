"""Replay conformance/memory_conformance.json against the native conformance experiment
and the lab report.

The native runtime must accept the lab fixture and report exactly the counts the
spec's plan predicts, reject every invalid fixture, refuse the six corruption
blobs without allocating storage, and the lab report produced twice from this
clone must be identical except its timing fields.
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

from trinity_memory.bridge import BridgeClient, BridgeError, BridgeServer
from trinity_memory.conformance import run_conformance
from trinity_memory.tensorpack import Tensor, encode_tensors

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "conformance" / "memory_conformance.json").read_text(encoding="utf-8"))
TOOLS = all(shutil.which(tool) for tool in ("iverilog", "vvp"))
RTL_DIR = ROOT / "build" / "t27" / "rtl"
sys.path.insert(0, str(ROOT / "tools"))


def build_invalid_fixture(item):
    if "document" in item:
        return item["document"]
    recipe = item["recipe"]
    if "vectors" in recipe:
        base = recipe["vector"]
        return {"schema": "trinity.conformance.v1", "vectors": [dict(base, name=f"v{i}") for i in range(recipe["vectors"])]}
    count = recipe["weights"]
    return {"schema": "trinity.conformance.v1",
            "vectors": [{"name": "long", "weights": [recipe["weight"]] * count, "activations": [recipe["activation"]] * count,
                         "dot": recipe["weight"] * recipe["activation"] * count}]}


def positive_checks(vectors):
    return 4 * (vectors + 7) + 2


def rtl_checks(vectors, rtl):
    return 2 * (vectors + 7) if rtl else 0


class SpecConformanceLab(unittest.TestCase):
    def test_manifest_identity_and_plan(self):
        self.assertEqual(MANIFEST["module"], "TrinityMemoryConformanceLabSpec")
        self.assertTrue((ROOT / MANIFEST["spec_path"]).is_file())
        plan = MANIFEST["constants"]["plan"]
        self.assertEqual(plan["codec_order"], ["dense5", "baseline2", "dense17", "dense22"])
        self.assertEqual(plan["random_case_counts"], [1, 4, 5, 6, 12, 31, 65])
        fixture = MANIFEST["fixture_document"]
        self.assertEqual(fixture["schema"], "trinity.conformance.v1")
        self.assertEqual(MANIFEST["sections"]["fixture"]["expected"]["positive_checks"], positive_checks(len(fixture["vectors"])))
        self.assertEqual(MANIFEST["sections"]["fixture"]["expected"]["rtl_checks"], rtl_checks(len(fixture["vectors"]), True))
        for vector in fixture["vectors"]:
            self.assertEqual(vector["dot"], sum(w * a for w, a in zip(vector["weights"], vector["activations"])))
            self.assertEqual(set(vector), {"name", "weights", "activations", "dot", "dense5_hex", "baseline2_hex", "dense17_hex", "dense22_hex"})
        self.assertEqual(json.loads(MANIFEST["fixture_text"]), fixture)
        # The repository example is the same fixture without the wide-codec golden fields.
        example = json.loads((ROOT / "examples" / "conformance.json").read_text(encoding="utf-8"))
        for original, extended in zip(example["vectors"], fixture["vectors"]):
            for key in original:
                self.assertEqual(original[key], extended[key], key)

    def test_native_experiment_accepts_the_fixture_with_the_predicted_counts(self):
        expected = MANIFEST["sections"]["fixture"]["expected"]
        with tempfile.TemporaryDirectory(prefix="trinity-spec-lab-") as directory:
            path = Path(directory) / "fixture.json"
            path.write_text(MANIFEST["fixture_text"], encoding="utf-8")
            report = run_conformance(path, rtl=TOOLS, seed=27)
            self.assertTrue(report["passed"])
            self.assertEqual(report["schema"], "trinity.conformance-report.v1")
            self.assertEqual(report["positive_checks"], expected["positive_checks"])
            self.assertEqual(report["corrupt_rejections"], expected["corrupt_rejections"])
            self.assertEqual(report["rtl_checks"], expected["rtl_checks"] if TOOLS else 0)
            self.assertEqual(report["fixture_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertFalse(report["physical_device_tested"])
            self.assertEqual(report["evidence"][0], "software-loopback-http")
            names = [check["name"] for check in report["checks"]]
            self.assertEqual(names[:4], [MANIFEST["fixture_document"]["vectors"][0]["name"]] * 4)
            self.assertEqual([check["codec"] for check in report["checks"][:4]], MANIFEST["constants"]["plan"]["codec_order"])
            self.assertEqual(len(report["checks"]), expected["positive_checks"])

    def test_native_experiment_rejects_every_invalid_fixture(self):
        with tempfile.TemporaryDirectory(prefix="trinity-spec-lab-") as directory:
            path = Path(directory) / "fixture.json"
            for item in MANIFEST["invalid_fixtures"]:
                path.write_text(json.dumps(build_invalid_fixture(item)), encoding="utf-8")
                with self.subTest(fixture=item["id"]), self.assertRaises(ValueError):
                    run_conformance(path, rtl=False, seed=27)

    def test_fixture_containers_and_corruption_blobs(self):
        for item in MANIFEST["fixture_containers"]:
            vector = next(v for v in MANIFEST["fixture_document"]["vectors"] if v["name"] == item["vector"])
            data = encode_tensors([Tensor("weights", (len(vector["weights"]),), tuple(vector["weights"]), item["codec"])])
            self.assertEqual(data.hex(), item["ttpk_hex"], item["vector"])
        corruption = MANIFEST["fixture_corruption"]
        blob = bytes.fromhex(corruption["blob_ttpk_hex"])
        self.assertEqual(len(blob), corruption["size"])
        self.assertEqual(blob, encode_tensors([Tensor("w", (5,), (1, 0, -1, 0, 1))]))
        with BridgeServer() as server:
            client = BridgeClient(server.url)
            client.upload(blob)
            for flip in corruption["flips"]:
                mutated = bytes.fromhex(flip["ttpk_hex"])
                self.assertEqual(sum(a != b for a, b in zip(mutated, blob)), 1, flip["index"])
                with self.assertRaises(BridgeError) as caught:
                    client.upload(mutated)
                self.assertEqual(caught.exception.code, MANIFEST["constants"]["plan"]["corruption_rpc_error"])
            self.assertEqual(server.object_count, 1)

    @unittest.skipUnless(TOOLS and (RTL_DIR / "dot_stream.v").is_file(), "Icarus Verilog or generated RTL missing")
    def test_lab_report_is_reproducible(self):
        with tempfile.TemporaryDirectory(prefix="trinity-spec-lab-") as directory:
            output = Path(directory) / "conformance-lab.json"
            result = subprocess.run([sys.executable, str(ROOT / "tools" / "conformance-lab.py"), "--repeat", "2", "--output", str(output)],
                                    capture_output=True, text=True, timeout=900)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(report["passed"])
        self.assertEqual(report["schema"], "trinity.conformance-lab.v1")
        self.assertEqual(report["deterministic"], {"runs": 2, "identical_except_timing": True})
        self.assertEqual(sorted(report["sections"]), sorted(MANIFEST["sections"]))
        for name, section in MANIFEST["sections"].items():
            self.assertTrue(report["sections"][name]["passed"], name)
            self.assertEqual(report["sections"][name]["evidence"], section["evidence"], name)
        fixture = report["sections"]["fixture"]
        self.assertEqual(fixture["native"]["positive_checks"], MANIFEST["sections"]["fixture"]["expected"]["positive_checks"])
        self.assertEqual(fixture["native"]["rtl_checks"], MANIFEST["sections"]["fixture"]["expected"]["rtl_checks"])
        self.assertEqual(fixture["corruption_flips_rejected"], 6)
        self.assertEqual(report["sections"]["stream"]["compared_cycles"], report["sections"]["stream"]["checks"])
        for family, cases in MANIFEST["catalogue"].items():
            self.assertEqual(report["catalogue"][family], cases, family)
        self.assertFalse(report["physical_device_tested"])
        self.assertIn("rtl-simulation", report["evidence"])
        self.assertIn("specs/memory/conformance.t27", report["sources"])

    def test_generator_output_is_committed(self):
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "generate-spec-vectors.py"), "--check"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
