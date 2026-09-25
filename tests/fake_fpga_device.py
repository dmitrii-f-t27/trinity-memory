"""A fake AX7203 behind a pseudo-terminal, for the Bridge's fpga backend tests (issue #64).

FakeDevice opens a pty pair; the Bridge opens the slave's path as its serial port, exactly as it
opens /dev/cu.usbserial-110. A thread reads the host's bytes from the master side and feeds them one
by one to tools/bridge_link_protocol.MatvecDevice (the loader's byte-level reference model plus the
activation and matvec commands); a pause of `byte_timeout` seconds inside a frame is the device's
inter-byte timeout (a timeout nak). The model's responses are written back as the device's bytes.
Accumulators come from `compute(trits, rows, cols, x)`: t27_compute uses t27/matvec.t27's
tmv_matvec through trinity_memory.matvec.

Faults act on the byte streams, each once:
  {"kind": "corrupt", "cmd": "L", "nth": 3, "offset": 20}  flip bit 0 of byte `offset` (from the A5) of the
                                                            nth frame with that command the host sent
  {"kind": "drop", "cmd": "L", "nth": 3, "offset": 20}     drop that byte instead
  {"kind": "drop_line", "tag": "A", "index": 1, "nth": 2}  drop the nth line with that tag (and, for A/N,
                                                            that command index) the device sends
  {"kind": "corrupt_line", "tag": "Y", "nth": 5}            flip one hex digit of the nth such line
  {"kind": "garbage", "before": "Y", "nth": 1, "data": "..."} send bytes (hex) before the nth such line
  {"kind": "reset", "after_frames": n}                      after the host's nth frame: the reset button
                                                            (the H line; the store keeps its data)
`result_faults` {row: delta} make the device's accumulator of that row wrong by delta.
`run_faults` [{"status": 2} | {"stray": n} | {"invalid": n}, ...]: the device's next runs, one
entry each, end early (status 2, only the Z lines) or report n stray words or n invalid codes.
Everything the device sent is in `tx_log`, everything it received in `rx_log`.

`pace_baud`: without it the fake answers as fast as the pty takes bytes. With it, the host's
bytes reach the model one per 10 / pace_baud seconds (the UART line into the device) and the
device's bytes leave on the same schedule (the line out), so the host's deadlines meet a line of
that rate: the wire-time terms and the per-line deadline extension of t27/fpga_link.t27 matter.
The pacing is Python time.monotonic() scheduling, not a UART: it is only as exact as the thread
wakes up.
"""
from __future__ import annotations

import collections
import ctypes as C
import os
import select
import sys
import threading
import time
import tty
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import bridge_link_protocol as link  # noqa: E402
import uart_loader_protocol as proto  # noqa: E402

FRAME_PAYLOAD_CMDS = (proto.CMD_LOAD, link.CMD_ACT, link.CMD_MATVEC)


def t27_compute(trits, rows, cols, x):
    """y = W x by t27/matvec.t27's tmv_matvec (one group of `cols` columns per row)."""
    from trinity_memory import matvec as mv
    values = (C.c_int32 * max(1, len(trits)))(*trits)
    xs = (C.c_int8 * max(1, cols))(*x)
    _, y = mv.matvec(values, rows, cols, xs, cols)
    return list(y)


