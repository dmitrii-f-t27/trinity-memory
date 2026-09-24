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
  fill equal the model's baseline2 and dense5 encodings byte for byte (the byte order of the
  128-bit Wishbone word and of the payload across words; the model stores whole words, so where
  UberDDR3 puts each byte on the DDR3 beats and lanes is not simulated here). The stalls and
  latencies come from the model's pseudo-random sequence: the clean runs use several seeds of
  it (+lfsr_seed) and 75 % random stalls as well as 0 and 20 %; one configuration runs until
  reset (RUNS 0, the board builds' mode) and is stopped after five runs. Injected faults (a
  stuck DQ bit, a stuck address bit, a dropped write) are detected, and every run's bad words,
  invalid groups, +1 and -1 counts, dot product and checksum equal those of a Python replay of
  the fault; an extra ack with no request behind it, in a fill, in a read or after the last
  ack of a read, shows as a stray ack; a port that stops taking requests or loses an ack stops
  the reader (t line).
- The host model's two paths (pure Python and numpy) agree, and consumer (A)'s results computed
  with tools/bram_trit_model.py's own lane codes, codec, activation and rotate step equal the
  DDR3 model's.
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
                                ("d5_enc4", (u64,), u64), ("b2_dec16", (u64,), u64),
                                ("rep_tag", (u32, u32), u32), ("rep_last", (u32,), u32),
                                ("head_b", (u32, u32), u64)):
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

    def test_report_line_order(self):
        """The run lines come in the decoder's order (READER_FORMAT 2 on the q line: x last)."""
        order = "".join(chr(self.lib.rep_tag(1, n)) for n in range(self.lib.rep_last(1) + 1))
        self.assertEqual(order, capture.RUN_ORDER_2)
        self.assertEqual("".join(chr(self.lib.rep_tag(0, n)) for n in range(self.lib.rep_last(0) + 1)), "qkv")
        self.assertEqual(self.lib.head_b(2, 64) >> 32, 2)

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
    "free": {"TRITS": 1237, "RUNS": 0},
}
CLEAN = (("base", ()), ("base", (("stall_pct", 0),)), ("base", (("ack_min", 30), ("ack_span", 40))),
         ("cap3", (("ack_min", 40), ("ack_span", 30))), ("exact", ()), ("one", ()),
         ("base", (("lfsr_seed", "1234abcd"),)), ("base", (("lfsr_seed", "0badf00d"),)),
         ("base", (("stall_pct", 75),)), ("base", (("lfsr_seed", "deadbeef"), ("stall_pct", 75))),
         ("cap3", (("lfsr_seed", "5eed0062"), ("ack_min", 40), ("ack_span", 30))),
         ("exact", (("lfsr_seed", "00c0ffee"), ("stall_pct", 75))))
