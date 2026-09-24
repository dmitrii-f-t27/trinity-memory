"""Device matvec (issue #64, consumer (B) of #65): t27/rtl/fpga_ddr3_matvec.t27 before any board run.

- The C that the pinned compiler generates from the module's functions agrees with Python
  statements of the same rules: the dense5 table decoder equals fpga_ddr3_reader.t27's on all
  256 codes and tools/ddr3_read_model.py's decode_word on random 128-bit words (lanes and invalid
  codes, both formats); the biased terms and the -1 lane bits give the exact dot product of eight
  lanes; the padding masks, the line check byte (tools/uart_loader_protocol.py's line_check) and
  the configuration rule. The host's image functions of t27/fpga_link.t27 (through the native
  library) equal tools/bridge_link_protocol.py's.
- Icarus runs the module with the real line emitter (t27/rtl/fpga_line_emitter.t27) and a
  transmitter model, fed through tests/tb_ddr3_matvec.v with random gaps between bus words:
  * the real chunk, q_proj rows 0-319 of BitNet b1.58 2B4T layer 0, as dense5 (32 words per row)
    and baseline2 (40 words per row) row images built by the host's t27 (and equal to the TMEM
    payloads of the chunk), with the seed-27 activations: every one of the 320 accumulators equals
    t27/matvec.t27's (the first 320 of the q_proj product whose sha256 is the committed report's);
  * rows of 6,912 columns (down_proj's width) with extreme activations (all -128, all +127),
    garbage in the padding lanes and past the last column of the activations, an invalid code in a
    padding byte: exact accumulators up to +-884,736 (above 2^19), padding never counted;
  * one-word rows and rows of exactly one word, no gaps and 75 % gaps; stray words before the run
    and after its last word; refused configurations.
  Every Y line's check byte, every row index, the Z counters (rows, words per row, words, the
  result checksum over the rows) are checked, and the Z counters that measure time (cycles, idle
  clocks, latency) and the stray words equal the bench's own counts of what it offered (the bench
  counts independently of the module). Consumer stalls are 0 by construction (in_ready is a
  constant): the bench confirms in_ready never went low, which says nothing more.
The summary of the chunk runs goes to build/fpga/matvec-sim/summary.json.
Runs with T27_ROOT set, Icarus installed and the native library built (tools/test-t27.sh);
the chunk runs also need the fixture cache (skipped without it, as tests/test_matvec.py).
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
import bridge_link_protocol as link  # noqa: E402
import ddr3_read_model as read_model  # noqa: E402
import uart_loader_protocol as proto  # noqa: E402

COMPILER = Path(os.environ["T27_ROOT"]) / "target/release/t27c" if os.environ.get("T27_ROOT") else None
HAVE_COMPILER = bool(COMPILER and COMPILER.is_file())
HAVE_ICARUS = bool(shutil.which("iverilog") and shutil.which("vvp"))
REQUIRE_CACHED = os.environ.get("TRINITY_REQUIRE_CACHED") == "1"
SUMMARY = ROOT / "build" / "fpga" / "matvec-sim" / "summary.json"
M32, M64 = (1 << 32) - 1, (1 << 64) - 1


def native():
    try:
        from trinity_memory import _native as n
        n.library()
        return n
    except Exception:  # noqa: BLE001 - the library is optional for the RTL-only parts
        return None


NATIVE = native()


def build_library(source: Path, work: Path) -> ctypes.CDLL:
    generated = subprocess.run([str(COMPILER), "gen-c", str(source)], capture_output=True, text=True, check=True).stdout
    # gen-c initializes a module array with "= 0", which C rejects (t27/rtl/README.md).
    generated = re.sub(r"^(static \w+ \w+\[\w+\]) = 0;$", r"\1 = {0};", generated, flags=re.M)
    c_file = work / f"{source.stem}.c"
    c_file.write_text(generated, encoding="ascii")
    library = work / f"lib{source.stem}.so"
    subprocess.run(["cc", "-shared", "-fPIC", "-O1", "-Wno-parentheses-equality", "-Wno-shift-count-overflow",
                    str(c_file), "-o", str(library)], check=True, capture_output=True)
    return ctypes.CDLL(str(library))


def host_image(trits, rows, cols, fmt) -> bytes:
    """The row-padded image through t27/fpga_link.t27's tl_image (native library)."""
    n = NATIVE
    values = (ctypes.c_int32 * max(1, len(trits)))(*trits)
    size = len(link.image([0] * cols, 1, cols, fmt)) * rows + 16
    out = n.buffer(size)
    got = n.call("tl_image", ctypes.c_int64, [n.I32, n.SZ, n.SZ, n.SZ, ctypes.c_uint32, n.U8, n.SZ],
                 values, 0, rows, cols, fmt, out, size)
    assert got >= 0, got
    return bytes(out[:got])


