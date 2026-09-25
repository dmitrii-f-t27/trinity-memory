"""The DDR3 matvec build of issue #64 in Icarus: fpga/ax7203/ddr3/tms_ddr3_loader_ax7203.v read with
`define DDR3_MATVEC (make ... DDR3_APP=matvec). That is the DDR3 loader at protocol 4
(t27/rtl/fpga_ddr3_loader.t27 with `matvec` high: activation frames X, matvec frames M), the device
matvec (t27/rtl/fpga_ddr3_matvec.t27), its feed on the arbiter's master 1
(t27/rtl/fpga_matvec_feed.t27) and the line arbiter that shares the line emitter
(t27/rtl/fpga_line_arbiter.t27), with tests/sim_ddr3_loader_model.v in place of UberDDR3 (our
behavioural Wishbone memory with stalls and late acks, not UberDDR3) and the bit-level host of
tests/tb_uart_loader.v, at the 200 MHz of the PLL stub.

Every byte the device sends is compared with tools/bridge_link_protocol.MatvecDevice on the DDR3
loader's base (37 status lines and not_ready, protocol 4 in the config word), except the values
that depend on timing: the status lines tests/test_ddr3_loader.py leaves out, and Z lines 4-6
(cycles, idle clocks, latency), which are checked against each other instead (cycles = words +
idle clocks). Where the device's lines interleave by timing (a nak, an ack or a read-back sent
while a run's Y and Z lines go out), the loader's events and the matvec's lines are each compared
in order. Bench monitors (tests/tb_ddr3_loader.v): acks reach the master that asked, the kept word
equals the memory, the status line `clocks` is two clocks behind the counter, and TBMV: the line
arbiter never saw both sides go in one clock, and the feed saw no ack while idle.

Scenarios:
- runs: weights loaded with L frames, activations with an X frame, M runs in both formats (5 rows
  of 200 columns, and 3 rows of 81 columns: dense5 padding lanes in the last word), a run
  repeated, and the status (protocol 4 in `config`, no frame counted for X or M).
- refusals: X and M header rules (nak `length`), runs refused after their ack (rows 0 and 1025,
  cols 0, format 2, more than 1024 words per row, a region past the store: status 1, only the Z
  lines), a repeated X answered `duplicate`, then a good run.
- busy: an X and an M sent while a run's lines go out are refused not_ready (their N lines
  interleave with the run's lines); the X after the run is taken.
- not_ready: an M before UberDDR3's calibration is refused not_ready, an X is taken; after it the
  same M runs.
- abort: the bench withholds every ack of a run: the feed's watchdog pulses abort, the run ends
  with status 2 and only its Z lines, the feed drops the late acks once they come, and the next
  run is right.
- arbitration: a load and a read-back sent while a run's lines go out: the load waits for the
  feed's port and is acknowledged, the read-back frame goes out whole (the line arbiter holds the
  matvec's lines while the loader sends it byte by byte), and every line of the run arrives.
- real chunk (with the fixture cache): q_proj rows 0-319 of the stage-1 chunk in both formats,
  the images put into the memory model before the run (+preload; their upload over the UART is
  tests/test_ddr3_loader.py's and the board's), the activations with X frames: 320 of 320
  accumulators equal t27/matvec.t27's.
Runs with T27_ROOT set and Icarus installed (tools/test-t27.sh); skipped otherwise.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
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
sys.path.insert(0, str(ROOT / "tests"))
import bridge_link_protocol as link  # noqa: E402
import uart_loader_protocol as proto  # noqa: E402
import test_ddr3_loader as base  # noqa: E402

COMPILER = base.COMPILER
HAVE_TOOLS = base.HAVE_TOOLS
CORES = base.CORES + ("fpga_ddr3_matvec", "fpga_matvec_feed", "fpga_line_arbiter")
CLK_PS = base.CLK_PS
STORE_LOG2 = base.STORE_LOG2
NAMES = base.NAMES
Z_TIMING = {4, 5, 6}                  # cycles, idle clocks, latency
REQUIRE_CACHED = os.environ.get("TRINITY_REQUIRE_CACHED") == "1"
SUMMARY = ROOT / "build/fpga/ddr3-matvec-top-sim/summary.json"
LOADER_TAGS = {"H", "A", "N", "C"}


class MvScenario(base.Scenario):
    """base.Scenario with the protocol-4 model and the matvec frames."""

    def __init__(self, name, feed_watchdog=32768, **kw):
        super().__init__(name, **kw)
        self.params["FEED_WATCHDOG"] = feed_watchdog
        old = self.model
        self.model = link.MatvecDevice(store=proto.SparseStore(STORE_LOG2, proto.bg_word), store_log2=STORE_LOG2,
                                       build_id=old.build_id, default_div=old.default_div, proto=proto.PROTO_DDR3,
                                       calibrated=old.calibrated)
        self.interleaved = False
        self.preload: dict[int, bytes] = {}          # burst -> 16 bytes, put into the memory model before the run
        self.masked_status.add(NAMES.index("arb_switches"))

    def act(self, seq, block, data, wait=True):
        self.send(link.act_frame(seq, block, data))
        if wait:
            self.wait()

    def matvec(self, seq, addr, rows, cols, fmt, wait=True):
        self.send(link.matvec_frame(seq, addr, rows, cols, fmt))
        if wait:
            self.wait()

    def load_image(self, seq, addr, image):
        for off in range(0, len(image), 4096):
            self.load(seq, addr + off, image[off:off + 4096])
            seq = (seq + 1) & 255
        return seq

    def put(self, addr, image):
        """The image in the memory model before the first clock (and in the protocol model's store)."""
        assert addr % 16 == 0 and len(image) % 16 == 0
        for off in range(0, len(image), 16):
            self.preload[(addr + off) >> 4] = image[off:off + 16]
        self.model.store[addr:addr + len(image)] = image


def build_benches(work: Path, scenarios):
    sources = []
    for core in CORES:
        verilog = subprocess.run([str(COMPILER), "gen-verilog", str(ROOT / f"t27/rtl/{core}.t27")],
                                 capture_output=True, text=True, check=True).stdout
        (work / f"{core}.v").write_text(verilog)
        sources.append(str(work / f"{core}.v"))
    common = [str(ROOT / "fpga/ax7203/sim/xilinx_stubs.v"), *sources,
              str(ROOT / "fpga/ax7203/ddr3/tms_ddr3_loader_ax7203.v"),
              str(ROOT / "tests/sim_ddr3_loader_model.v"), str(ROOT / "tests/tb_uart_loader.v"),
              str(ROOT / "tests/tb_ddr3_loader.v")]
    out = {}
    for s in scenarios:
        key = tuple(sorted(s.params.items()))
        if key in out:
            continue
        flags = [f"-Ptb_ddr3_loader.{k}={v}" for k, v in s.params.items()]
        vvp = work / f"matvec_{'_'.join(str(v) for _k, v in key)}.vvp"
        subprocess.run(["iverilog", "-g2012", "-DDDR3_MATVEC", *flags, "-s", "tb_ddr3_loader", "-o", str(vvp), *common],
                       check=True, capture_output=True, text=True)
        out[key] = vvp
    return out


def decode(received):
    decoder = link.LinkDecoder()
    events = []
    for t, value, _ferr in received:
        events += decoder.feed(bytes([value]), t)
    return events + decoder.finish(received[-1][0] if received else None)


def rand_trits(rng, n, zero=0.3):
    return [0 if rng.random() < zero else rng.choice((-1, 1)) for _ in range(n)]


def rand_x(rng, n):
    return [rng.randint(-128, 127) for _ in range(n)]


def exact_y(trits, rows, cols, x):
    return [sum(trits[r * cols + j] * x[j] for j in range(cols)) for r in range(rows)]


# ---- scenarios ----

def scenario_runs(rng):
    s = MvScenario("runs")
    runs = []
    seq = 1
    for fmt, rows, cols, addr in ((0, 5, 200, 0x0200_0000), (1, 5, 200, 0x0300_1000), (1, 3, 81, 0x0400_2000)):
        trits = rand_trits(rng, rows * cols)
        x = rand_x(rng, cols)
        image = link.image(trits, rows, cols, fmt)
        seq = s.load_image(seq, addr, image)
        s.act(seq, 0, link.act_image(x, cols, fmt))
        seq += 1
        s.matvec(seq, addr, rows, cols, fmt)
        runs.append({"seq": seq, "rows": rows, "cols": cols, "fmt": fmt, "y": exact_y(trits, rows, cols, x)})
        seq += 1
    # The last run again, as it is: the same activations and weights, the same accumulators.
    s.matvec(seq, 0x0400_2000, 3, 81, 1)
    runs.append(dict(runs[-1], seq=seq))
    seq += 1
    s.status(seq)
    s.extra = {"runs": runs, "status_seq": seq}
    return s


def scenario_refusals(rng):
    s = MvScenario("refusals")
    store = 1 << STORE_LOG2
    # Header rules: nak `length` after the header (sent without a payload: the device naks before
    # it, and the CRC bytes that follow are ignored as garbage).
    for seq, block, n in ((1, 0, 7), (2, 0, 4088), (3, 1023, 160), (4, 1024, 8)):
        s.send(proto.frame(link.CMD_ACT, seq, block, n))
        s.wait()
    for seq, addr, n in ((5, 8, 12), (6, store, 12), (7, 0, 11)):
        s.send(proto.frame(link.CMD_MATVEC, seq, addr, n))
        s.wait()
    # Refused after the ack: status 1, only the Z lines.
    for seq, rows, cols, fmt, addr in ((8, 0, 64, 0, 0), (9, 1025, 64, 0, 0), (10, 1, 0, 0, 0), (11, 1, 64, 2, 0),
                                       (12, 1, 81_921, 1, 0), (13, 2, 64, 0, store - 16)):
        s.matvec(seq, addr, rows, cols, fmt)
    # A repeated X (its ack lost) is answered `duplicate` and not written again.
    x = rand_x(rng, 64)
    s.act(14, 0, link.act_image(x, 64, 0))
    s.act(14, 0, link.act_image(x, 64, 0))
    trits = rand_trits(rng, 64)
    s.put(0x0500_0000, link.image(trits, 1, 64, 0))
    s.matvec(15, 0x0500_0000, 1, 64, 0)
    s.status(16)
    s.extra = {"y": exact_y(trits, 1, 64, x)}
    return s


def scenario_busy(rng):
    s = MvScenario("busy")
    rows, cols = 40, 200
    trits = rand_trits(rng, rows * cols)
    x = rand_x(rng, cols)
    s.put(0x0600_0000, link.image(trits, rows, cols, 0))
    s.act(1, 0, link.act_image(x, cols, 0))
    s.matvec(2, 0x0600_0000, rows, cols, 0, wait=False)
    # While the run's lines go out: an X and an M, refused not_ready after their CRC.
    s.cmds.append(f"w {40 * 10 * s.bit_ps}")
    s.model.busy = True
    s.act(3, 0, link.act_image([1] * cols, cols, 0), wait=False)
    s.matvec(4, 0x0600_0000, rows, cols, 0, wait=False)
    s.model.busy = False
    s.wait()
    # After the run: the same X and M are taken (the first X's activations are still in the banks
    # until this one writes them, so the run below uses x2).
    x2 = rand_x(rng, cols)
    s.act(5, 0, link.act_image(x2, cols, 0))
    s.matvec(6, 0x0600_0000, rows, cols, 0)
    s.status(7)
    s.interleaved = True
    s.extra = {"y1": exact_y(trits, rows, cols, x), "y2": exact_y(trits, rows, cols, x2)}
    return s


def scenario_not_ready(rng):
    calib = 400_000
    s = MvScenario("not_ready", plusargs={"calib_clocks": calib}, calibrated=False)
    rows, cols = 2, 100
    trits = rand_trits(rng, rows * cols)
    x = rand_x(rng, cols)
    s.put(0x0700_0000, link.image(trits, rows, cols, 1))
    s.matvec(1, 0x0700_0000, rows, cols, 1)             # before the calibration: not_ready
    s.act(2, 0, link.act_image(x, cols, 1))              # the banks need no calibration
    s.status(3)
    s.masked_status.add(NAMES.index("calib"))
    s.cmds.append(f"w {calib * CLK_PS}")
    s.model.calibrated = True
    s.matvec(4, 0x0700_0000, rows, cols, 1)
    s.extra = {"y": exact_y(trits, rows, cols, x)}
    return s


def scenario_abort(rng):
    s = MvScenario("abort", feed_watchdog=2000)
    rows, cols = 8, 160
    trits = rand_trits(rng, rows * cols)
    x = rand_x(rng, cols)
    s.put(0x0800_0000, link.image(trits, rows, cols, 1))
    s.act(1, 0, link.act_image(x, cols, 1))
    s.hang(2)                                            # every ack withheld: the requests are taken, never answered
    s.model.run_faults.append({"status": link.Z_SHORT})
    s.matvec(2, 0x0800_0000, rows, cols, 1)
    s.hang(0)                                            # the late acks come now; the feed drops them
    s.cmds.append(f"w {2000 * CLK_PS}")
    s.matvec(3, 0x0800_0000, rows, cols, 1)
    s.status(4)
    s.extra = {"y": exact_y(trits, rows, cols, x)}
    return s


def scenario_abort_long(rng):
    """An abort with the feed's cap nearly full: 80 words asked for while every ack is withheld, so the
    memory model's queue fills and the feed's next request is presented and stalled when its
    watchdog fires. The request stays presented until it is taken (the Wishbone rule: the model
    stops the run if a stalled request changes or goes away), and its ack is dropped with the rest."""
    s = MvScenario("abort_long", feed_watchdog=2000)
    rows, cols = 40, 160
    trits = rand_trits(rng, rows * cols)
    x = rand_x(rng, cols)
    s.put(0x0800_0000, link.image(trits, rows, cols, 1))
    s.act(1, 0, link.act_image(x, cols, 1))
    s.hang(2)
    s.model.run_faults.append({"status": link.Z_SHORT})
    s.matvec(2, 0x0800_0000, rows, cols, 1)
    s.hang(0)
    s.cmds.append(f"w {2000 * CLK_PS}")
    s.matvec(3, 0x0800_0000, rows, cols, 1)
    s.status(4)
    s.extra = {"y": exact_y(trits, rows, cols, x), "words": rows * link.words_per_row(cols, 1)}
    return s


def scenario_drain_busy(rng):
    """An M and an X while the feed still waits for the late acks of an aborted run (the matvec is
    idle again, the feed is not): both refused not_ready, then the next run exact."""
    s = MvScenario("drain_busy", feed_watchdog=2000)
    rows, cols = 8, 160
    trits = rand_trits(rng, rows * cols)
    x = rand_x(rng, cols)
    s.put(0x0800_0000, link.image(trits, rows, cols, 1))
    s.act(1, 0, link.act_image(x, cols, 1))
    s.hang(2)
    s.model.run_faults.append({"status": link.Z_SHORT})
    s.matvec(2, 0x0800_0000, rows, cols, 1)
    s.model.busy = True
    s.matvec(3, 0x0800_0000, rows, cols, 1)
    s.act(4, 0, link.act_image(x, cols, 1))
    s.model.busy = False
    s.hang(0)
    s.cmds.append(f"w {2000 * CLK_PS}")
    s.matvec(5, 0x0800_0000, rows, cols, 1)
    s.extra = {"y": exact_y(trits, rows, cols, x)}
    return s


def scenario_act_blocks(rng):
    """Activations in X frames at nonzero blocks and of lengths that are not whole blocks (the host's
    path for more than 51 blocks), overwriting parts of blocks, then runs in both formats compared
    with sums over the activations as the frames left them."""
    s = MvScenario("act_blocks")
    seq, runs = 1, []
    for fmt, rows, cols, addr in ((1, 4, 400, 0x0200_0000), (0, 3, 320, 0x0300_1000)):
        trits = rand_trits(rng, rows * cols)
        s.put(addr, link.image(trits, rows, cols, fmt))
        img = bytearray(link.act_image(rand_x(rng, cols), cols, fmt))       # 5 blocks of 80 bytes
        s.act(seq, 0, bytes(img[0:88]))                                    # block 0 and 8 bytes of block 1
        s.act(seq + 1, 1, bytes(img[80:400]))                              # blocks 1-4
        new2 = bytes(rng.getrandbits(8) for _ in range(72))
        img[160:232] = new2
        s.act(seq + 2, 2, new2)                                            # part of block 2
        new4 = bytes(rng.getrandbits(8) for _ in range(40))
        img[320:360] = new4
        s.act(seq + 3, 4, new4)                                            # part of block 4
        n = link.lanes(fmt)
        xs = [(b - 256 if b >= 128 else b) for k in range(5) for b in img[k * 80:k * 80 + n]][:cols]
        s.matvec(seq + 4, addr, rows, cols, fmt)
        runs.append((seq + 4, exact_y(trits, rows, cols, xs)))
        seq += 5
    s.extra = {"runs": runs}
    return s


def scenario_baud_busy(rng):
    """A B frame while a run's lines go out is refused not_ready (the lines share the transmitter);
    the run's lines all arrive at the old rate. After the run a B is taken and holds."""
    s = MvScenario("baud_busy")
    rows, cols = 40, 200
    trits = rand_trits(rng, rows * cols)
    x = rand_x(rng, cols)
    s.put(0x0600_0000, link.image(trits, rows, cols, 0))
    s.act(1, 0, link.act_image(x, cols, 0))
    s.matvec(2, 0x0600_0000, rows, cols, 0, wait=False)
    s.cmds.append(f"w {40 * 10 * s.bit_ps}")
    s.model.busy = True
    s.send(proto.baud_frame(3, 20))
    s.model.busy = False
    s.wait()
    s.send(proto.baud_frame(4, 20))
    s.wait()
    s.set_rate(20)
    s.cmds.append(f"w {4 * 20 * CLK_PS}")
    s.status(5)
    s.interleaved = True
    s.extra = {"y": exact_y(trits, rows, cols, x)}
    return s


def scenario_arbitration(rng):
    # A slow memory (90 % of the clocks stalled) makes the run's 1,000 words take longer than the
    # load frame sent right after the M, so the load's commit asks for the port while the feed
    # holds it (the loader's port watchdog is long here, as on the board: 50 ms there).
    s = MvScenario("arbitration", timeout=200_000, plusargs={"stall_pct": 90})
    rows, cols = 50, 1280
    trits = rand_trits(rng, rows * cols)
    x = rand_x(rng, cols)
    s.put(0x0900_0000, link.image(trits, rows, cols, 0))
    s.act(1, 0, link.act_image(x, cols, 0))
    s.matvec(2, 0x0900_0000, rows, cols, 0, wait=False)
    chunk = base.rand_bytes(rng, 16)
    # (bursts 0xA00400.., model slots from 1024: clear of the image's slots 0-999)
    s.load(3, 0x0A00_4003, chunk, wait=False)            # its commit waits for the feed's port
    s.read(4, 0x0A00_4003, 16, wait=False)               # a read-back frame among the run's lines
    s.wait()
    s.status(5)
    s.interleaved = True
    s.extra = {"y": exact_y(trits, rows, cols, x), "chunk": chunk}
    return s


def scenario_real_chunk(chunk):
    import stage1_chunk
    s = MvScenario("real_chunk")
    rows, cols = stage1_chunk.ROWS, stage1_chunk.COLS
    # The model answers with t27/matvec.t27's accumulators (the chunk's reference), set before the
    # frames are fed to it, so the device's Y lines are compared with those and not the model's sums.
    s.model.compute = lambda trits, r, c, x: list(chunk["y"])
    seq = 1
    for fmt, addr in ((1, 0x0100_0000), (0, 0x0B04_0000)):     # model slots 0.. and 16384.. (no alias)
        s.put(addr, link.image(chunk["trits"], rows, cols, fmt))
        s.act(seq, 0, link.act_image(chunk["x"], cols, fmt))
        s.matvec(seq + 1, addr, rows, cols, fmt)
        seq += 2
    s.status(seq)
    return s


@unittest.skipUnless(base.HAVE_COMPILER, "T27_ROOT with a built t27c required")
class Functions(unittest.TestCase):
    """The loader's protocol-4 functions in the C the pinned compiler generates, against the model."""

    @classmethod
    def setUpClass(cls):
        import ctypes
        cls.work = tempfile.TemporaryDirectory(prefix="trinity-ddr3-matvec-c-")
        cls.ld = base.build_library(ROOT / "t27/rtl/fpga_ddr3_loader.t27", Path(cls.work.name))
        u32, b = ctypes.c_uint32, ctypes.c_bool
        for name, args, res in (("header_ok_mv", (u32, u32, u32, u32, u32), b), ("act_room80", (u32,), u32),
                                ("mv_index", (u32, b), u32), ("resp_word", (u32, b, u32, u32, u32), u32)):
            function = getattr(cls.ld, name)
            function.argtypes, function.restype = args, res

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def test_header_rules_match_the_model(self):
        rng = random.Random(6401)
        store = 1 << STORE_LOG2
        for _ in range(4000):
            cmd = rng.choice([link.CMD_ACT, link.CMD_MATVEC])
            if cmd == link.CMD_ACT:
                addr = rng.choice([0, 1, 972, 973, 1022, 1023, 1024, 1025, rng.randrange(1024), rng.getrandbits(32)])
                length = rng.choice([0, 7, 8, 80, 88, 4072, 4080, 4088, 4096, 8 * rng.randrange(1, 511), rng.getrandbits(16)])
            else:
                addr = rng.choice([0, 8, 16, store - 16, store - 8, store, store + 16, 16 * rng.randrange(store // 16),
                                   rng.getrandbits(32)])
                length = rng.choice([0, 11, 12, 13, rng.getrandbits(16)])
            model = link.MatvecDevice(store=proto.SparseStore(STORE_LOG2), store_log2=STORE_LOG2, proto=proto.PROTO_DDR3)
            model.cmd, model.addr, model.len, model.seq, model.seq_known = cmd, addr, length, 0, True
            model.out.clear()
            model._check_header()
            # The loader registers act_room80(h_addr) a clock ahead of the rule (act_room_q); the rule with it
            # is the model's.
            room80 = self.ld.act_room80(addr)
            self.assertEqual(self.ld.header_ok_mv(cmd, addr, length, store, room80),
                             not (model.out and model.out[-1][0] == "N"), (chr(cmd), addr, length))
            if cmd == link.CMD_ACT and addr < 1024:
                self.assertEqual(room80, 80 * (1024 - addr), addr)

    def test_command_field_of_x_and_m(self):
        rng = random.Random(6402)
        for _ in range(2000):
            seq, reason, length = rng.getrandbits(8), rng.randrange(10), rng.getrandbits(16)
            cmd, known = rng.choice([0x4C, 0x52, 0x53, 0x42, 0x00, link.CMD_ACT, link.CMD_MATVEC]), bool(rng.getrandbits(1))
            with_mv = self.ld.resp_word(seq, known, reason, cmd, length) | (self.ld.mv_index(cmd, True) << 17)
            self.assertEqual(with_mv, link.resp_word(seq, known, reason, cmd, length))
            without = self.ld.resp_word(seq, known, reason, cmd, length) | (self.ld.mv_index(cmd, False) << 17)
            self.assertEqual(without, proto.resp_word(seq, known, reason, cmd, length))


class ProtocolFour(unittest.TestCase):
    """tools/bridge_link_protocol.MatvecDevice on the DDR3 loader's base, as the device answers."""

    def model(self, **kw):
        return link.MatvecDevice(store=proto.SparseStore(20), store_log2=20, build_id=0x5EED1063, default_div=16,
                                 proto=proto.PROTO_DDR3, **kw)

    def lines(self, m, data):
        m.out.clear()
        for x in data:
            m.rx(x)
        return decode([(0, x, False) for item in m.out for x in proto.expected_bytes(item)])

    def test_status_is_the_ddr3_loaders_under_protocol_4(self):
        m = self.model()
        status = proto.decode_status(self.lines(m, proto.status_frame(1)))
        self.assertEqual(len([n for n in status]), 37)
        self.assertEqual(status["config"] >> 24, 4)

    def test_m_waits_for_the_calibration_and_a_run(self):
        m = self.model(calibrated=False)
        (n,) = self.lines(m, link.matvec_frame(1, 0, 1, 64, 0))
        self.assertEqual((n.tag, proto.REASONS[(n.a >> 20) & 15], (n.a >> 17) & 7), ("N", "not_ready", 6))
        (a,) = self.lines(m, link.act_frame(2, 0, bytes(80)))           # the banks need no calibration
        self.assertEqual((a.tag, (a.a >> 17) & 7), ("A", 5))
        m.calibrated, m.busy = True, True
        for frame in (link.act_frame(3, 0, bytes(80)), link.matvec_frame(4, 0, 1, 64, 0)):
            (n,) = self.lines(m, frame)
            self.assertEqual((n.tag, proto.REASONS[(n.a >> 20) & 15]), ("N", "not_ready"))
        m.busy = False
        events = self.lines(m, link.matvec_frame(5, 0, 1, 64, 0))
        self.assertEqual([e.tag for e in events], ["A", "Y"] + ["Z"] * link.Z_COUNT)
        self.assertEqual(m.c["nak_not_ready"], 3)


@unittest.skipUnless(HAVE_TOOLS, "T27_ROOT with a built t27c and Icarus required")
class MatvecTop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        keep = os.environ.get("DDR3_MATVEC_SIM_DIR")
        cls.work = None if keep else tempfile.TemporaryDirectory(prefix="trinity-ddr3-matvec-top-")
        work = Path(keep) if keep else Path(cls.work.name)
        work.mkdir(parents=True, exist_ok=True)
        rng = random.Random(6464)
        scenarios = [scenario_runs(rng), scenario_refusals(rng), scenario_busy(rng), scenario_not_ready(rng),
                     scenario_abort(rng), scenario_arbitration(rng), scenario_abort_long(rng), scenario_drain_busy(rng),
                     scenario_act_blocks(rng), scenario_baud_busy(rng)]
        cls.chunk, cls.chunk_error = None, None
        try:
            import stage1_chunk
            cls.chunk = stage1_chunk.chunk()
            scenarios.insert(0, scenario_real_chunk(cls.chunk))      # the longest run starts first
        except Exception as error:  # noqa: BLE001 - fixtures.CacheMiss or a missing cache directory
            cls.chunk_error = error
        cls.scenarios = {s.name: s for s in scenarios}
        vvps = build_benches(work, cls.scenarios.values())

        def run(s):
            script, capture = work / f"{s.name}.script", work / f"{s.name}.capture"
            script.write_text(s.script())
            args = ["vvp", str(vvps[tuple(sorted(s.params.items()))]), f"+script={script}", f"+capture={capture}",
                    *[f"+{k}={v}" for k, v in s.plusargs.items()]]
            if s.preload:
                pre = work / f"{s.name}.preload"
                pre.write_text("".join(f"{a:x} {int.from_bytes(w, 'little'):032x}\n" for a, w in sorted(s.preload.items())))
                args.append(f"+preload={pre}")
            proc = subprocess.run(args, cwd=work, capture_output=True, text=True, timeout=7200)
            (work / f"{s.name}.log").write_text(proc.stdout + proc.stderr)
            return proc, capture

        with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
            results = dict(zip(cls.scenarios, pool.map(run, cls.scenarios.values())))
        cls.results = {}
        for name, (proc, capture) in results.items():
            received, markers, gave_up, cuts = base.parse_capture(capture) if capture.exists() else ([], {}, [], [])
            monitors = {}
            for tag in ("TBWB", "TBKEPT", "MODEL", "TBPORT", "TBCLK", "TBMV"):
                m = re.search(rf"^{tag} (.*)$", proc.stdout, re.M)
                if m:
                    monitors[tag] = {k: int(v) for k, v in (kv.split("=") for kv in m.group(1).split())}
            cls.results[name] = {"proc": proc, "received": received, "gave_up": gave_up, "cuts": cuts,
                                 "monitors": monitors, "events": decode(received)}
        cls.write_summary()

    @classmethod
    def write_summary(cls):
        import json
        summary = {"schema": "trinity.ddr3-matvec-top-sim.v1", "clock_hz": base.SIM_HZ,
                   "note": "Icarus runs of tests/test_ddr3_matvec_top.py: the DDR3 loader top with `define DDR3_MATVEC "
                           "(loader at protocol 4, device matvec, its feed on master 1, line arbiter) and the "
                           "behavioural Wishbone memory tests/sim_ddr3_loader_model.v, not UberDDR3; runs: the Y and Z "
                           "lines of every M the device answered, by seq",
                   "scenarios": {}}
        for name, s in cls.scenarios.items():
            r = cls.results[name]
            runs = {}
            for e in r["events"]:
                if e.kind == "line" and e.tag in "YZ":
                    run = runs.setdefault(e.a >> 24, {"y": [], "z": {}})
                    if e.tag == "Y":
                        run["y"].append(link.signed32(e.v))
                    else:
                        run["z"][link.Z_NAMES[e.a & 0xFFFF]] = e.v
            summary["scenarios"][name] = {
                "params": s.params, "plusargs": s.plusargs, "preloaded_words": len(s.preload),
                "bytes_received": len(r["received"]), "events": len(r["events"]), "monitors": r["monitors"],
                "runs": {str(seq): {"rows": len(run["y"]), "z": run["z"],
                                    "y_sha256_le": hashlib.sha256(struct.pack(f"<{len(run['y'])}q", *run["y"])).hexdigest(),
                                    "y_first8": run["y"][:8]} for seq, run in sorted(runs.items())}}
        SUMMARY.parent.mkdir(parents=True, exist_ok=True)
        SUMMARY.write_text(json.dumps(summary, indent=1) + "\n")

    @classmethod
    def tearDownClass(cls):
        if cls.work:
            cls.work.cleanup()

    def completed(self, name):
        r = self.results[name]
        self.assertEqual(r["proc"].returncode, 0, r["proc"].stdout[-3000:] + r["proc"].stderr[-2000:])
        self.assertIn("TB_PASS", r["proc"].stdout, r["proc"].stdout[-2000:])
        self.assertFalse(r["gave_up"], f"{name}: waited in vain for device bytes at {r['gave_up'][:3]}")
        mon = r["monitors"]
        self.assertEqual(mon["TBWB"]["misrouted"], 0, mon)
        self.assertEqual(mon["TBKEPT"]["mismatches"], 0, mon)
        self.assertEqual(mon["TBMV"]["collisions"], 0, mon)
        self.assertEqual(mon["TBMV"]["feed_stray"], 0, mon)
        if mon["TBCLK"]["lines"]:
            self.assertEqual((mon["TBCLK"]["age_min"], mon["TBCLK"]["age_max"]), (2, 2), mon)
        return r

    def same_event(self, name, i, g, w, s):
        self.assertEqual(g.kind, w.kind, (name, i, g.as_dict(), w.as_dict()))
        if g.kind == "line":
            self.assertTrue(g.check_ok, (name, i, g.as_dict()))
            self.assertEqual((g.tag, g.a), (w.tag, w.a), (name, i, g.as_dict(), w.as_dict()))
            masked = (g.tag == "C" and (g.a & 0xFFFF) in s.masked_status) or (g.tag == "Z" and (g.a & 0xFFFF) in Z_TIMING)
            if not masked:
                self.assertEqual(g.v, w.v, (name, i, g.as_dict(), w.as_dict()))
        elif g.kind == "readback":
            self.assertEqual((g.seq, g.addr, g.crc_ok, g.data), (w.seq, w.addr, w.crc_ok, w.data), (name, i))
        else:
            self.assertEqual((g.kind, g.raw), (w.kind, w.raw), (name, i, g.as_dict()))

    def assert_as_predicted(self, name):
        s, r = self.scenarios[name], self.completed(name)
        want = decode([(0, x, False) for item in s.model.out for x in proto.expected_bytes(item)])
        got = r["events"]
        self.assertEqual(len(got), len(want), f"{name}: {[e.as_dict() for e in got[-5:]]}")
        if s.interleaved:
            # The loader's events and the matvec's lines, each in order (they interleave by timing).
            def split(events):
                mine = [e for e in events if e.kind != "line" or e.tag in LOADER_TAGS]
                theirs = [e for e in events if e.kind == "line" and e.tag not in LOADER_TAGS]
                return mine, theirs
            for g_part, w_part in zip(split(got), split(want)):
                self.assertEqual(len(g_part), len(w_part), name)
                for i, (g, w) in enumerate(zip(g_part, w_part)):
                    self.same_event(name, i, g, w, s)
        else:
            for i, (g, w) in enumerate(zip(got, want)):
                self.same_event(name, i, g, w, s)
        # Z lines 4-6 are timing; cycles = words + idle clocks by construction.
        zs = [e for e in got if e.kind == "line" and e.tag == "Z"]
        for k in range(0, len(zs), link.Z_COUNT):
            z = {link.Z_NAMES[e.a & 0xFFFF]: e.v for e in zs[k:k + link.Z_COUNT]}
            if z["words"]:
                self.assertEqual(z["cycles"], z["words"] + z["idle_clocks"], (name, z))
            else:
                self.assertEqual((z["cycles"], z["idle_clocks"]), (0, 0), (name, z))
        return r

    def run_values(self, events, seq):
        ys = {e.a & 0xFFFF: link.signed32(e.v) for e in events if e.kind == "line" and e.tag == "Y" and e.a >> 24 == seq}
        z = {link.Z_NAMES[e.a & 0xFFFF]: e.v for e in events if e.kind == "line" and e.tag == "Z" and e.a >> 24 == seq}
        return [ys[i] for i in sorted(ys)], z

    def test_runs_in_both_formats(self):
        r = self.assert_as_predicted("runs")
        s = self.scenarios["runs"]
        for run in s.extra["runs"]:
            y, z = self.run_values(r["events"], run["seq"])
            self.assertEqual(y, run["y"], run["seq"])
            self.assertEqual(z["status"], link.Z_RAN)
            self.assertEqual(z["words"], run["rows"] * link.words_per_row(run["cols"], run["fmt"]))
        status = proto.decode_status(r["events"][-37:])
        self.assertEqual(status["config"] >> 24, 4)
        self.assertEqual(status["frames_committed"], s.model.c["frames_committed"])
        self.assertEqual(status["frames_committed"], 3)                     # the three images; X and M count no frame
        mon = r["monitors"]
        words = sum(run["rows"] * link.words_per_row(run["cols"], run["fmt"]) for run in s.extra["runs"])
        self.assertEqual((mon["TBWB"]["taken1"], mon["TBWB"]["acks1"]), (words, words))
        self.assertEqual((mon["TBMV"]["feed_runs"], mon["TBMV"]["mv_runs"]), (4, 4))
        self.assertEqual(mon["TBMV"]["lines1"], sum(run["rows"] + link.Z_COUNT for run in s.extra["runs"]))

    def test_refusals(self):
        r = self.assert_as_predicted("refusals")
        s = self.scenarios["refusals"]
        naks = [e for e in r["events"] if e.kind == "line" and e.tag == "N"]
        self.assertEqual([proto.REASONS[(e.a >> 20) & 15] for e in naks], ["length"] * 7)
        self.assertEqual([(e.a >> 17) & 7 for e in naks], [5, 5, 5, 5, 6, 6, 6])
        for seq in range(8, 14):
            y, z = self.run_values(r["events"], seq)
            self.assertEqual((y, z["status"], z["rows"], z["words"]), ([], link.Z_REFUSED, 0, 0), seq)
        dup = [e for e in r["events"] if e.kind == "line" and e.tag == "A" and e.a >> 24 == 14]
        self.assertEqual([proto.REASONS[(e.a >> 20) & 15] for e in dup], ["ok", "duplicate"])
        self.assertEqual(self.run_values(r["events"], 15)[0], s.extra["y"])
        self.assertEqual(r["monitors"]["TBMV"]["feed_runs"], 1)             # the refused runs never read DDR3
        self.assertEqual(r["monitors"]["TBMV"]["feed_timeouts"], 0)

    def test_frames_while_a_run_is_busy(self):
        r = self.assert_as_predicted("busy")
        s = self.scenarios["busy"]
        events = r["events"]
        naks = [e for e in events if e.kind == "line" and e.tag == "N"]
        self.assertEqual([(e.a >> 24, proto.REASONS[(e.a >> 20) & 15]) for e in naks], [(3, "not_ready"), (4, "not_ready")])
        # The naks came while the run's lines were going out (between its first Y and its last Z).
        pos = {id(e): i for i, e in enumerate(events)}
        run = [pos[id(e)] for e in events if e.kind == "line" and e.tag in "YZ" and e.a >> 24 == 2]
        self.assertTrue(all(run[0] < pos[id(n)] < run[-1] for n in naks), [pos[id(n)] for n in naks])
        self.assertEqual(self.run_values(events, 2)[0], s.extra["y1"])
        self.assertEqual(self.run_values(events, 6)[0], s.extra["y2"])
        status = proto.decode_status(events[-37:])
        self.assertEqual(status["nak_not_ready"], 2)

    def test_matvec_waits_for_the_calibration(self):
        r = self.assert_as_predicted("not_ready")
        s = self.scenarios["not_ready"]
        first = [e for e in r["events"] if e.kind == "line" and e.a >> 24 == 1]
        self.assertEqual([(e.tag, proto.REASONS[(e.a >> 20) & 15]) for e in first], [("N", "not_ready")])
        self.assertEqual(self.run_values(r["events"], 4)[0], s.extra["y"])

    def test_abort_on_withheld_acks(self):
        r = self.assert_as_predicted("abort")
        s = self.scenarios["abort"]
        y, z = self.run_values(r["events"], 2)
        self.assertEqual((y, z["status"], z["words"]), ([], link.Z_SHORT, 0))
        # Ended by the feed's abort (its watchdog, 2,000 clocks here), not by the matvec's own idle
        # limit (65,536): a matvec that never saw the abort passes every other check of this test.
        self.assertLess(z["latency"], 2 * 2000, z)
        self.assertEqual(self.run_values(r["events"], 3)[0], s.extra["y"])
        mon = r["monitors"]["TBMV"]
        self.assertEqual(mon["feed_timeouts"], 1)
        self.assertGreater(mon["feed_dropped"], 0)                           # the late acks of the aborted run
        self.assertEqual(r["monitors"]["TBWB"]["taken1"], mon["feed_dropped"] + 8 * link.words_per_row(160, 1))

    def test_abort_with_a_stalled_request(self):
        r = self.assert_as_predicted("abort_long")
        s = self.scenarios["abort_long"]
        y, z = self.run_values(r["events"], 2)
        self.assertEqual((y, z["status"], z["words"]), ([], link.Z_SHORT, 0))
        self.assertLess(z["latency"], 2 * 2000, z)                          # the feed's abort, as above
        self.assertEqual(self.run_values(r["events"], 3)[0], s.extra["y"])
        mon = r["monitors"]
        self.assertEqual(mon["TBMV"]["feed_timeouts"], 1)
        # Every request of the aborted run was taken, the stalled one too, and every ack dropped.
        self.assertEqual(mon["TBWB"]["taken1"], mon["TBMV"]["feed_dropped"] + s.extra["words"])
        self.assertGreater(mon["TBMV"]["feed_dropped"], 62)

    def test_frames_while_the_feed_drains(self):
        r = self.assert_as_predicted("drain_busy")
        s = self.scenarios["drain_busy"]
        naks = [e for e in r["events"] if e.kind == "line" and e.tag in "AN" and e.a >> 24 in (3, 4)]
        self.assertEqual([(e.tag, proto.REASONS[(e.a >> 20) & 15]) for e in naks], [("N", "not_ready")] * 2)
        y, z = self.run_values(r["events"], 5)
        self.assertEqual((y, z["status"]), (s.extra["y"], link.Z_RAN))

    def test_activations_at_blocks_and_in_parts(self):
        r = self.assert_as_predicted("act_blocks")
        for seq, want in self.scenarios["act_blocks"].extra["runs"]:
            y, z = self.run_values(r["events"], seq)
            self.assertEqual((y, z["status"]), (want, link.Z_RAN), seq)

    def test_baud_change_waits_for_the_run(self):
        r = self.assert_as_predicted("baud_busy")
        s = self.scenarios["baud_busy"]
        answers = [e for e in r["events"] if e.kind == "line" and e.tag in "AN" and e.a >> 24 in (3, 4)]
        self.assertEqual([(e.tag, e.a >> 24, (e.a >> 17) & 7, proto.REASONS[(e.a >> 20) & 15]) for e in answers],
                         [("N", 3, 4, "not_ready"), ("A", 4, 4, "ok")])
        self.assertEqual(self.run_values(r["events"], 2)[0], s.extra["y"])
        status = proto.decode_status(r["events"][-37:])
        self.assertEqual(status["baud_div"], 20)

    def test_load_and_read_back_during_a_run(self):
        r = self.assert_as_predicted("arbitration")
        s = self.scenarios["arbitration"]
        reads = [e for e in r["events"] if e.kind == "readback"]
        self.assertEqual([(e.seq, e.crc_ok, e.data) for e in reads], [(4, True, s.extra["chunk"])])
        self.assertEqual(self.run_values(r["events"], 2)[0], s.extra["y"])
        mon = r["monitors"]
        self.assertGreater(mon["TBWB"]["owner_changes"], 0)
        # The read-back went out among the run's lines: some matvec line before it and some after.
        events = r["events"]
        at = next(i for i, e in enumerate(events) if e.kind == "readback")
        self.assertTrue(any(e.kind == "line" and e.tag in "YZ" for e in events[:at]))
        self.assertTrue(any(e.kind == "line" and e.tag in "YZ" for e in events[at:]))

    def test_real_chunk_both_formats(self):
        if self.chunk is None:
            if REQUIRE_CACHED:
                self.fail(f"TRINITY_REQUIRE_CACHED=1: {self.chunk_error}")
            self.skipTest(f"fixture cache: {self.chunk_error}")
        import stage1_chunk
        self.assertEqual(self.chunk["full_sha256"], self.chunk["report_sha256"])
        r = self.assert_as_predicted("real_chunk")
        want = list(self.chunk["y"])
        for seq in (2, 4):
            y, z = self.run_values(r["events"], seq)
            self.assertEqual(len(y), stage1_chunk.ROWS)
            self.assertEqual(y, want, seq)
            self.assertEqual(z["status"], link.Z_RAN)
            self.assertEqual(z["invalid_codes"], 0)
        self.assertEqual(want[:8], [-2561, 2660, -52, -2385, 1269, 3446, 3451, -2365])
        self.assertEqual(hashlib.sha256(struct.pack(f"<{len(want)}q", *want)).hexdigest()[:8], "a2366b57")


if __name__ == "__main__":
    unittest.main()
