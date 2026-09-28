#!/usr/bin/env python3
"""Tests of the DDR3 device matvec (issue #64): t27/rtl/fpga_ddr3_matvec.t27.

Two layers, as tests/test_ddr3_reader.py for the #62 reader:
- the module's functions (gen-c, through ctypes) against tools/matvec_device_model.py;
- the whole module in Icarus (tests/tb_ddr3_matvec.v: a behavioural Wishbone
  memory with stalls and ack latency) against the model's prediction of every
  report line, on seeded random trits and activations in both formats, with an
  invalid dense5 code in one word, and with a doorbell whose magic does not
  match (no run, only the header lines).

The stage-1 golden chunk (q_proj rows 0-319 against the first 320 accumulators
of reports/ternary-check/matvec-2026-09-23.json) runs when the fixture ranges
are cached (python3 tools/fetch-fixtures.py); like the wasm replay, it is
skipped on a clean clone.
"""
from __future__ import annotations

import ctypes as C
import importlib.util
import json
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
import matvec_device_model as model  # noqa: E402

COMPILER = ROOT / "build/compiler/target/release/t27c"
SOURCE = ROOT / "t27/rtl/fpga_ddr3_matvec.t27"
HAVE_TOOLS = bool(COMPILER.is_file() and shutil.which("iverilog") and shutil.which("vvp"))
M64 = (1 << 64) - 1


def to_i64(value: int) -> int:
    return value - (1 << 64) if value >= (1 << 63) else value


@unittest.skipUnless(COMPILER.is_file(), "build/compiler at native/compiler.lock with a built t27c required")
class MatvecFunctions(unittest.TestCase):
    """The module's C functions against the model."""

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix="trinity-matvec-fn-")
        work = Path(cls.work.name)
        generated = subprocess.run([str(COMPILER), "gen-c", str(SOURCE)], capture_output=True,
                                   text=True, check=True).stdout
        c_file = work / "matvec.c"
        # The pinned gen-c emits "type name[N] = 0;" for module arrays, which is
        # not C; the store's arrays show the same gap (only gen-verilog is used
        # there). Zero-initialise as an initializer list for the function tests.
        generated = re.sub(r"(\w+\[\d+\]) = 0;", r"\1 = {0};", generated)
        c_file.write_text(generated)
        library = work / "libmatvec.dylib" if sys.platform == "darwin" else work / "libmatvec.so"
        subprocess.run(["cc", "-shared", "-fPIC", "-O1", "-Wno-parentheses-equality",
                        "-Wno-shift-count-overflow", str(c_file), "-o", str(library)],
                       check=True, capture_output=True)
        cls.lib = C.CDLL(str(library))
        cls.functions = {}
        for name in ("words_per_row", "subs_of", "act_words_of", "invalid_of"):
            function = getattr(cls.lib, name)
            function.argtypes = [C.c_uint32, C.c_uint32]
            function.restype = C.c_uint32
            cls.functions[name] = function

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def test_alignment_guard(self):
        for fmt, cols, want in ((0, 2560, 40), (1, 2560, 32), (0, 6912, 108), (1, 6912, 0),
                                (0, 2559, 39), (1, 2559, 0), (0, 64, 1), (1, 80, 1)):
            self.assertEqual(self.functions["words_per_row"](cols, fmt), want, (fmt, cols))
            self.assertEqual(self.functions["subs_of"](fmt, 0), model.subs_of(fmt))

    def test_act_words(self):
        for cols in (1, 16, 2560, 6912, 7168):
            self.assertEqual(self.functions["act_words_of"](cols, 0), model.act_words_of(cols))

    def test_host_summary_decoder_matches_generated_rtl_contract_functions(self):
        # Distinct sentinels detect swapped UART halves and shifted tags.
        run_a, run_b = self.lib.run_a, self.lib.run_b
        run_a.argtypes, run_a.restype = [C.c_uint32] * 8, C.c_uint32
        run_b.argtypes = [C.c_uint64] * 5 + [C.c_uint32, C.c_uint64, C.c_uint32]
        run_b.restype = C.c_uint64
        lines = [(tag, run_a(3, 10240, 4, 900, 0, 160, 7, i),
                  run_b(11, 1000, 12, 13, 14, 15, 0, i))
                 for i, tag in enumerate("dcownuz")]
        decoded = model.decode_run_summary(lines)
        self.assertEqual(decoded["cycles"], 1000)
        self.assertEqual(decoded["issue_hold_cycles"], 900)
        self.assertEqual(decoded["command_stalls"], 12)
        self.assertEqual(decoded["wait_stalls"], 13)
        self.assertEqual(decoded["consumer_stalls"], 14)
        self.assertEqual(decoded["stray_acks"], 15)

    def test_buffered_result_sign_extension(self):
        signed = self.lib.signed_row
        signed.argtypes, signed.restype = [C.c_uint32], C.c_uint64
        for value in (-917504, -1048576, -1, 0, 1, 917504, 1048575):
            self.assertEqual(to_i64(signed(value & ((1 << 21) - 1))), value)


