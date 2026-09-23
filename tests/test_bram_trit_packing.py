"""Block-RAM trit packing: the t27 codec and engine rules against the host model.

The C that the pinned compiler generates from t27/rtl/bram_trit_codec.t27 and
t27/rtl/bram_trit_engine.t27 is loaded with ctypes and compared with
tools/bram_trit_model.py, an independent Python statement of the same rules, on
random inputs (valid and invalid words). The whole bench (three engines, the
sequencer, the UART report) is checked in Icarus by `make -C fpga/ax7203 bram-sim`.
Runs with T27_ROOT set (tools/test-t27.sh, the native CI job); skipped without the compiler.
"""
from __future__ import annotations

import ctypes
import importlib.util
import os
import random
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import bram_trit_model as model  # noqa: E402

COMPILER = Path(os.environ["T27_ROOT"]) / "target/release/t27c" if os.environ.get("T27_ROOT") else None


def build_library(source: Path, work: Path) -> ctypes.CDLL:
    generated = subprocess.run([str(COMPILER), "gen-c", str(source)], capture_output=True, text=True, check=True).stdout
    # The pinned gen-c initializes a module array with `= 0`, which C rejects.
    generated = re.sub(r"^(static \w+ \w+\[\w+\]) = 0;$", r"\1 = {0};", generated, flags=re.M)
    header = work / f"{source.stem}.c"
    header.write_text(generated, encoding="ascii")
    library = work / f"lib{source.stem}.so"
    subprocess.run(["cc", "-shared", "-fPIC", "-O1", "-Wno-parentheses-equality", str(header), "-o", str(library)],
                   check=True, capture_output=True)
    return ctypes.CDLL(str(library))