FREE = ("free", (("stop_runs", 5),))
# Acks 0-19 are run 0's fill (baseline2, 20 words), 20-39 its read, 40-55 run 1's fill (dense5).
DUP_ACKS = ((("dup_ack", 5),), (("dup_ack", 25),), (("dup_ack", 39), ("stall_pct", 0)), (("dup_ack", 45),))
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
        cls.runs = (list(CLEAN) + [("base", fault) for fault in FAULTS + DUP_ACKS] + [FREE]
                    + [("wd", (("hang_after", 5),)), ("wd", (("hang_after", 15),)),
                       ("wd", (("lose_ack", 4),)), ("wd", (("lose_ack", 12),))])
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
            self.assertEqual(reader["header"]["format"], 2)
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
                self.assertEqual(r["stray_acks"], 0)
                self.assertTrue(r["checks"]["all_acks_equal_the_words"], (config, r))
            self.assertEqual(reader["runs"][-1]["all_acks"],
                             sum(r["fill_words"] + r["words"] for r in reader["runs"]))
            for pair in reader["pairs"]:
                self.assertTrue(pair["same_logical_trits"], pair)

    def test_the_seeds_change_the_traces(self):
        """+lfsr_seed and +stall_pct give other stall and latency sequences (not one fixed trace)."""
        stalls = {plusargs: tuple(r["command_stalls"] for r in self.decoded(config, plusargs)["reader"]["runs"])
                  for config, plusargs in CLEAN if config == "base"}
        self.assertEqual(len(set(stalls.values())), len(stalls), stalls)
        heavy = self.decoded("base", (("stall_pct", 75),))["reader"]["runs"]
        light = self.decoded("base")["reader"]["runs"]
        self.assertGreater(min(r["command_stalls"] for r in heavy), 2 * max(r["command_stalls"] for r in light))

    def test_until_reset_mode(self):
        """RUNS 0 (the board builds): runs go on without a z line; stopped after five runs."""
        decoded = self.decoded(*FREE)
        self.assertTrue(decoded["pass"], decoded["checks"])
        reader = decoded["reader"]
        self.assertEqual(reader["header"]["runs_configured"], 0)
        self.assertIsNone(reader["done"])
        self.assertIsNone(reader["timeout"])
        self.assertEqual([r["run"] for r in reader["runs"]], [0, 1, 2, 3, 4])
        self.assertEqual(reader["totals"]["model_checked_runs"], 5)
        self.assertTrue(all(r["pass"] for r in reader["runs"]))

    def test_an_extra_ack_shows_as_a_stray_ack(self):
        """Words and fill words are latched at the W-th ack, so an extra ack moves the phase end and a
        real ack lands outside FILL and READ: the stray-ack count catches it wherever it came."""
        for plusargs in DUP_ACKS:
            result = self.results[("base", plusargs)]
            self.assertIn("MODEL_DUP_ACK", result["stdout"], plusargs)
            decoded = self.decoded("base", plusargs)
            self.assertFalse(decoded["pass"], plusargs)
            runs = decoded["reader"]["runs"]
            first = next(r for r in runs if r["stray_acks"])
            self.assertEqual(first["stray_acks"], 1, plusargs)
            self.assertFalse(first["checks"]["no_stray_ack"], plusargs)
            self.assertTrue(first["checks"]["all_acks_equal_the_words"], plusargs)
            # The words, bus bytes and fill words do not show it: they are fixed by the stop condition.
            for r in runs:
                self.assertEqual(r["words"], model.words_of(r["format"], CONFIGS["base"]["TRITS"]))
                self.assertEqual(r["fill_words"], r["words"])
                self.assertEqual(r["bus_bytes"], 16 * r["words"])
            self.assertEqual(first["run"], 1 if plusargs[0][1] >= 40 else 0, plusargs)

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
        for fault, read_phase in (((("hang_after", 5),), False), ((("hang_after", 15),), True),
                                  ((("lose_ack", 4),), False), ((("lose_ack", 12),), True)):
            decoded = self.decoded("wd", fault)
            reader = decoded["reader"]
            self.assertIsNotNone(reader["timeout"], fault)
            self.assertEqual(reader["timeout"]["read_phase"], read_phase, fault)
            self.assertEqual(reader["timeout"]["run"], 0)
            self.assertFalse(decoded["pass"])
            self.assertEqual(reader["runs"], [])


def have_numpy() -> bool:
    try:
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


class ReaderModelPaths(unittest.TestCase):
    """The host model's pure-Python statement and its numpy path agree, and both agree with consumer
    (A)'s results recomputed from tools/bram_trit_model.py's own definitions (lane codes, dense5 and
    baseline2 codec, activation (i mod 8) + 1, rotl1) on the DDR3 stream's lanes."""
    TRITS = (1, 4, 5, 63, 64, 65, 79, 80, 81, 159, 160, 161, 1237, 4096, 12345)

    def keys(self):
        rng = random.Random(621)
        return [model.pair_key(98, 0), 0, M64] + [model.pair_key(rng.getrandbits(32), rng.getrandbits(16))
                                                  for _ in range(2)]

    @unittest.skipUnless(have_numpy(), "numpy not installed")
    def test_pure_and_numpy_paths_agree(self):
        for trits in self.TRITS:
            for fmt in (0, 1):
                for key in self.keys():
                    self.assertEqual(model.run_results(fmt, key, trits), model.run_results_fast(fmt, key, trits),
                                     (fmt, trits, hex(key)))

    def test_definitions_are_the_block_ram_models(self):
        import bram_trit_model as bram
        for trits in self.TRITS:
            for fmt in (0, 1):
                for key in self.keys()[:3]:
                    self.assertEqual(self.via_bram_model(bram, fmt, key, trits),
                                     {k: model.run_results(fmt, key, trits)[k] for k in ("words", "pos", "neg",
                                                                                          "dot", "chk")},
                                     (fmt, trits, hex(key)))

    @staticmethod
    def via_bram_model(bram, fmt, key, trits):
        """Consumer (A) on the region with the block-RAM model's pieces: each 128-bit word as four
        32-bit chunks of its codec (16 baseline2 lanes or four dense5 bytes of 20 lanes)."""
        per, bfmt = (16, bram.FMT_B2) if fmt == 0 else (20, bram.FMT_D5)
        words = model.words_of(fmt, trits)
        pos = neg = dot = chk = 0
        for w in range(words):
            lanes = model.word_lanes(fmt, key, trits, w)
            data = back = invalid = 0
            for c in range(4):
                chunk = (lanes >> (2 * per * c)) & ((1 << (2 * per)) - 1)
                code = bram.encode_word(bfmt, chunk) & M32
                data |= code << (32 * c)
                got, bad = bram.decode_word(bfmt, code)
                back |= (got & ((1 << (2 * per)) - 1)) << (2 * per * c)
                invalid += bad
            assert (back, invalid) == (lanes, 0)
            for j in range(model.LANES[fmt]):
                t = bram.trit_of_lane((lanes >> (2 * j)) & 3)
                pos += t == 1
                neg += t == -1
                dot += t * bram.activation(model.LANES[fmt] * w + j)
            chk = bram.rotl1(chk) ^ (data & M64)
            chk = bram.rotl1(chk) ^ (data >> 64)
        return {"words": words, "pos": pos, "neg": neg, "dot": dot, "chk": chk}