@unittest.skipUnless(HAVE_TOOLS, "build/compiler t27c and Icarus required")
class MatvecSimulation(unittest.TestCase):
    """The whole module in Icarus against the model's line-by-line prediction."""

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix="trinity-matvec-sim-")
        work = Path(cls.work.name)
        generated = subprocess.run([str(COMPILER), "gen-verilog", str(SOURCE)], capture_output=True,
                                   text=True, check=True).stdout
        (work / "matvec.v").write_text(generated)
        arbiter = subprocess.run([str(COMPILER), "gen-verilog", str(ROOT / "t27/rtl/fpga_wb_arbiter.t27")],
                                 capture_output=True, text=True, check=True).stdout
        (work / "arbiter.v").write_text(arbiter)
        cls.work_path = work
        cls.vvp = {}
        for name, params in CONFIGS.items():
            out = work / f"{name}.vvp"
            flags = []
            for key, value in params.items():
                flags.append(f"-Ptb_ddr3_matvec.{key}={value}")
            subprocess.run(["iverilog", "-g2012", *flags, "-s", "tb_ddr3_matvec", "-o", str(out),
                            str(work / "matvec.v"), str(work / "arbiter.v"), str(ROOT / "tests/tb_ddr3_matvec.v")],
                           check=True, capture_output=True, text=True)
            cls.vvp[name] = out

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def write_region(self, path, base_addr, words64):
        """One $readmemh file: 64-bit words at the folded indices of `base_addr`."""
        start = (base_addr & 32767) * 2
        with path.open("w") as handle:
            handle.write(f"@{start:x}\n")  # $readmemh addresses parse as hex
            for value in words64:
                handle.write(f"{value & M64:016x}\n")

    def simulate(self, config, knobs, activations, words, cols, rows, fmt, magic=model.MAGIC):
        tag = f"{config}-{'-'.join(f'{k}{v}' for k, v in knobs) or 'base'}"
        work = self.work_path
        capture = work / f"{tag}.lines"
        doorbell = work / f"{tag}.doorbell.mem"
        acts_mem = work / f"{tag}.acts.mem"
        weights = work / f"{tag}.weights.mem"
        run_lo, run_hi = model.doorbell(1, magic)
        self.write_region(doorbell, DOORBELL_ADDR, [run_lo | (run_hi << 32)])
        act_halves = []
        for lo, hi in model.act_words(activations):
            act_halves.extend((lo, hi))
        self.write_region(acts_mem, ACTS_ADDR, act_halves)
        weight_halves = []
        for lo, hi in words:
            weight_halves.extend((lo, hi))
        self.write_region(weights, WEIGHTS_ADDR, weight_halves)
        plusargs = [f"+capture={capture}", f"+doorbell={doorbell}", f"+acts={acts_mem}",
                    f"+weights={weights}"] + [f"+{k}={v}" for k, v in knobs]
        subprocess.run(["vvp", str(self.vvp[config]), *plusargs], cwd=work,
                       capture_output=True, text=True, check=True, timeout=600)
        lines = []
        for row in capture.read_text().splitlines():
            when, name, a, b = row.split("\t")
            lines.append((name, int(a), int(b)))
        return lines

    def chunk_words(self, trits, cols, rows, fmt, corrupt=None):
        words = []
        wpr = model.words_per_row(cols, fmt)
        for r in range(rows):
            for w in range(wpr):
                lo, hi = model.encode_word(fmt, model.lane_word(trits, r * cols + w * model.LANES[fmt]))
                words.append((lo, hi))
        if corrupt is not None:
            lo, hi = words[corrupt]
            words[corrupt] = (lo | (255 << (8 * 3)), hi)  # an invalid dense5 byte in chunk 0
        return words

    def assert_run_matches(self, lines, words, activations, cols, rows, fmt, run_number=1):
        expectation = expect_from_words(words, activations, cols, rows, fmt)
        head = model.head_lines(cols, rows, fmt, CONFIG_CAP[fmt], POLL_DIV)
        self.assertEqual([line[0] for line in lines[:3]], ["q", "k", "v"])
        for got, want in zip(lines[:3], head):
            self.assertEqual(got, want, (got, want))
        body = lines[3:]
        self.assertEqual([name for name, _, _ in body[:rows]],
                         ["y"] * rows, "one Y line per row before the summary")
        self.assertEqual([(name, a, b) for name, a, b in body[:rows]], expectation["y_lines"])
        summary = body[rows:]
        self.assertEqual([name for name, _, _ in summary], ["d", "c", "o", "w", "n", "u", "z"])
        by_tag = {name: (a, b) for name, a, b in summary}
        self.assertEqual(by_tag["d"][0], run_number)
        self.assertEqual(by_tag["c"][0], expectation["words"])
        self.assertEqual(by_tag["n"][0], expectation["bad_words"])
        self.assertEqual(by_tag["u"][0], expectation["act_words"])
        self.assertEqual(by_tag["z"], (run_number, (run_number * expectation["bad_words"]) & model.B40))
        return expectation

    def run_chunk(self, config, knobs, seed, cols, rows, fmt, corrupt=None, magic=model.MAGIC):
        rng = random.Random(seed)
        trits = [rng.choice((-1, 0, 1)) for _ in range(rows * cols)]
        activations = [rng.randint(-128, 127) for _ in range(cols)]
        words = self.chunk_words(trits, cols, rows, fmt, corrupt)
        lines = self.simulate(config, knobs, activations, words, cols, rows, fmt, magic)
        return self.assert_run_matches(lines, words, activations, cols, rows, fmt)

    def test_dense5_chunk(self):
        self.run_chunk("d5", (), 0x64D5, 2560, 8, model.FMT_D5)

    def test_dense5_with_stalls_and_a_corrupt_word(self):
        self.run_chunk("d5c", (("stall_pct", 50), ("ack_min", 2), ("ack_span", 6)),
                       0xC0DEC5, 2560, 4, model.FMT_D5, corrupt=1)

    def test_baseline2_chunk(self):
        self.run_chunk("b2", (), 0xB2B2B2, 2560, 6, model.FMT_B2)

    def test_baseline2_wide_columns(self):
        # 432 act words + the first 108-word row at one request at a time cross
        # the bench's default 6000 quiet clocks before the first Y line.
        self.run_chunk("b2_wide", (("stop_after", 40000),), 0x6912, 6912, 2, model.FMT_B2)

    def test_deferred_output_timing_is_independent_of_uart_latency(self):
        for fmt, config in ((model.FMT_D5, "d5_deferred"), (model.FMT_B2, "b2_deferred"),
                             (model.FMT_D5, "d5_deferred_fast"), (model.FMT_B2, "b2_deferred_fast")):
            with self.subTest(format=fmt):
                rng = random.Random(6455)
                cols, rows = 2560, 2
                trits = [rng.choice((-1, 0, 1)) for _ in range(rows * cols)]
                activations = [rng.randint(-128, 127) for _ in range(cols)]
                words = self.chunk_words(trits, cols, rows, fmt)
                counters = []
                for line_clocks in (24, 2000):
                    knobs = (("line_clocks", line_clocks), ("ack_min", 12), ("ack_span", 0),
                             ("stop_after", 10000))
                    lines = self.simulate(config, knobs, activations, words, cols, rows, fmt)
                    self.assert_run_matches(lines, words, activations, cols, rows, fmt)
                    counters.append(model.decode_run_summary(lines[3 + rows:]))
                self.assertEqual(counters[0]["cycles"], counters[1]["cycles"])
                for counter in counters:
                    self.assertEqual(counter["max_outstanding"], CONFIG_CAP[fmt])
                    self.assertEqual(counter["stray_acks"], 0)

    def test_deferred_buffer_rejects_more_rows_than_it_can_hold(self):
        lines = self.simulate("too_many_rows", (("stop_after", 800),), [0] * 2560,
                              [(0, 0)], 2560, 4097, model.FMT_D5)
        self.assertEqual([name for name, _, _ in lines], ["q", "k", "v"])

    def test_pipeline_with_real_arbiter_competing_master_and_memory_stalls(self):
        for fmt, config in ((model.FMT_D5, "d5_shared"), (model.FMT_B2, "b2_shared")):
            with self.subTest(format=fmt):
                self.run_chunk(config, (("stall_pct", 50), ("ack_min", 12), ("ack_span", 6),
                                        ("stop_after", 10000)), 0x12345, 2560, 2, fmt)

    def test_repeated_doorbells_reload_activations_and_overwrite_results(self):
        for fmt, config, rows in ((model.FMT_D5, "d5", 8), (model.FMT_D5, "d5_deferred", 2),
                                   (model.FMT_B2, "b2_deferred", 2),
                                   (model.FMT_D5, "d5_deferred_fast", 2),
                                   (model.FMT_B2, "b2_deferred_fast", 2)):
            with self.subTest(format=fmt, mode=config):
                rng = random.Random(88)
                cols = 2560
                acts = [rng.randint(-128, 127) for _ in range(cols)]
                trits = [rng.choice((-1, 0, 1)) for _ in range(rows * cols)]
                words = self.chunk_words(trits, cols, rows, fmt)
                lines = self.simulate(config, (("runs", 2), ("clear_acts_between_runs", 1),
                                               ("stop_after", 10000)), acts, words, cols, rows, fmt)
                size = rows + 7
                self.assertEqual(len(lines), 3 + 2 * size, "both doorbells must produce a full run")
                self.assert_run_matches(lines[:3 + size], words, acts, cols, rows, fmt)
                self.assert_run_matches(lines[:3] + lines[3 + size:], words, [0] * cols,
                                        cols, rows, fmt, run_number=2)

    def test_unchanged_doorbell_does_not_repeat_a_completed_run(self):
        for fmt, config, rows in ((model.FMT_D5, "d5", 8), (model.FMT_D5, "d5_deferred", 2),
                                  (model.FMT_B2, "b2_deferred", 2),
                                   (model.FMT_D5, "d5_deferred_fast", 2),
                                   (model.FMT_B2, "b2_deferred_fast", 2)):
            with self.subTest(format=fmt, mode=config):
                rng = random.Random(89)
                acts = [rng.randint(-128, 127) for _ in range(2560)]
                words = self.chunk_words([rng.choice((-1, 0, 1)) for _ in range(rows * 2560)],
                                         2560, rows, fmt)
                lines = self.simulate(config, (("stop_on_summary", 0), ("stop_after", 4000)),
                                      acts, words, 2560, rows, fmt)
                self.assertEqual(len(lines), 3 + rows + 7, "unchanged doorbell must execute exactly once")
                self.assert_run_matches(lines, words, acts, 2560, rows, fmt)

    def test_compute_pipeline_preserves_results_and_reduces_cycles(self):
        for fmt, name in ((model.FMT_D5, "d5_deferred"), (model.FMT_B2, "b2_deferred")):
            with self.subTest(format=fmt):
                rng = random.Random(0x88)
                cols, rows = 2560, 2
                # Signed endpoints and varying adjacent lanes expose RAM/subgroup skew.
                acts = [(-128, 127, 0, -1, 1)[i % 5] for i in range(cols)]
                trits = [rng.choice((-1, 0, 1)) for _ in range(cols * rows)]
                words = self.chunk_words(trits, cols, rows, fmt)
                cycles = []
                for config in (name, name + "_fast"):
                    lines = self.simulate(config, (("ack_min", 12), ("ack_span", 0)),
                                          acts, words, cols, rows, fmt)
                    self.assert_run_matches(lines, words, acts, cols, rows, fmt)
                    cycles.append(model.decode_run_summary(lines[3 + rows:])["cycles"])
                self.assertLess(cycles[1] * 10, cycles[0] * 7, cycles)

    def test_compute_pipeline_with_invalid_words_and_shared_bus(self):
        for fmt, name in ((model.FMT_D5, "d5_shared_fast"), (model.FMT_B2, "b2_shared_fast")):
            with self.subTest(format=fmt):
                rng = random.Random(0xBAD88)
                cols, rows = 2560, 2
                acts = [rng.randint(-128, 127) for _ in range(cols)]
                words = self.chunk_words([rng.choice((-1, 0, 1)) for _ in range(cols * rows)],
                                         cols, rows, fmt)
                for index in (0, len(words) // 2 - 1, len(words) - 1):
                    lo, hi = words[index]
                    words[index] = (lo | (255 if fmt == model.FMT_D5 else 3), hi)
                lines = self.simulate(name, (("stall_pct", 60), ("ack_min", 2), ("ack_span", 30),
                                            ("stop_after", 10000)), acts, words, cols, rows, fmt)
                self.assert_run_matches(lines, words, acts, cols, rows, fmt)
                self.assertEqual(model.decode_run_summary(lines[3 + rows:])["stray_acks"], 0)

    def test_a_doorbell_without_the_magic_starts_nothing(self):
        # The bench writes the descriptor with the model's magic; this config
        # binds a different one, so only the header lines may appear.
        rng = random.Random(1)
        activations = [rng.randint(-128, 127) for _ in range(2560)]
        words = [(0, 0)] * 64
        lines = self.simulate("wrong_magic", (("stop_after", 800),), activations, words,
                              2560, 2, model.FMT_D5, magic=model.MAGIC)
        self.assertEqual([name for name, _, _ in lines], ["q", "k", "v"])


CONFIG_CAP = {model.FMT_D5: 4, model.FMT_B2: 3}
POLL_DIV = 8
DOORBELL_ADDR, ACTS_ADDR, WEIGHTS_ADDR = 0x40, 0x1000, 0x4000
CONFIGS = {
    "d5": {"FMT": 1, "ROWS": 8, "CAP": 4, "POLL_DIV": 8},
    "d5c": {"FMT": 1, "ROWS": 4, "CAP": 4, "POLL_DIV": 8},
    "b2": {"FMT": 0, "ROWS": 6, "CAP": 3, "POLL_DIV": 8},
    "b2_wide": {"FMT": 0, "ROWS": 2, "COLS": 6912, "CAP": 3, "POLL_DIV": 8},
    "wrong_magic": {"FMT": 1, "ROWS": 2, "CAP": 4, "POLL_DIV": 8, "MAGIC": 1950306164},
    "d5_deferred": {"FMT": 1, "ROWS": 2, "CAP": 4, "POLL_DIV": 8,
                    "SERIAL_READS": 0, "READ_GAP": 0, "DEFER_RESULTS": 1},
    "b2_deferred": {"FMT": 0, "ROWS": 2, "CAP": 3, "POLL_DIV": 8,
                    "SERIAL_READS": 0, "READ_GAP": 0, "DEFER_RESULTS": 1},
    "too_many_rows": {"FMT": 1, "ROWS": 4097, "CAP": 4, "POLL_DIV": 8,
                      "SERIAL_READS": 0, "READ_GAP": 0, "DEFER_RESULTS": 1},
    "d5_shared": {"FMT": 1, "ROWS": 2, "CAP": 4, "POLL_DIV": 8,
                  "SERIAL_READS": 0, "READ_GAP": 0, "DEFER_RESULTS": 1, "USE_ARBITER": 1},
    "b2_shared": {"FMT": 0, "ROWS": 2, "CAP": 3, "POLL_DIV": 8,
                  "SERIAL_READS": 0, "READ_GAP": 0, "DEFER_RESULTS": 1, "USE_ARBITER": 1},
    "d5_golden": {"FMT": 1, "ROWS": 320, "CAP": 4, "POLL_DIV": 8,
                  "SERIAL_READS": 0, "READ_GAP": 0, "DEFER_RESULTS": 1, "USE_ARBITER": 1},
    "b2_golden": {"FMT": 0, "ROWS": 320, "CAP": 3, "POLL_DIV": 8,
                  "SERIAL_READS": 0, "READ_GAP": 0, "DEFER_RESULTS": 1, "USE_ARBITER": 1},
    "d5_down": {"FMT": 1, "ROWS": 16, "COLS": 7040, "CAP": 4, "POLL_DIV": 8,
                "SERIAL_READS": 0, "READ_GAP": 0, "DEFER_RESULTS": 1, "USE_ARBITER": 1},
    "b2_down": {"FMT": 0, "ROWS": 16, "COLS": 7040, "CAP": 3, "POLL_DIV": 8,
                "SERIAL_READS": 0, "READ_GAP": 0, "DEFER_RESULTS": 1, "USE_ARBITER": 1},
}


CONFIGS.update({name + "_fast": dict(params, COMPUTE_PIPELINE=1)
                for name, params in list(CONFIGS.items())})


def expect_from_words(words, activations, cols, rows, fmt):
    """The model's expectation when the weights region holds exactly `words`."""
    acts = []
    for lo, hi in model.act_words(activations):
        acts.extend((lo, hi))
    wpr = model.words_per_row(cols, fmt)
    subs = model.subs_of(fmt)
    accumulators, bad_words, y_lines = [], 0, []
    for r in range(rows):
        acc = 0
        for w in range(wpr):
            lo, hi = words[r * wpr + w]
            lanes, invalid = model.decode_word(fmt, lo, hi)
            if invalid:
                bad_words += 1
            for s in range(subs):
                acc += model.dot_chunk(lanes, s * 8, acts[w * subs + s])
        accumulators.append(acc)
        y_lines.append(("y", r, acc & model.B40))
    return {"accumulators": accumulators, "bad_words": bad_words, "y_lines": y_lines,
            "words": rows * wpr, "act_words": model.act_words_of(cols)}


class MatvecGoldenChunk(unittest.TestCase):
    """The stage-1 q_proj chunk against the committed golden accumulators."""

    GOLDEN = ROOT / "reports/ternary-check/matvec-2026-09-23.json"

    @unittest.skipUnless(HAVE_TOOLS, "built t27c and Icarus required")
    def test_down_proj_padding_preserves_golden_values_through_arbiter(self):
        if not any((ROOT / "build/fixtures").rglob("*.bin")):
            self.skipTest("fixture ranges not cached: python3 tools/fetch-fixtures.py")
        spec = importlib.util.spec_from_file_location("matvec_down_host", ROOT / "tools/fpga-matvec-run.py")
        host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(host)
        trits, acts, expected, cols = host.chunk_reference(16, "down_proj")
        self.assertEqual(cols, 7040)
        self.assertEqual(acts[6912:], [0] * 128)
        for row in range(16):
            self.assertEqual(trits[row * cols + 6912:(row + 1) * cols], [0] * 128)
        # Independent host integer dot verifies that padding kept every row.
        self.assertEqual([sum(w * x for w, x in zip(trits[r * cols:(r + 1) * cols], acts))
                          for r in range(16)], expected)
        MatvecSimulation.setUpClass()
        try:
            case = MatvecSimulation()
            for fmt, config in ((model.FMT_D5, "d5_down_fast"), (model.FMT_B2, "b2_down_fast")):
                with self.subTest(format=fmt):
                    stream = host.weight_stream(trits, cols, 16, fmt)
                    import struct
                    words = list(struct.iter_unpack("<QQ", stream))
                    lines = case.simulate(config, (("stop_after", 100000), ("ack_min", 12),
                                                   ("ack_span", 6), ("stall_pct", 35)),
                                          acts, words, cols, 16, fmt)
                    case.assert_run_matches(lines, words, acts, cols, 16, fmt)
                    self.assertEqual([b for tag, _, b in lines if tag == "y"],
                                     [v & model.B40 for v in expected])
        finally:
            MatvecSimulation.tearDownClass()

    @unittest.skipUnless(HAVE_TOOLS, "built t27c and Icarus required")
    def test_actual_golden_chunk_in_both_formats_through_arbiter(self):
        if not any((ROOT / "build/fixtures").rglob("*.bin")):
            self.skipTest("fixture ranges not cached: python3 tools/fetch-fixtures.py")
        spec = importlib.util.spec_from_file_location("matvec_host_run", ROOT / "tools/fpga-matvec-run.py")
        host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(host)
        # This computes the full matrix reference and verifies its accumulator
        # hash against the committed report before selecting the first 320 rows.
        trits, acts, expected, cols = host.chunk_reference(320)
        MatvecSimulation.setUpClass()
        try:
            case = MatvecSimulation()
            for fmt, config in ((model.FMT_D5, "d5_golden_fast"), (model.FMT_B2, "b2_golden_fast")):
                with self.subTest(format=fmt):
                    words = case.chunk_words(trits, cols, 320, fmt)
                    lines = case.simulate(config, (("stop_after", 400000), ("ack_min", 12),
                                                   ("ack_span", 6), ("stall_pct", 35)),
                                          acts, words, cols, 320, fmt)
                    case.assert_run_matches(lines, words, acts, cols, 320, fmt)
                    self.assertEqual([b for tag, _, b in lines if tag == "y"],
                                     [v & model.B40 for v in expected])
        finally:
            MatvecSimulation.tearDownClass()


if __name__ == "__main__":
    unittest.main()
