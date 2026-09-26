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


class MatvecSimulation(unittest.TestCase):
    """The whole module in Icarus against the model's line-by-line prediction."""

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix="trinity-matvec-sim-")
        work = Path(cls.work.name)
        generated = subprocess.run([str(COMPILER), "gen-verilog", str(SOURCE)], capture_output=True,
                                   text=True, check=True).stdout
        (work / "matvec.v").write_text(generated)
        cls.work_path = work
        cls.vvp = {}
        for name, params in CONFIGS.items():
            out = work / f"{name}.vvp"
            flags = []
            for key, value in params.items():
                flags.append(f"-Ptb_ddr3_matvec.{key}={value}")
            subprocess.run(["iverilog", "-g2012", *flags, "-s", "tb_ddr3_matvec", "-o", str(out),
                            str(work / "matvec.v"), str(ROOT / "tests/tb_ddr3_matvec.v")],
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

    def assert_run_matches(self, lines, words, activations, cols, rows, fmt):
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
        self.assertEqual(by_tag["d"][0], 1)
        self.assertEqual(by_tag["c"][0], expectation["words"])
        self.assertEqual(by_tag["n"][0], expectation["bad_words"])
        self.assertEqual(by_tag["u"][0], expectation["act_words"])
        self.assertEqual(by_tag["z"], (1, expectation["bad_words"] & model.B40))
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
        self.run_chunk("b2_wide", (), 0x6912, 6912, 2, model.FMT_B2)

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
}


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

    def test_fixture_cache_gate(self):
        manifest = json.loads((ROOT / "fixtures/manifest.json").read_text())
        cached = (ROOT / "build/fixtures").is_dir() and any(
            (ROOT / "build/fixtures").rglob("*q_proj*"))
        if not cached:
            self.skipTest("fixture ranges not cached: python3 tools/fetch-fixtures.py "
                          f"({manifest['url']} — {manifest['cache']})")
        report = json.loads(self.GOLDEN.read_text())
        self.assertIn("tensors", report)


if __name__ == "__main__":
    unittest.main()