def host_act_image(x, cols, fmt) -> bytes:
    n = NATIVE
    data = n.octets(bytes(v & 255 for v in x))
    size = link.words_per_row(cols, fmt) * 80 + 80
    out = n.buffer(size)
    got = n.call("tl_act_image", ctypes.c_int64, [n.U8, n.SZ, ctypes.c_uint32, n.U8, n.SZ], data, cols, fmt, out, size)
    assert got >= 0, got
    return bytes(out[:got])


def expected_rows(trits, rows, cols, x):
    return [sum(trits[r * cols + j] * x[j] for j in range(cols)) for r in range(rows)]


@unittest.skipUnless(HAVE_COMPILER, "T27_ROOT with a built t27c required")
class MatvecFunctions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix="trinity-matvec-c-")
        work = Path(cls.work.name)
        cls.lib = build_library(ROOT / "t27/rtl/fpga_ddr3_matvec.t27", work)
        cls.reader = build_library(ROOT / "t27/rtl/fpga_ddr3_reader.t27", work)
        u64, u32, b = ctypes.c_uint64, ctypes.c_uint32, ctypes.c_bool
        for lib, name, args, res in ((cls.lib, "d5_dec", (u64,), u64), (cls.reader, "d5_dec", (u64,), u64),
                                     (cls.lib, "d5_at", (u64, u32), u64), (cls.lib, "d5_join", (u32,) + (u64,) * 16, u64),
                                     (cls.lib, "d5_flags", (u64,) * 8, u64), (cls.lib, "b2_clean", (u64,), u64),
                                     (cls.lib, "b2_flags", (u64,), u64), (cls.lib, "flag_counts", (u64, u64), u64),
                                     (cls.lib, "sum8x8", (u64,), u32),
                                     (cls.lib, "therm", (u32, u32), u64), (cls.lib, "terms8", (u64, u32, u64), u64),
                                     (cls.lib, "neg8", (u64, u32), u64), (cls.lib, "quad2", (u64,), u64),
                                     (cls.lib, "pop16", (u64,), u64), (cls.lib, "line_check", (u32, u32, u32), u32),
                                     (cls.lib, "line_bits", (u32, u32, u32), u64),
                                     (cls.lib, "config_ok", (u32, u32, u32), b), (cls.lib, "rotl1_32", (u32,), u32)):
            function = getattr(lib, name)
            function.argtypes, function.restype = args, res

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def test_dense5_table_is_the_readers_and_the_models(self):
        for code in range(256):
            self.assertEqual(self.lib.d5_dec(code), self.reader.d5_dec(code), code)
            lanes, bad = read_model.decode_word(1, code)
            got = self.lib.d5_dec(code)
            self.assertEqual((got & 1023, got >> 10), (lanes & 1023, bad), code)

    def test_word_decode_both_formats(self):
        rng = random.Random(64)
        for _ in range(3000):
            word = rng.getrandbits(128)
            if rng.random() < 0.5:        # mostly valid dense5 bytes
                word = sum(rng.randrange(243 if rng.random() < 0.95 else 256) << (8 * n) for n in range(16))
            lo, hi = word & M64, word >> 64
            lanes, bad = read_model.decode_word(1, word)
            decoded = [self.lib.d5_at(lo, i) for i in range(8)] + [self.lib.d5_at(hi, i) for i in range(8)]
            got = sum(self.lib.d5_join(p, *decoded) << (64 * p) for p in range(3))
            self.assertEqual(got, lanes)
            flags = self.lib.d5_flags(*decoded[:8]) | (self.lib.d5_flags(*decoded[8:]) << 8)
            self.assertEqual(bin(flags).count("1"), bad)
            self.assertEqual(self.lib.sum8x8(self.lib.flag_counts(flags, 0)), bad)
            lanes2, bad2 = read_model.decode_word(0, word)
            self.assertEqual(self.lib.b2_clean(lo) | (self.lib.b2_clean(hi) << 64), lanes2)
            f0, f1 = self.lib.b2_flags(lo), self.lib.b2_flags(hi)
            self.assertEqual(self.lib.sum8x8(self.lib.flag_counts(f0, f1)), bad2)

    def test_biased_terms_give_the_exact_dot_product(self):
        rng = random.Random(27)
        value = {0: 0, 1: 1, 2: -1, 3: 0}
        for _ in range(5000):
            lanes = rng.getrandbits(64)
            first = rng.choice((0, 8, 16, 24))
            x = [rng.randint(-128, 127) for _ in range(8)]
            act = sum((v & 255) << (8 * i) for i, v in enumerate(x))
            terms = self.lib.terms8(lanes, first, act)
            neg = self.lib.neg8(lanes, first)
            codes = [(lanes >> (2 * (first + i))) & 3 for i in range(8)]
            for i, code in enumerate(codes):
                want = (x[i] + 128) if code == 1 else (~x[i] + 128) if code == 2 else 128
                self.assertEqual((terms >> (8 * i)) & 255, want)
                self.assertEqual((neg >> i) & 1, int(code == 2))
            total = sum((terms >> (8 * i)) & 255 for i in range(8)) - 128 * 8 + bin(neg).count("1")
            self.assertEqual(total, sum(value[c] * v for c, v in zip(codes, x)))
            quad = self.lib.quad2(terms)
            self.assertEqual(quad & 65535, sum((terms >> (8 * i)) & 255 for i in range(4)))
            self.assertEqual(quad >> 16, sum((terms >> (8 * i)) & 255 for i in range(4, 8)))
            self.assertEqual(self.lib.pop16(neg | (rng.getrandbits(48) << 16)), bin(neg & 65535).count("1"))

    def test_padding_masks(self):
        for n in range(0, 82):
            for base in (0, 32, 64):
                want = sum(3 << (2 * j) for j in range(32) if base + j < n)
                self.assertEqual(self.lib.therm(n, base), want, (n, base))

    def test_line_check_and_bits_are_the_loaders(self):
        rng = random.Random(5)
        for _ in range(2000):
            tag, a, v = rng.choice(b"YZACN"), rng.getrandbits(32), rng.getrandbits(32)
            self.assertEqual(self.lib.line_check(tag, a, v), proto.line_check(tag, a, v))
            self.assertEqual(self.lib.line_bits(tag, a, v), (proto.line_check(tag, a, v) << 32) | v)
        self.assertEqual(self.lib.rotl1_32(0x80000001), 3)

    def test_configuration_rule(self):
        for rows, cols, fmt, ok in ((1, 1, 0, True), (1024, 2560, 1, True), (0, 2560, 1, False), (1025, 1, 0, False),
                                    (1, 0, 0, False), (1, 1, 2, False), (320, 6912, 1, True)):
            self.assertEqual(self.lib.config_ok(rows, cols, fmt), ok, (rows, cols, fmt))

    @unittest.skipUnless(NATIVE, "native library required")
    def test_host_images_are_the_protocols(self):
        rng = random.Random(9)
        for rows, cols in ((1, 1), (3, 5), (2, 63), (2, 64), (2, 65), (3, 79), (3, 80), (2, 81), (2, 161), (1, 6912)):
            trits = [rng.choice((-1, 0, 1)) for _ in range(rows * cols)]
            x = [rng.randint(-128, 127) for _ in range(cols)]
            for fmt in (0, 1):
                self.assertEqual(host_image(trits, rows, cols, fmt), link.image(trits, rows, cols, fmt), (rows, cols, fmt))
                self.assertEqual(host_act_image(x, cols, fmt), link.act_image(x, cols, fmt), (cols, fmt))
                wpr = NATIVE.call("tl_words_per_row", ctypes.c_uint64, [ctypes.c_uint64, ctypes.c_uint32], cols, fmt)
                self.assertEqual(wpr, link.words_per_row(cols, fmt))


