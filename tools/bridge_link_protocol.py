"""Matvec extension of the AX7203 UART loader protocol (issue #64), in Python.

docs/bridge.md, "Wire protocol extension", is the specification; the host side is
t27/fpga_link.t27 and the device matvec t27/rtl/fpga_ddr3_matvec.t27. This module states
the same rules for the tests (tests/test_bridge_fpga.py, tests/test_ddr3_matvec.py):

- the row-padded weight image and the activation image the host sends;
- the two new frames, activations 'X' and matvec 'M', in the loader's frame format;
- the two new device lines, 'Y' (one per row) and 'Z' (eleven run counters);
- `MatvecDevice`, the loader's byte-level `DeviceModel` extended with those commands, so a
  fake device can answer a host byte for byte. Its accumulators come from a `compute`
  callable (the tests pass t27/matvec.t27's tmv_matvec through trinity_memory.matvec).

Everything of the loader protocol itself (tools/uart_loader_protocol.py) is reused as is.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

import uart_loader_protocol as proto

CMD_ACT, CMD_MATVEC = 0x58, 0x4D                      # 'X' 'M'
CMD_INDEX = {**proto.CMD_INDEX, CMD_ACT: 5, CMD_MATVEC: 6}
TAG_Y, TAG_Z = "Y", "Z"
PROTO = 3                                            # the loader's protocol 2 plus this extension
WORD_BYTES = 16
ACT_BLOCK_BYTES = 80                                 # one activation block per word, both formats
ACT_BANKS = 10                                       # eight activations (a u64) per bank
ACT_ENTRIES = 1024
ACT_FRAME_BLOCKS = 51                                # 51 * 80 = 4080 <= 4096 bytes per frame
MAX_ROWS = 1024                                      # rows of one run (the result memory)
MAX_WPR = 1024                                       # words per row (the activation banks)
MATVEC_PAYLOAD = 12                                  # rows u32, cols u32, fmt u32
Z_NAMES = ["status", "rows", "words_per_row", "words", "cycles", "idle_clocks", "latency",
           "invalid_codes", "stray_words", "consumer_stalls", "checksum"]
Z_COUNT = len(Z_NAMES)
M32 = proto.M32


def lanes(fmt: int) -> int:
    """Lanes per 128-bit word: 64 baseline2 (fmt 0), 80 dense5 (fmt 1)."""
    return 80 if fmt == 1 else 64


def words_per_row(cols: int, fmt: int) -> int:
    return -(-cols // lanes(fmt))


def _byte(trits, fmt: int) -> int:
    if fmt == 1:
        return sum((t + 1) * 3 ** i for i, t in enumerate(trits))
    return sum((1 if t == 1 else 2 if t == -1 else 0) << (2 * i) for i, t in enumerate(trits))


def image(trits, rows: int, cols: int, fmt: int) -> bytes:
    """Row-padded device image: every row starts at a 16-byte word, padding lanes are zero trits."""
    group = 5 if fmt == 1 else 4
    row_bytes = words_per_row(cols, fmt) * WORD_BYTES
    out = bytearray()
    for r in range(rows):
        row = list(trits[r * cols:(r + 1) * cols]) + [0] * (row_bytes * group - cols)
        out += bytes(_byte(row[n * group:(n + 1) * group], fmt) for n in range(row_bytes))
    return bytes(out)


def decode_row(data: bytes, cols: int, fmt: int) -> tuple[list[int], int]:
    """Trits of one row image (the first `cols` lanes) and the number of invalid codes in all of it
    (dense5 codes 243-255 in any byte, baseline2 lane code 11 in any lane): the device's decode."""
    out, invalid = [], 0
    for b in data:
        if fmt == 1:
            if b >= 243:
                invalid += 1
                digits = [1] * 5
            else:
                digits = [(b // 3 ** i) % 3 for i in range(5)]
            out += [d - 1 for d in digits]
        else:
            for i in range(4):
                code = (b >> (2 * i)) & 3
                invalid += code == 3
                out.append({0: 0, 1: 1, 2: -1, 3: 0}[code])
    return out[:cols], invalid


def act_image(x, cols: int, fmt: int) -> bytes:
    """wpr blocks of 80 bytes: block k byte i (i < lanes) = x[k * lanes + i] as a byte (0 past cols)."""
    n = lanes(fmt)
    out = bytearray()
    for k in range(words_per_row(cols, fmt)):
        block = bytearray(ACT_BLOCK_BYTES)
        for i in range(n):
            if k * n + i < cols:
                block[i] = x[k * n + i] & 255
        out += block
    return bytes(out)


def act_frame(seq: int, block: int, data: bytes) -> bytes:
    return proto.frame(CMD_ACT, seq, block, len(data), data)


def matvec_payload(rows: int, cols: int, fmt: int) -> bytes:
    return struct.pack("<III", rows, cols, fmt)


def matvec_frame(seq: int, addr: int, rows: int, cols: int, fmt: int) -> bytes:
    return proto.frame(CMD_MATVEC, seq, addr, MATVEC_PAYLOAD, matvec_payload(rows, cols, fmt))


def resp_word(seq: int, known: bool, reason: int, cmd: int, length: int) -> int:
    return (((seq & 255) << 24) | ((reason & 15) << 20) | ((CMD_INDEX.get(cmd, 0) & 7) << 17)
            | (int(known) << 16) | (length & 0xFFFF))


def y_word(seq: int, row: int) -> int:
    return ((seq & 255) << 24) | (row & 0xFFFFFF)


def z_word(seq: int, index: int) -> int:
    return ((seq & 255) << 24) | (Z_COUNT << 16) | index


def result_checksum(values) -> int:
    c = 0
    for v in values:
        c = (((c << 1) | (c >> 31)) & M32) ^ (v & M32)
    return c


def config_word(store_log2: int) -> int:
    return (PROTO << 24) | (12 << 16) | ((store_log2 & 255) << 8) | 10


class LinkDecoder(proto.StreamDecoder):
    """The loader's decoder with the two new line tags."""
    TAGS = b"HANCYZ"


def signed32(v: int) -> int:
    return v - (1 << 32) if v & 0x80000000 else v


@dataclass
class MatvecDevice(proto.DeviceModel):
    """DeviceModel plus the activation and matvec commands.

    compute(trits, rows, cols, x) -> list of int accumulators (default: exact Python sums).
    result_faults: row -> value added to that row's accumulator (a wrong device result).
    The Z counters of a run are those of an ideal device (one word per clock, no idle clock,
    no latency): this is a model of the protocol, not of the timing.
    """
    compute: object = None
    result_faults: dict = field(default_factory=dict)

    def __post_init__(self):
        self.act = bytearray(ACT_ENTRIES * ACT_BLOCK_BYTES)
        self.last_act = None
        self.runs = []
        super().__post_init__()

    def reset(self, first=False):
        super().reset(first)
        self.last_act = None
        self.c["config"] = config_word(self.store_log2)
        self.out[-1] = ("H", self.build_id, config_word(self.store_log2))

    def _nak(self, reason):
        self.naks += 1
        self.c[proto.NAK_COUNTERS[reason]] += 1
        self.out.append(("N", resp_word(self.seq, self.seq_known, reason, self.cmd, self.len), self.counts()))
        self.state = "hunt"

    def _check_header(self):
        cmd, addr, length = self.cmd, self.addr, self.len
        if cmd == CMD_ACT:
            ok = 8 <= length <= ACT_FRAME_BLOCKS * ACT_BLOCK_BYTES and length % 8 == 0 \
                and addr + -(-length // ACT_BLOCK_BYTES) <= ACT_ENTRIES
        elif cmd == CMD_MATVEC:
            ok = length == MATVEC_PAYLOAD and addr % WORD_BYTES == 0 and addr < len(self.store)
        else:
            return super()._check_header()
        if not ok:
            return self._nak(proto.R_LENGTH)
        self.state = "payload"

    def _check_frame(self):
        if self.cmd not in (CMD_ACT, CMD_MATVEC):
            return super()._check_frame()
        body = bytes(self.hdr) + bytes(self.payload)
        got = struct.unpack("<I", bytes(self.crcb))[0]
        if proto.crc32(body) != got:
            return self._nak(proto.R_CRC)
        self.state = "hunt"
        self.probation = False
        if self.cmd == CMD_ACT:
            key = (self.seq, self.addr, self.len, got)
            if self.last_act == key:
                self.out.append(("A", resp_word(self.seq, True, proto.R_DUP, self.cmd, self.len), self.counts()))
                return
            start = self.addr * ACT_BLOCK_BYTES
            self.act[start:start + self.len] = self.payload
            self.last_act = key
            self.out.append(("A", resp_word(self.seq, True, proto.R_OK, self.cmd, self.len), self.counts()))
            return
        rows, cols, fmt = struct.unpack("<III", bytes(self.payload))
        self.out.append(("A", resp_word(self.seq, True, proto.R_OK, self.cmd, self.len), self.counts()))
        self.run_matvec(self.seq, self.addr, rows, cols, fmt)

    def run_matvec(self, seq, addr, rows, cols, fmt):
        wpr = words_per_row(cols, fmt) if cols else 0
        refused = not (1 <= rows <= MAX_ROWS and cols >= 1 and fmt in (0, 1) and wpr <= MAX_WPR
                       and addr + rows * wpr * WORD_BYTES <= len(self.store))
        z = [0] * Z_COUNT
        if refused:
            z[0] = 1
            self.runs.append({"seq": seq, "refused": True})
        else:
            n = lanes(fmt)
            trits, invalid = [], 0
            for r in range(rows):
                start = addr + r * wpr * WORD_BYTES
                row, bad = decode_row(bytes(self.store[start:start + wpr * WORD_BYTES]), cols, fmt)
                trits += row
                invalid += bad
            x = [b - 256 if b >= 128 else b
                 for k in range(wpr) for b in self.act[k * ACT_BLOCK_BYTES:k * ACT_BLOCK_BYTES + n]][:cols]
            if self.compute is not None:
                y = list(self.compute(trits, rows, cols, x))
            else:
                y = [sum(trits[r * cols + j] * x[j] for j in range(cols)) for r in range(rows)]
            for row, delta in self.result_faults.items():
                if row < rows:
                    y[row] += delta
            for r, v in enumerate(y):
                self.out.append(("Y", y_word(seq, r), v & M32))
            z[1:4] = [rows, wpr, rows * wpr]
            z[4] = rows * wpr
            z[7] = invalid
            z[10] = result_checksum(y)
            self.runs.append({"seq": seq, "rows": rows, "cols": cols, "fmt": fmt, "addr": addr, "y": y})
        for i, v in enumerate(z):
            self.out.append(("Z", z_word(seq, i), v & M32))
