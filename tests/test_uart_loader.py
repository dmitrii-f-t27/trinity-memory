"""UART loader (issue #63): t27/rtl/fpga_uart_{rx,loader}.t27 and fpga_loader_store.t27
before and besides any board run.

- The C that the pinned compiler generates from the loader's functions (CRC-32 byte
  step, response check byte, response word, header rules, read-back header bytes,
  FIFO entry) agrees with tools/uart_loader_protocol.py and zlib on random inputs.
- Icarus runs the board top fpga/ax7203/tms_uart_loader.v (200 MHz differential
  clock, 25 MHz design clock, the t27 block-RAM store) and tb_uart_loader_ports (the
  same t27 cores with a behavioural memory that stalls the write and read ports at
  random, tests/tb_uart_loader.v). A host model drives RX at the bit level with its
  own bit period, a few per cent off the device's, and decodes TX. Every byte the
  device sends is compared with what tools/uart_loader_protocol.DeviceModel predicts
  for the same byte stream (only the status lines' FIFO high-water mark and clock
  counter depend on timing and are not predicted), so these runs check the protocol
  exactly: randomized transfer lengths and addresses with read-back, corrupt,
  dropped and duplicated chunks, invalid commands and lengths, framing errors,
  garbage and glitches on the line (a port that opens), glitches close to the
  receiver's sampling points, a host that stops mid-frame, the reset button while a
  frame arrives, while a commit waits on the write port, while a read-back and while
  status lines are being sent, frames sent back to back without waiting
  (backpressure through the receive FIFO, every ack delivered), a FIFO overflow, a
  baud change with its fallback, the port watchdog (a write or read port that stops
  answering), a memory that answers in the accepting clock, and the board's own
  divisor (217, 115200 baud at 25 MHz) with the host 4.5 % fast and 5 % slow. The
  simulation baud rate is 25 MHz / 16 = 1.5625 Mbaud unless a scenario says
  otherwise, which keeps the runs short; the RTL is the same.
- The turnaround from the end of a load frame to its ack is recorded per frame
  (build/fpga/loader-sim/summary.json).
Runs with T27_ROOT set and Icarus installed (tools/test-t27.sh); skipped otherwise.
"""
from __future__ import annotations

import collections
import concurrent.futures
import contextlib
import ctypes
import io
import json
import os
import random
import re
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import uart_loader_protocol as proto  # noqa: E402

COMPILER = Path(os.environ["T27_ROOT"]) / "target/release/t27c" if os.environ.get("T27_ROOT") else None
HAVE_COMPILER = bool(COMPILER and COMPILER.is_file())
HAVE_TOOLS = bool(HAVE_COMPILER and shutil.which("iverilog") and shutil.which("vvp"))
CORES = ("fpga_tick", "fpga_reset", "fpga_uart_tx", "fpga_uart_rx", "fpga_line_emitter", "fpga_uart_loader",
         "fpga_loader_store")
CLK_PS = 40_000                      # 25 MHz design clock
MASKED_STATUS = {proto.STATUS_NAMES.index("fifo_high_water"), proto.STATUS_NAMES.index("clocks")}
SUMMARY = ROOT / "build/fpga/loader-sim/summary.json"


def generated_c() -> str:
    source = subprocess.run([str(COMPILER), "gen-c", str(ROOT / "t27/rtl/fpga_uart_loader.t27")],
                            capture_output=True, text=True, check=True).stdout
    # The pinned gen-c initializes a module array with `= 0`, which C rejects.
    return re.sub(r"^(static \w+ \w+\[\d+\]) = 0;", r"\1 = {0};", source, flags=re.M)


@unittest.skipUnless(HAVE_COMPILER, "T27_ROOT with a built t27c required")
class LoaderFunctions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix="trinity-uart-loader-c-")
        work = Path(cls.work.name)
        (work / "loader.c").write_text(generated_c(), encoding="ascii")
        library = work / "libloader.so"
        subprocess.run(["cc", "-shared", "-fPIC", "-O1", "-Wno-parentheses-equality", str(work / "loader.c"),
                        "-o", str(library)], check=True, capture_output=True)
        cls.lib = ctypes.CDLL(str(library))
        u32, b = ctypes.c_uint32, ctypes.c_bool
        for name, args, res in (("crc_byte", (u32, u32), u32), ("line_check", (u32, u32, u32), u32),
                                ("resp_word", (u32, b, u32, u32, u32), u32), ("header_ok", (u32, u32, u32, u32), b),
                                ("rb_hdr_byte", (u32, u32, u32, u32), u32), ("fifo_entry", (u32, b, b), u32)):
            function = getattr(cls.lib, name)
            function.argtypes, function.restype = args, res

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def crc(self, data: bytes) -> int:
        c = 0xFFFFFFFF
        for x in data:
            c = self.lib.crc_byte(c, x)
        return c ^ 0xFFFFFFFF

    def test_crc_is_zlib(self):
        rng = random.Random(63)
        for n in (0, 1, 9, 100, 4096):
            data = bytes(rng.getrandbits(8) for _ in range(n))
            self.assertEqual(self.crc(data), zlib.crc32(data))
        self.assertEqual(self.crc(b"123456789"), 0xCBF43926)

    def test_line_check_and_words(self):
        rng = random.Random(64)
        for _ in range(2000):
            tag, a, v = rng.choice(b"HANC"), rng.getrandbits(32), rng.getrandbits(32)
            self.assertEqual(self.lib.line_check(tag, a, v), proto.line_check(tag, a, v))
            seq, reason, length = rng.getrandbits(8), rng.randrange(8), rng.getrandbits(16)
            cmd, known = rng.choice([0x4C, 0x52, 0x53, 0x42, 0x00, 0x41]), bool(rng.getrandbits(1))
            self.assertEqual(self.lib.resp_word(seq, known, reason, cmd, length),
                             proto.resp_word(seq, known, reason, cmd, length))

    def test_header_rules_match_the_model(self):
        rng = random.Random(65)
        store = 1 << 18
        edges = [0, 1, 15, 16, 17, 4095, 4096, 4097, store - 4096, store - 1, store, store + 1, 65535, 65536,
                 0xFFFFFFFF]
        for _ in range(4000):
            cmd = rng.choice([0x4C, 0x52, 0x53, 0x42])
            addr = rng.choice(edges + [rng.getrandbits(32), rng.randrange(store)])
            length = rng.choice([0, 1, 2, 4095, 4096, 4097, rng.getrandbits(16)])
            model = proto.DeviceModel(store=bytearray(store))
            model.cmd, model.addr, model.len, model.seq, model.seq_known = cmd, addr, length, 0, True
            model.out.clear()
            model._check_header()
            want = not (model.out and model.out[-1][0] == "N")
            self.assertEqual(self.lib.header_ok(cmd, addr, length, store), want, (chr(cmd), addr, length))

    def test_readback_header_and_fifo_entry(self):
        rng = random.Random(66)
        for _ in range(500):
            seq, addr, length = rng.getrandbits(8), rng.getrandbits(32), rng.randrange(1, 4097)
            header = proto.readback_frame(seq, addr, bytes(length))[:proto.HEADER_BYTES]
            self.assertEqual(bytes(self.lib.rb_hdr_byte(k, seq, addr, length) for k in range(10)), header)
            d, fe, gap = rng.getrandbits(8), bool(rng.getrandbits(1)), bool(rng.getrandbits(1))
            self.assertEqual(self.lib.fifo_entry(d, fe, gap), d | (256 if fe else 0) | (512 if gap else 0))