class FakeDevice:
    def __init__(self, *, build_id: int = 0x1D474000, store_log2: int = 20, compute=t27_compute, faults=(),
                 result_faults=None, byte_timeout: float = 0.05, protocol: int | None = None,
                 run_faults=(), pace_baud: int | None = None):
        # The DDR3 loader's base (37 status lines, not_ready) under protocol 4, as the matvec build.
        self.model = link.MatvecDevice(store_log2=store_log2, build_id=build_id, compute=compute,
                                       result_faults=dict(result_faults or {}), run_faults=list(run_faults),
                                       proto=proto.PROTO_DDR3)
        if protocol is not None:
            self.model.c["config"] = (protocol << 24) | (self.model.c["config"] & 0xFFFFFF)
        self.model.out.clear()            # powered up long ago: no H line is waiting
        self.faults = [dict(f, done=False) for f in faults]
        self.byte_timeout = byte_timeout
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)
        self.path = os.ttyname(self.slave)
        os.set_blocking(self.master, False)
        self.rx_log, self.tx_log = bytearray(), bytearray()
        self.frames = {}                  # command -> frames started by the host
        self.frame_count = 0
        self.lines = {}                   # (tag, index) -> lines produced
        self.pending = bytearray()
        self.lock = threading.Lock()
        self._frame = None                # (cmd, start position, expected length) of the frame being received
        self._stop = False
        self.byte_time = 10.0 / pace_baud if pace_baud else 0.0
        self.inq = collections.deque()   # paced: (arrival time, byte) of the host's bytes on the line
        self.rx_free = self.tx_free = 0.0
        self.tx_idle = True
        self.thread = threading.Thread(target=self._run_paced if pace_baud else self._run, daemon=True)
        self.thread.start()

    # ---- the host's byte stream ----
    def _track(self, position: int):
        """Frame boundaries of the host's stream (A5 5A cmd seq addr len), for the byte faults."""
        log = self.rx_log
        if self._frame and position >= self._frame[1] + self._frame[2]:
            self._frame = None
        if self._frame is None and position >= 2 and log[position - 2] == 0xA5 and log[position - 1] == 0x5A:
            cmd = log[position]
            self.frames[cmd] = self.frames.get(cmd, 0) + 1
            self.frame_count += 1
            self._frame = [cmd, position - 2, 14, self.frames[cmd]]
        elif self._frame and position == self._frame[1] + 9 and self._frame[0] in FRAME_PAYLOAD_CMDS:
            self._frame[2] = 14 + (log[position - 1] | (log[position] << 8))

    def _rx_fault(self, byte: int) -> int | None:
        position = len(self.rx_log) - 1
        if not self._frame:
            return byte
        cmd, start, _, nth = self._frame
        for fault in self.faults:
            if fault["done"] or fault["kind"] not in ("corrupt", "drop"):
                continue
            if ord(fault["cmd"]) == cmd and fault["nth"] == nth and position - start == fault["offset"]:
                fault["done"] = True
                return None if fault["kind"] == "drop" else byte ^ 1
        return byte

    def _receive(self, data: bytes):
        for byte in data:
            self.rx_log.append(byte)
            self._track(len(self.rx_log) - 1)
            got = self._rx_fault(byte)
            if got is not None:
                self.model.rx(got)
            self._flush()
            if self._frame and len(self.rx_log) - 1 == self._frame[1] + self._frame[2] - 1:
                for fault in self.faults:
                    if fault["kind"] == "reset" and not fault["done"] and self.frame_count == fault["after_frames"]:
                        fault["done"] = True
                        self.model.reset()
                        self._flush()

    # ---- the device's byte stream ----
    def _line_fault(self, item) -> bytes | None:
        tag = item[0]
        index = (item[1] >> 17) & 7 if tag in "AN" else None
        key = (tag, index)
        self.lines[key] = self.lines.get(key, 0) + 1
        self.lines[(tag, None)] = self.lines.get((tag, None), 0) + (index is not None)
        data = proto.expected_bytes(item)
        prefix = b""
        for fault in self.faults:
            if fault["done"] or fault.get("tag", fault.get("before")) != tag:
                continue
            if fault.get("index") is not None and fault["index"] != index:
                continue
            count = self.lines[key] if fault.get("index") is not None else self.lines[(tag, None)] if index is not None else self.lines[key]
            if count != fault["nth"]:
                continue
            fault["done"] = True
            if fault["kind"] == "drop_line":
                return prefix
            if fault["kind"] == "corrupt_line":
                digit = data[5]
                data = data[:5] + bytes([ord("0") if digit != ord("0") else ord("1")]) + data[6:]
            if fault["kind"] == "garbage":
                prefix += bytes.fromhex(fault["data"])
        return prefix + data

    def _flush(self):
        while self.model.out:
            item = self.model.out.pop(0)
            data = self._line_fault(item) if item[0] != "r" else item[1]
            self.pending += data
            self.tx_log += data

    def _run(self):
        last = time.monotonic()
        while not self._stop:
            timeout = 0.02
            in_frame = self.model.state not in ("hunt",)
            if in_frame:
                timeout = max(0.0, self.byte_timeout - (time.monotonic() - last))
            want_write = bool(self.pending)
            try:
                readable, writable, _ = select.select([self.master], [self.master] if want_write else [], [], timeout)
            except (OSError, ValueError):
                return
            with self.lock:
                if readable:
                    try:
                        data = os.read(self.master, 65536)
                    except BlockingIOError:
                        data = b""
                    except OSError:
                        data = b""
                    if data:
                        last = time.monotonic()
                        self._receive(data)
                if writable and self.pending:
                    try:
                        sent = os.write(self.master, bytes(self.pending[:4096]))
                        del self.pending[:sent]
                    except BlockingIOError:
                        pass
                    except OSError:
                        pass
                if not readable and self.model.state != "hunt" and time.monotonic() - last >= self.byte_timeout:
                    self.model.timeout()
                    self._flush()
                    last = time.monotonic()

    def _run_paced(self):
        last = time.monotonic()
        while not self._stop:
            now = time.monotonic()
            with self.lock:
                batch = bytearray()
                while self.inq and self.inq[0][0] <= now:
                    arrival, byte = self.inq.popleft()
                    batch.append(byte)
                    last = arrival
                if batch:
                    self._receive(bytes(batch))
                if not self.inq and self.model.state != "hunt" and now - last >= self.byte_timeout:
                    self.model.timeout()
                    self._flush()
                    last = now
            waits = [0.02]
            if self.inq:
                waits.append(max(0.0, self.inq[0][0] - now))
            elif self.model.state != "hunt":
                waits.append(max(0.0, self.byte_timeout - (now - last)))
            want_write = bool(self.pending)
            if want_write and not self.tx_idle and self.tx_free > now + self.byte_time:
                waits.append(self.tx_free - now - self.byte_time)
                want_write = False
            try:
                readable, writable, _ = select.select([self.master], [self.master] if want_write else [], [], min(waits))
            except (OSError, ValueError):
                return
            now = time.monotonic()
            if readable:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    data = b""
                for byte in data:
                    self.rx_free = max(self.rx_free, now) + self.byte_time
                    self.inq.append((self.rx_free, byte))
            if writable:
                with self.lock:
                    if self.pending:
                        if self.tx_idle:
                            self.tx_free = max(self.tx_free, now)
                            self.tx_idle = False
                        due = int((now - self.tx_free) / self.byte_time) + 1
                        count = max(1, min(len(self.pending), due, 512))
                        try:
                            sent = os.write(self.master, bytes(self.pending[:count]))
                        except OSError:
                            sent = 0
                        del self.pending[:sent]
                        self.tx_free += sent * self.byte_time
                    if not self.pending:
                        self.tx_idle = True

    def close(self):
        self._stop = True
        self.thread.join(timeout=2)
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