def load_generator():
    spec = importlib.util.spec_from_file_location("generate_bram_bench", ROOT / "tools/generate-bram-bench.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def random_lanes(rng: random.Random, count: int) -> int:
    value = 0
    for j in range(count):
        value |= rng.choice((0, 1, 2)) << (2 * j)
    return value


@unittest.skipUnless(COMPILER and COMPILER.is_file(), "T27_ROOT with a built t27c required")
class BramTritPacking(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix="trinity-bram-")
        work = Path(cls.work.name)
        cls.codec = build_library(ROOT / "t27/rtl/bram_trit_codec.t27", work)
        cls.engine = build_library(ROOT / "t27/rtl/bram_trit_engine.t27", work)
        u64, u32 = ctypes.c_uint64, ctypes.c_uint32
        for name, args in (("encode", (u32, u64)), ("decode", (u32, u64)), ("on_comb", (u32, u32, u64))):
            function = getattr(cls.codec, name)
            function.argtypes, function.restype = args, u64
        cls.engine.advance.argtypes, cls.engine.advance.restype = (u64, u32), u64
        cls.engine.lanes_from.argtypes, cls.engine.lanes_from.restype = (u64,), u64
        cls.engine.count_code.argtypes, cls.engine.count_code.restype = (u64, u64), u32
        cls.engine.weighted.argtypes, cls.engine.weighted.restype = (u64, u32), ctypes.c_int64

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def test_encoder_matches_the_model(self):
        rng = random.Random(27)
        for fmt, lanes in model.LANES.items():
            for _ in range(3000):
                value = random_lanes(rng, lanes)
                self.assertEqual(self.codec.encode(fmt, value), model.encode_word(fmt, value), (fmt, hex(value)))
                self.assertEqual(self.codec.on_comb(fmt, 0, value), model.encode_word(fmt, value))

    def test_decoder_matches_the_model_on_any_word(self):
        rng = random.Random(28)
        for fmt in model.LANES:
            for _ in range(3000):
                word = rng.getrandbits(36)
                lanes, bad = model.decode_word(fmt, word)
                self.assertEqual(self.codec.decode(fmt, word), lanes | (bad << 48), (fmt, hex(word)))
                self.assertEqual(self.codec.on_comb(fmt, 1, word), lanes | (bad << 48))

    def test_round_trip_fits_the_36_bit_word(self):
        rng = random.Random(29)
        for fmt, lanes in model.LANES.items():
            for _ in range(500):
                value = random_lanes(rng, lanes)
                word = self.codec.encode(fmt, value)
                self.assertLess(word, 1 << 36)
                self.assertEqual(self.codec.decode(fmt, word), value)

    def test_generator_banks_and_block_count(self):
        generator = load_generator()
        self.assertEqual(generator.bank_sizes(56320), [16384, 16384, 16384, 7168])
        self.assertEqual(generator.bank_sizes(46080), [16384, 16384, 13312, 0])
        with self.assertRaises(SystemExit):
            generator.bank_sizes(4 * 16384 + 1)
        blocks = [sum(-(-size // 1024) for size in generator.bank_sizes(1013760 // k)) for k in (18, 20, 22)]
        self.assertEqual(blocks, [55, 50, 45])
        text = generator.specialize(model.FMT_D5D2, 46080, model.DEFAULT_SEED)
        for line in ("module TrinityBramTritEngineD5D2T27;", "const LANES: u32 = 22;", "const WORDS: u32 = 46080;",
                     "const B2_WORDS: u32 = 13312;", "const B3_WORDS: u32 = 1;"):
            self.assertIn(line, text)

    def test_lfsr_advance_and_lanes_match_the_model(self):
        rng = random.Random(30)
        for _ in range(2000):
            state = rng.getrandbits(64)
            for bits in (2, 36, 40, 44):
                self.assertEqual(self.engine.advance(state, bits), model.advance(state, bits))
            self.assertEqual(self.engine.lanes_from(state), model.lanes_from(state, 22))

    def test_counts_and_weighted_sum(self):
        rng = random.Random(31)
        for _ in range(2000):
            value = random_lanes(rng, 22)
            base = rng.randrange(8)
            trits = [model.trit_of_lane((value >> (2 * j)) & 3) for j in range(22)]
            self.assertEqual(self.engine.count_code(value, 1), trits.count(1))
            self.assertEqual(self.engine.count_code(value, 2), trits.count(-1))
            self.assertEqual(self.engine.weighted(value, base), sum(t * (((base + j) & 7) + 1) for j, t in enumerate(trits)))

    def test_every_format_stores_the_same_stream(self):
        trits = 1980 * 2
        reference = model.stream_trits(trits)
        results = [model.engine_results(fmt, trits // k) for fmt, k in model.LANES.items()]
        self.assertEqual({(r["pos"], r["neg"], r["dot"]) for r in results},
                         {(reference.count(1), reference.count(-1),
                           sum(t * model.activation(i) for i, t in enumerate(reference)))})

    def test_rom_model_agrees_with_the_engine_model(self):
        # The same trits as a fixed tensor: the read-only store must report what the
        # engine reports after writing the stream, with no write phase.
        trits = model.stream_trits(1980 * 2)
        for fmt, k in model.LANES.items():
            engine = model.engine_results(fmt, len(trits) // k)
            rom = model.rom_results(fmt, trits)
            for key in ("words", "pos", "neg", "dot", "chk", "read_ticks"):
                self.assertEqual(rom[key], engine[key], (fmt, key))
            self.assertEqual(rom["write_ticks"], 0)
            lanes_check = 0
            for lanes, _ in model.words_of_trits(fmt, trits):
                lanes_check = model.rotl1(lanes_check) ^ lanes
            self.assertEqual(rom["lanes_check"], lanes_check)

    def test_rom_generator_writes_the_words_and_the_check(self):
        generator = load_generator()
        rng = random.Random(32)
        trits = [rng.choice((-1, 0, 1)) for _ in range(1980)]
        for fmt, k in model.LANES.items():
            words = [word for _, word in model.words_of_trits(fmt, trits)]
            check = model.rom_results(fmt, trits)["lanes_check"]
            text = generator.specialize_rom(fmt, words, check)
            self.assertIn(f"const LANES_CHECK: u64 = {check};", text)
            self.assertIn(f"const WORDS: u32 = {1980 // k};", text)
            body = re.search(r"var mem_b0: \[B0_WORDS\]u64 = \[B0_WORDS\]u64\{(.*?)\};", text, re.S).group(1)
            self.assertEqual([int(x) for x in body.replace("\n", " ").split(",")], words)
            self.assertIn("var mem_b1: [B1_WORDS]u64 = [B1_WORDS]u64{\n    0\n};", text)

    def test_rom_functions_match_the_model(self):
        with tempfile.TemporaryDirectory(prefix="trinity-bram-rom-") as work:
            rom = build_library(ROOT / "t27/rtl/bram_trit_rom.t27", Path(work))
            rom.count_code.argtypes, rom.count_code.restype = (ctypes.c_uint64, ctypes.c_uint64), ctypes.c_uint32
            rom.weighted.argtypes, rom.weighted.restype = (ctypes.c_uint64, ctypes.c_uint32), ctypes.c_int64
            rng = random.Random(33)
            for _ in range(2000):
                value = random_lanes(rng, 22)
                base = rng.randrange(8)
                trits = [model.trit_of_lane((value >> (2 * j)) & 3) for j in range(22)]
                self.assertEqual(rom.count_code(value, 1), trits.count(1))
                self.assertEqual(rom.count_code(value, 2), trits.count(-1))
                self.assertEqual(rom.weighted(value, base), sum(t * (((base + j) & 7) + 1) for j, t in enumerate(trits)))


if __name__ == "__main__":
    unittest.main()
