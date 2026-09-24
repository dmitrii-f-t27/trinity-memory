"""DDR3 read path (issue #62): t27/rtl/fpga_ddr3_reader.t27 before any board run.

- The C that the pinned compiler generates from the module's functions (the trit stream
  generator, the region arithmetic, the padding masks, the dense5 and baseline2 decoders and
  the dense5 encoder, the lane assembly, the per-class counts, the weighted sums, the checksum
  step) agrees with tools/ddr3_read_model.py on random inputs; the dense5 tables agree with the
  C of t27/rtl/bram_trit_codec.t27 (the block-RAM benches' codec) on all 256 codes and all 243
  valid lane groups, and a 128-bit word decoded through the four chunks and the assembly equals
  the model's decode; lanes encoded through the four dense5 encoders equal the model's encoding.
- Icarus runs the whole top fpga/ax7203/ddr3/tms_ddr3_ax7203.v read with `define DDR3_READER,
  the t27 cores and tests/sim_ddr3_top_model.v in place of UberDDR3 (our behavioural Wishbone
  memory: random stalls, refresh-like stall windows, in-order acks after a random latency,
  protocol checks). The UART lines go through tools/fpga-ddr3-capture.py's decoder, which
  checks every run against the host model. Clean runs pass for a region that is not a
  multiple of a word, of five or of four trits (1,237 trits: padding in both formats), for
  160 trits (dense5 whole words, baseline2 padded) and for 1 trit; both formats of a pair
  deliver the same logical trits; the bench's own count of every phase (clocks from the first
  request to the last ack, requests, acks, command stalls, wait stalls, most outstanding)
  equals the reader's counters; with a cap of 3 and long ack latency at most 3 requests are
  outstanding and the cap holds are counted; the memory model's stored bursts after the last
  fill equal the model's baseline2 and dense5 encodings byte for byte (the bit order of the
  128-bit word and of the payload in memory). Injected faults (a stuck DQ bit, a stuck address
  bit, a dropped write) are detected, and every run's bad words, invalid groups, +1 and -1
  counts, dot product and checksum equal those of a Python replay of the fault; a port that
  stops taking requests stops the reader (t line).
Runs with T27_ROOT set and Icarus installed (tools/test-t27.sh); skipped otherwise.
"""
from __future__ import annotations

import concurrent.futures
import ctypes
import importlib.util
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import ddr3_pattern_model as pattern_model  # noqa: E402
import ddr3_read_model as model  # noqa: E402

COMPILER = Path(os.environ["T27_ROOT"]) / "target/release/t27c" if os.environ.get("T27_ROOT") else None
CORES = ("fpga_reset", "fpga_uart_tx", "fpga_line_emitter", "fpga_ddr3_status", "fpga_ddr3_reader")
HAVE_TOOLS = bool(COMPILER and COMPILER.is_file() and shutil.which("iverilog") and shutil.which("vvp"))
SIM_HZ = 200e6   # the PLL stub passes the 200 MHz input to the controller clock
M32, M64 = (1 << 32) - 1, (1 << 64) - 1


