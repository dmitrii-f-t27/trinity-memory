"""DDR3 bring-up on the AX7203 (issue #61): self-test model, capture decoder, committed evidence.

Board-free checks:
- tools/uberddr3-bist-model.py: the slice form of the self-test coverage agrees with an
  operation-by-operation run on a reduced geometry, the address bits it calls blind give
  no wrong read under a stuck-at fault there and the others do, and at full size
  BIST_MODE 1 reads back 3/4 of the burst addresses (rows 8192-24575 of banks 0, 1, 6, 7
  are never written or read);
- tools/fpga-ddr3-capture.py decodes H and S lines, fits the controller clock over host
  times, infers the wraps of the 40-bit clock count from the load time, and fails a wrong
  build id, a return to IDLE or a reset far from the load; it reads openFPGALoader's XADC,
  DNA and IDCODE output;
- the committed capture under reports/fpga/ddr3-bringup-* decodes to what its record says
  and its transcript hashes match.
"""
from __future__ import annotations

import importlib.util
import json
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bist = load("uberddr3_bist_model", "uberddr3-bist-model.py")
capture = load("fpga_ddr3_capture", "fpga-ddr3-capture.py")


class BistModel(unittest.TestCase):
    # ROW 6, COL 5 (2 burst-address column bits), BA 3, data from counter[2:0]: the
    # data bits cover the columns and bank[0] in the burst phase, as at full size.
    SMALL = bist.Geometry(row_bits=6, col_bits=5, ba_bits=3, data_bits=3)

    def test_slices_agree_with_the_operations(self):
        g = self.SMALL
        for mode in (1, 2):
            wrong, written, read = bist.simulate(g, mode)
            self.assertEqual(wrong, 0)
            cov = bist.coverage(g, mode)
            self.assertEqual(cov["written"], len(written))
            self.assertEqual(cov["read_back"], len(read))
            self.assertEqual(cov["written_never_read"], sorted(written - read)[:8])

    def test_blind_bits_are_exactly_the_undetected_faults(self):
        g = self.SMALL
        blind = set(bist.blind_bits(g))
        self.assertEqual(sorted(blind), [3, 4, 8, 9, 10])   # bank[1], bank[2], row[3..5]
        for mode in (1, 2):
            for bit in range(g.addr_bits):
                for stuck in (0, 1):
                    fault = (lambda a, b=bit, v=stuck: (a | (1 << b)) if v else (a & ~(1 << b)))
                    wrong = bist.simulate(g, mode, fault)[0]
                    if bit in blind:
                        self.assertEqual(wrong, 0, (mode, bit, stuck))
                    else:
                        self.assertGreater(wrong, 0, (mode, bit, stuck))

    def test_full_size(self):
        g = bist.Geometry()
        self.assertEqual(g.addr_bits, 25)
        one = bist.coverage(g, 1)
        self.assertEqual(one["written"], 25_165_824)
        self.assertEqual(one["read_back"], 25_165_823)
        self.assertEqual(one["never_written"], 8_388_608)
        self.assertEqual(one["read_more_than_once"], 8_388_608)
        self.assertEqual(one["written_never_read"], [(1 << 25) - 1])
        self.assertEqual(one["banks_rows_never_read"], {0: [[8192, 24575]], 1: [[8192, 24575]],
                                                        6: [[8192, 24575]], 7: [[8192, 24575]]})
        two = bist.coverage(g, 2)
        self.assertEqual(two["read_back"], 1 << 25)
        self.assertEqual([bist.name_of(g, b) for b in bist.blind_bits(g)],
                         ["bank[1]", "bank[2]"] + [f"row[{i}]" for i in range(8, 15)])


def s_line(calib, state, top, returns, clocks):
    return f"S{(calib << 24) | (state << 16) | (top << 8) | returns:08x}{clocks & ((1 << 40) - 1):010x}"