class ReaderBoardRecords(unittest.TestCase):
    """The committed board captures of the read path (reports/fpga/ddr3-reader-*/load*/) decode to
    what their records say. The host model's run results take ~0.3 s each with numpy for the
    2560 x 6912 region, so a fresh decode checks the first three runs of each load against the
    model (none without numpy) and the rest against the record, which checked every run."""
    RUN_FIELDS = ("run", "format", "fill_words", "fill_cycles", "fill_max_outstanding", "fill_command_stalls",
                  "words", "cycles", "consumer_stalls", "bus_bytes", "scale_metadata_bytes", "payload_bytes",
                  "bad_words", "padding_bytes", "invalid_groups", "logical_trits", "max_outstanding",
                  "command_stalls", "cap_holds", "wait_stalls", "minus", "plus", "dot", "checksum")

    def records(self):
        return sorted((ROOT / "reports/fpga").glob("ddr3-reader-*/load*/"))

    def test_records_decode_again(self):
        model_runs = 3 if have_numpy() else 0
        self.assertEqual(len(self.records()), 5)      # seed 1: loads 1-2; seed 6: loads 1-3
        for directory in self.records():
            import json
            record = json.loads(capture.read_kept(directory / "capture.json"))
            entries = []
            for raw in capture.read_kept(directory / record["uart_transcript"]["file"]).decode().splitlines():
                if raw.startswith("#") or not raw.strip():
                    continue
                t_s, _utc, line = raw.split("\t", 2)
                entries.append({"t_s": float(t_s), "line": line})
            expect = record["expect"]
            self.assertTrue(expect["reader"], directory)
            again = capture.decode(entries, expect_build_id=expect["build_id"], expect_lanes=expect["lanes"],
                                   expect_period_ps=expect["period_ps"], load_end_s=record["run"].get("load_end_s"),
                                   nominal_hz=expect["nominal_hz"], expect_pattern=expect.get("pattern"),
                                   expect_reader=True, reader_model_runs=model_runs)
            kept = record["decoded"]
            self.assertEqual(again["final"], kept["final"], directory)
            self.assertEqual(again["pass"], kept["pass"], directory)
            self.assertEqual(again["checks"], kept["checks"], directory)
            got = [{k: r[k] for k in self.RUN_FIELDS} for r in again["reader"]["runs"]]
            want = [{k: r[k] for k in self.RUN_FIELDS} for r in kept["reader"]["runs"]]
            self.assertEqual(got, want, directory)
            self.assertEqual(kept["reader"]["totals"]["model_checked_runs"], len(kept["reader"]["runs"]), directory)
            for r in again["reader"]["runs"][:model_runs]:
                self.assertTrue(r["checks"]["equals_host_model"], (directory, r["run"]))

    def test_pinned_board_results(self):
        """The figures docs/hardware.md quotes ("DDR3 read path (#62)")."""
        import json
        summary = json.loads((ROOT / "reports/fpga/ddr3-reader-summary-2026-09-24-a6d9745f-x16.json").read_text())
        seed6 = summary["board"]["seed6"]
        self.assertEqual([x["runs"] for x in seed6], [549, 553, 551])
        self.assertTrue(all(x["passing_runs"] == x["runs"] == x["model_checked_runs"] for x in seed6))
        self.assertTrue(all(x["bad_words"] == x["invalid_groups"] == x["consumer_stalls"] == 0 for x in seed6))
        self.assertTrue(all((x["calib_complete"], x["state"], x["returns_to_idle"]) == (1, 23, 0) for x in seed6))
        for x in seed6:
            self.assertEqual(x["per_format"]["baseline2"]["words"], [276480])
            self.assertEqual(x["per_format"]["dense5"]["words"], [221184])
            self.assertEqual((x["per_format"]["baseline2"]["cycles_min"], x["per_format"]["baseline2"]["cycles_max"]),
                             (292614, 292652))
            self.assertEqual((x["per_format"]["dense5"]["cycles_min"], x["per_format"]["dense5"]["cycles_max"]),
                             (234085, 234125))
            self.assertEqual(x["per_format"]["dense5"]["cap_holds_max"], 0)
        seed1 = summary["board"]["seed1"]
        self.assertTrue(all((x["calib_complete"], x["highest_state"], x["returns_to_idle"], x["runs"]) == (0, 14, 255, 0)
                            for x in seed1))