class Scenario:
    """A host script for tests/tb_uart_loader.v and the model's prediction of every
    byte the device sends in reply."""

    def __init__(self, name, bench="top", baud_div=16, timeout=3840, probation=48000, skew=0.0,
                 build_id=0x5EED1063, sink_mode=0):
        self.name, self.bench = name, bench
        self.params = {"BAUD_DIV": baud_div, "TIMEOUT_CLOCKS": timeout, "PROBATION_CLOCKS": probation}
        if bench == "ports":
            self.params["SINK_MODE"] = sink_mode
        self.base = 0                  # model.out index where the device's byte count restarts (x command)
        self.cuts: list[int] = []      # model.out indices where a reset cut the device's output
        self.timeout_ps = timeout * CLK_PS
        self.probation_ps = probation * CLK_PS
        self.cmds: list[str] = []
        self.model = proto.DeviceModel(build_id=build_id, default_div=baud_div)
        self.predictable = True
        self.seen = 0
        self.load_markers: dict[int, int] = {}
        self.marker_id = 0
        self.skew = skew
        self.set_rate(baud_div)
        self.wait()          # the H line after reset

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
        """Stay silent long enough for the device to drop a partial frame."""
        self.pause(self.timeout_ps * 3 // 2 + 20 * self.bit_ps)

    def wait(self, pending=None):
        pending = self.expected_bytes() if pending is None else pending
        new, self.seen = pending - self.seen, pending
        # Room for a 4096-byte commit or read-back at up to 20 clocks per byte (the stalling sink),
        # or for the port watchdog (one timeout).
        self.cmds.append(f"t {pending} {new * 12 * self.bit_ps + 4 * self.timeout_ps + 20 * 4096 * CLK_PS}")

    def marker(self) -> int:
        self.marker_id += 1
        self.cmds.append(f"m {self.marker_id}")
        return self.marker_id

    def load(self, seq, addr, payload, wait=True):
        self.send(proto.load_frame(seq, addr, payload))
        self.load_markers[self.marker()] = len(payload)
        if wait:
            self.wait()

    def read(self, seq, addr, length, wait=True):
        self.send(proto.read_frame(seq, addr, length))
        if wait:
            self.wait()

    def status(self, seq, wait=True):
        self.send(proto.status_frame(seq))
        if wait:
            self.wait()

    def glitch(self, bits: float):
        """RX low for `bits` device bit periods (a glitch, a port that opens, a break).

        The receiver (t27/rtl/fpga_uart_rx.t27) samples the start bit (D >> 1) + f
        clocks after the line falls, f in [0, 1) depending on where the fall lands in
        the clock period, and bit k (1..8 data, 9 stop) k * D clocks after that. A
        sample reads low when it lies inside the pulse. Pulses that end within a clock
        of a sampling point are ambiguous and refused, so every glitch here has one
        outcome."""
        d = self.div
        pulse = bits * d                                   # clocks
        points = [(d >> 1) + k * d for k in range(10)]     # earliest time of each sample
        for p in points:
            if p - 0.25 <= pulse <= p + 1.25:
                raise ValueError(f"glitch of {bits} bits ends within a clock of a sampling point")
        self.cmds.append(f"l {round(bits * self.bit_ps)}")
        self.cmds.append(f"w {12 * self.bit_ps}")
        low = [pulse > p for p in points]
        if not low[0]:
            self.model.false_start()
            return
        byte = sum(1 << k for k in range(8) if not low[k + 1])
        self.model.rx(byte, ferr=low[9])

    def reset_button(self, abandon=False):
        """The reset button while nothing is being sent (abandon: the last response the
        model predicted was never sent, e.g. a commit that waited on a hung port)."""
        if abandon:
            self.model.out.pop()
        self.cmds.append("r 2000000")
        self.model.reset()
        self.wait()

    def reset_cutting(self, after_bytes: int):
        """The reset button once `after_bytes` more bytes have arrived from the device
        (in the middle of a read-back or of status lines): the rest of that output is
        never sent. The press lasts 64 bit periods, longer than a character."""
        self.cmds.append(f"t {self.seen + after_bytes} {(after_bytes + 20) * 12 * self.bit_ps + 4 * self.timeout_ps}")
        self.cmds.append(f"x {64 * self.bit_ps}")
        self.cuts.append(len(self.model.out))
        self.base = len(self.model.out)
        self.seen = 0
        self.model.reset()
        self.wait()

    def hang(self, mask: int):
        """Port faults of the stalling sink (tests/tb_uart_loader.v, h command)."""
        self.cmds.append(f"h {mask}")

    def script(self) -> str:
        return "\n".join(self.cmds + ["w 20000000", "e"]) + "\n"


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


def scenario_transfer(rng, skew, bench="top", sink_mode=0):
    """Randomized lengths and addresses, each chunk loaded, acknowledged and read back."""
    label = bench + ("0" if bench == "ports" and sink_mode == 1 else "")
    s = Scenario(f"transfer_{label}_{skew:+.2f}", bench=bench, skew=skew, sink_mode=sink_mode,
                 build_id=0x5EED1064 if bench == "ports" else 0x5EED1063)
    lengths = [1, 2, 3, 17, 255, 256, 1000, 4096] + [rng.randrange(1, 4097) for _ in range(4)]
    rng.shuffle(lengths)
    seq = 0
    for length in lengths:
        addr = rng.randrange(0, (1 << 18) - length + 1)
        payload = bytes(rng.getrandbits(8) for _ in range(length))
        seq = (seq + 1) & 255
        s.load(seq, addr, payload)
        seq = (seq + 1) & 255
        s.read(seq, addr, length)
    s.status((seq + 1) & 255)
    return s


def scenario_faults(rng, skew):
    """Every fault the host tool can inject, and invalid frames, each detected and recovered."""
    s = Scenario(f"faults_{skew:+.2f}", skew=skew)
    payload = bytes(rng.getrandbits(8) for _ in range(300))
    good = proto.load_frame(1, 0x1000, payload)
    # Corrupt byte: one payload bit flipped -> CRC nak, then the retransmission is acknowledged.
    bad = bytearray(good)
    bad[40] ^= 0x10
    s.send(bytes(bad))
    s.wait()
    s.load(1, 0x1000, payload)
    # Duplicate chunk (its ack was lost): acknowledged again as a duplicate, not committed twice.
    s.load(1, 0x1000, payload)
    # Dropped byte: the frame is one byte short, the timeout drops it, then the retransmission.
    payload2 = bytes(rng.getrandbits(8) for _ in range(200))
    frame2 = proto.load_frame(2, 0x2000, payload2)
    s.send(frame2[:100] + frame2[101:])
    s.drop_partial()
    s.wait()
    s.load(2, 0x2000, payload2)
    # The host stops mid-frame (host reset during a transfer), then starts again.
    s.send(proto.load_frame(3, 0x3000, payload)[:57])
    s.drop_partial()
    s.wait()
    s.load(3, 0x3000, payload)
    # Invalid frames: unknown command, zero and too long lengths, outside the store, status with a length.
    s.send(proto.frame(0x51, 4, 0, 0))
    s.wait()
    s.send(proto.frame(proto.CMD_LOAD, 5, 0, 0))
    s.wait()
    s.send(proto.frame(proto.CMD_READ, 6, 0, 4097))
    s.wait()
    s.send(proto.frame(proto.CMD_READ, 7, (1 << 18) - 10, 11))
    s.wait()
    s.send(proto.frame(proto.CMD_STATUS, 8, 0, 3))
    s.wait()
    s.send(proto.frame(proto.CMD_BAUD, 9, 8, 0))
    s.wait()
    # The rest of those frames' bytes (CRC) were hunted through as garbage; let the line settle.
    s.drop_partial()
    # A framing error inside a frame.
    frame3 = proto.load_frame(10, 0x4000, payload[:50])
    s.send(frame3[:20])
    s.send_ferr(frame3[20])
    s.wait()
    s.drop_partial()
    # The reset button during a transfer: the H line again, then the frame again.
    s.send(proto.load_frame(14, 0x8000, payload[:100])[:40], feed=False)
    s.reset_button()
    s.load(14, 0x8000, payload[:100])
    # Garbage, stray magic bytes and a lone A5 before a good frame.
    s.send(bytes([0x00, 0xFF, 0x13, 0xA5, 0x00, 0xA5, 0xA5, 0x37]))
    s.send(bytes([0xA5]))
    s.drop_partial()
    s.send(bytes(rng.getrandbits(8) for _ in range(40)).replace(b"\xa5", b"\x11"))
    s.load(11, 0x5000, payload[:77])
    # Glitches on the line: shorter than half a bit (a false start), three bits (a byte), a break,
    # and pulses that end just before or after a sampling point (at 16 clocks per bit the start
    # bit is sampled 8-9 clocks after the fall, the stop bit 152-153).
    s.glitch(0.2)
    s.glitch(3.0)
    s.glitch(30.0)
    s.glitch(0.45)     # 7.2 clocks: a false start
    s.glitch(0.62)     # 9.9 clocks: a byte, 0xFF
    s.glitch(2.65)     # 42.4 clocks: bits 0 and 1 low, 0xFC
    s.glitch(9.3)      # 148.8 clocks: 0x00 with a good stop bit
    s.glitch(9.7)      # 155.2 clocks: 0x00 with a framing error
    s.load(12, 0x6000, payload[:90])
    # A glitch shorter than half a bit in the middle of a frame is no byte: the frame is intact.
    s.send(proto.load_frame(13, 0x7000, payload[:64])[:30])
    s.glitch(0.2)
    s.send(proto.load_frame(13, 0x7000, payload[:64])[30:])
    s.wait()
    # Reads of what was loaded before and after the reset; the store keeps its contents.
    s.read(15, 0x1000, 300)
    s.read(16, 0x2000, 200)
    s.read(17, 0x8000, 100)
    s.status(18)
    return s


def scenario_pipelined(rng):
    """Frames back to back without waiting for acks: the parser stalls on the busy
    emitter, the receive FIFO holds the bytes, every response comes, in order."""
    s = Scenario("pipelined")
    burst = b""
    lengths = []
    for i in range(6):
        payload = bytes(rng.getrandbits(8) for _ in range(rng.randrange(20, 120)))
        lengths.append(len(payload))
        burst += proto.load_frame(i + 1, 0x100 * i, payload)
    # Read only what was written: the simulated block RAM holds X elsewhere.
    burst += proto.status_frame(7) + proto.read_frame(8, 0x100, min(64, lengths[1]))
    burst += proto.load_frame(1, 0x9000, b"dup-free?") + proto.load_frame(1, 0x9000, b"dup-free?")
    s.send(burst)
    s.wait()
    s.status(9)
    return s


def scenario_overflow(rng):
    """A 2000-byte load sent while the device sends a 4096-byte read-back: the parser
    does not take bytes meanwhile, the FIFO fills and drops the excess, the load gets
    an OVERFLOW nak, and its retransmission is acknowledged."""
    s = Scenario("overflow")
    fill = bytes(rng.getrandbits(8) for _ in range(4096)).replace(b"\xa5", b"\x5b")
    s.load(1, 0, fill)
    s.read(2, 0, 4096, wait=False)
    big = bytes(rng.getrandbits(8) for _ in range(2000)).replace(b"\xa5", b"\x5b")
    s.send(proto.load_frame(3, 0x10000, big), feed=False)
    s.predictable = False
    # The read-back (4110 bytes) ends about 2100 byte times after the load; then the
    # parser takes the 1024 bytes the FIFO kept, and the timeout ends the frame.
    s.cmds.append(f"w {3000 * 10 * s.bit_ps}")
    s.cmds.append("m 900")
    s.send(proto.load_frame(3, 0x10000, big), feed=False)
    s.cmds.append(f"w {3000 * 10 * s.bit_ps}")
    s.send(proto.read_frame(4, 0x10000, 2000), feed=False)
    s.cmds.append(f"w {2200 * 10 * s.bit_ps}")
    s.send(proto.status_frame(5), feed=False)
    s.cmds.append(f"w {600 * 10 * s.bit_ps}")
    s.extra = {"fill": fill, "big": big}
    return s


def scenario_baud(rng):
    """A baud change to 16 clocks per bit, then one the host does not follow: the device
    returns to its default after the probation time."""
    s = Scenario("baud", baud_div=32, timeout=7680, probation=64000)
    s.send(proto.baud_frame(1, 16))
    s.wait()
    s.set_rate(16)
    s.cmds.append(f"w {4 * 32 * CLK_PS}")
    s.status(2)
    payload = bytes(rng.getrandbits(8) for _ in range(500))
    s.load(3, 0x400, payload)
    s.read(4, 0x400, 500)
    s.send(proto.baud_frame(5, 20))
    s.wait()
    # The host stays at 16 and sends nothing; the device waits at 20, then returns to 32.
    s.cmds.append(f"w {s.probation_ps + 4 * s.timeout_ps}")
    s.model.revert_baud()
    s.set_rate(32)
    s.status(6)
    s.read(7, 0x400, 500)
    return s


def scenario_board_rate(skew):
    """The board's divisor: 217 clocks per bit at 25 MHz (115200 baud), board timeout."""
    s = Scenario(f"board_rate_{skew * 100:+.1f}%", baud_div=217, timeout=1_250_000, probation=75_000_000, skew=skew)
    payload = bytes(range(64))
    s.load(1, 0x3FFC0, payload)
    s.read(2, 0x3FFC0, 64)
    return s


def scenario_watchdog(rng):
    """The port watchdog and resets while a commit waits on the port, behind the
    stalling sink: a write port that stops accepting, a write that never finishes
    (wr_idle low) and a read port that stops accepting each end in a PORT nak (the
    read-back is completed with zeros and a CRC that does not match); the reset
    button during a commit stuck on either write fault gives the H line; every chunk
    is then loaded again and reads back identical."""
    s = Scenario("watchdog", bench="ports", build_id=0x5EED1064)
    chunks = {addr: bytes(rng.getrandbits(8) for _ in range(n))
              for addr, n in ((0x100, 300), (0x1000, 200), (0x2000, 150), (0x3000, 100), (0x4000, 100))}
    s.load(1, 0x100, chunks[0x100])
    # Reset while the commit waits for wr_ready (wr_valid high), then while it waits for wr_idle.
    for seq, addr, mask in ((2, 0x3000, 1), (3, 0x4000, 2)):
        s.hang(mask)
        s.send(proto.load_frame(seq, addr, chunks[addr]))
        s.cmds.append(f"w {1500 * CLK_PS}")             # well inside the watchdog's 3840 clocks
        s.reset_button(abandon=True)
        s.hang(0)
        s.load(seq, addr, chunks[addr])
    # The watchdog on each port fault.
    for seq, addr, mask in ((4, 0x1000, 1), (5, 0x2000, 2)):
        previous = s.model.last
        s.hang(mask)
        s.send(proto.load_frame(seq, addr, chunks[addr]))
        s.model.port_failed_commit(previous)
        s.wait()
        s.hang(0)
        s.load(seq, addr, chunks[addr])
    s.hang(4)
    s.send(proto.read_frame(6, 0x100, 300))
    s.model.port_failed_read(0)
    s.wait()
    s.hang(0)
    for seq, addr in enumerate(sorted(chunks), start=7):
        s.read(seq, addr, len(chunks[addr]))
    s.status(12)
    return s


def scenario_resets(rng):
    """The reset button in the middle of a 4096-byte read-back and in the middle of the
    status lines (board top): the output stops, the H line follows, and the store
    still holds what was loaded before."""
    s = Scenario("resets")
    fill = bytes(rng.getrandbits(8) for _ in range(4096))
    s.load(1, 0, fill)
    s.read(2, 0, 4096, wait=False)
    s.reset_cutting(200)
    s.status(3, wait=False)
    s.reset_cutting(57)
    s.read(4, 0, 4096)
    more = bytes(rng.getrandbits(8) for _ in range(300))
    s.load(5, 0x8000, more)
    s.read(6, 0x8000, 300)
    s.status(7)
    s.extra = {"fill": fill}
    return s


def build_benches(work: Path, configs):
    sources = []
    for core in CORES:
        verilog = subprocess.run([str(COMPILER), "gen-verilog", str(ROOT / f"t27/rtl/{core}.t27")],
                                 capture_output=True, text=True, check=True).stdout
        (work / f"{core}.v").write_text(verilog)
        sources.append(str(work / f"{core}.v"))
    out = {}
    for bench, params in configs:
        key = (bench, tuple(sorted(params.items())))
        if key in out:
            continue
        top = "tb_uart_loader" if bench == "top" else "tb_uart_loader_ports"
        flags = [f"-P{top}.{k}={v}" for k, v in params.items()]
        vvp = work / f"{bench}_{'_'.join(str(v) for _k, v in sorted(params.items()))}.vvp"
        subprocess.run(["iverilog", "-g2012", *flags, "-s", top, "-o", str(vvp),
                        str(ROOT / "fpga/ax7203/sim/xilinx_stubs.v"), *sources,
                        str(ROOT / "fpga/ax7203/tms_uart_loader.v"), str(ROOT / "tests/tb_uart_loader.v")],
                       check=True, capture_output=True, text=True)
        out[key] = vvp
    return out


@unittest.skipUnless(HAVE_TOOLS, "T27_ROOT with a built t27c and Icarus required")
class LoaderSimulation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # UART_LOADER_SIM_DIR keeps the scripts, captures and simulator output for inspection.
        keep = os.environ.get("UART_LOADER_SIM_DIR")
        cls.work = None if keep else tempfile.TemporaryDirectory(prefix="trinity-uart-loader-sim-")
        work = Path(keep) if keep else Path(cls.work.name)
        work.mkdir(parents=True, exist_ok=True)
        rng = random.Random(1063)
        # Host bit periods: at 16 clocks per bit the receiver takes a host 4.375 % fast or
        # 5.5 % slow (arithmetic in docs/uart-loader.md); the scenarios run 4 % fast and 5 %
        # slow, and at the board's 217 clocks per bit 4.5 % fast and 5 % slow.
        cls.scenarios = {s.name: s for s in [
            scenario_transfer(rng, 0.05), scenario_transfer(rng, -0.04, bench="ports"),
            scenario_transfer(rng, 0.05, bench="ports", sink_mode=1), scenario_faults(rng, -0.04),
            scenario_pipelined(rng), scenario_overflow(rng), scenario_baud(rng), scenario_watchdog(rng),
            scenario_resets(rng), scenario_board_rate(0.05), scenario_board_rate(-0.045)]}
        vvps = build_benches(work, [(s.bench, s.params) for s in cls.scenarios.values()])

        def run(s):
            script, capture = work / f"{s.name}.script", work / f"{s.name}.capture"
            script.write_text(s.script())
            vvp = vvps[(s.bench, tuple(sorted(s.params.items())))]
            proc = subprocess.run(["vvp", str(vvp), f"+script={script}", f"+capture={capture}"], cwd=work,
                                  capture_output=True, text=True, timeout=1800)
            (work / f"{s.name}.log").write_text(proc.stdout + proc.stderr)
            return proc, capture

        with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
            results = dict(zip(cls.scenarios, pool.map(run, cls.scenarios.values())))
        cls.results = {}
        for name, (proc, capture) in results.items():
            received, markers, gave_up, cuts = parse_capture(capture) if capture.exists() else ([], {}, [], [])
            cls.results[name] = {"proc": proc, "received": received, "markers": markers, "gave_up": gave_up,
                                 "cuts": cuts, "events": decode([r for r in received if not cuts or r[0] > cuts[-1]])}
        cls.write_summary()

    @classmethod
    def tearDownClass(cls):
        if cls.work:
            cls.work.cleanup()

    @classmethod
    def latencies(cls, name):
        """Per load frame: design clocks from the end of its last stop bit to the middle
        of the first stop bit of the reply, and the payload length."""
        s, r = cls.scenarios[name], cls.results[name]
        out = []
        for marker, length in s.load_markers.items():
            t0 = r["markers"].get(marker)
            later = [t for t, _v, _f in r["received"] if t0 is not None and t > t0]
            if later:
                reply_clocks = (later[0] - t0) / CLK_PS
                # The first reply byte is sampled 9.5 device bits after its start bit begins.
                out.append({"payload": length, "end_to_reply_stop_clocks": round(reply_clocks),
                            "turnaround_clocks": round(reply_clocks - 9.5 * s.div)})
        return out

    @classmethod
    def write_summary(cls):
        summary = {"schema": "trinity.uart-loader-sim.v1", "clock_hz": 25_000_000,
                   "note": "Icarus runs of tests/test_uart_loader.py; turnaround = design clocks from the end of "
                           "a load frame's last stop bit to the start bit of its ack (commit of 3 clocks per byte "
                           "included)", "scenarios": {}}
        for name, s in cls.scenarios.items():
            r = cls.results[name]
            summary["scenarios"][name] = {
                "bench": s.bench, "params": s.params, "host_bit_skew": s.skew,
                "bytes_received": len(r["received"]), "events": len(r["events"]),
                "load_turnaround": cls.latencies(name),
            }
            sink = re.search(r"SINK writes=(\d+) stalls=(\d+) lasts=(\d+)", r["proc"].stdout)
            if sink:
                summary["scenarios"][name]["sink"] = dict(zip(("writes", "stalled_write_clocks", "wr_last"),
                                                              map(int, sink.groups())))
        SUMMARY.parent.mkdir(parents=True, exist_ok=True)
        SUMMARY.write_text(json.dumps(summary, indent=1) + "\n")

    def completed(self, name):
        r = self.results[name]
        self.assertEqual(r["proc"].returncode, 0, r["proc"].stdout[-2000:] + r["proc"].stderr[-2000:])
        self.assertIn("TB_PASS", r["proc"].stdout, name)
        return r

    def assert_as_predicted(self, name):
        s, r = self.scenarios[name], self.completed(name)
        self.assertFalse(r["gave_up"], f"{name}: waited in vain for device bytes at {r['gave_up'][:3]}")
        # Output cut off by a reset: what arrived before each cut is the start of what the
        # model predicts up to it (the last character may be cut short); decoded events are
        # compared after the last cut.
        self.assertEqual(len(r["cuts"]), len(s.cuts), name)
        bounds = [0] + s.cuts
        times = [-1] + r["cuts"]
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
                if not (g.tag == "C" and (g.a & 0xFFFF) in MASKED_STATUS):
                    self.assertEqual(g.v, w.v, (name, i, g.as_dict(), w.as_dict()))
            elif g.kind == "readback":
                self.assertEqual(g.crc_ok, w.crc_ok, (name, i))
                self.assertEqual((g.seq, g.addr, g.data), (w.seq, w.addr, w.data), (name, i))
            else:
                # Only where the model predicts it too: the bytes after the A5 of a read-back
                # whose CRC does not match are decoded again (the watchdog's cut read-back).
                self.assertEqual((g.kind, g.raw), (w.kind, w.raw), (name, i, g.as_dict()))
        return r

    def test_randomized_transfers_read_back_identically(self):
        for name in ("transfer_top_+0.05", "transfer_ports_-0.04", "transfer_ports0_+0.05"):
            r = self.assert_as_predicted(name)
            reads = [e for e in r["events"] if e.kind == "readback"]
            self.assertEqual(len(reads), 12)
            self.assertIn(4096, [len(e.data) for e in reads])

    def test_backpressure_on_the_ports(self):
        r = self.completed("transfer_ports_-0.04")
        m = re.search(r"SINK writes=(\d+) stalls=(\d+) lasts=(\d+)", r["proc"].stdout)
        self.assertIsNotNone(m, r["proc"].stdout[-500:])
        writes, stalls, lasts = map(int, m.groups())
        s = self.scenarios["transfer_ports_-0.04"]
        self.assertEqual(writes, s.model.c["bytes_committed"])
        self.assertEqual(lasts, s.model.frames_ok)
        self.assertGreater(stalls, 1000)

    def test_faults_are_detected_and_recovered(self):
        r = self.assert_as_predicted("faults_-0.04")
        naks = [e.resp()["reason_name"] for e in r["events"] if e.kind == "line" and e.tag == "N"]
        for reason in ("crc", "timeout", "command", "length", "framing"):
            self.assertIn(reason, naks)
        acks = [e.resp()["reason_name"] for e in r["events"] if e.kind == "line" and e.tag == "A"]
        self.assertIn("duplicate", acks)
        status = proto.decode_status(r["events"][-len(proto.STATUS_NAMES):])
        self.assertEqual(status["rx_false_starts"], 3)
        self.assertGreaterEqual(status["rx_framing_errors"], 1)
        self.assertEqual([e.tag for e in r["events"]].count("H"), 2)

    def test_pipelined_frames_get_every_response(self):
        r = self.assert_as_predicted("pipelined")
        status = proto.decode_status(r["events"][-len(proto.STATUS_NAMES):])
        self.assertGreater(status["fifo_high_water"], 20)
        self.assertEqual(status["fifo_overflow_bytes"], 0)
        self.assertEqual(status["frames_duplicate"], 1)

    def test_fifo_overflow_is_a_nak_not_silent_loss(self):
        s, r = self.scenarios["overflow"], self.completed("overflow")
        self.assertFalse(r["gave_up"])
        events = r["events"]
        kinds = [(e.kind, e.tag) for e in events]
        self.assertEqual(kinds[:3], [("line", "H"), ("line", "A"), ("readback", "")])
        self.assertEqual(events[2].data, s.extra["fill"])
        naks = [e.resp() for e in events if e.kind == "line" and e.tag == "N"]
        self.assertEqual(naks[0]["reason_name"], "overflow")
        self.assertEqual((naks[0]["seq"], naks[0]["cmd"], naks[0]["len"]), (3, proto.CMD_LOAD, 2000))
        acks = [e.resp() for e in events if e.kind == "line" and e.tag == "A"]
        self.assertEqual([(a["seq"], a["reason_name"]) for a in acks], [(1, "ok"), (3, "ok")])
        reads = [e for e in events if e.kind == "readback"]
        self.assertEqual(reads[1].data, s.extra["big"])
        status = proto.decode_status(events[-len(proto.STATUS_NAMES):])
        self.assertGreater(status["fifo_overflow_bytes"], 0)
        self.assertEqual(status["fifo_high_water"], proto.FIFO_DEPTH)
        self.assertEqual(status["nak_overflow"], 1)
        self.assertEqual(status["frames_committed"], 2)

    def test_baud_change_and_fallback(self):
        r = self.assert_as_predicted("baud")
        status = [proto.decode_status([e for e in r["events"] if e.kind == "line" and e.tag == "C" and e.a >> 24 == q])
                  for q in (2, 6)]
        self.assertEqual((status[0]["baud_div"], status[0]["baud_reverts"]), (16, 0))
        self.assertEqual((status[1]["baud_div"], status[1]["baud_reverts"]), (32, 1))

    def test_port_watchdog_and_resets_during_a_commit(self):
        r = self.assert_as_predicted("watchdog")
        naks = [e.resp() for e in r["events"] if e.kind == "line" and e.tag == "N"]
        self.assertEqual([(n["reason_name"], n["cmd"]) for n in naks],
                         [("port", proto.CMD_LOAD), ("port", proto.CMD_LOAD), ("port", proto.CMD_READ)])
        cut = [e for e in r["events"] if e.kind == "readback" and not e.crc_ok]
        self.assertEqual(len(cut), 1)
        self.assertEqual(cut[0].data, bytes(300))
        self.assertEqual([e.tag for e in r["events"] if e.kind == "line"].count("H"), 3)
        status = proto.decode_status(r["events"][-len(proto.STATUS_NAMES):])
        self.assertEqual((status["nak_port"], status["readbacks"]), (3, 5))

    def test_reset_during_readback_and_status_lines(self):
        s, r = self.scenarios["resets"], self.assert_as_predicted("resets")
        self.assertEqual(len(r["cuts"]), 2)
        reads = [e for e in r["events"] if e.kind == "readback"]
        self.assertEqual(reads[0].data, s.extra["fill"])

    def test_board_divisor_with_clock_mismatch(self):
        for name in ("board_rate_+5.0%", "board_rate_-4.5%"):
            r = self.assert_as_predicted(name)
            self.assertEqual(r["events"][-1].data, bytes(range(64)))

    def test_turnaround_is_recorded(self):
        lat = self.latencies("transfer_top_+0.05")
        self.assertEqual(len(lat), 12)
        for item in lat:
            # Commit takes 3 clocks per byte; the receiver hands over the last byte half a
            # stop bit (8 clocks) before the host's stop bit ends.
            self.assertGreaterEqual(item["turnaround_clocks"], 3 * item["payload"] - 16)
            self.assertLess(item["turnaround_clocks"], 3 * item["payload"] + 100)
        self.assertTrue(statistics.median(x["turnaround_clocks"] for x in lat) > 0)


class ProtocolModel(unittest.TestCase):
    """The shared Python protocol on its own (no tools needed)."""

    def test_stream_decoder_splits_lines_frames_and_garbage(self):
        line = proto.format_line("A", proto.resp_word(5, True, 0, proto.CMD_LOAD, 10), 0x00010000)
        rb = proto.readback_frame(6, 0x20, b"\xa5\x5aabc")
        decoder = proto.StreamDecoder()
        events = []
        stream = b"\x00\xfe" + line + rb + b"zz" + line
        for i in range(0, len(stream), 3):
            events += decoder.feed(stream[i:i + 3])
        events += decoder.finish()
        self.assertEqual([e.kind for e in events], ["garbage", "line", "readback", "garbage", "line"])
        self.assertTrue(events[1].check_ok)
        self.assertEqual(events[2].data, b"\xa5\x5aabc")
        self.assertEqual(events[1].resp()["seq"], 5)

    def test_check_byte_catches_a_flipped_digit(self):
        line = bytearray(proto.format_line("A", 0x05030004, 7))
        line[5] = ord("f") if line[5] != ord("f") else ord("e")
        events = proto.StreamDecoder().feed(bytes(line))
        self.assertFalse(events[0].check_ok)

    def test_a_length_out_of_bounds_is_not_a_frame(self):
        # One bit error in the length field of a read-back (32784 > MAX_LEN) must not make
        # the decoder wait for 32 KiB and swallow the replies behind it.
        frame = bytearray(proto.readback_frame(7, 0x100, bytes(16)))
        frame[9] ^= 0x80
        line = proto.format_line("A", proto.resp_word(9, True, 0, proto.CMD_LOAD, 16), 0x00010000)
        decoder = proto.StreamDecoder()
        events = decoder.feed(bytes(frame) + line * 50)
        self.assertEqual([e.kind for e in events], ["garbage"] + ["line"] * 50)
        self.assertEqual(len(decoder.buf), 0)
        zero = bytearray(proto.readback_frame(7, 0x100, bytes(16)))
        zero[8:10] = b"\0\0"
        self.assertEqual([e.kind for e in proto.StreamDecoder().feed(bytes(zero) + line)], ["garbage", "line"])

    def test_a_readback_cut_by_a_reset_gives_back_the_lines_behind_it(self):
        fill = bytes(random.Random(5).getrandbits(8) for _ in range(4096)).replace(b"\xa5", b"\x11")
        cut = proto.readback_frame(2, 0, fill)[:100]
        hello = proto.format_line("H", 0x1234, proto.config_word(18))
        acks = b"".join(proto.format_line("A", proto.resp_word(k, True, 0, proto.CMD_LOAD, 16), k) for k in range(10))
        again = proto.readback_frame(3, 0, fill)
        events = proto.StreamDecoder().feed(cut + hello + acks + again)
        lines = [e.tag for e in events if e.kind == "line"]
        self.assertEqual(lines, ["H"] + ["A"] * 10)
        reads = [e for e in events if e.kind == "readback"]
        self.assertEqual([(e.seq, e.crc_ok) for e in reads], [(2, False), (3, True)])
        self.assertEqual(reads[1].data, fill)

    def test_model_duplicate_and_timeout(self):
        m = proto.DeviceModel()
        f = proto.load_frame(9, 16, b"hello")
        for x in f + f:
            m.rx(x)
        for x in f[:7]:
            m.rx(x)
        m.timeout()
        tags = [(item[0], proto.decode_resp_word(item[1])["reason_name"]) for item in m.out[1:]]
        self.assertEqual(tags, [("A", "ok"), ("A", "duplicate"), ("N", "timeout")])
        self.assertEqual(bytes(m.store[16:21]), b"hello")



class FakeDevice:
    """The far end of FakePort: tools/uart_loader_protocol.DeviceModel behind a line with
    wire time (10 bits per byte at each side's rate).

    A byte the host writes reaches the model when its last bit would have arrived; a gap
    of more than `timeout_s` inside a frame is the model's timeout; the model's responses
    leave in order, each after the byte that caused it (plus `reply_delays[k]` seconds
    for the k-th response: USB or driver latency, which delays everything behind it) at
    the device's rate. The device's rate is DESIGN_HZ / model.div and changes after a
    'B' ack has left; a rate still on probation returns to the default after
    `probation_s` without a good frame. Bytes sent at a rate more than 2 % away from
    the receiving side's are lost. Bytes that arrive while the host's port is closed,
    and until it has been opened again, are lost too.
    """

    def __init__(self, model, design_hz, timeout_s, probation_s=3.0, reply_delays=None, reopen_s=0.1):
        self.model, self.design_hz, self.timeout_s, self.probation_s = model, design_hz, timeout_s, probation_s
        self.lock = threading.RLock()
        self.inbox = collections.deque()        # (arrival, byte, host baud)
        self.outbox = collections.deque()       # (arrival, byte, device baud)
        self.in_free = self.out_free = 0.0
        self.last_rx = None
        self.emitted = len(model.out)
        self.reply_delays = list(reply_delays or [])
        self.reopen_s = reopen_s
        self.probation_until = None
        self.lost_host_bytes = 0

    def baud(self):
        return self.design_hz / self.model.div

    def now(self):
        return time.monotonic()

    def host_write(self, data, host_baud):
        with self.lock:
            now = self.now()
            self.advance(now)
            t = max(now, self.in_free)
            for x in data:
                t += 10 / host_baud
                self.inbox.append((t, x, host_baud))
            self.in_free = t

    def _emit(self, t, baud):
        for item in self.model.out[self.emitted:]:
            delay = self.reply_delays.pop(0) if self.reply_delays else 0.0
            start = max(t, self.out_free) + delay
            for x in proto.expected_bytes(item):
                start += 10 / baud
                self.outbox.append((start, x, baud))
            self.out_free = start
        self.emitted = len(self.model.out)

    def advance(self, now):
        with self.lock:
            while True:
                candidates = []
                if self.inbox and self.inbox[0][0] <= now:
                    candidates.append((self.inbox[0][0], "byte"))
                if self.last_rx is not None and self.model.state != "hunt" and self.last_rx + self.timeout_s <= now:
                    candidates.append((self.last_rx + self.timeout_s, "timeout"))
                if self.model.probation and self.probation_until is not None and self.probation_until <= now:
                    candidates.append((self.probation_until, "revert"))
                if not candidates:
                    return
                t, kind = min(candidates)
                before = self.baud()
                if kind == "timeout":
                    self.model.timeout()
                    self.last_rx = None
                elif kind == "revert":
                    self.model.revert_baud()
                    self.probation_until = None
                else:
                    _t, x, host_baud = self.inbox.popleft()
                    if abs(host_baud / before - 1) > 0.02:
                        self.lost_host_bytes += 1
                        continue
                    self.model.rx(x)
                    self.last_rx = t
                self._emit(t, before)
                if self.model.probation and self.baud() != before:
                    self.probation_until = self.out_free + self.probation_s

    def deliver(self, now, host_baud, n):
        with self.lock:
            self.advance(now)
            out = bytearray()
            while self.outbox and self.outbox[0][0] <= now and len(out) < n:
                _t, x, baud = self.outbox.popleft()
                if abs(baud / host_baud - 1) <= 0.02:
                    out.append(x)
            return bytes(out)

    def waiting(self, now):
        with self.lock:
            self.advance(now)
            return sum(1 for t, _x, _b in self.outbox if t <= now)

    def reopened(self):
        """The host's port was closed and is being opened again: what the device sent
        meanwhile is lost (an open also clears the input buffer)."""
        time.sleep(self.reopen_s)
        with self.lock:
            now = self.now()
            self.advance(now)
            while self.outbox and self.outbox[0][0] <= now:
                self.outbox.popleft()


class FakePort:
    """A serial port on FakeDevice. write() returns at once (the OS takes every byte),
    flush() waits until the line has sent them, read() waits up to `timeout`."""

    def __init__(self, device, baud, timeout=0.005):
        self.device, self.baudrate, self.timeout = device, baud, timeout
        self.closed = False

    @property
    def in_waiting(self):
        return self.device.waiting(self.device.now())

    def write(self, data):
        self.device.host_write(bytes(data), self.baudrate)
        return len(data)

    def flush(self):
        while True:
            with self.device.lock:
                left = self.device.in_free - self.device.now()
            if left <= 0:
                return
            time.sleep(min(left, 0.01))

    def read(self, n=1):
        end = time.monotonic() + self.timeout
        while True:
            data = self.device.deliver(self.device.now(), self.baudrate, n)
            if data or time.monotonic() >= end:
                return data
            time.sleep(0.001)

    def close(self):
        self.closed = True


class HostTool(unittest.TestCase):
    """tools/fpga-uart-loader.py against the protocol model on a timed fake line:
    retransmission after every injected fault, late replies, duplicate handling,
    read-back, baud trials with lost acks, and the record's statistics."""

    DIV = 25                    # 1 000 000 baud at 25 MHz: short runs, the same logic
    BAUD = 1_000_000

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("fpga_uart_loader_tool", ROOT / "tools/fpga-uart-loader.py")
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)

    def rig(self, model=None, **device_kwargs):
        model = model or proto.DeviceModel(default_div=self.DIV)
        model.out.clear()
        device = FakeDevice(model, self.tool.DESIGN_HZ, self.tool.DEVICE_TIMEOUT_S, **device_kwargs)
        ports = []

        def opener(_name, baud):
            if ports:
                device.reopened()
            ports.append(FakePort(device, baud))
            return ports[-1]
        return model, device, opener

    def loader(self, opener, **kwargs):
        options = dict(ack_timeout=0.2, guard=0.12, max_attempts=4, first_seq=1, garbage_bytes=16, drain=False)
        options.update(kwargs)
        link = self.tool.Link("fake", self.BAUD, time.monotonic(), opener=opener)
        return link, self.tool.Loader(link, argparse_namespace(**options), random.Random(3))

    def test_faults_are_retransmitted_and_the_data_reads_back(self):
        model, device, opener = self.rig()
        link, loader = self.loader(opener)
        try:
            rng = random.Random(7)
            data = bytes(rng.getrandbits(8) for _ in range(5000))
            faults = {"corrupt": {0}, "drop": {1}, "abort": {2}, "garbage": {3}, "duplicate": {4}}
            result = self.tool.transfer(loader, data, 0x100, 1000, faults, read_before=True)
            status = loader.status()
        finally:
            link.close()
        self.assertTrue(result["identical"])
        self.assertEqual((result["chunks"], result["acked"]), (5, 5))
        # The abort's timeout nak is sent while the port is being opened again and is lost,
        # as on the board: the host resends after its own wait.
        self.assertEqual(result["retransmits"], {"crc": 1, "timeout": 1, "no_reply": 1})
        self.assertEqual(result["per_chunk"][4]["duplicate"]["reply"]["reason_name"], "duplicate")
        self.assertEqual(result["store_before"]["bytes_the_load_changes"], sum(1 for x in data if x))
        for record in result["per_chunk"]:
            frame_s = self.tool.wire_s(record["len"] + proto.FRAME_OVERHEAD, self.BAUD)
            self.assertGreaterEqual(record["latency_s"], frame_s)
            self.assertLess(record["latency_s"], frame_s + 0.05)
        self.assertEqual(model.c["frames_duplicate"], 1)
        self.assertEqual(model.frames_ok, 5)
        self.assertEqual(bytes(model.store[0x100:0x100 + 5000]), data)
        self.assertEqual((status["nak_crc"], status["nak_timeout"]), (1, 2))

    def test_a_late_reply_is_not_taken_for_the_retransmission(self):
        # The first ack arrives 0.28 s late: after the host's wait (wire time + 0.2 s) and
        # before its retransmission (0.12 s of quiet later).
        model, device, opener = self.rig(reply_delays=[0.28])
        link, loader = self.loader(opener)
        try:
            data = bytes(range(256)) * 4
            result = self.tool.transfer(loader, data, 0, 1024, {})
        finally:
            link.close()
        chunk = result["per_chunk"][0]
        self.assertTrue(result["identical"])
        self.assertIsNone(chunk["attempts"][0]["reply"])
        self.assertEqual([r["reason_name"] for r in chunk["attempts"][0]["late_replies"]], ["ok"])
        self.assertEqual(chunk["reason"], "duplicate")
        self.assertGreater(chunk["latency_s"], 0)
        self.assertEqual(result["late_replies"], 1)
        self.assertEqual(model.c["frames_duplicate"], 1)

    def run_main(self, device_model, argv_extra, probation_s=0.3, **device_kwargs):
        model, device, opener = self.rig(model=device_model, probation_s=probation_s, **device_kwargs)
        tool = self.tool
        saved = tool.OPENER, tool.PROBATION_S
        tool.OPENER, tool.PROBATION_S = opener, probation_s
        work = tempfile.TemporaryDirectory(prefix="trinity-uart-loader-host-")
        try:
            payload = Path(work.name) / "payload.bin"
            payload.write_bytes(bytes(random.Random(11).getrandbits(8) for _ in range(3000)))
            out = Path(work.name) / "run.json"
            with contextlib.redirect_stdout(io.StringIO()):
                code = tool.main(["--port", "fake", "--baud", str(self.BAUD), "--payload", str(payload), "--chunk",
                                  "1000", "--ack-timeout", "0.2", "--guard", "0.12", "--output", str(out), *argv_extra])
            record = json.loads(out.read_text())
        finally:
            tool.OPENER, tool.PROBATION_S = saved
            work.cleanup()
        return code, record, model

    def test_baud_trials_write_new_bytes_and_come_back(self):
        code, record, model = self.run_main(None, ["--read-before", "--baud-try", "500000,250000",
                                                    "--baud-bytes", "2000"])
        self.assertEqual(code, 0, record.get("checks"))
        self.assertTrue(record["checks"]["baud_trials_all_ok"])
        keys = [t["pattern"]["xor"] for t in record["baud_trials"]]
        self.assertEqual(len(set(keys)), 2)
        for trial in record["baud_trials"]:
            self.assertEqual(trial["transfer"]["store_before"]["bytes_the_load_changes"], 2000)
            self.assertTrue(trial["transfer"]["per_chunk"])
            self.assertEqual(trial["status_after"]["baud_div"], self.DIV)
        self.assertEqual(model.div, self.DIV)

    def test_a_lost_switch_ack_waits_for_the_device_to_return(self):
        # The device switches, but its ack arrives with another seq (as if lost): the host
        # returns to its rate and waits until the device has gone back by itself.
        class LoseFirstBaudAck(proto.DeviceModel):
            lost = False

            def _check_frame(self):
                super()._check_frame()
                if self.cmd == proto.CMD_BAUD and not self.lost and self.out and self.out[-1][0] == "A":
                    type(self).lost = True
                    self.out[-1] = ("A", self.out[-1][1] ^ 0x0F000000, self.out[-1][2])   # a line for no seq sent
        model = LoseFirstBaudAck(default_div=self.DIV)
        code, record, model = self.run_main(model, ["--baud-try", "500000,250000", "--baud-bytes", "2000"])
        first, second = record["baud_trials"]
        self.assertTrue(first["fell_back"])
        self.assertIsNotNone(first["status_after"])
        self.assertEqual(first["status_after"]["baud_reverts"], 1)
        self.assertTrue(self.tool.trial_ok(second), second)
        self.assertEqual(code, 1)               # the first trial did not run: the run does not pass

    def test_a_refused_switch_back_is_retried_at_the_trial_rate(self):
        class RefuseFirstBack(proto.DeviceModel):
            refused = 0

            def _check_frame(self):
                body_ok = proto.crc32(bytes(self.hdr) + bytes(self.payload)) == struct.unpack("<I", bytes(self.crcb))[0]
                if body_ok and self.cmd == proto.CMD_BAUD and self.addr == self.default_div and not type(self).refused:
                    type(self).refused = 1
                    return self._nak(proto.R_CRC)
                return super()._check_frame()
        model = RefuseFirstBack(default_div=self.DIV)
        code, record, model = self.run_main(model, ["--baud-try", "500000", "--baud-bytes", "2000"])
        trial = record["baud_trials"][0]
        self.assertEqual(code, 0, record.get("checks"))
        self.assertTrue(trial["back"]["switched"])
        self.assertNotIn("ack_lost", trial["back"])
        self.assertEqual(len([a for a in trial["back"]["attempts"] if "baud" in a]), 2)
        self.assertEqual(trial["status_after"]["baud_div"], self.DIV)

    def test_a_trial_whose_bytes_never_land_fails_the_run(self):
        class DropsAtOtherRates(proto.DeviceModel):
            def _check_frame(self):
                keep = bytes(self.store)
                super()._check_frame()
                if self.cmd == proto.CMD_LOAD and self.div != self.default_div:
                    self.store[:] = keep
        model = DropsAtOtherRates(default_div=self.DIV)
        code, record, model = self.run_main(model, ["--baud-try", "500000", "--baud-bytes", "2000"])
        self.assertEqual(code, 1)
        self.assertFalse(record["baud_trials"][0]["transfer"]["identical"])
        self.assertFalse(record["checks"]["baud_trials_all_ok"])
        self.assertTrue(record["checks"]["read_back_identical"])


def argparse_namespace(**kwargs):
    import argparse
    return argparse.Namespace(**kwargs)


if __name__ == "__main__":
    unittest.main()
