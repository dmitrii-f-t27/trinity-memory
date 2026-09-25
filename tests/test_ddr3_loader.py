"""UART loader on DDR3 (issue #63, part 2): t27/rtl/fpga_ddr3_loader.t27, fpga_loader_wb.t27 and
fpga_wb_arbiter.t27 in the top fpga/ax7203/ddr3/tms_ddr3_loader_ax7203.v, before any board run.

- The DDR3 loader is the part-1 loader (t27/rtl/fpga_uart_loader.t27) with marked changes:
  every hunk in which the two files differ carries a `[ddr3]` mark (or is the header).
- The C that the pinned compiler generates from the Wishbone master's and the arbiter's
  functions (byte of a word, byte merge, select bit, outstanding count) and from the DDR3
  loader's new functions agrees with Python on random inputs.
- The protocol model (tools/uart_loader_protocol.py) for protocol 3: not_ready before the
  calibration, the 37 status lines, the Wishbone master's word counts and kept word, the
  sparse store with the simulation's background words.
- Icarus runs the whole top with UberDDR3 replaced by tests/sim_ddr3_loader_model.v (our
  behavioural Wishbone memory: the 512 MiB x16 address space folded with tags, a background
  pattern in never-written words, byte selects, random stalls and ack latency, a calibration
  delay; not UberDDR3) and the PLL by a pass-through stub, so the controller clock is 200 MHz
  here. A host model (tests/tb_uart_loader.v) drives RX at the bit level and every byte the
  device sends is compared with DeviceModel's prediction (the status values that depend on
  timing are not: FIFO high water, clocks, calib_clocks, most outstanding, read latency,
  command stalls). Scenarios: randomized loads and reads over the whole 512 MiB (unaligned,
  above 256 MiB, lengths 1..4096, read-before of never-written memory), back-to-back chunks
  sharing a word, a write into the word the read side keeps, the part-1 fault set (corrupt,
  duplicate, dropped byte, host stop, invalid frames, framing error, garbage, glitches, reset
  during a frame and during a read-back), loads and reads before the calibration (not_ready),
  the port watchdog on a port that takes no request and on acks that do not come (a read's
  late ack dropped, not delivered to the next request), frames back to back, a FIFO overflow,
  a baud change, the board's divisor 723 with the host 5 % slow and 4.5 % fast, and
  arbitration with the #62 burst reader as the second master (its runs equal its host model
  while the loader loads and reads, including words of the reader's region). Monitors in the
  bench: every ack reaches the master that issued the request, ownership changes only with
  nothing outstanding, and every read answered from the kept word equals the memory model.
Runs with T27_ROOT set and Icarus installed (tools/test-t27.sh); skipped otherwise.
"""
from __future__ import annotations

import concurrent.futures
import ctypes
import difflib
import importlib.util
import json
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import uart_loader_protocol as proto  # noqa: E402

COMPILER = Path(os.environ["T27_ROOT"]) / "target/release/t27c" if os.environ.get("T27_ROOT") else None
HAVE_COMPILER = bool(COMPILER and COMPILER.is_file())
HAVE_TOOLS = bool(HAVE_COMPILER and shutil.which("iverilog") and shutil.which("vvp"))
CORES = ("fpga_reset", "fpga_uart_tx", "fpga_uart_rx", "fpga_line_emitter", "fpga_ddr3_loader", "fpga_loader_wb",
         "fpga_wb_arbiter", "fpga_ddr3_reader")
CLK_PS = 5_000                        # the PLL stub: the controller clock is the 200 MHz input
SIM_HZ = 200e6
STORE_LOG2 = 29
NAMES = proto.STATUS_NAMES_DDR3
TIMING = {"fifo_high_water", "clocks", "calib_clocks", "wb_max_outstanding", "wb_read_latency_max", "wb_cmd_stalls"}
MASKED = {NAMES.index(n) for n in TIMING}
SUMMARY = ROOT / "build/fpga/ddr3-loader-sim/summary.json"
M32, M64 = (1 << 32) - 1, (1 << 64) - 1