class CaptureDecoder(unittest.TestCase):
    HZ = 83_333_333.3

    def transcript(self, *, build="f07e91cd", returns=0, reset_at=10.0, wraps=0, seconds=60, jitter=0.02):
        rng = random.Random(7)
        entries = [{"t_s": 0.5, "line": "011717007cbb0317c7"}]   # a partial line from before the load
        entries.append({"t_s": reset_at + 0.001, "line": f"H{build}0102000bb8"})
        t = reset_at + 0.002
        while t < reset_at + seconds:
            clocks = int((t - reset_at) * self.HZ) + wraps * (1 << 40)
            done = t - reset_at > 5.2
            entries.append({"t_s": round(t + rng.uniform(0, jitter), 4),
                            "line": s_line(int(done), 23 if done else 17, 23 if done else 17, returns, clocks)})
            t += 0.807
        return entries

    def decode(self, entries, load_end_s):
        return capture.decode(entries, expect_build_id="f07e91cd", expect_lanes=2, expect_period_ps=3000,
                              load_end_s=load_end_s, nominal_hz=self.HZ)

    def test_a_good_run_passes(self):
        result = self.decode(self.transcript(), load_end_s=9.99)
        self.assertTrue(result["pass"], result["checks"])
        self.assertEqual(result["header"][0]["byte_lanes"], 2)
        self.assertEqual(result["reset"]["wraps_of_the_40_bit_count"], 0)
        self.assertAlmostEqual(result["reset"]["reset_minus_load_end_s"], 0.01, delta=0.05)
        self.assertLess(abs(result["clock"]["ppm_from_nominal"]), 200)
        self.assertEqual(result["calibration"]["state_timeline"][-1]["state"], 23)

    def test_failures(self):
        self.assertFalse(self.decode(self.transcript(build="b74926ed"), 9.99)["checks"]["build_id"])
        self.assertFalse(self.decode(self.transcript(returns=1), 9.99)["checks"]["no_return_to_idle"])
        # A reset a minute after the load: something reset the design in between.
        self.assertFalse(self.decode(self.transcript(reset_at=70.0), 9.99)["checks"]["reset_at_load"])
        # A load run without an H line fails.
        entries = [e for e in self.transcript() if not e["line"].startswith("H")]
        self.assertFalse(self.decode(entries, 9.99)["checks"]["header_line"])

    def test_wraps_come_from_the_load_time(self):
        # 2^40 clocks is 3.665 h: a load 1 wrap earlier than the count alone suggests.
        wrap_s = (1 << 40) / self.HZ
        result = self.decode(self.transcript(reset_at=10.0 + wrap_s, wraps=1), load_end_s=10.0)
        self.assertEqual(result["reset"]["wraps_of_the_40_bit_count"], 1)
        # Without a load the count is ambiguous and the record says so.
        result = capture.decode(self.transcript(), load_end_s=None, nominal_hz=self.HZ)
        self.assertIsNone(result["reset"]["wraps_of_the_40_bit_count"])
        self.assertIsNone(result["checks"]["header_line"])

    def test_openfpgaloader_output(self):
        xadc = ('empty\nJtag frequency : requested 6.00MHz    -> real 6.00MHz   \n{"temp": 48.9342, \n'
                '    "maxtemp": 49.6936, \n"raw":  {"0": 41887, "1": 21718},\n"vccaux": 1.78857, \n'
                '   "minvccaux": 1.78711, \n}')
        self.assertEqual(capture.parse_xadc(xadc)["temp"], 48.9342)
        self.assertEqual(capture.parse_dna('empty\n{"dna": "0x00389c0c2d85e85c"}'), "0x00389c0c2d85e85c")
        self.assertEqual(capture.parse_idcode("index 0:\n\tidcode 0x3636093\n"), "0x3636093")


class CommittedEvidence(unittest.TestCase):
    def test_bringup_captures(self):
        runs = sorted(ROOT.glob("reports/fpga/ddr3-bringup-*/*/capture.json"))
        self.assertTrue(runs)
        for path in runs:
            record = json.loads(path.read_text())
            self.assertEqual(record["schema"], "trinity.ddr3-capture.v1")
            tsv = path.parent / record["uart_transcript"]["file"]
            raw = path.parent / record["uart_transcript"]["raw_file"]
            self.assertEqual(capture.sha256_file(tsv), record["uart_transcript"]["sha256"], path)
            self.assertEqual(capture.sha256_file(raw), record["uart_transcript"]["raw_sha256"], path)
            entries = []
            for line in tsv.read_text().splitlines():
                if line.startswith("#"):
                    continue
                t_s, _utc, text = line.split("\t", 2)
                entries.append({"t_s": float(t_s), "line": text})
            expect = record["expect"]
            again = capture.decode(entries, expect_build_id=expect["build_id"], expect_lanes=expect["lanes"],
                                   expect_period_ps=expect["period_ps"], load_end_s=record["run"].get("load_end_s"),
                                   nominal_hz=expect["nominal_hz"])
            self.assertEqual(again["pass"], record["decoded"]["pass"], path)
            self.assertEqual(again["final"], record["decoded"]["final"], path)
            self.assertEqual(record["board"]["dna"], "0x00389c0c2d85e85c")


if __name__ == "__main__":
    unittest.main()