def load_capture():
    spec = importlib.util.spec_from_file_location("fpga_ddr3_capture_reader", ROOT / "tools/fpga-ddr3-capture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


capture = load_capture()


def build_library(source: Path, work: Path) -> ctypes.CDLL:
    generated = subprocess.run([str(COMPILER), "gen-c", str(source)], capture_output=True, text=True, check=True).stdout
    c_file = work / f"{source.stem}.c"
    c_file.write_text(generated, encoding="ascii")
    library = work / f"lib{source.stem}.so"
    subprocess.run(["cc", "-shared", "-fPIC", "-O1", "-Wno-parentheses-equality", "-Wno-shift-count-overflow",
                    str(c_file), "-o", str(library)], check=True, capture_output=True)
    return ctypes.CDLL(str(library))


def random_lanes(rng: random.Random, count: int) -> int:
    return sum(rng.choice((0, 1, 2)) << (2 * j) for j in range(count))


@unittest.skipUnless(COMPILER and COMPILER.is_file(), "T27_ROOT with a built t27c required")
class ReaderFunctions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix="trinity-ddr3-reader-c-")
        work = Path(cls.work.name)
        cls.lib = build_library(ROOT / "t27/rtl/fpga_ddr3_reader.t27", work)
        cls.codec = build_library(ROOT / "t27/rtl/bram_trit_codec.t27", work)
        u64, u32, b = ctypes.c_uint64, ctypes.c_uint32, ctypes.c_bool
        for name, args, res in (("lin", (u64,), u64), ("mix", (u64,), u64), ("post", (u64,), u64),
                                ("clean32", (u64,), u64), ("raw32", (u64, u32), u64),
                                ("words_of", (u32, u32), u32), ("last_lanes", (u32, u32), u32),
                                ("last_payload", (u32, u32), u32), ("therm", (u32, u32), u64),
                                ("top_mask", (u32, u64), u64),
                                ("dec_part", (u32, u64, u64, u64, u64, u32), u64),
                                ("invalid_of", (u64, u64, u64, u64), u32),
                                ("class_counts", (u64, u64, u64, u32), u64), ("class_sums", (u64,), u64),
                                ("pair_sums", (u64,), u64), ("weighted_pairs", (u64,), u64), ("sum4", (u64,), u64),
                                ("rotl1", (u64,), u64), ("rotl2", (u64,), u64), ("key_base", (u32, u32), u64),
                                ("pick64", (b, u64, u64), u64), ("pick32", (b, u32, u32), u32),
                                ("d5_dec", (u64,), u64), ("d5_enc", (u64,), u64), ("d5_dec4", (u64,), u64),
                                ("d5_enc4", (u64,), u64), ("b2_dec16", (u64,), u64)):
            function = getattr(cls.lib, name)
            function.argtypes, function.restype = args, res
        cls.codec.on_comb.argtypes, cls.codec.on_comb.restype = (u32, u32, u64), u64

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def lanes3(self, lanes: int):
        return lanes & M64, (lanes >> 64) & M64, (lanes >> 128) & M64

    def decode(self, fmt: int, word: int):
        chunks = [(word >> (32 * c)) & M32 for c in range(4)]
        d = [self.lib.d5_dec4(x) if fmt == 1 else self.lib.b2_dec16(x) for x in chunks]
        # The chunk layout is the codec's decode (lanes in the low bits, invalid groups in 55:48).
        self.assertEqual(d, [self.codec.on_comb(fmt, 1, x) for x in chunks])
        parts = [self.lib.dec_part(fmt, *d, p) for p in range(3)]
        return parts[0] | (parts[1] << 64) | (parts[2] << 128), self.lib.invalid_of(*d)

    def test_dense5_tables_are_the_codec(self):
        for code in range(256):
            want = self.codec.on_comb(1, 1, code)       # lanes 9:0, invalid count in 55:48
            got = self.lib.d5_dec(code)
            self.assertEqual(got & 1023, want & 1023, code)
            self.assertEqual(got >> 10, want >> 48, code)
            lanes, bad = model.decode_word(1, code)
            self.assertEqual((got & 1023, got >> 10), (lanes & 1023, bad), code)
        for n in range(243):
            lanes = sum(((2, 0, 1)[(n // 3 ** i) % 3]) << (2 * i) for i in range(5))
            self.assertEqual(self.lib.d5_enc(lanes), self.codec.on_comb(1, 0, lanes) & 255, lanes)
            self.assertEqual(self.lib.d5_enc(lanes), model.encode_word(1, lanes) & 255, lanes)

    def test_stream_matches_the_model(self):
        rng = random.Random(62)
        for _ in range(3000):
            x, key, block = rng.getrandbits(64), rng.getrandbits(64), rng.getrandbits(28)
            self.assertEqual(self.lib.lin(x), pattern_model.lin(x))
            self.assertEqual(self.lib.mix(x), model.mix(x))
            self.assertEqual(self.lib.post(x), model.post(x))
            self.assertEqual(self.lib.clean32(x & M32), model.clean32(x & M32))
            self.assertEqual(self.lib.raw32(key, block), model.block_raw(key, block))
        # The key as T_KEY0..T_KEY2 compute it.
        for seed, pair in ((0x62, 0), (0x62, 7), (0xFFFFFFFF, 12345)):
            key = self.lib.lin(((self.lib.lin(self.lib.key_base(seed, pair))) + pattern_model.KADD) & M64)
            self.assertEqual(key, model.pair_key(seed, pair))

    def test_region_arithmetic(self):
        rng = random.Random(63)
        values = [1, 2, 4, 5, 63, 64, 65, 79, 80, 81, 159, 160, 161, 1237, 17694720, (1 << 31) - 1]
        values += [rng.randrange(1, 1 << 31) for _ in range(500)]
        for trits in values:
            for fmt in (0, 1):
                lanes = model.LANES[fmt]
                words = model.words_of(fmt, trits)
                self.assertEqual(self.lib.words_of(trits, fmt), words)
                last = trits - lanes * (words - 1)
                self.assertEqual(self.lib.last_lanes(trits, fmt), last)
                payload_last = model.payload_bytes(fmt, trits) - 16 * (words - 1)
                self.assertEqual(self.lib.last_payload(last, fmt), payload_last, (trits, fmt))
        for n in range(0, 97):
            for base in (0, 32, 64):
                want = sum(3 << (2 * j) for j in range(32) if base + j < n)
                self.assertEqual(self.lib.therm(n, base), want)
        self.assertEqual(self.lib.top_mask(0, M64), 0)
        self.assertEqual(self.lib.top_mask(1, M64), M32)

    def test_decode_path_matches_the_model(self):
        rng = random.Random(64)
        for fmt in (0, 1):
            for i in range(1500):
                if i % 3 == 0:
                    word = rng.getrandbits(128)          # invalid groups included
                else:
                    word = model.encode_word(fmt, random_lanes(rng, model.LANES[fmt]))
                self.assertEqual(self.decode(fmt, word), model.decode_word(fmt, word), (fmt, hex(word)))

    def test_encode_path_matches_the_model(self):
        rng = random.Random(65)
        for _ in range(1500):
            lanes = random_lanes(rng, 80)
            chunks = [self.lib.d5_enc4((lanes >> (40 * c)) & ((1 << 40) - 1)) for c in range(4)]
            word = sum(chunk << (32 * c) for c, chunk in enumerate(chunks))
            self.assertEqual(word, model.encode_word(1, lanes))

    def test_counts_match_the_model(self):
        rng = random.Random(66)
        for _ in range(2000):
            count = rng.choice((64, 80))
            lanes = random_lanes(rng, count)
            l0, l1, l2 = self.lanes3(lanes)
            hp, hn = self.lib.class_counts(l0, l1, l2, 0), self.lib.class_counts(l0, l1, l2, 1)
            p, n = self.lib.class_sums(hp), self.lib.class_sums(hn)
            pos, neg = self.lib.sum4(self.lib.pair_sums(p)), self.lib.sum4(self.lib.pair_sums(n))
            dot = self.lib.sum4(self.lib.weighted_pairs(p)) - self.lib.sum4(self.lib.weighted_pairs(n))
            codes = [(lanes >> (2 * j)) & 3 for j in range(80)]
            self.assertEqual(pos, codes.count(1))
            self.assertEqual(neg, codes.count(2))
            want = sum(((j & 7) + 1) * (1 if c == 1 else -1 if c == 2 else 0) for j, c in enumerate(codes))
            self.assertEqual(dot, want)

    def test_checksum_step_and_selects(self):
        rng = random.Random(67)
        for _ in range(1000):
            chk, lo, hi = rng.getrandbits(64), rng.getrandbits(64), rng.getrandbits(64)
            want = model.rotl(model.rotl(chk, 1) ^ lo, 1) ^ hi
            self.assertEqual(self.lib.rotl2(chk) ^ (self.lib.rotl1(lo) ^ hi), want)
        self.assertEqual(self.lib.pick64(True, 1, 2), 1)
        self.assertEqual(self.lib.pick32(False, 1, 2), 2)


# ---- whole-top simulation ----

def replay(seed, trits, runs, fault):
    """What the reader must report, run by run, when the memory model applies `fault`: the same
    fills and reads as tests/sim_ddr3_top_model.v on tools/ddr3_read_model.py's data."""
    mem, drops = {}, 0
    bit, dq = fault.get("stuck_addr_bit"), fault.get("stuck_dq")

    def phys(a):
        if bit is None:
            return a
        return a | (1 << bit) if fault["stuck_addr_val"] else a & ~(1 << bit)

    def store(a, value):
        if dq is not None:
            for beat in range(8):
                pos = 8 * (2 * beat + dq // 8) + dq % 8
                value = value | (1 << pos) if fault["stuck_dq_val"] else value & ~(1 << pos)
        mem[phys(a)] = value

    out = []
    for run in range(runs):
        fmt, key = run & 1, model.pair_key(seed, run >> 1)
        words = model.words_of(fmt, trits)
        for w in range(words):
            data = model.encode_word(fmt, model.word_lanes(fmt, key, trits, w))
            if w == fault.get("drop_addr"):
                drops += 1
                if drops - 1 == fault.get("drop_nth", 0):
                    continue
            store(w, data)
        bad = inv = pos = neg = dot = chk = 0
        for w in range(words):
            data = mem.get(phys(w), 0)
            lanes, invalid = model.decode_word(fmt, data)
            bad += lanes != model.word_lanes(fmt, key, trits, w)
            inv += invalid
            for j in range(model.LANES[fmt]):
                code = (lanes >> (2 * j)) & 3
                pos += code == 1
                neg += code == 2
                dot += ((j & 7) + 1) * (1 if code == 1 else -1 if code == 2 else 0)
            chk = model.rotl(chk, 1) ^ (data & M64)
            chk = model.rotl(chk, 1) ^ (data >> 64)
        out.append({"run": run, "bad_words": bad, "invalid_groups": inv, "plus": pos, "minus": neg, "dot": dot,
                    "checksum": f"{chk:#018x}"})
    return out


CONFIGS = {
    "base": {"TRITS": 1237, "RUNS": 4},
    "cap3": {"TRITS": 1237, "RUNS": 2, "CAP": 3},
    "exact": {"TRITS": 160, "RUNS": 2},
    "one": {"TRITS": 1, "RUNS": 2},
    "wd": {"TRITS": 640, "RUNS": 2, "WATCHDOG": 2000},
}
CLEAN = (("base", ()), ("base", (("stall_pct", 0),)), ("base", (("ack_min", 30), ("ack_span", 40))),
         ("cap3", (("ack_min", 40), ("ack_span", 30))), ("exact", ()), ("one", ()))
FAULTS = ((("stuck_dq", 3), ("stuck_dq_val", 1)), (("stuck_dq", 12), ("stuck_dq_val", 0)),
          (("stuck_addr_bit", 2), ("stuck_addr_val", 1)), (("drop_addr", 5), ("drop_nth", 1)),
          (("drop_addr", 17), ("drop_nth", 1)))
FIELDS = ("bad_words", "invalid_groups", "plus", "minus", "dot", "checksum")
PHASE = re.compile(r"TB_PHASE n=(\d+) we=(\d) clocks=(\d+) taken=(\d+) acks=(\d+) cmd_stalls=(\d+) "
                   r"wait_stalls=(\d+) max_outstanding=(\d+)")


@unittest.skipUnless(HAVE_TOOLS, "T27_ROOT with a built t27c and Icarus required")
class ReaderSimulation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix="trinity-ddr3-reader-sim-")
        work = Path(cls.work.name)
        sources = []
        for core in CORES:
            verilog = subprocess.run([str(COMPILER), "gen-verilog", str(ROOT / f"t27/rtl/{core}.t27")],
                                     capture_output=True, text=True, check=True).stdout
            (work / f"{core}.v").write_text(verilog)
            sources.append(str(work / f"{core}.v"))
        sources += [str(ROOT / "fpga/ax7203/sim/xilinx_stubs.v"), str(ROOT / "fpga/ax7203/ddr3/tms_ddr3_ax7203.v"),
                    str(ROOT / "fpga/ax7203/ddr3/tms_ddr3_reader.v"), str(ROOT / "tests/sim_ddr3_top_model.v"),
                    str(ROOT / "tests/tb_ddr3_reader.v")]
        cls.vvp = {}
        for name, params in CONFIGS.items():
            out = work / f"{name}.vvp"
            flags = [f"-Ptb_ddr3_reader.{k}={v}" for k, v in params.items()]
            subprocess.run(["iverilog", "-g2012", "-DDDR3_READER", *flags, "-s", "tb_ddr3_reader", "-o", str(out),
                            *sources], check=True, capture_output=True, text=True)
            cls.vvp[name] = out
        cls.runs = list(CLEAN) + [("base", fault) for fault in FAULTS] + [("wd", (("hang_after", 5),)),
                                                                           ("wd", (("hang_after", 15),))]
        cls.results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
            for key, result in zip(cls.runs, pool.map(lambda k: cls.simulate(*k), cls.runs)):
                cls.results[key] = result

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    @classmethod
    def simulate(cls, config, plusargs):
        work = Path(cls.work.name)
        tag = config + "".join(f"_{k}{v}" for k, v in plusargs)
        capture_file, dump = work / f"{tag}.txt", work / f"{tag}.dump"
        p = CONFIGS[config]
        dump_words = max(model.words_of(0, p["TRITS"]), model.words_of(1, p["TRITS"]))
        args = ["vvp", str(cls.vvp[config]), f"+capture={capture_file}", f"+dump={dump}", f"+dump_words={dump_words}",
                *[f"+{k}={v}" for k, v in plusargs]]
        run = subprocess.run(args, cwd=work, capture_output=True, text=True, timeout=900)
        entries = []
        if capture_file.exists():
            for raw in capture_file.read_text().splitlines():
                t_ns, line = raw.split("\t", 1)
                entries.append({"t_s": int(t_ns) / 1e9, "line": line})
        phases = [dict(zip(("n", "we", "clocks", "taken", "acks", "cmd_stalls", "wait_stalls", "max_outstanding"),
                           map(int, m.groups()))) for m in PHASE.finditer(run.stdout)]
        words = [int(x, 16) for x in dump.read_text().split()] if dump.exists() else []
        return {"returncode": run.returncode, "stdout": run.stdout, "entries": entries, "phases": phases,
                "dump": words}

    def decoded(self, config, plusargs=()):
        result = self.results[(config, plusargs)]
        self.assertEqual(result["returncode"], 0, result["stdout"][-2000:])
        self.assertIn("TB_PASS", result["stdout"])
        return capture.decode(result["entries"], expect_build_id="5eedc0de", expect_lanes=2, expect_period_ps=3000,
                              nominal_hz=SIM_HZ, expect_reader=True)

    def test_clean_runs_equal_the_model(self):
        for config, plusargs in CLEAN:
            p = CONFIGS[config]
            decoded = self.decoded(config, plusargs)
            self.assertTrue(decoded["pass"], (config, plusargs, decoded["checks"]))
            reader = decoded["reader"]
            self.assertEqual(reader["header"]["trits"], p["TRITS"])
            self.assertEqual(reader["header"]["seed"], 98)
            self.assertEqual(reader["done"]["runs"], p["RUNS"])
            self.assertEqual(reader["totals"]["runs"], p["RUNS"])
            self.assertEqual(reader["totals"]["model_checked_runs"], p["RUNS"])
            self.assertEqual(reader["totals"]["complete_pairs"], p["RUNS"] // 2)
            self.assertFalse(decoded["unparsed_lines"])
            for r in reader["runs"]:
                self.assertTrue(r["checks"]["equals_host_model"], (config, r))
                self.assertEqual(r["consumer_stalls"], 0)
                self.assertEqual(r["bus_bytes"], 16 * r["words"])
                self.assertEqual(r["padding_bytes"], r["bus_bytes"] - r["payload_bytes"])
                self.assertEqual(r["logical_trits"], p["TRITS"])
                self.assertLessEqual(r["max_outstanding"], p.get("CAP", 64))
            for pair in reader["pairs"]:
                self.assertTrue(pair["same_logical_trits"], pair)

    def test_padding_is_exercised(self):
        runs = {r["format"]: r for r in self.decoded("base")["reader"]["runs"]}
        self.assertEqual((runs[0]["words"], runs[0]["payload_bytes"], runs[0]["padding_bytes"]), (20, 310, 10))
        self.assertEqual((runs[1]["words"], runs[1]["payload_bytes"], runs[1]["padding_bytes"]), (16, 248, 8))
        runs = {r["format"]: r for r in self.decoded("exact")["reader"]["runs"]}
        self.assertEqual((runs[0]["padding_bytes"], runs[1]["padding_bytes"]), (8, 0))
        runs = {r["format"]: r for r in self.decoded("one")["reader"]["runs"]}
        self.assertEqual((runs[0]["words"], runs[0]["payload_bytes"], runs[1]["payload_bytes"]), (1, 1, 1))

    def test_counters_equal_the_bench_monitor(self):
        """The reader's own clock, request and stall counts equal the bench's count of the port."""
        for config, plusargs in CLEAN:
            result = self.results[(config, plusargs)]
            runs = self.decoded(config, plusargs)["reader"]["runs"]
            phases = result["phases"]
            self.assertEqual(len(phases), 2 * len(runs), (config, plusargs))
            for r in runs:
                fill, read = phases[2 * r["run"]], phases[2 * r["run"] + 1]
                self.assertEqual((fill["we"], read["we"]), (1, 0))
                self.assertEqual((fill["clocks"], fill["taken"], fill["acks"], fill["cmd_stalls"],
                                  fill["max_outstanding"]),
                                 (r["fill_cycles"], r["fill_words"], r["fill_words"], r["fill_command_stalls"],
                                  r["fill_max_outstanding"]), (config, plusargs, r["run"]))
                self.assertEqual((read["clocks"], read["taken"], read["acks"], read["cmd_stalls"], read["wait_stalls"],
                                  read["max_outstanding"]),
                                 (r["cycles"], r["words"], r["words"], r["command_stalls"], r["wait_stalls"],
                                  r["max_outstanding"]), (config, plusargs, r["run"]))

    def test_cap_bounds_the_outstanding_requests(self):
        runs = self.decoded("cap3", (("ack_min", 40), ("ack_span", 30)))["reader"]["runs"]
        for r in runs:
            self.assertEqual(r["max_outstanding"], 3)
            self.assertGreater(r["cap_holds"], 0)
        # With the default cap (64) and the same latency more are in flight and the cap never holds.
        for r in self.decoded("base", (("ack_min", 30), ("ack_span", 40)))["reader"]["runs"]:
            self.assertGreater(r["max_outstanding"], 3)
            self.assertEqual(r["cap_holds"], 0)

    def test_memory_holds_the_payload_in_stream_order(self):
        """After the last fill (run 3, dense5, pair 1) the first 16 bursts hold its words and bursts
        16-19 still hold the baseline2 words of run 2 (same pair): byte n of a word at bits [8n, 8n+7],
        region byte k at byte k % 16 of burst k / 16."""
        p = CONFIGS["base"]
        dump = self.results[("base", ())]["dump"]
        key = model.pair_key(98, 1)
        d5, b2 = model.words_of(1, p["TRITS"]), model.words_of(0, p["TRITS"])
        self.assertEqual(len(dump), b2)
        for w in range(b2):
            fmt = 1 if w < d5 else 0
            self.assertEqual(dump[w], model.encode_word(fmt, model.word_lanes(fmt, key, p["TRITS"], w)), w)
        stream = b"".join(dump[w].to_bytes(16, "little") for w in range(d5))
        payload = model.payload_bytes(1, p["TRITS"])
        digits = [(stream[k // 5] // 3 ** (k % 5)) % 3 for k in range(p["TRITS"])]
        trits = [model.word_lanes(1, key, p["TRITS"], i // 80) >> (2 * (i % 80)) & 3 for i in range(p["TRITS"])]
        self.assertEqual(digits, [model.digit_of_lane(c) for c in trits])
        self.assertTrue(all(b == 121 for b in stream[payload:]), "padding bytes are logical zeros")

    def test_faults_are_detected_with_the_replayed_counts(self):
        for fault in FAULTS:
            decoded = self.decoded("base", fault)
            self.assertFalse(decoded["pass"], fault)
            self.assertFalse(decoded["checks"]["reader_runs_pass"], fault)
            self.assertTrue(decoded["checks"]["reader_well_formed"], fault)
            got = [{k: r[k] for k in ("run",) + FIELDS} for r in decoded["reader"]["runs"]]
            want = replay(98, CONFIGS["base"]["TRITS"], CONFIGS["base"]["RUNS"], dict(fault))
            self.assertEqual(got, want, fault)
            self.assertGreater(sum(r["bad_words"] for r in want), 0, fault)

    def test_a_hung_port_stops_the_reader(self):
        # 640 trits: run 0 (baseline2) writes and reads 10 words, so the 5th request is a write, the 15th a read.
        for fault, read_phase in (((("hang_after", 5),), False), ((("hang_after", 15),), True)):
            decoded = self.decoded("wd", fault)
            reader = decoded["reader"]
            self.assertIsNotNone(reader["timeout"], fault)
            self.assertEqual(reader["timeout"]["read_phase"], read_phase, fault)
            self.assertEqual(reader["timeout"]["run"], 0)
            self.assertFalse(decoded["pass"])
            self.assertEqual(reader["runs"], [])


class ReaderDecoder(unittest.TestCase):
    HZ = 83_333_333.3

    def line(self, tag, a, b):
        return f"{tag}{a:08x}{b:010x}"

    def entries(self, trits=1237, runs=2, seed=0x62, tamper=None):
        out = [{"t_s": 1.0, "line": "H5eedc0de0102000bb8"},
               {"t_s": 1.1, "line": "S01171700" + f"{100:010x}"},
               {"t_s": 1.2, "line": self.line("q", trits, (1 << 32) | (2 << 24) | 64)},
               {"t_s": 1.3, "line": self.line("k", seed, 433_000_000)},
               {"t_s": 1.4, "line": self.line("v", 0, 1 << 24)}]
        t = 2.0
        for run in range(runs):
            want = model.expected_run(seed, run, trits)
            fmt = run & 1
            dot = want["dot"] & M64
            fields = {"a": (run, (fmt << 32) | (model.LANES[fmt] << 24)), "f": (want["words"], want["words"] + 30),
                      "g": (12, 5), "c": (want["words"], want["words"] + 25), "y": (0, want["bus_bytes"]),
                      "p": (0, want["payload_bytes"]), "d": (0, want["padding_bytes"]), "n": (0, trits),
                      "o": (12, 4), "w": (0, 20), "u": (want["neg"], want["pos"]),
                      "s": (dot >> 40, dot & ((1 << 40) - 1)), "h": (want["chk"] >> 32, want["chk"] & M32)}
            if tamper:
                tamper(run, fields)
            for tag in "afgcypdnowush":
                out.append({"t_s": t, "line": self.line(tag, *fields[tag])})
                t += 0.002
        return out

    def test_clean_record_passes(self):
        result = capture.decode(self.entries(), nominal_hz=self.HZ, expect_reader=True)
        self.assertTrue(result["pass"], result["checks"])
        reader = result["reader"]
        self.assertEqual(reader["totals"]["runs"], 2)
        self.assertTrue(reader["pairs"][0]["same_logical_trits"])
        self.assertAlmostEqual(reader["runs"][0]["words_per_clock"], 20 / 45, 6)
        self.assertNotIn("pattern", result)

    def test_counters_that_do_not_add_up_fail(self):
        def bus(run, f):
            if run == 1:
                f["y"] = (0, f["y"][1] + 16)
        def model_dot(run, f):
            if run == 0:
                f["s"] = (0, 12345)
        def stall(run, f):
            f["y"] = (1, f["y"][1])
        for tamper, check in ((bus, "bus_bytes_is_words_x16"), (model_dot, "equals_host_model"),
                              (stall, "no_consumer_stall")):
            result = capture.decode(self.entries(tamper=tamper), nominal_hz=self.HZ, expect_reader=True)
            self.assertFalse(result["pass"], check)
            self.assertFalse(result["checks"]["reader_runs_pass"], check)
            self.assertIn(False, [r["checks"][check] for r in result["reader"]["runs"]], check)

    def test_lost_or_repeated_lines_fail(self):
        entries = self.entries(runs=4)
        lost = [e for i, e in enumerate(entries) if i != 12]
        self.assertFalse(capture.decode(lost, nominal_hz=self.HZ, expect_reader=True)["checks"]["reader_well_formed"])
        repeated = entries[:5 + 13] + entries[5:5 + 13] + entries[5 + 13:]
        result = capture.decode(repeated, nominal_hz=self.HZ, expect_reader=True)
        self.assertFalse(result["checks"]["reader_well_formed"])
        self.assertFalse(result["pass"])

    def test_a_build_without_the_reader_has_no_reader_checks(self):
        result = capture.decode(self.entries()[:2], nominal_hz=self.HZ)
        self.assertNotIn("reader", result)
        self.assertNotIn("reader_present", result["checks"])
        result = capture.decode(self.entries()[:2], nominal_hz=self.HZ, expect_reader=True)
        self.assertFalse(result["checks"]["reader_present"])

    def test_expectations_read_the_variant(self):
        report = {"ddr3": {"variant": "x16 BYTE_LANES 2, READER (trits 1237, seed 98, cap 64, runs 0)", "clocks": []}}
        expect = capture.expectations(report)
        self.assertTrue(expect["reader"])
        self.assertFalse(expect["pattern"])
        report = {"ddr3": {"variant": "x16 BYTE_LANES 2, PATTERN_TEST 1 (bursts 33554432)", "clocks": []}}
        self.assertFalse(capture.expectations(report)["reader"])

    def test_makefile_keeps_the_pattern_test_the_default(self):
        makefile = (ROOT / "fpga/ax7203/Makefile").read_text()
        self.assertIn("DDR3_APP       ?= pattern", makefile)
        rule = makefile.split("$(DDR3_BUILD)/$(DDR3_TOP).json:", 1)[1].split("\n\n", 1)[0]
        self.assertIn("$(DDR3_READER_DEFINE)", rule)
        self.assertIn("app-$(DDR3_APP)", makefile)
        top = (ROOT / "fpga/ax7203/ddr3/tms_ddr3_ax7203.v").read_text()
        # Everything of the reader in the top is under the define.
        code = re.sub(r"//[^\n]*", "", top)
        outside = re.sub(r"`ifdef DDR3_READER.*?`(else|endif)", "", code, flags=re.S)
        self.assertNotIn("READER_", outside)
        self.assertNotIn("tms_ddr3_reader", outside)


if __name__ == "__main__":
    unittest.main()
