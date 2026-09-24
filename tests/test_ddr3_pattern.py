"""DDR3 pattern test (issue #61): t27/rtl/fpga_ddr3_pattern.t27 before any board run.

- The C that the pinned compiler generates from the module's functions (data generator,
  key derivation, popcount, DQ fold, first failing word) agrees with
  tools/ddr3_pattern_model.py on random inputs.
- Icarus runs the whole top fpga/ax7203/ddr3/tms_ddr3_ax7203.v with PATTERN_TEST 1, the
  t27 cores and tests/sim_ddr3_top_model.v in place of UberDDR3 (a behavioural Wishbone
  memory with random stalls, refresh-like stall windows, in-order acks after 6-13 clocks,
  and protocol checks). The UART lines go through tools/fpga-ddr3-capture.py's decoder.
  A clean run passes (x16 and x32, two rounds with Z), and every injected fault is
  detected with exactly the counts that a Python replay of the same fault on the model's
  data predicts: a stuck-at 0 and 1 on every burst-address bit the simulated region spans
  (bits 0-7 with 256 bursts, 8-11 with 4096), a stuck DQ bit (x16 DQ 0, 7, 8, 15; x32 DQ 16,
  31), a dropped write in a true and in a complement pass, and a hung port (T line).
  The board region is 2^25 bursts; address bits 12-24 are not simulated, the same
  counter and data rules cover them.
Runs with T27_ROOT set and Icarus installed (tools/test-t27.sh); skipped otherwise.
"""
from __future__ import annotations

import concurrent.futures
import ctypes
import importlib.util
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import ddr3_pattern_model as model  # noqa: E402

COMPILER = Path(os.environ["T27_ROOT"]) / "target/release/t27c" if os.environ.get("T27_ROOT") else None
CORES = ("fpga_reset", "fpga_uart_tx", "fpga_line_emitter", "fpga_ddr3_status", "fpga_ddr3_pattern")
HAVE_TOOLS = bool(COMPILER and COMPILER.is_file() and shutil.which("iverilog") and shutil.which("vvp"))
SIM_HZ = 200e6   # the PLL stub passes the 200 MHz input to the controller clock


def load_capture():
    spec = importlib.util.spec_from_file_location("fpga_ddr3_capture_pattern", ROOT / "tools/fpga-ddr3-capture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


capture = load_capture()


@unittest.skipUnless(COMPILER and COMPILER.is_file(), "T27_ROOT with a built t27c required")
class PatternFunctions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix="trinity-ddr3-pattern-c-")
        work = Path(cls.work.name)
        source = subprocess.run([str(COMPILER), "gen-c", str(ROOT / "t27/rtl/fpga_ddr3_pattern.t27")],
                                capture_output=True, text=True, check=True).stdout
        (work / "pattern.c").write_text(source, encoding="ascii")
        library = work / "libpattern.so"
        subprocess.run(["cc", "-shared", "-fPIC", "-O1", "-Wno-parentheses-equality", str(work / "pattern.c"),
                        "-o", str(library)], check=True, capture_output=True)
        cls.lib = ctypes.CDLL(str(library))
        u64, u32 = ctypes.c_uint64, ctypes.c_uint32
        for name, args, res in (("lin", (u64,), u64), ("gen", (u64, u32, u32), u64), ("base_of", (u32, u64), u64),
                                ("round_offset", (u32,), u64), ("pc64", (u64,), u32), ("fold", (u64, u32), u32),
                                ("first_of", (u64, u64, u64, u64), u64), ("nz", (u64,), u32)):
            function = getattr(cls.lib, name)
            function.argtypes, function.restype = args, res

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def test_generator_and_key_match_the_model(self):
        rng = random.Random(61)
        for _ in range(3000):
            x, key = rng.getrandbits(64), rng.getrandbits(64)
            addr, w = rng.getrandbits(25), rng.randrange(4)
            self.assertEqual(self.lib.lin(x), model.lin(x))
            self.assertEqual(self.lib.gen(key, addr, w), model.word(key, addr, w, False))
            self.assertEqual(self.lib.gen(key ^ model.M64, addr, w), model.word(key, addr, w, True))
        seed, clocks, rnd = 0x61, rng.getrandbits(40), 5
        base = self.lib.base_of(seed, clocks)
        self.assertEqual(base, model.base_key(seed, clocks))
        # The key as T_KEY0..T_KEY3 compute it.
        key = model.lin(((model.lin((base + self.lib.round_offset(rnd)) & model.M64)) + model.KADD) & model.M64)
        self.assertEqual(key, model.round_key(base, rnd))

    def test_generator_is_injective_on_a_sample(self):
        key = 0x0123456789ABCDEF
        words = {model.word(key, a, w, False) for a in range(4096) for w in range(4)}
        self.assertEqual(len(words), 4096 * 4)

    def test_counts_match_the_model(self):
        rng = random.Random(62)
        for _ in range(3000):
            d = [rng.getrandbits(64) if rng.random() < 0.6 else 0 for _ in range(4)]
            for lanes in (2, 4):
                self.assertEqual(self.lib.fold(d[0], lanes), model.fold(d[0], lanes))
            self.assertEqual(self.lib.pc64(d[0]), model.popcount(d[0]))
            self.assertEqual(self.lib.nz(d[1]), int(d[1] != 0))
            first = next((i for i in range(4) if d[i]), None)
            want = 0 if first is None else (first << 32) | (d[first] & model.M32)
            self.assertEqual(self.lib.first_of(*d), want)


