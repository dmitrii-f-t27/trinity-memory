"""Replay conformance/memory_stream_compute.json against the executable stream compute path.

Frame vectors run through the native RTL runner (trinity_memory.rtl_compute,
real Icarus execution); cycle traces run through tests/spec_stream_replay.py
against the generated RTL; the runner's packet layout is checked against the
spec constants. Evidence from every check is rtl-simulation, never fpga.
"""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import spec_stream_replay as replay  # noqa: E402

from trinity_memory.rtl_compute import RTLSimulationError, _packet, _packets, _run_packets, run_rtl_dot  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = replay.load_document()
RTL_DIR = ROOT / "build" / "t27" / "rtl"
TOOLS = all(shutil.which(tool) for tool in ("iverilog", "vvp"))


def by_kind(kind):
    return [vector for vector in DOCUMENT["vectors"] if vector["kind"] == kind]


class SpecStreamComputeConformance(unittest.TestCase):
    def test_document_identity(self):
        self.assertEqual(DOCUMENT["module"], "TrinityMemoryStreamComputeSpec")
        self.assertTrue((ROOT / DOCUMENT["spec_path"]).is_file())
        self.assertEqual(len({vector["id"] for vector in DOCUMENT["vectors"]}), len(DOCUMENT["vectors"]))
        self.assertEqual(DOCUMENT["constants"]["evidence"]["label"], "rtl-simulation")

    def test_runner_packet_layout_matches_the_spec(self):
        layout = DOCUMENT["constants"]["runner_packet"]
        word = _packet(5, [-128, 127, 0, 1, -1], 31, True)
        self.assertEqual(word & ((1 << 10) - 1), 5)
        for lane, value in enumerate([-128, 127, 0, 1, -1]):
            self.assertEqual((word >> (layout["activations"][1] + 8 * lane)) & 255, value & 255)
        self.assertEqual((word >> layout["mask"][1]) & 31, 31)
        self.assertEqual((word >> layout["last"]) & 1, 1)
        self.assertEqual(word >> layout["reset"], 0)
        with self.assertRaises(ValueError):
            _packet(1024, [0] * 5, 0, False)
        with self.assertRaises(ValueError):
            _packet(0, [128, 0, 0, 0, 0], 0, False)

    @unittest.skipUnless(TOOLS, "Icarus Verilog not installed")
    def test_frame_vectors_agree_between_software_reference_and_rtl(self):
        for vector in by_kind("frame"):
            with self.subTest(vector=vector["id"]):
                expect = vector["expect"]
                if vector["acc_width"] == 32:
                    observed = run_rtl_dot(vector["weights"], vector["activations"], vector["codec"], seed=27)
                    self.assertEqual((observed["result"], bool(observed["error"])), (expect["result"], expect["error"]))
                    self.assertEqual(observed["evidence"], "rtl-simulation")
                else:
                    packets = _packets(vector["weights"], vector["activations"], vector["codec"])
                    observed = _run_packets(packets, [(expect["result"], expect["error"])], vector["codec"],
                                            seed=27, acc_width=vector["acc_width"])
                    self.assertEqual(len(observed["results"]), 1)
                    self.assertEqual((observed["results"][0]["result"], bool(observed["results"][0]["error"])),
                                     (expect["result"], expect["error"]))
                    self.assertEqual(observed["accumulator_bits"], vector["acc_width"])

    @unittest.skipUnless(TOOLS, "Icarus Verilog not installed")
    def test_runner_rejects_unsupported_accumulator_widths(self):
        packets = _packets([1], [1], "dense5")
        for width in (11, 33):
            with self.assertRaises((ValueError, RTLSimulationError)):
                _run_packets(packets, [(1, False)], "dense5", seed=27, acc_width=width)

    @unittest.skipUnless(TOOLS and (RTL_DIR / "dot_stream.v").is_file(), "generated RTL or Icarus missing")
    def test_cycle_traces_replay_in_icarus(self):
        dots, storages, cycles = replay.replay(DOCUMENT, RTL_DIR)
        self.assertEqual(dots, len(by_kind("dot_trace")))
        self.assertEqual(storages, len(by_kind("storage_trace")))
        self.assertEqual(cycles, sum(vector["cycle_count"] for vector in DOCUMENT["vectors"] if "cycle_count" in vector))

    def test_generator_output_is_committed(self):
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "generate-spec-vectors.py"), "--check"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