class ReaderDecoder(unittest.TestCase):
    HZ = 83_333_333.3

    def line(self, tag, a, b):
        return f"{tag}{a:08x}{b:010x}"

    def entries(self, trits=1237, runs=2, seed=0x62, tamper=None, reader_format=2):
        out = [{"t_s": 1.0, "line": "H5eedc0de0102000bb8"},
               {"t_s": 1.1, "line": "S01171700" + f"{100:010x}"},
               {"t_s": 1.2, "line": self.line("q", trits, (reader_format << 32) | (2 << 24) | 64)},
               {"t_s": 1.3, "line": self.line("k", seed, 433_000_000)},
               {"t_s": 1.4, "line": self.line("v", 0, 1 << 24)}]
        t = 2.0
        acks = 0
        for run in range(runs):
            want = model.expected_run(seed, run, trits)
            fmt = run & 1
            dot = want["dot"] & M64
            fields = {"a": (run, (fmt << 32) | (model.LANES[fmt] << 24)), "f": (want["words"], want["words"] + 30),
                      "g": (12, 5), "c": (want["words"], want["words"] + 25), "y": (0, want["bus_bytes"]),
                      "p": (0, want["payload_bytes"]), "d": (0, want["padding_bytes"]), "n": (0, trits),
                      "o": (12, 4), "w": (0, 20), "u": (want["neg"], want["pos"]),
                      "s": (dot >> 40, dot & ((1 << 40) - 1)), "h": (want["chk"] >> 32, want["chk"] & M32)}
            acks += 2 * want["words"]
            fields["x"] = (0, acks)
            if tamper:
                tamper(run, fields)
            for tag in capture.RUN_ORDER_2 if reader_format >= 2 else capture.RUN_ORDER:
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
        def stray(run, f):
            if run == 1:
                f["x"] = (1, f["x"][1] + 1)
        def acks(run, f):
            if run == 0:
                f["x"] = (0, f["x"][1] + 1)
        for tamper, check in ((bus, "bus_bytes_is_words_x16"), (model_dot, "equals_host_model"),
                              (stall, "no_consumer_stall"), (stray, "no_stray_ack"),
                              (acks, "all_acks_equal_the_words")):
            result = capture.decode(self.entries(tamper=tamper), nominal_hz=self.HZ, expect_reader=True)
            self.assertFalse(result["pass"], check)
            self.assertFalse(result["checks"]["reader_runs_pass"], check)
            self.assertIn(False, [r["checks"][check] for r in result["reader"]["runs"]], check)

    def test_lost_or_repeated_lines_fail(self):
        entries = self.entries(runs=4)
        lost = [e for i, e in enumerate(entries) if i != 12]
        self.assertFalse(capture.decode(lost, nominal_hz=self.HZ, expect_reader=True)["checks"]["reader_well_formed"])
        n = len(capture.RUN_ORDER_2)
        repeated = entries[:5 + n] + entries[5:5 + n] + entries[5 + n:]
        result = capture.decode(repeated, nominal_hz=self.HZ, expect_reader=True)
        self.assertFalse(result["checks"]["reader_well_formed"])
        self.assertFalse(result["pass"])

    def test_reader_format_1_records_have_no_x_line(self):
        """The a6d9745f build (READER_FORMAT 1) ends a run with h; its records decode as before."""
        result = capture.decode(self.entries(reader_format=1), nominal_hz=self.HZ, expect_reader=True)
        self.assertTrue(result["pass"], result["checks"])
        self.assertNotIn("no_stray_ack", result["reader"]["runs"][0]["checks"])
        self.assertIsNone(result["reader"]["totals"]["stray_acks"])
        # An x line in a format-1 record, or a format-2 run without one, is a malformed record.
        entries = self.entries(reader_format=1)
        entries.insert(5 + len(capture.RUN_ORDER), {"t_s": 2.5, "line": self.line("x", 0, 1)})
        self.assertFalse(capture.decode(entries, nominal_hz=self.HZ, expect_reader=True)["checks"]["reader_well_formed"])
        entries = [e for e in self.entries() if not e["line"].startswith("x")]
        self.assertFalse(capture.decode(entries, nominal_hz=self.HZ, expect_reader=True)["checks"]["reader_well_formed"])

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