def load_tool(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_library(source: Path, work: Path) -> ctypes.CDLL:
    generated = subprocess.run([str(COMPILER), "gen-c", str(source)], capture_output=True, text=True,
                               check=True).stdout
    generated = re.sub(r"^(static \w+ \w+\[\d+\]) = 0;", r"\1 = {0};", generated, flags=re.M)
    c_file = work / f"{source.stem}.c"
    c_file.write_text(generated, encoding="ascii")
    library = work / f"lib{source.stem}.so"
    subprocess.run(["cc", "-shared", "-fPIC", "-O1", "-Wno-parentheses-equality", "-Wno-shift-count-overflow",
                    str(c_file), "-o", str(library)], check=True, capture_output=True)
    return ctypes.CDLL(str(library))


class SourceDiff(unittest.TestCase):
    """The DDR3 loader may differ from the part-1 loader only where it says so."""

    def test_every_difference_is_marked(self):
        part1 = (ROOT / "t27/rtl/fpga_uart_loader.t27").read_text().splitlines()
        ddr3 = (ROOT / "t27/rtl/fpga_ddr3_loader.t27").read_text().splitlines()
        header_end = next(i for i, line in enumerate(ddr3) if line.startswith("// Host-to-board UART loader"))
        matcher = difflib.SequenceMatcher(a=part1, b=ddr3, autojunk=False)
        hunks = 0
        for op, i1, i2, j1, j2 in matcher.get_opcodes():
            if op == "equal":
                continue
            hunks += 1
            if j2 <= header_end:
                continue                     # the module name and the header that lists the changes
            text = "\n".join(ddr3[j1:j2])
            self.assertIn("[ddr3]", text, f"unmarked difference at line {j1 + 1}: {part1[i1:i2]} -> {ddr3[j1:j2]}")
        self.assertGreater(hunks, 5)
        # Nothing of part 1 is removed except in hunks that replace it with marked lines.
        removed = [part1[i1:i2] for op, i1, i2, j1, j2 in matcher.get_opcodes() if op == "delete"]
        self.assertEqual([r for r in removed if r and not r[0].startswith("// ")], [])


@unittest.skipUnless(HAVE_COMPILER, "T27_ROOT with a built t27c required")
class Functions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix="trinity-ddr3-loader-c-")
        work = Path(cls.work.name)
        cls.wb = build_library(ROOT / "t27/rtl/fpga_loader_wb.t27", work)
        cls.arb = build_library(ROOT / "t27/rtl/fpga_wb_arbiter.t27", work)
        cls.ld = build_library(ROOT / "t27/rtl/fpga_ddr3_loader.t27", work)
        u64, u32, b = ctypes.c_uint64, ctypes.c_uint32, ctypes.c_bool
        for lib, name, args, res in ((cls.wb, "byte_of", (u64, u64, u32), u32),
                                     (cls.wb, "merge_half", (u64, u32, u32, u32), u64),
                                     (cls.wb, "lane_bit", (u32,), u32), (cls.wb, "out_sel", (b, b, u32, u32, u32), u32),
                                     (cls.arb, "out_sel", (b, b, u32, u32, u32), u32),
                                     (cls.ld, "cal_word", (b, u32, u32, u32), u32), (cls.ld, "bump", (u32,), u32),
                                     (cls.ld, "status_ddr3", (u32,) * 15, u32),
                                     (cls.ld, "status_value", (u32,) * 24, u32),
                                     (cls.ld, "status_all", (u32,) * 38, u32),
                                     (cls.ld, "crc_byte", (u32, u32), u32), (cls.ld, "crc_byte_par", (u32, u32), u32),
                                     (cls.ld, "line_check", (u32, u32, u32), u32),
                                     (cls.ld, "resp_word", (u32, b, u32, u32, u32), u32),
                                     (cls.ld, "header_ok", (u32, u32, u32, u32), b)):
            function = getattr(lib, name)
            function.argtypes, function.restype = args, res

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def test_word_bytes(self):
        rng = random.Random(631)
        for _ in range(3000):
            word = rng.getrandbits(128)
            lo, hi, lane, v = word & M64, word >> 64, rng.randrange(16), rng.getrandbits(8)
            self.assertEqual(self.wb.byte_of(lo, hi, lane), word.to_bytes(16, "little")[lane])
            merged = bytearray(word.to_bytes(16, "little"))
            merged[lane] = v
            want = int.from_bytes(merged, "little")
            self.assertEqual(self.wb.merge_half(lo, 0, lane, v), want & M64)
            self.assertEqual(self.wb.merge_half(hi, 1, lane, v), want >> 64)
            self.assertEqual(self.wb.lane_bit(lane), 1 << lane)

    def test_outstanding_counts(self):
        for lib in (self.wb, self.arb):
            for n in range(5):
                for taken in (False, True):
                    for ack in (False, True):
                        if ack and n == 0:
                            continue                  # an ack with nothing outstanding is not counted
                        self.assertEqual(lib.out_sel(taken, ack, n, n + 1, (n - 1) & M32), n + taken - ack,
                                         (n, taken, ack))

    def test_new_status_lines_and_calibration_word(self):
        rng = random.Random(632)
        for _ in range(500):
            calib, state, top, n = bool(rng.getrandbits(1)), rng.randrange(32), rng.randrange(32), rng.randrange(256)
            self.assertEqual(self.ld.cal_word(calib, state, top, n), proto.calib_word(calib, state, top, n))
            values = [rng.getrandbits(32) for _ in range(14)]
            for i in range(23, 37):
                self.assertEqual(self.ld.status_ddr3(i, *values), values[i - 23])
        self.assertEqual([self.ld.bump(x) for x in (0, 254, 255)], [1, 255, 255])

    def test_status_tree_is_the_status_lines(self):
        rng = random.Random(635)
        for _ in range(200):
            values = [rng.getrandbits(32) for _ in range(37)]
            for i in range(37):
                want = self.ld.status_value(i, *values[:23]) if i < 23 else self.ld.status_ddr3(i, *values[23:])
                self.assertEqual(self.ld.status_all(i, *values), want, i)
                self.assertEqual(want, values[i])

    def test_parallel_crc_step_is_the_crc(self):
        import zlib
        rng = random.Random(634)
        for _ in range(20000):
            c, b = rng.getrandbits(32), rng.getrandbits(8)
            self.assertEqual(self.ld.crc_byte_par(c, b), self.ld.crc_byte(c, b), (c, b))
        for n in (1, 9, 100, 4096):
            data = bytes(rng.getrandbits(8) for _ in range(n))
            c = M32
            for x in data:
                c = self.ld.crc_byte_par(c, x)
            self.assertEqual(c ^ M32, zlib.crc32(data))

    def test_unchanged_functions_still_match_the_model(self):
        rng = random.Random(633)
        store = 1 << STORE_LOG2
        for _ in range(2000):
            tag, a, v = rng.choice(b"HANC"), rng.getrandbits(32), rng.getrandbits(32)
            self.assertEqual(self.ld.line_check(tag, a, v), proto.line_check(tag, a, v))
            seq, reason, length = rng.getrandbits(8), rng.randrange(10), rng.getrandbits(16)
            cmd, known = rng.choice([0x4C, 0x52, 0x53, 0x42, 0x00]), bool(rng.getrandbits(1))
            self.assertEqual(self.ld.resp_word(seq, known, reason, cmd, length),
                             proto.resp_word(seq, known, reason, cmd, length))
            cmd = rng.choice([0x4C, 0x52, 0x53, 0x42])
            addr = rng.choice([0, store - 1, store, store - 4096, rng.getrandbits(32), rng.randrange(store)])
            length = rng.choice([0, 1, 4096, 4097, rng.getrandbits(16)])
            model = proto.DeviceModel(store=proto.SparseStore(STORE_LOG2), store_log2=STORE_LOG2, proto=3)
            model.cmd, model.addr, model.len, model.seq, model.seq_known = cmd, addr, length, 0, True
            model.out.clear()
            model._check_header()
            self.assertEqual(self.ld.header_ok(cmd, addr, length, store), not (model.out and model.out[-1][0] == "N"))


class ProtocolThree(unittest.TestCase):
    """The protocol model's protocol-3 additions (no tools needed)."""

    def test_not_ready_until_calibrated(self):
        m = proto.DeviceModel(store=proto.SparseStore(STORE_LOG2), store_log2=STORE_LOG2, proto=3, calibrated=False)
        for x in proto.load_frame(1, 0x1000_0003, b"abc") + proto.read_frame(2, 0, 4):
            m.rx(x)
        self.assertEqual([proto.decode_resp_word(item[1])["reason_name"] for item in m.out[1:]],
                         ["not_ready", "not_ready"])
        self.assertEqual(m.c["nak_not_ready"], 2)
        m.calibrated = True
        for x in proto.load_frame(1, 0x1000_0003, b"abc") + proto.read_frame(2, 0x1000_0002, 5) + proto.status_frame(3):
            m.rx(x)
        self.assertEqual(m.out[3][0], "A")
        self.assertEqual(m.out[4][1][proto.HEADER_BYTES:-4], b"\0abc\0")
        status = {NAMES[item[1] & 0xFFFF]: item[2] for item in m.out[5:]}
        self.assertEqual(len(m.out[5:]), 37)
        self.assertEqual(status["calib"], proto.calib_word(True, 23, 23, 0))
        self.assertEqual((status["wb_writes"], status["wb_reads"], status["wb_read_hits"]), (1, 1, 4))
        self.assertEqual(m.out[0][2] >> 24, 3)

    def test_kept_word_and_word_counts(self):
        m = proto.DeviceModel(store=proto.SparseStore(STORE_LOG2), store_log2=STORE_LOG2, proto=3)
        m._wishbone_writes(0x0F, 2)                    # words 0 and 1
        self.assertEqual(m.c["wb_writes"], 2)
        m._wishbone_reads(0x10, 20)                   # words 1, 2
        self.assertEqual((m.c["wb_reads"], m.c["wb_read_hits"], m.kept), (2, 18, 2))
        m._wishbone_writes(0x2F, 1)                   # a write to the kept word forgets it
        self.assertIsNone(m.kept)
        m._wishbone_reads(0x20, 1)
        self.assertEqual(m.c["wb_reads"], 3)

    def test_sparse_store_background(self):
        store = proto.SparseStore(STORE_LOG2, proto.bg_word)
        self.assertEqual(store[0x100:0x110], proto.bg_word(0x10))
        store[0x105:0x107] = b"xy"
        self.assertEqual(store[0x100:0x110], proto.bg_word(0x10)[:5] + b"xy" + proto.bg_word(0x10)[7:])
        self.assertEqual(len(store), 1 << 29)

    def test_status_names_by_count(self):
        lines = [proto.Event("line", tag="C", a=(5 << 24) | (37 << 16) | i, v=i) for i in range(37)]
        self.assertEqual(proto.decode_status(lines)["arb_stray"], 36)
        lines = [proto.Event("line", tag="C", a=(5 << 24) | (23 << 16) | i, v=i) for i in range(23)]
        self.assertEqual(proto.decode_status(lines)["nak_port"], 22)