def words_hex(image: bytes) -> list[str]:
    assert len(image) % 16 == 0
    return [f"{int.from_bytes(image[i:i + 16], 'little'):032x}" for i in range(0, len(image), 16)]


def acts_hex(act_image: bytes, garbage: random.Random | None = None, lanes_used: int = 80, cols: int = 0) -> list[str]:
    """One u64 per (entry, bank), index entry * 10 + bank; with `garbage`, the bytes of columns at
    and past `cols`, the bytes past lanes_used of each block and two whole blocks past the image
    get random values (the module must mask them)."""
    blocks = len(act_image) // 80
    out = []
    for k in range(blocks + (2 if garbage else 0)):
        block = bytearray(act_image[k * 80:(k + 1) * 80]) if k < blocks else bytearray(80)
        if garbage:
            for i in range(80):
                if k >= blocks or i >= lanes_used or k * lanes_used + i >= cols:
                    block[i] = garbage.randrange(256)
        out += [f"{int.from_bytes(block[8 * b:8 * b + 8], 'little'):016x}" for b in range(10)]
    return out


@unittest.skipUnless(HAVE_COMPILER and HAVE_ICARUS and NATIVE, "T27_ROOT, Icarus and the native library required")
class MatvecIcarus(unittest.TestCase):
    summary: dict = {}

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix="trinity-matvec-sim-")
        work = Path(cls.work.name)
        sources = []
        for module in ("fpga_ddr3_matvec", "fpga_line_emitter"):
            text = subprocess.run([str(COMPILER), "gen-verilog", str(ROOT / f"t27/rtl/{module}.t27")],
                                  capture_output=True, text=True, check=True).stdout
            path = work / f"{module}.v"
            path.write_text(text, encoding="ascii")
            sources.append(str(path))
        cls.binary = work / "tb.vvp"
        subprocess.run(["iverilog", "-g2012", "-o", str(cls.binary), str(ROOT / "tests/tb_ddr3_matvec.v"), *sources],
                       check=True, capture_output=True, text=True)
        cls.case = 0

    @classmethod
    def tearDownClass(cls):
        if cls.summary:
            SUMMARY.parent.mkdir(parents=True, exist_ok=True)
            SUMMARY.write_text(json.dumps(cls.summary, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        cls.work.cleanup()

    def simulate(self, *, image: bytes, acts: list[str], rows: int, cols: int, fmt: int, seq: int = 7, gap: int = 30,
                 seed: int = 1, stray_before: int = 0, stray_after: int = 0, txbusy: int = 2):
        type(self).case += 1
        work = Path(self.work.name) / f"case{self.case}"
        work.mkdir()
        words = words_hex(image)
        (work / "words.hex").write_text("\n".join(words) + "\n")
        (work / "acts.hex").write_text("\n".join(acts) + "\n")
        args = ["vvp", "-n", str(self.binary), f"+words={work / 'words.hex'}", f"+acts={work / 'acts.hex'}",
                f"+out={work / 'out.hex'}", f"+summary={work / 'summary.txt'}", f"+nwords={len(words)}",
                f"+nacts={len(acts)}", f"+rows={rows}", f"+cols={cols}", f"+fmt={fmt}", f"+seq={seq}", f"+gap={gap}",
                f"+seed={seed}", f"+stray_before={stray_before}", f"+stray_after={stray_after}", f"+txbusy={txbusy}"]
        start = time.monotonic()
        result = subprocess.run(args, capture_output=True, text=True, timeout=900)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        elapsed = time.monotonic() - start
        data = bytes(int(line, 16) for line in (work / "out.hex").read_text().split())
        bench = dict((k, int(v)) for k, v in (line.split() for line in (work / "summary.txt").read_text().splitlines()))
        decoder = link.LinkDecoder()
        events = decoder.feed(data) + decoder.finish()
        return events, bench, elapsed, len(data)

    def check_run(self, events, bench, *, rows, cols, fmt, seq, want, stray=0):
        self.assertEqual(bench["done"], 1)
        self.assertTrue(all(e.kind == "line" for e in events), [e.as_dict() for e in events if e.kind != "line"][:3])
        self.assertTrue(all(e.check_ok for e in events))
        y_lines = [e for e in events if e.tag == "Y"]
        z_lines = [e for e in events if e.tag == "Z"]
        self.assertEqual([e.a for e in y_lines], [link.y_word(seq, r) for r in range(rows)])
        self.assertEqual([e.a for e in z_lines], [link.z_word(seq, i) for i in range(link.Z_COUNT)])
        self.assertEqual([e.tag for e in events], ["Y"] * rows + ["Z"] * link.Z_COUNT)
        got = [link.signed32(e.v) for e in y_lines]
        mismatches = [r for r in range(rows) if got[r] != want[r]]
        z = dict(zip(link.Z_NAMES, (e.v for e in z_lines)))
        wpr = link.words_per_row(cols, fmt)
        self.assertEqual(mismatches, [])
        self.assertEqual((z["status"], z["rows"], z["words_per_row"], z["words"]), (0, rows, wpr, rows * wpr))
        self.assertEqual(z["checksum"], link.result_checksum(want))
        self.assertEqual(bench["offered"], rows * wpr)
        # Not identities: the bench counts what it offered; the module counts what it saw.
        self.assertEqual(z["cycles"], bench["first_to_last"])
        self.assertEqual(z["idle_clocks"], bench["gap_clocks"])
        self.assertEqual(z["latency"], bench["latency"])
        self.assertEqual(z["stray_words"], stray)
        self.assertEqual(bench["stray"], stray)
        self.assertEqual((z["consumer_stalls"], bench["ever_not_ready"], bench["not_ready_offers"]), (0, 0, 0))
        return got, z

    def test_real_chunk_both_formats(self):
        import stage1_chunk
        try:
            chunk = stage1_chunk.chunk()
        except Exception as error:  # noqa: BLE001 - fixtures.CacheMiss or a missing cache directory
            if REQUIRE_CACHED:
                self.fail(f"TRINITY_REQUIRE_CACHED=1: {error}")
            self.skipTest(f"fixture cache: {error}")
        self.assertEqual(chunk["full_sha256"], chunk["report_sha256"])
        from trinity_memory import container
        rows, cols = stage1_chunk.ROWS, stage1_chunk.COLS
        trits, x, want = chunk["trits"], chunk["x"], chunk["y"]
        for fmt, codec, seed in ((1, "dense5", 11), (0, "baseline2", 12)):
            with self.subTest(codec=codec):
                image = host_image(trits, rows, cols, fmt)
                tmem = container.encode_file(trits, codec)
                # 2,560 columns fill whole words in both formats: the image is the TMEM payload.
                self.assertEqual(image, tmem[24:])
                acts = acts_hex(host_act_image(x, cols, fmt))
                events, bench, elapsed, nbytes = self.simulate(image=image, acts=acts, rows=rows, cols=cols, fmt=fmt,
                                                               seq=64 + fmt, gap=30, seed=seed)
                got, z = self.check_run(events, bench, rows=rows, cols=cols, fmt=fmt, seq=64 + fmt, want=want)
                self.summary[f"qproj_rows0_319_{codec}"] = {
                    "tensor": stage1_chunk.TENSOR, "rows": [0, rows], "cols": cols, "format": codec,
                    "words": rows * link.words_per_row(cols, fmt), "image_bytes": len(image),
                    "image_sha256": hashlib.sha256(image).hexdigest(), "tmem_payload_equal": True,
                    "activations": "tmv_activations seed 27 (t27/matvec.t27), 2560",
                    "reference": "first 320 accumulators of t27/matvec.t27's q_proj product; sha256 of all 2560 "
                                 f"{chunk['full_sha256']} = reports/ternary-check/matvec-2026-09-23.json hf_packed",
                    "accumulators_match": f"{rows} of {rows}", "y_sha256_le": hashlib.sha256(struct.pack(f"<{rows}q", *got)).hexdigest(),
                    "y_first8": got[:8], "gap_percent": 30, "seed": seed, "z": z, "bench": bench,
                    "uart_bytes": nbytes, "icarus_seconds": round(elapsed, 1)}

    def test_down_proj_width_extremes_and_padding(self):
        cols, rng = 6912, random.Random(6912)
        rows_spec = [([1] * cols, [-128] * cols), ([-1] * cols, [-128] * cols), ([1] * cols, [127] * cols),
                     ([-1] * cols, [127] * cols), ([(-1) ** j for j in range(cols)], [-128 if j % 2 else 127 for j in range(cols)]),
                     ([rng.choice((-1, 0, 1)) for _ in range(cols)], None), ([0] * cols, None)]
        for fmt in (1, 0):
            with self.subTest(fmt=fmt):
                # One activation vector per run: the rows above that need their own vector run alone.
                for index, (row, xs) in enumerate(rows_spec):
                    x = xs or [rng.randint(-128, 127) for _ in range(cols)]
                    trits = row * 2
                    image = bytearray(link.image(trits, 2, cols, fmt))
                    wpr = link.words_per_row(cols, fmt)
                    n = link.lanes(fmt)
                    pad_lanes = wpr * n - cols
                    if fmt == 1 and pad_lanes:
                        # Garbage in the padding of each row's last word: valid nonzero codes, and one
                        # invalid code (250) in a padding byte of row 1.
                        for r in range(2):
                            last = (r + 1) * wpr * 16
                            first_pad_byte = r * wpr * 16 + -(-cols // 5)
                            for p in range(first_pad_byte, last):
                                image[p] = 242
                            if r == 1:
                                image[last - 1] = 250
                        invalid = 1
                    else:
                        invalid = 0
                    acts = acts_hex(link.act_image(x, cols, fmt), garbage=rng, lanes_used=n, cols=cols)
                    want = expected_rows(trits, 2, cols, x)
                    events, bench, _, _ = self.simulate(image=bytes(image), acts=acts, rows=2, cols=cols, fmt=fmt,
                                                        seq=index, gap=20, seed=index + 3)
                    got, z = self.check_run(events, bench, rows=2, cols=cols, fmt=fmt, seq=index, want=want)
                    self.assertEqual(z["invalid_codes"], invalid)
                    if index < 4:
                        self.assertEqual(abs(got[0]), 128 * cols if xs[0] == -128 else 127 * cols)
                self.assertEqual(128 * cols, 884736)
                self.assertGreater(128 * cols, 1 << 19)

    def test_short_rows_gaps_and_stray_words(self):
        rng = random.Random(37)
        cases = [(9, 37, 1, 0), (9, 37, 1, 75), (5, 80, 1, 0), (5, 81, 1, 50), (6, 64, 0, 0), (6, 65, 0, 75), (1, 1, 0, 10),
                 (3, 160, 0, 30)]
        for rows, cols, fmt, gap in cases:
            with self.subTest(rows=rows, cols=cols, fmt=fmt, gap=gap):
                trits = [rng.choice((-1, 0, 1)) for _ in range(rows * cols)]
                x = [rng.randint(-128, 127) for _ in range(cols)]
                image = host_image(trits, rows, cols, fmt)
                acts = acts_hex(host_act_image(x, cols, fmt))
                events, bench, _, _ = self.simulate(image=image, acts=acts, rows=rows, cols=cols, fmt=fmt, seq=200 + rows,
                                                    gap=gap, seed=cols)
                self.check_run(events, bench, rows=rows, cols=cols, fmt=fmt, seq=200 + rows,
                               want=expected_rows(trits, rows, cols, x))
        # Stray words: three while the module sets up (a 40-word row takes 40 clocks of SETUP), five
        # after the last word of the run.
        rows, cols = 4, 2560
        trits = [rng.choice((-1, 0, 1)) for _ in range(rows * cols)]
        x = [rng.randint(-128, 127) for _ in range(cols)]
        events, bench, _, _ = self.simulate(image=host_image(trits, rows, cols, 0), acts=acts_hex(host_act_image(x, cols, 0)),
                                            rows=rows, cols=cols, fmt=0, seq=9, gap=10, seed=4, stray_before=3, stray_after=5)
        self.check_run(events, bench, rows=rows, cols=cols, fmt=0, seq=9, want=expected_rows(trits, rows, cols, x), stray=8)

    def test_refused_configurations(self):
        for rows, cols, fmt in ((0, 80, 1), (1025, 80, 1), (1, 0, 1), (1, 80, 2), (1, 1024 * 80 + 1, 1)):
            with self.subTest(rows=rows, cols=cols, fmt=fmt):
                events, bench, _, _ = self.simulate(image=bytes(16), acts=acts_hex(bytes(80)), rows=rows, cols=cols,
                                                    fmt=fmt, seq=3, gap=0)
                self.assertEqual(bench["done"], 1)
                self.assertEqual([e.tag for e in events], ["Z"] * link.Z_COUNT)
                self.assertTrue(all(e.check_ok for e in events))
                z = dict(zip(link.Z_NAMES, (e.v for e in events)))
                self.assertEqual((z["status"], z["rows"], z["words"]), (1, 0, 0))


if __name__ == "__main__":
    unittest.main()
