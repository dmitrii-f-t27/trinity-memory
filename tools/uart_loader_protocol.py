"""Frame protocol of the AX7203 UART loader (issue #63), shared by the host tool
(tools/fpga-uart-loader.py) and the tests (tests/test_uart_loader.py).

The device side is t27/rtl/fpga_uart_loader.t27; docs/uart-loader.md is the
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

REASONS = ["ok", "duplicate", "crc", "length", "timeout", "framing", "command", "overflow"]
R_OK, R_DUP, R_CRC, R_LENGTH, R_TIMEOUT, R_FRAMING, R_COMMAND, R_OVERFLOW = range(8)

PROTO = 1
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
    "baud_reverts", "last_seq", "config", "clocks", "build_id",
]
NAK_COUNTERS = {R_CRC: "nak_crc", R_LENGTH: "nak_length", R_TIMEOUT: "nak_timeout", R_FRAMING: "nak_framing",
                R_COMMAND: "nak_command", R_OVERFLOW: "nak_overflow"}
M32 = 0xFFFFFFFF


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
    return {"seq": a >> 24, "reason": (a >> 20) & 15, "reason_name": REASONS[(a >> 20) & 7] if (a >> 20) & 15 < 8 else "?",
            "cmd": INDEX_CMD.get(index), "seq_known": bool((a >> 16) & 1), "len": a & 0xFFFF}


def config_word(store_log2: int) -> int:
    return (PROTO << 24) | (12 << 16) | ((store_log2 & 255) << 8) | 10


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
    a byte in "HANCZ" starts a 20-byte line. Anything else is garbage, reported
    as one event per run of such bytes. feed(data, t) returns the completed events.
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

    def __post_init__(self):
        if self.store is None:
            self.store = bytearray(1 << self.store_log2)
        self.reset(first=True)

    def reset(self, first=False):
        self.c = {name: 0 for name in STATUS_NAMES}
        self.c["config"] = config_word(self.store_log2)
        self.c["build_id"] = self.build_id
        self.state = "hunt"
        self.last = None
        self.div = self.default_div
        self.frames_ok = 0
        self.naks = 0
        self.out.append(("H", self.build_id, config_word(self.store_log2)))

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
        if self.cmd == CMD_LOAD:
            key = (self.seq, self.addr, self.len, got)
            if self.last == key:
                self.c["frames_duplicate"] += 1
                self.out.append(("A", resp_word(self.seq, True, R_DUP, self.cmd, self.len), self.counts()))
                return
            self.store[self.addr:self.addr + self.len] = self.payload
            self.frames_ok += 1
            self.c["frames_committed"] += 1
            self.c["bytes_committed"] += self.len
            self.last = key
            self.out.append(("A", resp_word(self.seq, True, R_OK, self.cmd, self.len), self.counts()))
        elif self.cmd == CMD_READ:
            self.c["readbacks"] += 1
            self.out.append(("r", readback_frame(self.seq, self.addr, bytes(self.store[self.addr:self.addr + self.len]))))
        elif self.cmd == CMD_STATUS:
            values = self.status_values()
            for i, name in enumerate(STATUS_NAMES):
                self.out.append(("C", ((self.seq & 255) << 24) | (len(STATUS_NAMES) << 16) | i, values[name]))
        else:
            self.out.append(("A", resp_word(self.seq, True, R_OK, self.cmd, self.len), self.counts()))
            self.div = self.addr

    def status_values(self) -> dict:
        values = dict(self.c)
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
        self.div = self.default_div
        self.c["baud_reverts"] += 1


def expected_bytes(item) -> bytes:
    """The bytes the device sends for one entry of DeviceModel.out."""
    if item[0] == "r":
        return item[1]
    return format_line(item[0], item[1], item[2])


def decode_status(events: list[Event]) -> dict:
    """Counter name -> value from the C lines of one status response."""
    values = {}
    for event in events:
        if event.kind == "line" and event.tag == "C":
            index = event.a & 0xFFFF
            if index < len(STATUS_NAMES):
                values[STATUS_NAMES[index]] = event.v
    return values