class Scenario:
    """A host script for tests/tb_ddr3_loader.v and the model's prediction of every byte the
    device sends (tests/test_uart_loader.py's Scenario at the DDR3 build's clock)."""

    def __init__(self, name, baud_div=16, timeout=3840, probation=48000, skew=0.0, reader=False,
                 plusargs=None, calibrated=True, reader_trits=1237):
        self.name = name
        self.reader = reader
        self.params = {"BAUD_DIV": baud_div, "TIMEOUT_CLOCKS": timeout, "PROBATION_CLOCKS": probation}
        if reader:
            self.params["READER_TRITS"] = reader_trits
        self.plusargs = dict(plusargs or {})
        self.base = 0
        self.cuts: list[int] = []
        self.timeout_ps = timeout * CLK_PS
        self.probation_ps = probation * CLK_PS
        self.cmds: list[str] = []
        self.model = proto.DeviceModel(store=proto.SparseStore(STORE_LOG2, proto.bg_word), store_log2=STORE_LOG2,
                                       build_id=0x5EED1063, default_div=baud_div, proto=proto.PROTO_DDR3,
                                       calibrated=calibrated)
        self.seen = 0
        self.skew = skew
        self.unpredictable_reads: set[int] = set()       # seqs of read-backs whose data are not predicted
        self.masked_status = set(MASKED)
        self.load_markers: dict[int, int] = {}
        self.marker_id = 0
        self.set_rate(baud_div)
        self.wait()

    def set_rate(self, div, host_follows=True):
        self.div = div
        self.bit_ps = div * CLK_PS
        if host_follows:
            self.cmds.append(f"p {round(self.bit_ps * (1 + self.skew))}")
            self.cmds.append(f"d {self.bit_ps}")

    def expected_bytes(self) -> int:
        return sum(len(proto.expected_bytes(item)) for item in self.model.out[self.base:])

    def send(self, data: bytes, feed=True):
        for x in data:
            self.cmds.append(f"b {x:02x}")
            if feed:
                self.model.rx(x)

    def send_ferr(self, x: int):
        self.cmds.append(f"f {x:02x}")
        self.model.rx(x, ferr=True)

    def pause(self, ps: int):
        self.cmds.append(f"w {ps}")
        if ps > self.timeout_ps:
            self.model.timeout()

    def drop_partial(self):
        self.pause(self.timeout_ps * 3 // 2 + 20 * self.bit_ps)

    def wait(self, pending=None):
        pending = self.expected_bytes() if pending is None else pending
        new, self.seen = pending - self.seen, pending
        self.cmds.append(f"t {pending} {new * 12 * self.bit_ps + 4 * self.timeout_ps + 60 * 4096 * CLK_PS}")

    def marker(self) -> int:
        self.marker_id += 1
        self.cmds.append(f"m {self.marker_id}")
        return self.marker_id

    def load(self, seq, addr, payload, wait=True):
        self.send(proto.load_frame(seq, addr, payload))
        self.load_markers[self.marker()] = len(payload)
        if wait:
            self.wait()

    def read(self, seq, addr, length, wait=True, predict=True):
        self.send(proto.read_frame(seq, addr, length))
        if not predict:
            self.unpredictable_reads.add(seq)
        if wait:
            self.wait()

    def status(self, seq, wait=True):
        self.send(proto.status_frame(seq))
        if wait:
            self.wait()

    def glitch(self, bits: float):
        d = self.div
        pulse = bits * d
        points = [(d >> 1) + k * d for k in range(10)]
        for p in points:
            if p - 0.25 <= pulse <= p + 1.25:
                raise ValueError(f"glitch of {bits} bits ends within a clock of a sampling point")
        self.cmds.append(f"l {round(bits * self.bit_ps)}")
        self.cmds.append(f"w {12 * self.bit_ps}")
        low = [pulse > p for p in points]
        if not low[0]:
            self.model.false_start()
            return
        self.model.rx(sum(1 << k for k in range(8) if not low[k + 1]), ferr=low[9])

    def reset_button(self, abandon=False, calib_clocks=3000):
        """The reset button: UberDDR3 (the model) is reset too and calibrates again; the
        model's memory keeps its words (the board's is not claimed to: UberDDR3's self-test
        rewrites three quarters of U6 at every calibration, docs/uart-loader.md, "Limits")."""
        if abandon:
            self.model.out.pop()
        self.cmds.append("r 2000000")
        self.model.reset()
        # The H line leaves before the calibration ends; wait until it has.
        self.wait()
        self.cmds.append(f"w {calib_clocks * CLK_PS}")

    def reset_cutting(self, after_bytes: int, calib_clocks=3000):
        self.cmds.append(f"t {self.seen + after_bytes} {(after_bytes + 20) * 12 * self.bit_ps + 4 * self.timeout_ps}")
        self.cmds.append(f"x {64 * self.bit_ps}")
        self.cuts.append(len(self.model.out))
        self.base = len(self.model.out)
        self.seen = 0
        self.model.reset()
        self.wait()
        self.cmds.append(f"w {calib_clocks * CLK_PS}")

    def hang(self, mask: int):
        self.cmds.append(f"h {mask}")

    def script(self) -> str:
        return "\n".join(self.cmds + ["w 2000000", "e"]) + "\n"


def parse_capture(path: Path):
    received, markers, gave_up, cuts = [], {}, [], []
    for line in path.read_text().splitlines():
        parts = line.split()
        if parts[0] == "R":
            received.append((int(parts[1]), int(parts[2], 16), len(parts) > 3))
        elif parts[0] == "M":
            markers[int(parts[2])] = int(parts[1])
        elif parts[0] == "T":
            gave_up.append((int(parts[1]), int(parts[2])))
        elif parts[0] == "X":
            cuts.append(int(parts[1]))
    return received, markers, gave_up, cuts


def decode(received):
    decoder = proto.StreamDecoder()
    events = []
    for t, value, _ferr in received:
        events += decoder.feed(bytes([value]), t)
    return events + decoder.finish(received[-1][0] if received else None)


def rand_bytes(rng, n):
    return bytes(rng.getrandbits(8) for _ in range(n))


def scenario_transfer(rng, skew):
    """Loads and reads over the whole 512 MiB: unaligned, above 256 MiB, 1..4096 bytes, each read
    first (never-written memory reads as the model's background), loaded and read back; chunks
    back to back sharing a word; a write into the word the read side keeps."""
    s = Scenario(f"transfer_{skew:+.2f}", skew=skew)
    seq = 0

    def nxt():
        nonlocal seq
        seq = (seq + 1) & 255
        return seq
    lengths = [1, 2, 15, 16, 17, 255, 1000, 4096] + [rng.randrange(1, 4097) for _ in range(3)]
    rng.shuffle(lengths)
    # Regions spread over the address space; burst addresses must not collide modulo the model's
    # 2^16 slots (the model stops on an alias), so each region sits in its own 64 KiB of slots.
    bases = [0x0000_0000, 0x0080_0000, 0x0FF0_0000, 0x1000_0000, 0x1AB0_0000, 0x1FF0_0000]
    for i, length in enumerate(lengths):
        slot_base = (i % len(bases)) * 0x1_0000 + (i // len(bases)) * 0x8000
        addr = bases[i % len(bases)] + slot_base + rng.randrange(0, 0x2000)
        addr = min(addr, (1 << 29) - length)
        payload = rand_bytes(rng, length)
        s.read(nxt(), addr, length)                       # background (or zeros: never written)
        s.load(nxt(), addr, payload)
        s.read(nxt(), addr, length)
    # The last byte of the store and a chunk ending there.
    s.load(nxt(), (1 << 29) - 37, rand_bytes(rng, 37))
    s.read(nxt(), (1 << 29) - 40, 40)
    # Back-to-back chunks at an unaligned address: each chunk's first and last words are shared.
    addr = 0x1234_5007 % (1 << 29) & ~0xF0000 | 0x60000
    blob = rand_bytes(rng, 3 * 1000 + 13)
    for k in range(0, len(blob), 1000):
        s.load(nxt(), addr + k, blob[k:k + 1000])
    s.read(nxt(), addr - 5, len(blob) + 10)
    # A write into the kept word: read a word (kept), load one byte into it, read it again.
    kept = 0x0A0B_0C05 % (1 << 29) & ~0xF0000 | 0x70000
    s.load(nxt(), kept - 5, rand_bytes(rng, 32))
    s.read(nxt(), kept - 5, 32)                          # the last word read is kept
    s.load(nxt(), kept + 20, b"\x5a")                     # a byte inside that word
    s.read(nxt(), kept + 20, 1)                           # must be the new byte, not the kept one
    s.read(nxt(), kept + 16, 16)
    s.status(nxt())
    return s


def scenario_faults(rng, skew):
    """The part-1 fault set on the DDR3 top."""
    s = Scenario(f"faults_{skew:+.2f}", skew=skew)
    payload = rand_bytes(rng, 300)
    good = proto.load_frame(1, 0x1000_1003, payload)
    bad = bytearray(good)
    bad[40] ^= 0x10
    s.send(bytes(bad))
    s.wait()
    s.load(1, 0x1000_1003, payload)
    s.load(1, 0x1000_1003, payload)                        # duplicate
    payload2 = rand_bytes(rng, 200)
    frame2 = proto.load_frame(2, 0x2000, payload2)
    s.send(frame2[:100] + frame2[101:])                   # dropped byte
    s.drop_partial()
    s.wait()
    s.load(2, 0x2000, payload2)
    s.send(proto.load_frame(3, 0x3000, payload)[:57])      # host stops mid-frame
    s.drop_partial()
    s.wait()
    s.load(3, 0x3000, payload)
    s.send(proto.frame(0x51, 4, 0, 0))                     # unknown command
    s.wait()
    s.send(proto.frame(proto.CMD_LOAD, 5, 0, 0))           # length 0
    s.wait()
    s.send(proto.frame(proto.CMD_READ, 6, 0, 4097))        # too long
    s.wait()
    s.send(proto.frame(proto.CMD_READ, 7, (1 << 29) - 10, 11))   # past the 512 MiB store
    s.wait()
    s.send(proto.frame(proto.CMD_STATUS, 8, 0, 3))
    s.wait()
    s.send(proto.frame(proto.CMD_BAUD, 9, 8, 0))
    s.wait()
    s.drop_partial()
    frame3 = proto.load_frame(10, 0x4000, payload[:50])
    s.send(frame3[:20])
    s.send_ferr(frame3[20])                               # framing error inside a frame
    s.wait()
    s.drop_partial()
    s.send(proto.load_frame(14, 0x8000, payload[:100])[:40], feed=False)
    s.reset_button()                                      # reset during a frame
    s.load(14, 0x8000, payload[:100])
    s.send(bytes([0x00, 0xFF, 0x13, 0xA5, 0x00, 0xA5, 0xA5, 0x37]))
    s.send(bytes([0xA5]))
    s.drop_partial()
    s.send(rand_bytes(rng, 40).replace(b"\xa5", b"\x11"))
    s.load(11, 0x5000, payload[:77])
    for bits in (0.2, 3.0, 30.0, 0.45, 0.62, 2.65, 9.3, 9.7):
        s.glitch(bits)
    s.load(12, 0x6000, payload[:90])
    s.send(proto.load_frame(13, 0x7000, payload[:64])[:30])
    s.glitch(0.2)
    s.send(proto.load_frame(13, 0x7000, payload[:64])[30:])
    s.wait()
    s.read(15, 0x1000_1003, 300)
    s.read(16, 0x2000, 200)
    s.read(17, 0x8000, 100)
    # The reset button 200 bytes into a 300-byte read-back: the output stops, H follows.
    s.read(18, 0x1000_1003, 300, wait=False)
    s.reset_cutting(200)
    s.read(19, 0x1000_1003, 300)
    s.status(20)
    return s


def scenario_not_ready():
    """Loads and reads before UberDDR3's calibration completes are refused (not_ready); the
    status shows the calibration state; after it, the same frames are acknowledged."""
    calib = 400_000                                        # 2 ms at 200 MHz: several frames fit before it
    s = Scenario("not_ready", plusargs={"calib_clocks": calib}, calibrated=False)
    s.load(1, 0x100, b"early bytes")
    s.read(2, 0x100, 11)
    s.status(3)                                           # calib 0, state walking up (masked: timing)
    s.masked_status.add(NAMES.index("calib"))
    s.cmds.append(f"w {calib * CLK_PS}")
    s.model.calibrated = True
    s.load(1, 0x100, b"early bytes")
    s.read(4, 0x100, 11)
    s.status(5)
    return s


def scenario_watchdog(rng):
    """The port watchdog on the Wishbone side: a port that takes no request (o_wb_stall held)
    during a commit and during a read-back, acks that do not come during a commit, and a read
    whose ack comes only after the loader gave up on it and asked for the next byte: that ack
    is dropped, the next request answered right. The reset button while a commit waits on a
    stalled port. Every chunk is loaded again and reads back identical."""
    s = Scenario("watchdog")
    chunks = {addr: rand_bytes(rng, n) for addr, n in
              ((0x100, 300), (0x1000, 200), (0x2003, 150), (0x3000, 100), (0x4000, 100))}
    s.load(1, 0x100, chunks[0x100])
    # Reset while the commit waits on a port that takes no request.
    s.hang(1)
    s.send(proto.load_frame(2, 0x3000, chunks[0x3000]))
    s.cmds.append(f"w {1500 * CLK_PS}")
    s.reset_button(abandon=True)
    s.hang(0)
    s.load(2, 0x3000, chunks[0x3000])
    # Watchdog: no request taken; then acks withheld (the writes never finish).
    for seq, addr, mask in ((4, 0x1000, 1), (5, 0x2003, 2)):
        previous = s.model.last
        s.hang(mask)
        s.send(proto.load_frame(seq, addr, chunks[addr]))
        s.model.port_failed_commit(previous)
        s.wait()
        s.hang(0)
        s.cmds.append(f"w {200 * CLK_PS}")
        s.load(seq, addr, chunks[addr])
    # A read-back whose first request is never taken: zeros, a CRC that does not match, a port nak.
    s.hang(1)
    s.send(proto.read_frame(6, 0x100, 300))
    s.model.port_failed_read(0)
    s.wait()
    s.hang(0)
    s.cmds.append(f"w {200 * CLK_PS}")
    s.model.kept = None
    # A read taken on the bus whose ack is withheld: the loader gives up; the next read's
    # request comes while that read still waits; then the acks flow. The late ack is dropped.
    s.hang(2)
    s.send(proto.read_frame(7, 0x3000, 50))
    s.model.port_failed_read(0)
    s.wait()
    s.send(proto.read_frame(8, 0x1000, 40))
    # The loader sends the 10 header bytes (1,600 clocks at 16 per bit) before it asks for the
    # first data byte, then waits up to one timeout (3,840) for the port: release in between.
    s.cmds.append(f"w {3000 * CLK_PS}")
    s.hang(0)
    s.model.kept = None
    s.wait()
    for seq, addr in enumerate(sorted(chunks), start=9):
        s.read(seq, addr, len(chunks[addr]))
    s.status(14)
    # The Wishbone counts after watchdog aborts are not modelled.
    s.masked_status |= {NAMES.index(n) for n in ("wb_writes", "wb_reads", "wb_read_hits", "wb_acks_dropped")}
    s.extra = {"chunks": chunks}
    return s


def scenario_late_ack(rng):
    """The ack of a read the loader gave up on reaches the Wishbone master in the clock the next
    read-back asks for its first data byte (+race_late_ack=1 releases it then). It is dropped and
    the read-back is right (review of #63 part 2: it once answered that request with the stale byte,
    in a frame with a good CRC)."""
    s = Scenario("late_ack", plusargs={"race_late_ack": 1})
    a, b = rand_bytes(rng, 16), rand_bytes(rng, 16)
    s.load(1, 0x100, a)
    s.load(2, 0x2000, b)
    s.hang(2)                                  # acks withheld: the read is taken and never answered
    s.send(proto.read_frame(3, 0x100, 16))
    s.model.port_failed_read(0)
    s.wait()
    s.model.kept = None
    s.read(4, 0x2000, 16)                      # the bench releases the withheld ack at rb_k 9 -> 10
    s.hang(0)
    s.read(5, 0x2000, 16)
    s.status(6)
    s.masked_status |= {NAMES.index(n) for n in ("wb_writes", "wb_reads", "wb_read_hits", "wb_acks_dropped")}
    s.extra = {"a": a, "b": b}
    return s


def scenario_pipelined(rng):
    s = Scenario("pipelined")
    burst = b""
    for i in range(6):
        payload = rand_bytes(rng, rng.randrange(20, 120))
        burst += proto.load_frame(i + 1, 0x1100_0000 + 0x100 * i + i, payload)
    burst += proto.status_frame(7) + proto.read_frame(8, 0x1100_0101, 64)
    burst += proto.load_frame(1, 0x9000, b"dup-free?") + proto.load_frame(1, 0x9000, b"dup-free?")
    s.send(burst)
    s.wait()
    s.status(9)
    return s


def scenario_overflow(rng):
    """A load sent while the device sends a 4096-byte read-back: the FIFO overflows, the load
    gets an overflow nak (as part 1); not predicted byte for byte, checked by content."""
    s = Scenario("overflow")
    fill = rand_bytes(rng, 4096).replace(b"\xa5", b"\x5b")
    s.load(1, 0x40000, fill)
    s.read(2, 0x40000, 4096, wait=False)
    big = rand_bytes(rng, 2000).replace(b"\xa5", b"\x5b")
    s.send(proto.load_frame(3, 0x50000, big), feed=False)
    s.predictable = False
    s.cmds.append(f"w {3000 * 10 * s.bit_ps}")
    s.send(proto.load_frame(3, 0x50000, big), feed=False)
    s.cmds.append(f"w {3000 * 10 * s.bit_ps}")
    s.send(proto.read_frame(4, 0x50000, 2000), feed=False)
    s.cmds.append(f"w {2200 * 10 * s.bit_ps}")
    s.send(proto.status_frame(5), feed=False)
    s.cmds.append(f"w {1200 * 10 * s.bit_ps}")
    s.extra = {"fill": fill, "big": big}
    return s


def scenario_baud(rng):
    s = Scenario("baud", baud_div=32, timeout=7680, probation=64000)
    s.send(proto.baud_frame(1, 16))
    s.wait()
    s.set_rate(16)
    s.cmds.append(f"w {4 * 32 * CLK_PS}")
    s.status(2)
    payload = rand_bytes(rng, 500)
    s.load(3, 0x1C00_0400, payload)
    s.read(4, 0x1C00_0400, 500)
    s.send(proto.baud_frame(5, 20))
    s.wait()
    s.cmds.append(f"w {s.probation_ps + 4 * s.timeout_ps}")
    s.model.revert_baud()
    s.set_rate(32)
    s.status(6)
    s.read(7, 0x1C00_0400, 500)
    # A second change after a revert holds (the registered probation compare once read the
    # count of the probation that had ended and undid the new rate one clock after it took effect).
    s.send(proto.baud_frame(8, 16))
    s.wait()
    s.set_rate(16)
    s.cmds.append(f"w {4 * 32 * CLK_PS}")
    s.status(9)
    s.read(10, 0x1C00_0400, 500)
    return s


def scenario_board_rate(skew):
    """The board's divisor, 723 controller clocks per bit (115200 at 83.33 MHz), timeout 50 ms of
    83.33 MHz; the host's bit period off by `skew`."""
    s = Scenario(f"board_rate_{skew * 100:+.1f}%", baud_div=723, timeout=4_166_666, probation=250_000_000, skew=skew)
    payload = bytes(range(64))
    s.load(1, 0x1FFF_FFC0, payload)
    s.read(2, 0x1FFF_FFC0, 64)
    return s


def scenario_arbitration(rng):
    """The #62 reader as master 1 (runs until the bench stops, 1,237 trits, region bursts 0-19)
    while the loader loads and reads elsewhere, and reads words of the reader's region, which
    the reader rewrites every run (their data are not predicted; the kept-word monitor checks
    them against the memory)."""
    s = Scenario("arbitration", reader=True)
    seq = 0
    for k in range(6):
        payload = rand_bytes(rng, rng.choice([700, 1500, 4096]))
        addr = 0x0100_4000 + k * 0x1003
        seq += 1
        s.load(seq, addr, payload)
        seq += 1
        s.read(seq, addr, len(payload))
        seq += 1
        s.read(seq, 0x30 + 7 * k, 40, predict=False)     # bursts 3-6 of the reader's region
        s.cmds.append(f"w {4000 * CLK_PS}")
    seq += 1
    s.status(seq)
    s.masked_status |= {NAMES.index(n) for n in ("wb_reads", "wb_read_hits", "arb_switches")}
    return s


def build_benches(work: Path, scenarios):
    sources = []
    for core in CORES:
        verilog = subprocess.run([str(COMPILER), "gen-verilog", str(ROOT / f"t27/rtl/{core}.t27")],
                                 capture_output=True, text=True, check=True).stdout
        (work / f"{core}.v").write_text(verilog)
        sources.append(str(work / f"{core}.v"))
    common = [str(ROOT / "fpga/ax7203/sim/xilinx_stubs.v"), *sources,
              str(ROOT / "fpga/ax7203/ddr3/tms_ddr3_loader_ax7203.v"), str(ROOT / "fpga/ax7203/ddr3/tms_ddr3_reader.v"),
              str(ROOT / "tests/sim_ddr3_loader_model.v"), str(ROOT / "tests/tb_uart_loader.v"),
              str(ROOT / "tests/tb_ddr3_loader.v")]
    out = {}
    for s in scenarios:
        key = (s.reader, tuple(sorted(s.params.items())))
        if key in out:
            continue
        flags = [f"-Ptb_ddr3_loader.{k}={v}" for k, v in s.params.items()]
        vvp = work / f"{'reader' if s.reader else 'loader'}_{'_'.join(str(v) for _k, v in sorted(s.params.items()))}.vvp"
        subprocess.run(["iverilog", "-g2012", *(["-DDDR3_LOADER_READER"] if s.reader else []), *flags,
                        "-s", "tb_ddr3_loader", "-o", str(vvp), *common], check=True, capture_output=True, text=True)
        out[key] = vvp
    return out


@unittest.skipUnless(HAVE_TOOLS, "T27_ROOT with a built t27c and Icarus required")
class LoaderSimulation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        keep = os.environ.get("DDR3_LOADER_SIM_DIR")
        cls.work = None if keep else tempfile.TemporaryDirectory(prefix="trinity-ddr3-loader-sim-")
        work = Path(keep) if keep else Path(cls.work.name)
        work.mkdir(parents=True, exist_ok=True)
        rng = random.Random(1632)
        cls.scenarios = {s.name: s for s in [
            scenario_transfer(rng, 0.05), scenario_transfer(rng, -0.04), scenario_faults(rng, -0.04),
            scenario_not_ready(), scenario_watchdog(rng), scenario_pipelined(rng), scenario_overflow(rng),
            scenario_baud(rng), scenario_board_rate(0.05), scenario_board_rate(-0.045), scenario_arbitration(rng),
            scenario_late_ack(rng)]}
        vvps = build_benches(work, cls.scenarios.values())

        def run(s):
            script, capture, capture2 = work / f"{s.name}.script", work / f"{s.name}.capture", work / f"{s.name}.lines"
            script.write_text(s.script())
            vvp = vvps[(s.reader, tuple(sorted(s.params.items())))]
            args = ["vvp", str(vvp), f"+script={script}", f"+capture={capture}", f"+capture2={capture2}",
                    *[f"+{k}={v}" for k, v in s.plusargs.items()]]
            proc = subprocess.run(args, cwd=work, capture_output=True, text=True, timeout=3600)
            (work / f"{s.name}.log").write_text(proc.stdout + proc.stderr)
            return proc, capture, capture2

        with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
            results = dict(zip(cls.scenarios, pool.map(run, cls.scenarios.values())))
        cls.results = {}
        for name, (proc, capture, capture2) in results.items():
            received, markers, gave_up, cuts = parse_capture(capture) if capture.exists() else ([], {}, [], [])
            lines = []
            if capture2.exists():
                for raw in capture2.read_text().splitlines():
                    t_ns, text = raw.split("\t", 1)
                    lines.append({"t_s": int(t_ns) / 1e9, "line": text})
            monitors = {}
            for tag in ("TBWB", "TBKEPT", "MODEL", "TBPORT", "TBRACE"):
                m = re.search(rf"^{tag} (.*)$", proc.stdout, re.M)
                if m:
                    monitors[tag] = {k: int(v) for k, v in (kv.split("=") for kv in m.group(1).split())}
            cls.results[name] = {"proc": proc, "received": received, "markers": markers, "gave_up": gave_up,
                                 "cuts": cuts, "reader_lines": lines, "monitors": monitors,
                                 "events": decode([r for r in received if not cuts or r[0] > cuts[-1]])}
        cls.write_summary()

    @classmethod
    def tearDownClass(cls):
        if cls.work:
            cls.work.cleanup()

    @classmethod
    def latencies(cls, name):
        s, r = cls.scenarios[name], cls.results[name]
        out = []
        for marker, length in s.load_markers.items():
            t0 = r["markers"].get(marker)
            later = [t for t, _v, _f in r["received"] if t0 is not None and t > t0]
            if later:
                reply_clocks = (later[0] - t0) / CLK_PS
                out.append({"payload": length, "turnaround_clocks": round(reply_clocks - 9.5 * s.div)})
        return out

    @classmethod
    def write_summary(cls):
        summary = {"schema": "trinity.ddr3-loader-sim.v1", "clock_hz": SIM_HZ,
                   "note": "Icarus runs of tests/test_ddr3_loader.py (the DDR3 loader top with the behavioural "
                           "Wishbone memory tests/sim_ddr3_loader_model.v, not UberDDR3); turnaround = controller "
                           "clocks from the end of a load frame's last stop bit to the start bit of its ack",
                   "scenarios": {}}
        for name, s in cls.scenarios.items():
            r = cls.results[name]
            summary["scenarios"][name] = {"params": s.params, "plusargs": s.plusargs, "host_bit_skew": s.skew,
                                          "second_master": "fpga_ddr3_reader" if s.reader else None,
                                          "bytes_received": len(r["received"]), "events": len(r["events"]),
                                          "reader_lines": len(r["reader_lines"]), "monitors": r["monitors"],
                                          "load_turnaround": cls.latencies(name)}
        SUMMARY.parent.mkdir(parents=True, exist_ok=True)
        SUMMARY.write_text(json.dumps(summary, indent=1) + "\n")

    def completed(self, name):
        r = self.results[name]
        self.assertEqual(r["proc"].returncode, 0, r["proc"].stdout[-3000:] + r["proc"].stderr[-2000:])
        self.assertIn("TB_PASS", r["proc"].stdout, r["proc"].stdout[-2000:])
        mon = r["monitors"]
        self.assertEqual(mon["TBWB"]["misrouted"], 0, mon)
        self.assertEqual(mon["TBKEPT"]["mismatches"], 0, mon)
        self.assertEqual(mon["TBWB"]["taken0"] + mon["TBWB"]["taken1"], mon["MODEL"]["writes"] + mon["MODEL"]["reads"])
        # Waiting for a frame, the loader does not hold the port (a nak in the middle of a chunk
        # once left an unfinished word buffered and wb_cyc high until the next command).
        self.assertLessEqual(mon["TBPORT"]["idle_held_max"], 1, mon)
        return r

    def assert_as_predicted(self, name):
        s, r = self.scenarios[name], self.completed(name)
        self.assertFalse(r["gave_up"], f"{name}: waited in vain for device bytes at {r['gave_up'][:3]}")
        self.assertEqual(len(r["cuts"]), len(s.cuts), name)
        bounds, times = [0] + s.cuts, [-1] + r["cuts"]
        for i, cut in enumerate(s.cuts):
            sent = b"".join(proto.expected_bytes(item) for item in s.model.out[bounds[i]:cut])
            got_bytes = bytes(v for t, v, _f in r["received"] if times[i] < t <= times[i + 1])
            self.assertGreater(len(got_bytes), 0, (name, i))
            self.assertLessEqual(len(got_bytes), len(sent), (name, i))
            self.assertEqual(got_bytes[:-1], sent[:len(got_bytes) - 1], (name, i))
        want = decode([(0, x, False) for item in s.model.out[bounds[-1]:] for x in proto.expected_bytes(item)])
        got = r["events"]
        self.assertEqual(len(got), len(want), f"{name}: {[e.as_dict() for e in got[-5:]]}")
        for i, (g, w) in enumerate(zip(got, want)):
            self.assertEqual(g.kind, w.kind, (name, i, g.as_dict(), w.as_dict()))
            if g.kind == "line":
                self.assertTrue(g.check_ok, (name, i, g.as_dict()))
                self.assertEqual((g.tag, g.a), (w.tag, w.a), (name, i, g.as_dict(), w.as_dict()))
                if not (g.tag == "C" and (g.a & 0xFFFF) in s.masked_status):
                    self.assertEqual(g.v, w.v, (name, i, g.as_dict(), w.as_dict(), NAMES[g.a & 0xFFFF]
                                                if g.tag == "C" else ""))
            elif g.kind == "readback":
                self.assertEqual((g.seq, g.addr, len(g.data)), (w.seq, w.addr, len(w.data)), (name, i))
                if g.seq not in s.unpredictable_reads:
                    self.assertEqual(g.crc_ok, w.crc_ok, (name, i))
                    self.assertEqual(g.data, w.data, (name, i))
                else:
                    self.assertTrue(g.crc_ok, (name, i))
            else:
                self.assertEqual((g.kind, g.raw), (w.kind, w.raw), (name, i, g.as_dict()))
        return r

    def status_of(self, events, seq):
        return proto.decode_status([e for e in events if e.kind == "line" and e.tag == "C" and e.a >> 24 == seq])

    def test_transfers_over_the_whole_store(self):
        for name in ("transfer_+0.05", "transfer_-0.04"):
            r = self.assert_as_predicted(name)
            s = self.scenarios[name]
            reads = [e for e in r["events"] if e.kind == "readback"]
            self.assertEqual(len(reads), 27)
            self.assertTrue(any(e.addr >= 1 << 28 for e in reads))
            self.assertTrue(any(e.addr % 16 for e in reads))
            status = proto.decode_status(r["events"][-37:])
            self.assertEqual(proto.decode_calib(status["calib"]),
                             {"calib_complete": 1, "state": 23, "highest": 23, "returns_to_idle": 0})
            self.assertEqual(status["calib_lost_clocks"], 0)
            self.assertEqual((status["wb_acks_dropped"], status["wb_acks_stray"], status["arb_stray"]), (0, 0, 0))
            self.assertEqual(status["wb_writes"], s.model.c["wb_writes"])
            self.assertGreater(status["wb_read_hits"], 1000)
            mon = r["monitors"]
            self.assertGreater(mon["MODEL"]["partial"], 10)                  # unaligned first and last words
            self.assertEqual(mon["TBWB"]["taken0"], mon["TBWB"]["acks0"])
            self.assertEqual(mon["TBWB"]["taken1"], 0)
            self.assertGreater(mon["TBKEPT"]["hits"], 1000)

    def test_part1_faults_on_ddr3(self):
        r = self.assert_as_predicted("faults_-0.04")
        # The naks came before the reset that cut the read-back (their bytes are compared with the
        # model as the prefix of the output up to the cut).
        before = decode([x for x in r["received"] if x[0] <= r["cuts"][0]])
        naks = [e.resp()["reason_name"] for e in before if e.kind == "line" and e.tag == "N"]
        for reason in ("crc", "timeout", "command", "length", "framing"):
            self.assertIn(reason, naks)
        self.assertEqual(len(r["cuts"]), 1)
        # The status after the reset that cut the read-back: counters restart at the reset.
        status = proto.decode_status(r["events"][-37:])
        self.assertEqual((status["frames_committed"], status["readbacks"]), (0, 1))
        self.assertEqual(status["calib_lost_clocks"], 0)
        acks = [e.resp()["reason_name"] for e in r["events"] if e.kind == "line" and e.tag == "A"]
        self.assertEqual(acks, [])

    def test_not_ready_before_calibration(self):
        r = self.assert_as_predicted("not_ready")
        naks = [e.resp()["reason_name"] for e in r["events"] if e.kind == "line" and e.tag == "N"]
        self.assertEqual(naks, ["not_ready", "not_ready"])
        early = self.status_of(r["events"], 3)
        self.assertEqual(proto.decode_calib(early["calib"])["calib_complete"], 0)
        self.assertEqual(early["calib_clocks"], 0)
        late = self.status_of(r["events"], 5)
        self.assertEqual(late["nak_not_ready"], 2)
        self.assertGreaterEqual(late["calib_clocks"], 400_000)
        self.assertEqual(late["calib_lost_clocks"], 0)

    def test_port_watchdog_and_late_acks(self):
        r = self.assert_as_predicted("watchdog")
        naks = [(e.resp()["reason_name"], e.resp()["cmd"]) for e in r["events"] if e.kind == "line" and e.tag == "N"]
        self.assertEqual(naks, [("port", proto.CMD_LOAD)] * 2 + [("port", proto.CMD_READ)] * 2)
        status = proto.decode_status(r["events"][-37:])
        # The acks of both reads the loader gave up on are dropped: the one presented while the port
        # took no request (taken after the release) and the one whose ack was withheld. Before
        # port_give_up the first reached the idle loader as an unused rd_valid.
        self.assertEqual(status["wb_acks_dropped"], 2)
        self.assertEqual(r["monitors"]["TBWB"]["drops"], 2)
        self.assertEqual((status["wb_acks_stray"], status["arb_stray"]), (0, 0))
        reads = {e.addr: e.data for e in r["events"] if e.kind == "readback" and e.crc_ok}
        chunks = self.scenarios["watchdog"].extra["chunks"]
        for addr in (0x100, 0x1000, 0x2003, 0x3000):
            self.assertEqual(reads[addr], chunks[addr], hex(addr))
        # 0x4000 was never loaded: it reads as the memory model's background.
        self.assertEqual(reads[0x4000], b"".join(proto.bg_word(0x400 + k) for k in range(7))[:100])

    def test_late_ack_of_an_abandoned_read(self):
        r = self.assert_as_predicted("late_ack")
        self.assertEqual(r["monitors"]["TBRACE"]["state"], 2)            # the ack came in that clock
        self.assertEqual(r["monitors"]["TBWB"]["drops"], 1)
        reads = [e for e in r["events"] if e.kind == "readback" and e.crc_ok]
        self.assertEqual([(e.seq, e.data) for e in reads if e.seq in (4, 5)],
                         [(4, self.scenarios["late_ack"].extra["b"]), (5, self.scenarios["late_ack"].extra["b"])])
        self.assertEqual(self.status_of(r["events"], 6)["wb_acks_dropped"], 1)

    def test_pipelined_frames(self):
        r = self.assert_as_predicted("pipelined")
        status = proto.decode_status(r["events"][-37:])
        self.assertGreater(status["fifo_high_water"], 20)
        self.assertEqual(status["frames_duplicate"], 1)

    def test_fifo_overflow_is_a_nak(self):
        s, r = self.scenarios["overflow"], self.completed("overflow")
        events = r["events"]
        self.assertEqual(events[2].data, s.extra["fill"])
        naks = [e.resp() for e in events if e.kind == "line" and e.tag == "N"]
        self.assertEqual(naks[0]["reason_name"], "overflow")
        reads = [e for e in events if e.kind == "readback"]
        self.assertEqual(reads[1].data, s.extra["big"])

    def test_baud_change(self):
        r = self.assert_as_predicted("baud")
        self.assertEqual(self.status_of(r["events"], 6)["baud_reverts"], 1)
        late = self.status_of(r["events"], 9)                           # after the second change
        self.assertEqual((late["baud_reverts"], late["baud_div"]), (1, 16))

    def test_board_divisor(self):
        for name in ("board_rate_+5.0%", "board_rate_-4.5%"):
            r = self.assert_as_predicted(name)
            self.assertEqual(r["events"][-1].data, bytes(range(64)))

    def test_arbitration_with_the_reader(self):
        capture = load_tool("fpga_ddr3_capture_loader", "tools/fpga-ddr3-capture.py")
        r = self.assert_as_predicted("arbitration")
        mon = r["monitors"]["TBWB"]
        self.assertGreater(mon["taken1"], 1000)
        self.assertGreater(mon["owner_changes"], 10)
        self.assertEqual((mon["taken0"], mon["taken1"]), (mon["acks0"], mon["acks1"]))
        self.assertGreater(r["monitors"]["TBKEPT"]["hits"], 100)
        lines = []
        # The line being sent when the bench stopped is cut short; every other line is whole.
        entries = r["reader_lines"]
        if entries and len(entries[-1]["line"].strip()) != 19:
            entries = entries[:-1]
        for entry in entries:
            parsed = capture.parse_line(entry["line"].strip())
            self.assertIsNotNone(parsed, entry)
            parsed["t_s"] = entry["t_s"]
            lines.append(parsed)
        reader = capture.decode_reader(lines, hz=SIM_HZ)
        self.assertFalse(reader["problems"], reader["problems"])
        runs = reader["runs"]
        self.assertGreaterEqual(len(runs), 100)
        self.assertEqual(reader["totals"]["model_checked_runs"], len(runs))
        self.assertEqual(reader["totals"]["passing_runs"], len(runs))
        for run in runs:
            self.assertTrue(run["pass"], run)
            self.assertTrue(run["checks"]["equals_host_model"], run)
            self.assertEqual(run["stray_acks"], 0)
        status = proto.decode_status(r["events"][-37:])
        self.assertGreater(status["arb_switches"], 10)
        self.assertEqual((status["arb_stray"], status["wb_acks_stray"]), (0, 0))

    def test_turnaround_is_recorded(self):
        lat = self.latencies("transfer_+0.05")
        self.assertEqual(len(lat), 18)
        for item in lat:
            self.assertGreaterEqual(item["turnaround_clocks"], 3 * item["payload"] - 16)


class HostToolDdr3(unittest.TestCase):
    """tools/fpga-uart-loader.py against the protocol-3 model on the timed fake line of
    tests/test_uart_loader.py: waiting for the calibration, not_ready naks retransmitted, the
    margins around an unaligned range, the calibration check of the record."""

    DIV = 25
    BAUD = 1_000_000

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "tests"))
        import test_uart_loader as part1
        cls.part1 = part1
        cls.tool = load_tool("fpga_uart_loader_tool_ddr3", "tools/fpga-uart-loader.py")

    def run_main(self, argv_extra, calibrate_after_s, addr=0x1ABCD007, size=3000, watch=None, probation_s=None):
        """One run of the tool against the model; `watch(model)` is called under the device's lock
        every millisecond until it returns True (a reset, a byte changed behind the tool's back)."""
        import contextlib
        import io
        import threading
        model = proto.DeviceModel(store=proto.SparseStore(STORE_LOG2, proto.bg_word), store_log2=STORE_LOG2,
                                  default_div=self.DIV, proto=proto.PROTO_DDR3, calibrated=calibrate_after_s == 0,
                                  calib_clocks=433_000_000)
        model.out.clear()
        extra = {} if probation_s is None else {"probation_s": probation_s}
        device = self.part1.FakeDevice(model, self.tool.DESIGN_HZ, self.tool.DEVICE_TIMEOUT_S, **extra)
        ports = []
        stop = threading.Event()

        def watcher():
            while not stop.is_set():
                with device.lock:
                    if watch(model):
                        return
                stop.wait(0.001)

        def opener(_name, baud):
            if ports:
                device.reopened()
            ports.append(self.part1.FakePort(device, baud))
            return ports[-1]

        def calibrate():
            with device.lock:
                model.calibrated, model.calib_clocks = True, 433_000_000
        timer = threading.Timer(calibrate_after_s, calibrate)
        saved = self.tool.OPENER, self.tool.PROBATION_S
        self.tool.OPENER = opener
        if probation_s is not None:
            self.tool.PROBATION_S = probation_s
        work = tempfile.TemporaryDirectory(prefix="trinity-ddr3-loader-host-")
        try:
            if calibrate_after_s:
                timer.start()
            if watch is not None:
                threading.Thread(target=watcher, daemon=True).start()
            payload = Path(work.name) / "payload.bin"
            data = bytes(random.Random(12).getrandbits(8) for _ in range(size))
            payload.write_bytes(data)
            out = Path(work.name) / "run.json"
            with contextlib.redirect_stdout(io.StringIO()):
                code = self.tool.main(["--port", "fake", "--baud", str(self.BAUD), "--payload", str(payload),
                                       "--chunk", "1000", "--ack-timeout", "0.2", "--guard", "0.12",
                                       "--addr", hex(addr), "--output", str(out), *argv_extra])
            record = json.loads(out.read_text())
        finally:
            stop.set()
            timer.cancel()
            self.tool.OPENER, self.tool.PROBATION_S = saved
            work.cleanup()
        return code, record, model, data

    def test_waits_for_the_calibration_and_keeps_the_margins(self):
        code, record, model, data = self.run_main(["--wait-calib", "5", "--read-before", "--margin", "40"], 0.4)
        self.assertEqual(code, 0, record.get("checks"))
        self.assertGreaterEqual(len(record["calib_polls"]), 2)
        # Every status read before the load is in the record, the first one included.
        first_ack = min(e["t"] for e in record["device_events"] if e["kind"] == "line" and e["tag"] == "A")
        answered = [e for e in record["device_events"]
                    if e["kind"] == "line" and e["tag"] == "C" and int(e["a"], 16) & 0xFFFF == 0 and e["t"] < first_ack]
        self.assertEqual(len(record["calib_polls"]), len(answered))
        self.assertEqual(proto.decode_calib(record["calib_polls"][0]["status"]["calib"])["calib_complete"], 0)
        self.assertTrue(record["checks"]["calibration_held"])
        self.assertTrue(record["checks"]["margins_unchanged"])
        self.assertEqual(record["transfer"]["retransmits"], {})
        self.assertEqual(model.store[0x1ABCD007:0x1ABCD007 + 3000], data)
        # Read before the load: the model's background words, all but a few bytes differ from the payload.
        self.assertGreater(record["transfer"]["store_before"]["bytes_the_load_changes"], 2950)

    def test_a_reset_between_the_status_reads_fails_the_calibration_check(self):
        # UberDDR3 calibrates in the same number of clocks after every reset, so calib_clocks alone
        # cannot show one: the H line and frames_committed (zeroed) do.
        def reset_after_two_chunks(model):
            if model.c["frames_committed"] >= 2:
                model.reset()
                return True
            return False
        code, record, _model, _data = self.run_main(["--read-before"], 0, watch=reset_after_two_chunks)
        self.assertEqual(record["status_before"]["calib_clocks"], record["status_after"]["calib_clocks"])
        self.assertTrue(record["checks"]["all_chunks_acked"])
        self.assertTrue(record["checks"]["read_back_identical"])
        self.assertFalse(record["checks"]["calibration_held"])
        self.assertEqual(code, 1)

    def test_margins_stop_at_the_end_of_the_store(self):
        size = 3000
        code, record, model, data = self.run_main(["--margin", "40"], 0, addr=(1 << STORE_LOG2) - size, size=size)
        self.assertEqual(code, 0, record.get("checks"))
        margins = record["transfer"]["margins"]
        self.assertEqual((margins["below"][1], margins["above"][1]), (40, 0))
        self.assertTrue(record["checks"]["margins_unchanged"])
        self.assertEqual(model.store[(1 << STORE_LOG2) - size:], data)

    def test_a_baud_trial_that_changes_its_margins_fails(self):
        # A byte next to the trial's range changes while the trial loads (after the 3 main chunks).
        def change_a_neighbour(model):
            if model.c["frames_committed"] >= 4:
                spot = 0x1ABCD007 + 1010
                model.store[spot:spot + 1] = bytes([model.store[spot:spot + 1][0] ^ 0xFF])
                return True
            return False
        code, record, _model, _data = self.run_main(
            ["--margin", "40", "--baud-try", "1250000", "--baud-bytes", "1000"], 0, watch=change_a_neighbour,
            probation_s=0.3)
        trial = record["baud_trials"][0]
        self.assertTrue(trial["transfer"]["identical"])
        self.assertFalse(trial["transfer"]["margins"]["unchanged"])
        self.assertFalse(record["checks"]["baud_trials_all_ok"])
        self.assertEqual(code, 1)

    def test_not_ready_naks_are_retransmitted(self):
        # The status before the load is read 0.3 s after the port opens; the device calibrates at 0.8 s.
        code, record, model, data = self.run_main(["--max-attempts", "20"], 0.8)
        # The status before the load was read before the calibration: the record does not pass
        # (calibration_held needs it complete before and after), everything else does.
        self.assertEqual(code, 1, record.get("checks"))
        self.assertEqual({k: v for k, v in record["checks"].items() if not v}, {"calibration_held": False})
        self.assertIn("not_ready", record["transfer"]["retransmits"])
        self.assertEqual(model.store[0x1ABCD007:0x1ABCD007 + 3000], data)


if __name__ == "__main__":
    unittest.main()