# ---- whole-top simulation ----

def replay(lanes, bursts, rounds, seed, calib_clocks, fault):
    """What the pattern test must report, pass by pass, for the model's fault: the same
    writes and reads as tests/sim_ddr3_top_model.v applied to tools/ddr3_pattern_model.py's data."""
    mem, drops = {}, 0
    bit = fault.get("stuck_addr_bit")
    dq = fault.get("stuck_dq")

    def phys(a):
        if bit is None:
            return a
        return a | (1 << bit) if fault["stuck_addr_val"] else a & ~(1 << bit)

    def store(a, value):
        if dq is not None:
            for beat in range(8):
                pos = 8 * (lanes * beat + dq // 8) + dq % 8
                value = value | (1 << pos) if fault["stuck_dq_val"] else value & ~(1 << pos)
        mem[phys(a)] = value

    out = []
    for r in range(rounds):
        key = model.round_key(model.base_key(seed, calib_clocks), r)
        for p in (0, 1):
            for a in range(bursts):
                data = model.burst(key, a, lanes, bool(p))
                if a == fault.get("drop_addr"):
                    drops += 1
                    if drops - 1 == fault.get("drop_nth", 0):
                        continue
                store(a, data)
            bad = bits = words = mask = 0
            first = None
            for a in range(bursts):
                diff = mem.get(phys(a), 0) ^ model.burst(key, a, lanes, bool(p))
                if not diff:
                    continue
                bad += 1
                bits += model.popcount(diff)
                parts = [(diff >> (64 * w)) & model.M64 for w in range(lanes)]
                words += sum(1 for x in parts if x)
                for x in parts:
                    mask |= model.fold(x, lanes)
                if first is None:
                    w = next(i for i, x in enumerate(parts) if x)
                    first = (a, w, parts[w] & model.M32)
            out.append({"round": r, "pass": p, "bad_bursts": bad, "bit_errors": bits, "bad_words_64bit": words,
                        "dq_fail_mask": f"{mask:#010x}",
                        "first_fail_burst": first[0] if first else None,
                        "first_fail_word": first[1] if first else None,
                        "first_fail_xor_low32": f"{first[2]:#010x}" if first else None})
    return out


CONFIGS = {
    "x16": {"LANES": 2, "BURSTS": 256, "ROUNDS": 1},
    "x32": {"LANES": 4, "BURSTS": 256, "ROUNDS": 2},
    "x16_4096": {"LANES": 2, "BURSTS": 4096, "ROUNDS": 1},
    "x16_watchdog": {"LANES": 2, "BURSTS": 64, "ROUNDS": 1, "WATCHDOG": 2000},
}
FIELDS = ("bad_bursts", "bit_errors", "bad_words_64bit", "dq_fail_mask", "first_fail_burst", "first_fail_word",
          "first_fail_xor_low32")


@unittest.skipUnless(HAVE_TOOLS, "T27_ROOT with a built t27c and Icarus required")
class PatternSimulation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix="trinity-ddr3-pattern-sim-")
        work = Path(cls.work.name)
        sources = []
        for core in CORES:
            verilog = subprocess.run([str(COMPILER), "gen-verilog", str(ROOT / f"t27/rtl/{core}.t27")],
                                     capture_output=True, text=True, check=True).stdout
            (work / f"{core}.v").write_text(verilog)
            sources.append(str(work / f"{core}.v"))
        sources += [str(ROOT / "fpga/ax7203/sim/xilinx_stubs.v"), str(ROOT / "fpga/ax7203/ddr3/tms_ddr3_ax7203.v"),
                    str(ROOT / "tests/sim_ddr3_top_model.v"), str(ROOT / "tests/tb_ddr3_pattern.v")]
        cls.vvp = {}
        for name, params in CONFIGS.items():
            out = work / f"{name}.vvp"
            flags = [f"-Ptb_ddr3_pattern.{k}={v}" for k, v in params.items()]
            subprocess.run(["iverilog", "-g2012", *flags, "-s", "tb_ddr3_pattern", "-o", str(out), *sources],
                           check=True, capture_output=True, text=True)
            cls.vvp[name] = out
        cls.results = {}
        cls.runs = cls.scenarios()
        with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
            for key, result in zip(cls.runs, pool.map(lambda k: cls.simulate(*k), cls.runs)):
                cls.results[key] = result

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    @staticmethod
    def scenarios():
        runs = [("x16", ()), ("x32", ()), ("x16_4096", ())]
        for bit in range(8):
            for val in (0, 1):
                runs.append(("x16", (("stuck_addr_bit", bit), ("stuck_addr_val", val))))
        for bit in range(8, 12):
            for val in (0, 1):
                runs.append(("x16_4096", (("stuck_addr_bit", bit), ("stuck_addr_val", val))))
        for dq, val in ((0, 0), (7, 1), (8, 0), (15, 1)):
            runs.append(("x16", (("stuck_dq", dq), ("stuck_dq_val", val))))
        for dq, val in ((16, 1), (31, 0)):
            runs.append(("x32", (("stuck_dq", dq), ("stuck_dq_val", val))))
        runs.append(("x16", (("drop_addr", 77), ("drop_nth", 0))))
        runs.append(("x16", (("drop_addr", 77), ("drop_nth", 1))))
        runs.append(("x32", (("drop_addr", 200), ("drop_nth", 2))))
        runs.append(("x16_watchdog", (("hang_after", 40),)))
        return runs

    @classmethod
    def simulate(cls, config, fault):
        work = Path(cls.work.name)
        tag = config + "".join(f"_{k}{v}" for k, v in fault)
        capture_file = work / f"{tag}.txt"
        args = ["vvp", str(cls.vvp[config]), f"+capture={capture_file}", *[f"+{k}={v}" for k, v in fault]]
        run = subprocess.run(args, cwd=work, capture_output=True, text=True, timeout=600)
        entries = []
        if capture_file.exists():
            for raw in capture_file.read_text().splitlines():
                t_ns, line = raw.split("\t", 1)
                entries.append({"t_s": int(t_ns) / 1e9, "line": line})
        return {"returncode": run.returncode, "stdout": run.stdout, "entries": entries}

    def decoded(self, config, fault=()):
        result = self.results[(config, fault)]
        self.assertEqual(result["returncode"], 0, result["stdout"][-2000:])
        self.assertIn("TB_PASS", result["stdout"])
        return capture.decode(result["entries"], expect_build_id="5eedc0de", expect_lanes=CONFIGS[config]["LANES"],
                              expect_period_ps=3000, nominal_hz=SIM_HZ, expect_pattern=True)

    def expect(self, config, fault, decoded):
        p = CONFIGS[config]
        # The status reporter's H line comes through the test's arbiter, first.
        self.assertTrue(decoded["header"], (config, fault))
        self.assertEqual(decoded["header"][0]["build_id"], "5eedc0de")
        self.assertTrue(decoded["checks"]["build_id"])
        self.assertTrue(decoded["checks"]["calib_complete"])
        header = decoded["pattern"]["header"]
        self.assertEqual((header["bursts"], header["byte_lanes"], header["seed"]), (p["BURSTS"], p["LANES"], 0x61))
        want = replay(p["LANES"], p["BURSTS"], p["ROUNDS"], header["seed"], header["calib_complete_clocks_low40"],
                      dict(fault))
        got = [{k: q[k] for k in ("round", "pass") + FIELDS} for q in decoded["pattern"]["passes"]]
        self.assertEqual(got, want, (config, fault))
        return want

    def test_clean_runs_pass(self):
        for config in ("x16", "x32", "x16_4096"):
            decoded = self.decoded(config)
            self.assertTrue(decoded["pass"], (config, decoded["checks"]))
            pattern = decoded["pattern"]
            p = CONFIGS[config]
            self.assertEqual(pattern["totals"]["passes"], 2 * p["ROUNDS"])
            self.assertEqual(pattern["totals"]["rounds_complete"], p["ROUNDS"])
            self.assertEqual(pattern["done"]["rounds"], p["ROUNDS"])
            self.assertEqual(pattern["totals"]["bytes_written_and_read_back"], 2 * p["ROUNDS"] * p["BURSTS"] * 8 * p["LANES"])
            self.assertEqual(pattern["header"]["rounds_configured"], p["ROUNDS"])
            self.assertEqual(pattern["min_write_to_read_clocks"], p["BURSTS"] - 1)
            for q in pattern["passes"]:
                # At most one request per clock, and the model stalls some of them.
                self.assertGreater(q["write_clocks"], p["BURSTS"])
                self.assertGreater(q["read_clocks"], p["BURSTS"])
            self.assertEqual(self.expect(config, (), decoded)[0]["bad_bursts"], 0)
            self.assertFalse(decoded["unparsed_lines"])

    def test_rounds_use_new_keys(self):
        decoded = self.decoded("x32")
        header = decoded["pattern"]["header"]
        base = model.base_key(header["seed"], header["calib_complete_clocks_low40"])
        self.assertNotEqual(model.round_key(base, 0), model.round_key(base, 1))
        self.assertEqual([(q["round"], q["pass"]) for q in decoded["pattern"]["passes"]], [(0, 0), (0, 1), (1, 0), (1, 1)])

    def check_detected(self, config, fault):
        decoded = self.decoded(config, fault)
        self.assertFalse(decoded["checks"]["pattern_no_wrong_bits"], (config, fault))
        self.assertFalse(decoded["pass"])
        self.assertTrue(decoded["checks"]["pattern_well_formed"])
        want = self.expect(config, fault, decoded)
        self.assertGreater(sum(q["bad_bursts"] for q in want), 0)
        return decoded, want

    def test_stuck_address_bits_are_detected(self):
        for config, fault in self.runs:
            if fault and fault[0][0] == "stuck_addr_bit":
                _decoded, want = self.check_detected(config, fault)
                # Half of the region aliases onto the other half, in every pass.
                self.assertEqual({q["bad_bursts"] for q in want}, {CONFIGS[config]["BURSTS"] // 2}, fault)

    def test_stuck_dq_bits_are_detected(self):
        for config, fault in self.runs:
            if fault and fault[0][0] == "stuck_dq":
                decoded, want = self.check_detected(config, fault)
                dq = fault[0][1]
                self.assertEqual({q["dq_fail_mask"] for q in want if q["bad_bursts"]}, {f"{1 << dq:#010x}"})
                # Over a round every stored position of that DQ is wrong in exactly one pass: 8 beats per burst.
                self.assertEqual(sum(q["bit_errors"] for q in want[:2]), 8 * CONFIGS[config]["BURSTS"])

    def test_dropped_writes_are_detected(self):
        for config, fault in self.runs:
            if fault and fault[0][0] == "drop_addr":
                _decoded, want = self.check_detected(config, fault)
                failing = [q for q in want if q["bad_bursts"]]
                self.assertEqual(len(failing), 1, fault)
                self.assertEqual(failing[0]["first_fail_burst"], fault[0][1])
                if fault[1][1] % 2 == 1:
                    # A complement pass's write dropped: the burst still holds the true pass's data,
                    # its exact complement, so every bit is wrong.
                    self.assertEqual(failing[0]["bit_errors"], 64 * CONFIGS[config]["LANES"])

    def test_a_hung_port_stops_the_test(self):
        decoded = self.decoded("x16_watchdog", (("hang_after", 40),))
        pattern = decoded["pattern"]
        self.assertTrue(decoded["checks"]["build_id"])
        self.assertIsNotNone(pattern["timeout"])
        self.assertEqual(pattern["timeout"]["read_phase"], False)
        self.assertLessEqual(pattern["timeout"]["acks"], 40)
        self.assertFalse(decoded["checks"]["pattern_no_wrong_bits"])
        self.assertFalse(decoded["pass"])
        self.assertEqual(pattern["passes"], [])


class PatternDecoder(unittest.TestCase):
    def line(self, tag, a, b):
        return f"{tag}{a:08x}{b:010x}"

    def test_old_builds_have_no_pattern_section(self):
        entries = [{"t_s": 1.0, "line": "Hf07e91cd0102000bb8"},
                   {"t_s": 1.1, "line": "S01171700" + f"{100:010x}"}]
        result = capture.decode(entries, nominal_hz=83_333_333.3)
        self.assertNotIn("pattern", result)
        self.assertTrue(result["pass"], result["checks"])
        # A build that should have the test but sends nothing fails.
        result = capture.decode(entries, nominal_hz=83_333_333.3, expect_pattern=True)
        self.assertFalse(result["checks"]["pattern_present"])

    def test_blocks_and_throughput(self):
        hz = 83_333_333.3
        entries = [{"t_s": 1.0, "line": "Hf07e91cd0102000bb8"},
                   {"t_s": 1.1, "line": "S01171700" + f"{100:010x}"},
                   {"t_s": 1.2, "line": self.line("G", 1 << 25, (1 << 32) | (2 << 24))},
                   {"t_s": 1.3, "line": self.line("K", 0x61, 433_000_000)},
                   {"t_s": 1.4, "line": self.line("L", 0, 1 << 24)}]
        t = 2.0
        for rp in range(4):
            for tag, a, b in (("W", rp, 50_000_000), ("R", rp, 60_000_000), ("E", 0, 0), ("M", 0, 0),
                              ("F", 0xFFFFFFFF, 0)):
                entries.append({"t_s": t, "line": self.line(tag, a, b)})
                t += 0.002
        entries.append({"t_s": t, "line": self.line("W", 4, 50_000_000)})   # capture ends mid-pass
        result = capture.decode(entries, nominal_hz=hz, expect_pattern=True)
        pattern = result["pattern"]
        self.assertTrue(result["pass"], result["checks"])
        self.assertEqual(pattern["totals"]["rounds_complete"], 2)
        self.assertEqual(pattern["region_bytes"], 512 * 1024 * 1024)
        self.assertAlmostEqual(pattern["passes"][0]["pattern_test_write_MBps"], 512 * 2 ** 20 / (50e6 / hz) / 1e6, 0)
        self.assertEqual(pattern["partial_pass_at_end"]["round"], 2)
        self.assertEqual(pattern["min_write_to_read_clocks"], (1 << 25) - 1)
        # A lost line breaks the order and fails the record.
        broken = [e for e in entries if not e["line"].startswith("E")][:12]
        result = capture.decode(broken, nominal_hz=hz, expect_pattern=True)
        self.assertFalse(result["checks"]["pattern_well_formed"])
        self.assertFalse(result["pass"])
        # A wrong bit anywhere fails it.
        bad = [dict(e) for e in entries]
        bad[7]["line"] = self.line("E", 1, 3)
        result = capture.decode(bad, nominal_hz=hz, expect_pattern=True)
        self.assertFalse(result["checks"]["pattern_no_wrong_bits"])

    def test_makefile_bakes_the_pattern_settings(self):
        makefile = (ROOT / "fpga/ax7203/Makefile").read_text()
        self.assertIn("fpga_ddr3_pattern", makefile.split("DDR3_CORES     :=", 1)[1].splitlines()[0])
        rule = makefile.split("$(DDR3_BUILD)/$(DDR3_TOP).json:", 1)[1].split("\n\n", 1)[0]
        self.assertIn("pattern-$(DDR3_PATTERN_TAG).stamp", rule)
        for name in ("PATTERN_TEST", "PATTERN_BURSTS", "PATTERN_HOLD", "PATTERN_ROUNDS"):
            self.assertIn(f"-set {name} $(DDR3_{name.replace('PATTERN_TEST', 'PATTERN')})", rule)
        self.assertIn("$(DDR3_PATTERN_VARIANT)", makefile)
        top = (ROOT / "fpga/ax7203/ddr3/tms_ddr3_ax7203.v").read_text()
        self.assertIn("parameter integer PATTERN_TEST = 0", top)

    def test_expectations_read_the_variant(self):
        report = {"ddr3": {"variant": "x16 BYTE_LANES 2, PATTERN_TEST 1 bursts 33554432", "clocks": []}}
        self.assertTrue(capture.expectations(report)["pattern"])
        report = {"ddr3": {"variant": "x16 BYTE_LANES 2, PLL CLKFBOUT_MULT 5", "clocks": []}}
        self.assertFalse(capture.expectations(report)["pattern"])


if __name__ == "__main__":
    unittest.main()
