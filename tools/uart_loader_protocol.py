"""Frame protocol of the AX7203 UART loader (issue #63), shared by the host tool
(tools/fpga-uart-loader.py) and the tests (tests/test_uart_loader.py).

The device side is t27/rtl/fpga_uart_loader.t27 (part 1, block RAM, protocol 2) and
t27/rtl/fpga_ddr3_loader.t27 (part 2, DDR3, protocol 3: reason `not_ready` until the
DDR3 calibration completes, and 14 more status lines); docs/uart-loader.md is the
specification. Everything here is a statement of the same rules in Python:

- frame encoders for the four host commands (load, read, status, baud);
- a decoder of the device's byte stream: 20-byte response lines (tag, 8 hex
  digits, 10 hex digits, LF) with their check byte, and binary read-back frames;
- `DeviceModel`, a byte-level reference of the parser: what the device answers
  to a byte stream, given where the host paused longer than the inter-byte
  timeout. The tests predict every response of a simulated scenario with it.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field

MAGIC = b"\xa5\x5a"
CMD_LOAD, CMD_READ, CMD_STATUS, CMD_BAUD = 0x4C, 0x52, 0x53, 0x42   # 'L' 'R' 'S' 'B'
RESP_READ = 0x72                                                      # 'r'
COMMANDS = {CMD_LOAD: "load", CMD_READ: "read", CMD_STATUS: "status", CMD_BAUD: "baud"}
CMD_INDEX = {CMD_LOAD: 1, CMD_READ: 2, CMD_STATUS: 3, CMD_BAUD: 4}
INDEX_CMD = {v: k for k, v in CMD_INDEX.items()}

REASONS = ["ok", "duplicate", "crc", "length", "timeout", "framing", "command", "overflow", "port", "not_ready"]
R_OK, R_DUP, R_CRC, R_LENGTH, R_TIMEOUT, R_FRAMING, R_COMMAND, R_OVERFLOW, R_PORT, R_NOT_READY = range(10)

PROTO = 2                  # part 1 (block RAM)
PROTO_DDR3 = 3             # part 2 (DDR3): not_ready and the DDR3 status lines
MAX_LEN = 4096
FIFO_DEPTH = 1024
MIN_DIV, MAX_DIV = 16, 65535
HEADER_BYTES = 10          # magic, cmd, seq, addr, len
FRAME_OVERHEAD = HEADER_BYTES + 4
LINE_BYTES = 20
STATUS_NAMES = [
    "rx_bytes", "rx_framing_errors", "rx_false_starts", "fifo_overflow_bytes", "fifo_high_water",
    "ignored_bytes", "frames_committed", "frames_duplicate", "nak_crc", "nak_length", "nak_timeout",
    "nak_framing", "nak_command", "nak_overflow", "bytes_committed", "readbacks", "baud_div",
    "baud_reverts", "last_seq", "config", "clocks", "build_id", "nak_port",
]
# Protocol 3 (t27/rtl/fpga_ddr3_loader.t27) adds these after the 23 lines of protocol 2.
STATUS_NAMES_DDR3 = STATUS_NAMES + [
    "nak_not_ready",
    "calib",               # calib_complete << 24 | state << 16 | highest state << 8 | returns to IDLE (sat. 255)
    "calib_clocks",        # controller clocks since reset at the first calib_complete (0: not yet)
    "calib_lost_clocks",   # clocks since then with calib_complete low or a state other than 23
    "wb_writes", "wb_reads", "wb_read_hits", "wb_acks_dropped", "wb_acks_stray", "wb_max_outstanding",
    "wb_read_latency_max", "wb_cmd_stalls", "arb_switches", "arb_stray",
]
NAK_COUNTERS = {R_CRC: "nak_crc", R_LENGTH: "nak_length", R_TIMEOUT: "nak_timeout", R_FRAMING: "nak_framing",
                R_COMMAND: "nak_command", R_OVERFLOW: "nak_overflow", R_PORT: "nak_port",
                R_NOT_READY: "nak_not_ready"}
M32 = 0xFFFFFFFF
DONE_CALIBRATE = 23
WORD_BYTES = 16            # one Wishbone word of the x16 DDR3 build


def status_names(proto: int = PROTO) -> list[str]:
    return STATUS_NAMES_DDR3 if proto >= PROTO_DDR3 else STATUS_NAMES


def calib_word(calib: bool, state: int, highest: int, returns: int) -> int:
    return (int(calib) << 24) | ((state & 31) << 16) | ((highest & 31) << 8) | (returns & 255)


def decode_calib(word: int) -> dict:
    return {"calib_complete": word >> 24 & 1, "state": word >> 16 & 31, "highest": word >> 8 & 31,
            "returns_to_idle": word & 255}


def bg_word(burst: int) -> bytes:
    """The background word of burst address `burst` in the DDR3 simulation's memory model
    (tests/sim_ddr3_loader_model.v): what a never-written word reads as."""
    h = (burst * 0x9E3779B1) & M32
    return b"".join(struct.pack("<I", h ^ ((0x5EED0000 + k * 0x01010101) & M32)) for k in range(4))


class SparseStore:
    """A store of 2**log2 bytes held as the 16-byte words that were written; a word never
    written reads as `background(burst)` (default zeros). Slicing reads and writes bytes."""

    def __init__(self, log2: int, background=None):
        self.size = 1 << log2
        self.words: dict[int, bytearray] = {}
        self.background = background or (lambda burst: bytes(WORD_BYTES))

    def __len__(self):
        return self.size

    def _word(self, burst: int) -> bytearray:
        word = self.words.get(burst)
        if word is None:
            word = self.words[burst] = bytearray(self.background(burst))
        return word

    def __getitem__(self, key: slice) -> bytes:
        start, stop, _ = key.indices(self.size)
        out = bytearray()
        for a in range(start, stop):
            word = self.words.get(a >> 4)
            out.append(word[a & 15] if word is not None else self.background(a >> 4)[a & 15])
        return bytes(out)

    def __setitem__(self, key: slice, data: bytes):
        start, stop, _ = key.indices(self.size)
        assert stop - start == len(data)
        for i, a in enumerate(range(start, stop)):
            self._word(a >> 4)[a & 15] = data[i]


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & M32


def frame(cmd: int, seq: int, addr: int, length: int, payload: bytes = b"") -> bytes:
    body = bytes([cmd & 255, seq & 255]) + struct.pack("<IH", addr & M32, length & 0xFFFF) + payload
    return MAGIC + body + struct.pack("<I", crc32(body))


def load_frame(seq: int, addr: int, payload: bytes) -> bytes:
    return frame(CMD_LOAD, seq, addr, len(payload), payload)


def read_frame(seq: int, addr: int, length: int) -> bytes:
    return frame(CMD_READ, seq, addr, length)


def status_frame(seq: int) -> bytes:
    return frame(CMD_STATUS, seq, 0, 0)


def baud_frame(seq: int, divisor: int) -> bytes:
    return frame(CMD_BAUD, seq, divisor, 0)


def readback_frame(seq: int, addr: int, data: bytes) -> bytes:
    """What the device sends for a read: the same layout, command 'r'."""
    return frame(RESP_READ, seq, addr, len(data), data)


def line_check(tag: int, a: int, v: int) -> int:
    return crc32(bytes([tag & 255]) + struct.pack("<II", a & M32, v & M32)) & 255


def resp_word(seq: int, known: bool, reason: int, cmd: int, length: int) -> int:
    return (((seq & 255) << 24) | ((reason & 15) << 20) | ((CMD_INDEX.get(cmd, 0) & 7) << 17)
            | (int(known) << 16) | (length & 0xFFFF))


def decode_resp_word(a: int) -> dict:
    index = (a >> 17) & 7
    reason = (a >> 20) & 15
    return {"seq": a >> 24, "reason": reason, "reason_name": REASONS[reason] if reason < len(REASONS) else "?",
            "cmd": INDEX_CMD.get(index), "seq_known": bool((a >> 16) & 1), "len": a & 0xFFFF}


def config_word(store_log2: int, proto: int = PROTO) -> int:
    return (proto << 24) | (12 << 16) | ((store_log2 & 255) << 8) | 10


def format_line(tag: str, a: int, v: int) -> bytes:
    """The 20 bytes the line emitter sends for (tag, a, v)."""
    b = (line_check(ord(tag), a, v) << 32) | (v & M32)
    return f"{tag}{a & M32:08x}{b:010x}\n".encode("ascii")


@dataclass
class Event:
    kind: str                      # "line", "readback", "garbage"
    t: float | None = None         # host time (s) of the event's last byte, when known
    tag: str = ""
    a: int = 0
    v: int = 0
    check_ok: bool = True
    seq: int = 0
    addr: int = 0
    data: bytes = b""
    crc_ok: bool = True
    raw: bytes = b""

    def resp(self) -> dict:
        return decode_resp_word(self.a)

    def as_dict(self) -> dict:
        out = {"kind": self.kind, "t": self.t}
        if self.kind == "line":
            out.update(tag=self.tag, a=f"{self.a:08x}", v=self.v, check_ok=self.check_ok)
            if self.tag in "AN":
                out.update(self.resp())
        elif self.kind == "readback":
            out.update(seq=self.seq, addr=self.addr, len=len(self.data), crc_ok=self.crc_ok)
        else:
            out.update(bytes=self.raw.hex())
        return out


class StreamDecoder:
    """Incremental decoder of the device's byte stream.

    A byte A5 starts a binary read-back frame (A5 5A 'r' seq addr len data crc);
    a byte in "HANC" starts a 20-byte line. Anything else is garbage, reported
    as one event per run of such bytes. feed(data, t) returns the completed events.

    The device never sends a read-back longer than MAX_LEN, so a header whose length
    is 0 or above MAX_LEN (a bit error in the length field) is not a frame: its A5
    is garbage and decoding resumes at the next byte. A frame whose CRC does not
    match is reported (crc_ok False) and its bytes after the A5 are decoded again,
    so that lines inside it are not lost: a read-back cut short by a device reset is
    completed with the bytes that followed it (the H line and later responses), and
    those are recovered this way. A stall can therefore last at most one frame of
    MAX_LEN + FRAME_OVERHEAD bytes.
    """

    TAGS = b"HANC"

    def __init__(self):
        self.buf = bytearray()
        self.garbage = bytearray()

    def _flush_garbage(self, out, t):
        if self.garbage:
            out.append(Event("garbage", t=t, raw=bytes(self.garbage)))
            self.garbage = bytearray()

    def feed(self, data: bytes, t: float | None = None) -> list[Event]:
        self.buf += data
        out: list[Event] = []
        while self.buf:
            first = self.buf[0]
            if first == 0xA5:
                if len(self.buf) < HEADER_BYTES:
                    break
                if self.buf[1] != 0x5A or self.buf[2] != RESP_READ:
                    self.garbage.append(self.buf.pop(0))
                    continue
                length = struct.unpack_from("<H", self.buf, 8)[0]
                if length == 0 or length > MAX_LEN:
                    self.garbage.append(self.buf.pop(0))
                    continue
                total = FRAME_OVERHEAD + length
                if len(self.buf) < total:
                    break
                raw = bytes(self.buf[:total])
                del self.buf[:total]
                self._flush_garbage(out, t)
                body = raw[2:-4]
                seq, addr = raw[3], struct.unpack_from("<I", raw, 4)[0]
                ok = crc32(body) == struct.unpack_from("<I", raw, total - 4)[0]
                out.append(Event("readback", t=t, seq=seq, addr=addr, data=raw[HEADER_BYTES:-4], crc_ok=ok, raw=raw))
                if not ok:
                    self.buf[0:0] = raw[1:]
            elif first in self.TAGS:
                if len(self.buf) < LINE_BYTES:
                    break
                raw = bytes(self.buf[:LINE_BYTES])
                text = raw[1:19]
                if raw[19] != 0x0A or any(c not in b"0123456789abcdef" for c in text):
                    self.garbage.append(self.buf.pop(0))
                    continue
                del self.buf[:LINE_BYTES]
                self._flush_garbage(out, t)
                a, b = int(text[:8], 16), int(text[8:], 16)
                tag, v = chr(raw[0]), b & M32
                out.append(Event("line", t=t, tag=tag, a=a, v=v, check_ok=(b >> 32) == line_check(raw[0], a, v), raw=raw))
            else:
                self.garbage.append(self.buf.pop(0))
        return out

    def finish(self, t: float | None = None) -> list[Event]:
        out: list[Event] = []
        self.garbage += self.buf
        self.buf = bytearray()
        self._flush_garbage(out, t)
        return out


# ---- reference of the device's parser ----

@dataclass
class DeviceModel:
    """What t27/rtl/fpga_uart_loader.t27 answers, byte by byte.

    feed(b) consumes one received byte (ferr: its stop bit was low; gap: bytes
    before it were lost), timeout() is a pause of at least the inter-byte timeout,
    reset() the reset button. Responses are appended to `out` as the bytes the
    device sends. FIFO timing (overflow, high-water mark) and the clock counter are
    not modelled; everything else in the status lines is.
    """
    store_log2: int = 18
    build_id: int = 0
    default_div: int = 217
    store: bytearray = field(default=None)
    out: list = field(default_factory=list)
    proto: int = PROTO
    # Protocol 3 (DDR3): whether UberDDR3 has calibrated (loads and reads are refused with
    # not_ready until then), and the Wishbone master's words (t27/rtl/fpga_loader_wb.t27):
    # a write per run of bytes in one 16-byte word, a read per read-back byte outside the
    # kept word; the kept word is forgotten by a write to it and by reset.
    calibrated: bool = True
    calib_clocks: int = 0          # what the calib_clocks line says once calibrated (timing: not predicted)

    def __post_init__(self):
        if self.store is None:
            self.store = bytearray(1 << self.store_log2)
        self.reset(first=True)

    def reset(self, first=False):
        self.names = status_names(self.proto)
        self.c = {name: 0 for name in self.names}
        self.c["config"] = config_word(self.store_log2, self.proto)
        self.c["build_id"] = self.build_id
        self.state = "hunt"
        self.last = None
        self.div = self.default_div
        self.probation = False
        self.frames_ok = 0
        self.naks = 0
        self.kept = None
        self.out.append(("H", self.build_id, config_word(self.store_log2, self.proto)))

    # counters as the lines carry them
    def counts(self):
        return ((self.frames_ok & 0xFFFF) << 16) | (self.naks & 0xFFFF)

    def _nak(self, reason):
        self.naks += 1
        self.c[NAK_COUNTERS[reason]] += 1
        self.out.append(("N", resp_word(self.seq, self.seq_known, reason, self.cmd, self.len), self.counts()))
        self.state = "hunt"

    def rx(self, byte: int, ferr: bool = False):
        """A byte the receiver delivered (counted in rx_bytes)."""
        self.c["rx_bytes"] += 1
        if ferr:
            self.c["rx_framing_errors"] += 1
        self.feed(byte, ferr)

    def false_start(self):
        self.c["rx_false_starts"] += 1

    def feed(self, byte: int, ferr: bool = False, gap: bool = False):
        in_frame = self.state in ("hdr", "payload", "crc")
        if in_frame and gap:
            return self._nak(R_OVERFLOW)
        if in_frame and ferr:
            return self._nak(R_FRAMING)
        if self.state == "hunt":
            if byte == 0xA5 and not ferr:
                self.state = "sync1"
            else:
                self.c["ignored_bytes"] += 1
        elif self.state == "sync1":
            if byte == 0x5A and not ferr and not gap:
                self.state, self.hdr, self.crc = "hdr", bytearray(), 0
                self.cmd = self.seq = self.addr = self.len = 0
                self.seq_known = False
                self.payload = bytearray()
            elif byte == 0xA5 and not ferr:
                self.c["ignored_bytes"] += 1
            else:
                self.c["ignored_bytes"] += 2
                self.state = "hunt"
        elif self.state == "hdr":
            self.hdr.append(byte)
            n = len(self.hdr)
            if n == 1:
                self.cmd = byte
            elif n == 2:
                self.seq, self.seq_known = byte, True
            elif n == 6:
                self.addr = struct.unpack_from("<I", self.hdr, 2)[0]
            elif n == 7:
                # The device shifts the length in from the top: after one byte it holds byte << 8
                # (visible only in the len field of a timeout nak).
                self.len = byte << 8
            elif n == 8:
                self.len = struct.unpack_from("<H", self.hdr, 6)[0]
                self._check_header()
        elif self.state == "payload":
            self.payload.append(byte)
            if len(self.payload) == self.len:
                self.state, self.crcb = "crc", bytearray()
        elif self.state == "crc":
            self.crcb.append(byte)
            if len(self.crcb) == 4:
                self._check_frame()

    def _check_header(self):
        cmd, addr, length, store = self.cmd, self.addr, self.len, len(self.store)
        if cmd not in CMD_INDEX:
            return self._nak(R_COMMAND)
        if cmd in (CMD_LOAD, CMD_READ):
            ok = 1 <= length <= MAX_LEN and addr < store and length <= store - addr
        elif cmd == CMD_BAUD:
            ok = length == 0 and MIN_DIV <= addr <= MAX_DIV
        else:
            ok = length == 0
        if not ok:
            return self._nak(R_LENGTH)
        if cmd == CMD_LOAD:
            self.state = "payload"
        else:
            self.state, self.crcb = "crc", bytearray()

    def _check_frame(self):
        body = bytes(self.hdr) + bytes(self.payload)
        got = struct.unpack("<I", bytes(self.crcb))[0]
        if crc32(body) != got:
            return self._nak(R_CRC)
        self.state = "hunt"
        self.probation = False          # a good frame confirms the rate
        if self.proto >= PROTO_DDR3 and self.cmd in (CMD_LOAD, CMD_READ) and not self.calibrated:
            return self._nak(R_NOT_READY)
        if self.cmd == CMD_LOAD:
            key = (self.seq, self.addr, self.len, got)
            if self.last == key:
                self.c["frames_duplicate"] += 1
                self.out.append(("A", resp_word(self.seq, True, R_DUP, self.cmd, self.len), self.counts()))
                return
            self.store[self.addr:self.addr + self.len] = self.payload
            self._wishbone_writes(self.addr, self.len)
            self.frames_ok += 1
            self.c["frames_committed"] += 1
            self.c["bytes_committed"] += self.len
            self.last = key
            self.out.append(("A", resp_word(self.seq, True, R_OK, self.cmd, self.len), self.counts()))
        elif self.cmd == CMD_READ:
            self.c["readbacks"] += 1
            self._wishbone_reads(self.addr, self.len)
            self.out.append(("r", readback_frame(self.seq, self.addr, bytes(self.store[self.addr:self.addr + self.len]))))
        elif self.cmd == CMD_STATUS:
            values = self.status_values()
            for i, name in enumerate(self.names):
                self.out.append(("C", ((self.seq & 255) << 24) | (len(self.names) << 16) | i, values[name]))
        else:
            self.out.append(("A", resp_word(self.seq, True, R_OK, self.cmd, self.len), self.counts()))
            self.div = self.addr
            self.probation = self.addr != self.default_div

    def _wishbone_writes(self, addr: int, length: int):
        if self.proto < PROTO_DDR3:
            return
        first, last = addr >> 4, (addr + length - 1) >> 4
        self.c["wb_writes"] += last - first + 1
        if self.kept is not None and first <= self.kept <= last:
            self.kept = None

    def _wishbone_reads(self, addr: int, length: int):
        if self.proto < PROTO_DDR3:
            return
        for a in range(addr, addr + length):
            if self.kept == a >> 4:
                self.c["wb_read_hits"] += 1
            else:
                self.c["wb_reads"] += 1
                self.kept = a >> 4

    def status_values(self) -> dict:
        values = dict(self.c)
        if self.proto >= PROTO_DDR3:
            values["calib"] = calib_word(True, DONE_CALIBRATE, DONE_CALIBRATE, 0) if self.calibrated else 0
            values["calib_clocks"] = self.calib_clocks if self.calibrated else 0
        values["frames_committed"] = self.frames_ok
        values["baud_div"] = self.div
        values["last_seq"] = (256 | self.last[0]) if self.last else 0
        return values

    def timeout(self):
        """A pause longer than the inter-byte timeout."""
        if self.state == "sync1":
            self.c["ignored_bytes"] += 1
            self.state = "hunt"
        elif self.state in ("hdr", "payload", "crc"):
            self._nak(R_TIMEOUT)

    def revert_baud(self):
        """The probation time ran out without a good frame at the new rate."""
        assert self.probation, "the device reverts only a rate still on probation"
        self.div = self.default_div
        self.probation = False
        self.c["baud_reverts"] += 1

    def port_failed_commit(self, previous_last):
        """The last load's commit was ended by the port watchdog: a PORT nak instead of
        its ack, nothing counted as committed (the store holds whatever the port wrote;
        callers reload the chunk before they read it)."""
        tag, _a, _v = self.out.pop()
        assert tag == "A"
        self.frames_ok -= 1
        self.c["frames_committed"] -= 1
        self.c["bytes_committed"] -= self.len
        self.last = previous_last
        self._nak(R_PORT)

    def port_failed_read(self, good: int):
        """The last read-back's port stopped answering after `good` data bytes: the rest
        of the frame is zeros, the CRC has bit 0 flipped, and a PORT nak follows."""
        tag, raw = self.out.pop()
        assert tag == "r"
        data = raw[HEADER_BYTES:HEADER_BYTES + self.len][:good] + bytes(self.len - good)
        frame_bytes = bytearray(readback_frame(self.seq, self.addr, data))
        frame_bytes[-4] ^= 1
        self.out.append(("r", bytes(frame_bytes)))
        self.c["readbacks"] -= 1
        self._nak(R_PORT)


def expected_bytes(item) -> bytes:
    """The bytes the device sends for one entry of DeviceModel.out."""
    if item[0] == "r":
        return item[1]
    return format_line(item[0], item[1], item[2])


def decode_status(events: list[Event]) -> dict:
    """Counter name -> value from the C lines of one status response (23 lines: protocol 2,
    37: protocol 3; each line carries the count in a >> 16)."""
    values = {}
    for event in events:
        if event.kind == "line" and event.tag == "C":
            index = event.a & 0xFFFF
            names = STATUS_NAMES_DDR3 if (event.a >> 16) & 0xFF == len(STATUS_NAMES_DDR3) else STATUS_NAMES
            if index < len(names):
                values[names[index]] = event.v
    return values
