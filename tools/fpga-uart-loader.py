#!/usr/bin/env python3
"""Host side of the AX7203 UART loader (issue #63): load a file into the board's
block-RAM store (part 1) or DDR3 (part 2) chunk by chunk with stop-and-wait, read it
back and compare, with optional fault injection, and write a
`trinity.uart-loader-capture.v2` record.

Protocol: docs/uart-loader.md, in Python tools/uart_loader_protocol.py (shared
with tests/test_uart_loader.py and tests/test_ddr3_loader.py). The designs:
fpga/ax7203/tms_uart_loader.v (block RAM, 25 MHz, protocol 2) and
fpga/ax7203/ddr3/tms_ddr3_loader_ax7203.v (DDR3, 83.33 MHz, protocol 3: pass
--design-hz 83333333.33 so that baud divisors are computed for its clock; the status
has 37 lines, read from the lines themselves).

One run, in this order, every step timed on one monotonic clock:

1. With --identity: IDCODE, DNA and XADC through openFPGALoader (read only);
   stop when the die is at or above --max-temp.
2. The port opens (and, with --open-glitch N, closes and opens N more times: the
   glitch a port can put on RX when it opens). With --bit, the bitstream is loaded
   into SRAM (never flash) while the port records, so the design's H line is kept.
3. Status (the device's counters before the run). With --wait-calib S (DDR3): while
   the status says the DDR3 calibration is not complete, ask again every 0.5 s for up to
   S seconds (every status read is kept in the record).
4. With --read-before: the range is read first, so the record shows how many bytes
   the load changed (a read-back of bytes the store already held proves nothing).
5. Load: --payload (a file) or --payload-tmem-trits (a file of signed-byte trits,
   packed into a TMEM v1 container, docs/format.md, with --tmem-codec: dense5 by
   default, or baseline2) goes to --addr in
   chunks of --chunk bytes. Each chunk is one load frame; the host waits for the
   ack with its sequence number and retransmits after a nak or when no reply came
   by --ack-timeout after the later of (write start + the frame's wire time) and
   (the moment the OS had taken the whole frame), after a quiet time that lets the
   device drop a partial frame. Fault injection, on the first transmission of the
   chunks listed: --corrupt (one payload bit flipped), --drop (one byte left out),
   --abort (half the frame, then the port is closed and opened again: a host that
   restarts mid-transfer), --garbage (random bytes on the line before the frame),
   --duplicate (the chunk sent again after its ack, as when an ack is lost).
6. Read-back of the whole range in --chunk-sized read frames, each CRC-checked
   and compared byte for byte with the payload. With --margin N the N bytes before and
   the N bytes after the range are read before the load and after the read-back and
   must be unchanged (a partial-word write must not touch its neighbours).
7. Status again; with --identity, XADC after (IDCODE and DNA are read before only).
8. With --baud-try R1,R2,...: for each rate a 'B' frame switches the device, the
   host follows, and a load plus read-back of --baud-bytes runs at that rate. Each
   trial loads the payload XOR a byte key that differs per trial and from 0, so every
   byte it writes differs from what the main transfer and the other trials left at
   that address (with --read-before the record counts them). Then a 'B' frame at the
   trial rate switches back; when its ack does not come, the host checks whether the
   device already runs at --baud (the ack was lost) and otherwise tries again at the
   trial rate. When the first switch is not acknowledged, the host returns to --baud
   and waits out the device's probation time (3 s in the board build): without a good
   frame at the new rate the device returns to its built-in rate by itself.

Timing. A reader thread takes every byte from the port as it arrives and stamps the
events it completes (response lines, read-back frames) with the time it read them;
the host never waits in a blocking drain (tcdrain) before it looks for a reply.
`queued` is when write() returned (the OS had taken every byte, which is not when
they were on the line); with --drain the host also waits for tcdrain and records
when it returned (`drained`), for comparison.

The record (--output, JSON) holds per chunk every attempt with its times and the
reply (and any reply to an earlier copy that came after its wait: `late_replies`),
the retransmits by reason, payload throughput of the load and read-back phases, and
the latency distributions; the raw bytes received are kept next to it
(<output>.rx.bin.gz, gzip -n) with their sha256.

  ../.venv-hw/bin/python tools/fpga-uart-loader.py --port /dev/cu.usbserial-110 \\
      --payload-tmem-trits build/fpga/loader-payload/qproj-rows0-319.trits --identity \\
      --read-before --output reports/fpga/uart-loader-<date>-<build>/run1.json

Exit status 0 when every chunk was acknowledged and read back identical (in every
baud trial as well), 1 otherwise, 2 when the run was stopped (temperature, port,
openFPGALoader).
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import random
import re
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
import uart_loader_protocol as proto  # noqa: E402

SCHEMA = "trinity.uart-loader-capture.v2"
DESIGN_HZ = 25_000_000                        # the block-RAM build; --design-hz for the DDR3 build
DEVICE_TIMEOUT_S = 1_250_000 / DESIGN_HZ      # the board build's inter-byte timeout (50 ms)
PROBATION_S = 75_000_000 / DESIGN_HZ          # the board build's baud probation (3 s)
OPENER = None                                 # tests put a fake port here


def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def distribution(values: list[float]) -> dict | None:
    if not values:
        return None
    ordered = sorted(values)

    def pct(p):
        k = (len(ordered) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)

    return {"n": len(values), "min": ordered[0], "p50": pct(0.5), "p90": pct(0.9), "p99": pct(0.99),
            "max": ordered[-1], "mean": statistics.fmean(values),
            "sd": statistics.stdev(values) if len(values) > 1 else 0.0}


class Link:
    """The serial port. A reader thread takes every byte as it arrives, feeds the
    stream decoder and stamps each completed event with the time it was read."""

    def __init__(self, port: str, baud: int, t0: float, opener=None):
        self.port_name, self.baud, self.t0 = port, baud, t0
        self.opener = opener or OPENER or self._open_serial
        self.decoder = proto.StreamDecoder()
        self.rx = bytearray()
        self.events: list[proto.Event] = []
        self.unread: list[proto.Event] = []
        self.tx_bytes = 0
        self.cond = threading.Condition()
        self.reader: threading.Thread | None = None
        self.reader_error: str | None = None
        self.port = self.opener(port, baud)
        self._start()

    @staticmethod
    def _open_serial(port, baud):
        import serial  # pyserial

        return serial.Serial(port, baud, timeout=0.005, rtscts=False, dsrdtr=False)

    def now(self) -> float:
        return time.monotonic() - self.t0

    def _start(self):
        self.stop_flag = threading.Event()
        self.reader = threading.Thread(target=self._read_loop, args=(self.port, self.stop_flag), daemon=True)
        self.reader.start()

    def _stop(self):
        if self.reader is not None:
            self.stop_flag.set()
            self.reader.join()
            self.reader = None

    def _read_loop(self, port, stop):
        while not stop.is_set():
            try:
                chunk = port.read(max(1, port.in_waiting or 0))
            except Exception as error:  # noqa: BLE001 - reported to the waiting side
                with self.cond:
                    self.reader_error = f"{type(error).__name__}: {error}"
                    self.cond.notify_all()
                return
            if chunk:
                t = round(self.now(), 6)
                with self.cond:
                    self.rx += chunk
                    new = self.decoder.feed(chunk, t)
                    self.events += new
                    self.unread += new
                    self.cond.notify_all()

    def reopen(self, baud: int | None = None):
        self._stop()
        self.port.close()
        if baud is not None:
            self.baud = baud
        self.port = self.opener(self.port_name, self.baud)
        self._start()

    def set_baud(self, baud: int):
        self._stop()
        self.baud = baud
        self.port.baudrate = baud
        self._start()

    def write(self, data: bytes, drain: bool = False) -> tuple[float, float, float | None]:
        """Send; return (start, queued, drained): write() called, write() returned,
        tcdrain returned (only with drain)."""
        start = self.now()
        self.port.write(data)
        queued = self.now()
        self.tx_bytes += len(data)
        drained = None
        if drain:
            self.port.flush()
            drained = self.now()
        return start, queued, drained

    def drain(self):
        """Wait until the OS has sent everything written (tcdrain)."""
        self.port.flush()

    def take(self, match, until: float) -> proto.Event | None:
        """The first unread event for which match(event) is true, waiting until the
        host time `until`; it and the unread events before it are consumed."""
        with self.cond:
            while True:
                for i, event in enumerate(self.unread):
                    if match(event):
                        del self.unread[:i + 1]
                        return event
                left = until - self.now()
                if left <= 0 or self.reader_error:
                    return None
                self.cond.wait(left)

    def stale(self, match) -> list[proto.Event]:
        """Remove and return the unread events for which match(event) is true."""
        with self.cond:
            out = [e for e in self.unread if match(e)]
            self.unread = [e for e in self.unread if not match(e)]
            return out

    def quiet(self, seconds: float):
        """Stay silent for `seconds`; the reader keeps reading (the device drops a
        partial frame after its inter-byte timeout and may send a nak for it)."""
        time.sleep(max(0.0, seconds))

    def close(self):
        self._stop()
        self.port.close()


def wire_s(nbytes: int, baud: int) -> float:
    return nbytes * 10 / baud


class Loader:
    def __init__(self, link: Link, args, rng: random.Random):
        self.link, self.args, self.rng = link, args, rng
        self.seq = args.first_seq & 255
        self.drain = bool(getattr(args, "drain", False))
        self.design_hz = getattr(args, "design_hz", None) or DESIGN_HZ

    def next_seq(self) -> int:
        seq = self.seq
        self.seq = (self.seq + 1) & 255
        return seq

    def guard(self):
        self.link.quiet(max(self.args.guard, 2.5 * DEVICE_TIMEOUT_S))

    def deadline(self, start: float, queued: float, nbytes: int, extra: float = 0.0) -> float:
        return max(start + wire_s(nbytes, self.link.baud), queued) + self.args.ack_timeout + extra

    @staticmethod
    def reply_of(event: proto.Event | None) -> dict | None:
        if event is None:
            return None
        if event.kind == "readback":
            return {"kind": "readback", "t": event.t, "seq": event.seq, "addr": event.addr, "len": len(event.data),
                    "crc_ok": event.crc_ok}
        out = {"kind": "line", "t": event.t, "tag": event.tag, "check_ok": event.check_ok}
        if event.tag in "AN":
            out.update(proto.decode_resp_word(event.a))
            out["frames_committed"], out["naks"] = event.v >> 16, event.v & 0xFFFF
        return out

    @staticmethod
    def times(start, queued, drained) -> dict:
        out = {"start": round(start, 6), "queued": round(queued, 6)}
        if drained is not None:
            out["drained"] = round(drained, 6)
        return out

    def status(self) -> dict | None:
        """The device's counters; the number of lines (23, or 37 on the DDR3 build) is read
        from the lines themselves (a >> 16)."""
        for _ in range(3):
            seq = self.next_seq()
            frame = proto.status_frame(seq)
            start, queued, _ = self.link.write(frame)
            until = self.deadline(start, queued, len(frame) + len(proto.STATUS_NAMES_DDR3) * proto.LINE_BYTES, 0.3)
            lines, count = [], None
            while count is None or len(lines) < count:
                event = self.link.take(lambda e, s=seq: e.kind == "line" and e.tag == "C" and e.a >> 24 == s, until)
                if event is None:
                    break
                if event.check_ok:
                    lines.append(event)
                    count = (event.a >> 16) & 0xFF
            if count is not None and len(lines) == count and {e.a & 0xFFFF for e in lines} == set(range(count)):
                return proto.decode_status(lines)
            self.link.drain()
            self.guard()
        return None

    @staticmethod
    def _matches(seq, cmd, since=None):
        def match(event):
            if event.kind != "line" or event.tag not in "AN" or not event.check_ok:
                return False
            if since is not None and event.t is not None and event.t < since:
                return False
            word = proto.decode_resp_word(event.a)
            return word["seq_known"] and word["seq"] == seq and word["cmd"] == cmd
        return match

    def load_chunk(self, index: int, addr: int, payload: bytes, inject: set[str]) -> dict:
        seq = self.next_seq()
        frame = proto.load_frame(seq, addr, payload)
        record = {"index": index, "seq": seq, "addr": addr, "len": len(payload), "attempts": [], "injected": sorted(inject)}
        for attempt in range(self.args.max_attempts):
            if attempt:
                # Every byte of the previous copy has left, the device has dropped any
                # partial frame; a reply to an earlier copy that came after its wait is
                # recorded with that copy and not taken for this one.
                self.link.drain()
                self.guard()
                late = self.link.stale(self._matches(seq, proto.CMD_LOAD))
                if late:
                    record["attempts"][-1]["late_replies"] = [self.reply_of(e) for e in late]
            tx, note = frame, None
            if attempt == 0 and "garbage" in inject:
                junk = bytes(self.rng.getrandbits(8) for _ in range(self.args.garbage_bytes))
                self.link.write(junk)
                self.link.drain()
                self.guard()
                note = f"{len(junk)} random bytes before the frame"
            if attempt == 0 and "corrupt" in inject:
                k = proto.HEADER_BYTES + self.rng.randrange(len(payload))
                bad = bytearray(frame)
                bad[k] ^= 1 << self.rng.randrange(8)
                tx, note = bytes(bad), f"bit flipped in frame byte {k}"
            elif attempt == 0 and "drop" in inject:
                k = proto.HEADER_BYTES + self.rng.randrange(len(payload))
                tx, note = frame[:k] + frame[k + 1:], f"frame byte {k} left out"
            elif attempt == 0 and "abort" in inject:
                k = len(frame) // 2
                start, queued, _ = self.link.write(frame[:k])
                self.link.drain()
                self.link.reopen()
                note = f"first {k} of {len(frame)} bytes, then the port closed and opened again"
                reply = self.link.take(self._matches(seq, proto.CMD_LOAD, start),
                                       self.link.now() + self.args.ack_timeout + 3 * DEVICE_TIMEOUT_S)
                record["attempts"].append({**self.times(start, queued, None), "note": note,
                                           "reply": self.reply_of(reply)})
                continue
            start, queued, drained = self.link.write(tx, self.drain)
            reply = self.link.take(self._matches(seq, proto.CMD_LOAD, start), self.deadline(start, queued, len(tx)))
            entry = {**self.times(start, queued, drained), "reply": self.reply_of(reply)}
            if note:
                entry["note"] = note
            record["attempts"].append(entry)
            if reply is not None and reply.tag == "A":
                record["acked"] = True
                record["latency_s"] = round(reply.t - start, 6)
                record["chunk_s"] = round(reply.t - record["attempts"][0]["start"], 6)
                record["reason"] = proto.decode_resp_word(reply.a)["reason_name"]
                record["ack_t"] = reply.t
                break
        else:
            record["acked"] = False
        if record.get("acked") and "duplicate" in inject:
            start, queued, _ = self.link.write(frame)
            reply = self.link.take(self._matches(seq, proto.CMD_LOAD, start), self.deadline(start, queued, len(frame)))
            record["duplicate"] = {**self.times(start, queued, None), "reply": self.reply_of(reply)}
        return record

    def read_chunk(self, addr: int, length: int) -> dict:
        record = {"addr": addr, "len": length, "attempts": []}
        for attempt in range(self.args.max_attempts):
            if attempt:
                self.link.drain()
                self.guard()
            seq = self.next_seq()
            request = proto.read_frame(seq, addr, length)
            start, queued, _ = self.link.write(request)

            def match(event, s=seq, since=start):
                if event.kind == "readback":
                    return event.seq == s and (event.t is None or event.t >= since)
                return self._matches(s, proto.CMD_READ, since)(event)

            reply = self.link.take(match, self.deadline(start, queued, len(request) + length + proto.FRAME_OVERHEAD))
            record["attempts"].append({"seq": seq, **self.times(start, queued, None), "reply": self.reply_of(reply)})
            if reply is not None and reply.kind == "readback" and reply.crc_ok and reply.addr == addr \
                    and len(reply.data) == length:
                record["latency_s"] = round(reply.t - start, 6)
                record["data"] = reply.data
                return record
        return record

    def set_rate(self, baud: int) -> dict:
        hz = self.design_hz
        divisor = round(hz / baud)
        seq = self.next_seq()
        frame = proto.baud_frame(seq, divisor)
        start, queued, _ = self.link.write(frame)
        reply = self.link.take(self._matches(seq, proto.CMD_BAUD, start),
                               self.deadline(start, queued, len(frame) + proto.LINE_BYTES))
        out = {"baud": baud, "divisor": divisor, "device_baud": hz / divisor,
               "error_ppm": round((hz / divisor / baud - 1) * 1e6), "at_baud": self.link.baud,
               "reply": self.reply_of(reply)}
        if reply is None or reply.tag != "A":
            return out
        # The device changes its rate once the ack line has left its transmitter; the
        # ack has arrived here, so it has changed.
        self.link.quiet(0.02)
        self.link.set_baud(baud)
        self.link.quiet(0.05)
        out["switched"] = True
        return out


def load_payload(args) -> tuple[bytes, dict]:
    if args.payload_tmem_trits:
        path = Path(args.payload_tmem_trits)
        raw = path.read_bytes()
        trits = [b - 256 if b > 127 else b for b in raw]
        from trinity_memory import container

        codec = getattr(args, "tmem_codec", None) or "dense5"
        data = container.encode_file(trits, codec)
        info = {"kind": f"TMEM v1 {codec} container (docs/format.md) of the trits in the source file", "codec": codec,
                "source": str(path.resolve().relative_to(ROOT)) if path.resolve().is_relative_to(ROOT) else str(path),
                "source_sha256": sha256(raw), "trits": len(trits),
                "counts": {"+1": trits.count(1), "0": trits.count(0), "-1": trits.count(-1)},
                "tmem_header_hex": data[:24].hex()}
        sidecar = path.with_suffix(".json")
        if sidecar.is_file():
            info["source_sidecar"] = json.loads(sidecar.read_text())
        # The container must round-trip before it is used.
        if container.decode_file(data) != trits:
            raise SystemExit("TMEM round trip failed")
    else:
        data = Path(args.payload).read_bytes()
        info = {"kind": "file", "source": args.payload}
    if args.length:
        data = data[:args.length]
    info.update(bytes=len(data), sha256=sha256(data))
    return data, info


def run_identity(args, t0, name):
    loader = [args.openfpgaloader, "-c", args.cable]
    out = {}
    for key, extra in (("idcode", ["--detect"]), ("dna", ["--read-dna"]), ("xadc", ["--read-xadc"])):
        if name == "after" and key != "xadc":
            continue      # IDCODE and DNA do not change during a run; only the temperature is read again
        start = time.monotonic() - t0
        proc = subprocess.run(loader + extra, capture_output=True, text=True)
        text = proc.stdout + proc.stderr
        entry = {"cmd": loader + extra, "start_s": round(start, 3), "returncode": proc.returncode,
                 "output": text.strip().splitlines()[-12:]}
        if proc.returncode == 0:
            if key == "idcode":
                m = re.search(r"idcode\s+(0x[0-9a-fA-F]+)", text)
                entry["value"] = m.group(1).lower() if m else None
            elif key == "dna":
                m = re.search(r'"dna"\s*:\s*"(0x[0-9a-fA-F]+)"', text)
                entry["value"] = m.group(1).lower() if m else None
            else:
                body = text[text.find("{"):text.rfind("}") + 1]
                try:
                    entry["value"] = json.loads(re.sub(r",\s*}", "}", body))
                except ValueError:
                    entry["value"] = None
        out[key] = entry
    return out


def tool_revision() -> dict:
    def git(*cmd):
        proc = subprocess.run(["git", "-C", str(ROOT), *cmd], capture_output=True, text=True)
        return proc.stdout.strip() if proc.returncode == 0 else None
    status = git("status", "--porcelain", "--untracked-files=no")
    return {"head": git("rev-parse", "HEAD"), "tracked_changes": None if status is None else bool(status)}


def parse_chunks(text: str) -> set[int]:
    return {int(x) for x in text.split(",") if x.strip()} if text else set()


def read_range(loader: Loader, addr: int, length: int, chunk: int) -> tuple[bytes, list[dict]]:
    reads, got = [], bytearray()
    for offset in range(0, length, chunk):
        record = loader.read_chunk(addr + offset, min(chunk, length - offset))
        got += record.pop("data", b"")
        reads.append(record)
        if "latency_s" not in record:
            break
    return bytes(got), reads


def read_margins(loader: Loader, addr: int, length: int, margin: int, chunk: int) -> dict:
    """The `margin` bytes before and after [addr, addr + length) (clipped at 0)."""
    lo = max(0, addr - margin)
    before, _ = read_range(loader, lo, addr - lo, chunk) if addr > lo else (b"", [])
    after, _ = read_range(loader, addr + length, margin, chunk) if margin else (b"", [])
    return {"below": [lo, addr - lo], "above": [addr + length, margin], "below_bytes": before, "above_bytes": after,
            "complete": len(before) == addr - lo and len(after) == margin}


def transfer(loader: Loader, data: bytes, addr: int, chunk: int, faults: dict[str, set[int]],
             read_before: bool = False, margin: int = 0) -> dict:
    link = loader.link
    out: dict = {}
    margins_before = read_margins(loader, addr, len(data), margin, chunk) if margin else None
    if read_before:
        t_before = link.now()
        before, before_reads = read_range(loader, addr, len(data), chunk)
        complete = len(before) == len(data)
        out["store_before"] = {
            "read_s": round(link.now() - t_before, 6), "bytes_read": len(before), "complete": complete,
            "sha256": sha256(before),
            "bytes_the_load_changes": sum(1 for a, b in zip(before, data) if a != b) if complete else None,
            "note": "the range read before the load; bytes_the_load_changes counts the payload bytes that "
                    "differ from it, so a read-back identical to the payload shows that many bytes landed",
            "reads": before_reads}
    records = []
    t_start = link.now()
    for index, offset in enumerate(range(0, len(data), chunk)):
        inject = {name for name, chunks in faults.items() if index in chunks}
        records.append(loader.load_chunk(index, addr + offset, data[offset:offset + chunk], inject))
        if not records[-1].get("acked"):
            break
    t_load = link.now()
    got, reads = read_range(loader, addr, len(data), chunk)
    t_read = link.now()
    mismatch = next((i for i, (a, b) in enumerate(zip(got, data)) if a != b), None)
    if margin:
        margins_after = read_margins(loader, addr, len(data), margin, chunk)
        out["margins"] = {
            "bytes_each_side": margin, "below": margins_before["below"], "above": margins_before["above"],
            "complete": margins_before["complete"] and margins_after["complete"],
            "unchanged": margins_before["complete"] and margins_after["complete"]
            and margins_before["below_bytes"] == margins_after["below_bytes"]
            and margins_before["above_bytes"] == margins_after["above_bytes"],
            "below_sha256": sha256(margins_before["below_bytes"]), "above_sha256": sha256(margins_before["above_bytes"]),
            "note": "the bytes next to the range, read before the load and after the read-back: a write with byte "
                    "selects must leave the other bytes of its first and last words as they were"}
    retransmits: dict[str, int] = {}
    for record in records:
        for attempt in record["attempts"][:-1] if record.get("acked") else record["attempts"]:
            reply = attempt.get("reply")
            reason = "no_reply" if reply is None else (reply.get("reason_name") or reply.get("tag"))
            retransmits[reason] = retransmits.get(reason, 0) + 1
    acked = [r for r in records if r.get("acked")]
    payload = sum(r["len"] for r in acked)
    single = [r for r in acked if len(r["attempts"]) == 1]
    full = [r for r in single if r["len"] == chunk]
    gaps = [round(b["attempts"][0]["start"] - a["ack_t"], 6) for a, b in zip(records, records[1:])
            if a.get("acked") and not b["injected"] and not a["injected"]]
    out.update({
        "chunks": len(records), "acked": len(acked),
        "payload_bytes": payload, "load_s": round(t_load - t_start, 6),
        "load_payload_Bps": round(payload / (t_load - t_start), 1) if t_load > t_start else None,
        "readback_bytes": len(got), "readback_s": round(t_read - t_load, 6),
        "readback_payload_Bps": round(len(got) / (t_read - t_load), 1) if t_read > t_load else None,
        "identical": got == data, "first_mismatch": mismatch,
        "retransmits": retransmits, "attempts": sum(len(r["attempts"]) for r in records),
        "late_replies": sum(len(a.get("late_replies", [])) for r in records for a in r["attempts"]),
        "latency_s": distribution([r["latency_s"] for r in acked]),
        "latency_note": "per acknowledged chunk: from the start of the host's write of the transmission that was "
                        "acknowledged to the arrival of the ack line (the reader thread's time stamp); chunk_s also "
                        "counts the failed attempts before it",
        "chunk_s": distribution([r["chunk_s"] for r in acked]),
        "turnaround_s": distribution([r["latency_s"] - wire_s(r["len"] + proto.FRAME_OVERHEAD, link.baud) for r in full]),
        "turnaround_note": "per full-size chunk sent once: ack arrival minus write start minus the frame's wire time at "
                           "the nominal rate (10 bits per byte): gaps the host left on the line, the device (commit, "
                           "the 20-byte ack line) and USB and driver latency on the way back",
        "queued_s": distribution([r["attempts"][0]["queued"] - r["attempts"][0]["start"] for r in full]),
        "queued_note": "per full-size chunk sent once: write() call to return (the OS has taken every byte)",
        "drained_s": distribution([r["attempts"][0]["drained"] - r["attempts"][0]["start"] for r in full
                                   if "drained" in r["attempts"][0]]),
        "drained_note": "with --drain: write() call to the return of tcdrain (flush), for comparison with the ack",
        "gap_s": distribution(gaps),
        "gap_note": "ack arrival to the start of the next chunk's write (host loop), chunks without injected faults",
        "ideal_payload_Bps": round(link.baud / 10 * chunk / (chunk + proto.FRAME_OVERHEAD), 1),
        "per_chunk": records, "reads": reads,
    })
    return out


def calib_ok(status: dict | None) -> bool | None:
    """DDR3 build: calibration complete, state and highest state 23 (DONE_CALIBRATE), no
    return to IDLE, and no clock since the calibration with calib_complete low or another
    state. None on a build without these lines."""
    if not status or "calib" not in status:
        return None
    word = proto.decode_calib(status["calib"])
    return (word["calib_complete"], word["state"], word["highest"], word["returns_to_idle"]) == (1, 23, 23, 0) \
        and status["calib_lost_clocks"] == 0


def wait_calibrated(loader: Loader, seconds: float) -> list[dict]:
    """Status reads every 0.5 s until the DDR3 calibration is complete or `seconds` passed."""
    polls = []
    end = loader.link.now() + seconds
    while True:
        status = loader.status()
        polls.append({"t": round(loader.link.now(), 3), "status": status})
        if status is None or "calib" not in status or proto.decode_calib(status["calib"])["calib_complete"]:
            return polls
        if loader.link.now() >= end:
            return polls
        loader.link.quiet(0.5)


def trial_key(index: int) -> int:
    """The byte every trial's payload is XORed with: non-zero and different per trial."""
    return (0x5B * (index + 1)) & 0xFF or 0x01


def back_to_default(loader: Loader, args, rate: int) -> dict:
    """Switch the device from `rate` back to --baud: a 'B' frame at `rate`; when its ack
    does not come, a status at --baud shows whether the device switched anyway (its
    ack was lost); if not, again at `rate`."""
    link = loader.link
    attempts = []
    for _ in range(3):
        back = loader.set_rate(args.baud)
        attempts.append(back)
        if back.get("switched"):
            return {"attempts": attempts, "switched": True}
        link.drain()
        loader.guard()
        link.set_baud(args.baud)
        probe = loader.status()
        attempts.append({"probe_status_at": args.baud, "answered": probe is not None})
        if probe is not None:
            return {"attempts": attempts, "switched": True, "ack_lost": True}
        link.set_baud(rate)
    return {"attempts": attempts, "switched": False}


def baud_trials(loader: Loader, args, data: bytes) -> list[dict]:
    link = loader.link
    trials = []
    for index, rate in enumerate(int(x) for x in args.baud_try.split(",") if x.strip()):
        trial: dict = {"rate": rate}
        trials.append(trial)
        trial["switch"] = loader.set_rate(rate)
        if not trial["switch"].get("switched"):
            # The 'B' frame or its ack was lost. Without a good frame at the new rate the
            # device returns to its built-in rate after its probation time.
            link.set_baud(args.baud)
            link.quiet(PROBATION_S + 1.0)
            trial["fell_back"] = True
            trial["status_after"] = loader.status()
            if trial["status_after"] is None:
                trial["device_lost"] = True
                break
            continue
        trial["status"] = loader.status()
        if trial["status"] is not None:
            key = trial_key(index)
            size = min(args.baud_bytes, len(data))
            pattern = bytes(b ^ key for b in data[:size])
            trial["pattern"] = {"xor": key, "bytes": size, "sha256": sha256(pattern),
                                "note": "the payload's first bytes XOR this key, so that every byte written "
                                        "differs from the main transfer's and the other trials' at that address"}
            trial["transfer"] = transfer(loader, pattern, args.addr, args.chunk, {}, read_before=args.read_before,
                                         margin=args.margin)
        trial["back"] = back_to_default(loader, args, rate)
        if not trial["back"]["switched"]:
            # A good frame at the trial rate ended the device's probation: it stays there
            # until a 'B' frame or reset. Wait in case it did not, then look.
            link.set_baud(args.baud)
            link.quiet(PROBATION_S + 1.0)
            trial["fell_back"] = True
        trial["status_after"] = loader.status()
        if trial["status_after"] is None:
            trial["device_lost"] = True
            break
    return trials


def trial_ok(trial: dict) -> bool:
    t = trial.get("transfer") or {}
    return bool(trial.get("switch", {}).get("switched") and t and t.get("acked") == t.get("chunks")
                and t.get("identical") and trial.get("back", {}).get("switched")
                and trial.get("status_after") is not None)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", required=True)
    parser.add_argument("--baud", type=int, default=115200)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--payload", help="file to load")
    src.add_argument("--payload-tmem-trits", help="signed-byte trits (tools/extract-bram-trits.py), sent as TMEM dense5")
    parser.add_argument("--tmem-codec", default="dense5", choices=["dense5", "baseline2"],
                        help="the TMEM container's codec for --payload-tmem-trits")
    parser.add_argument("--length", type=int, default=0, help="load only the first N bytes")
    parser.add_argument("--addr", type=lambda x: int(x, 0), default=0)
    parser.add_argument("--chunk", type=int, default=proto.MAX_LEN)
    parser.add_argument("--first-seq", type=int, default=1)
    parser.add_argument("--ack-timeout", type=float, default=0.5,
                        help="seconds after the later of (write start + wire time) and write() returning")
    parser.add_argument("--guard", type=float, default=0.15, help="quiet seconds before a retransmission")
    parser.add_argument("--max-attempts", type=int, default=6)
    parser.add_argument("--corrupt", default="", help="chunk indices, comma-separated")
    parser.add_argument("--drop", default="")
    parser.add_argument("--abort", default="")
    parser.add_argument("--garbage", default="")
    parser.add_argument("--garbage-bytes", type=int, default=64)
    parser.add_argument("--duplicate", default="")
    parser.add_argument("--read-before", action="store_true", help="read the range before loading it")
    parser.add_argument("--margin", type=int, default=0,
                        help="read N bytes on each side of the range before and after, which must not change")
    parser.add_argument("--design-hz", type=float, default=DESIGN_HZ,
                        help="the design clock (25e6 block RAM, 83333333.33 DDR3): baud divisors of --baud-try")
    parser.add_argument("--wait-calib", type=float, default=0.0,
                        help="DDR3: wait up to S seconds for the calibration before loading")
    parser.add_argument("--drain", action="store_true",
                        help="wait for tcdrain after each load frame and record when it returned")
    parser.add_argument("--open-glitch", type=int, default=0, help="close and reopen the port N times first")
    parser.add_argument("--baud-try", default="", help="rates to try after the main run, comma-separated")
    parser.add_argument("--baud-bytes", type=int, default=16384)
    parser.add_argument("--bit", help="load this bitstream into SRAM first (openFPGALoader, never flash)")
    parser.add_argument("--identity", action="store_true", help="IDCODE, DNA and XADC before, XADC after")
    parser.add_argument("--max-temp", type=float, default=80.0)
    parser.add_argument("--openfpgaloader", default="openFPGALoader")
    parser.add_argument("--cable", default="digilent_hs2")
    parser.add_argument("--seed", type=int, default=63)
    parser.add_argument("--label", default="")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    rng = random.Random(args.seed)
    data, payload_info = load_payload(args)
    faults = {name: parse_chunks(getattr(args, name)) for name in ("corrupt", "drop", "abort", "garbage", "duplicate")}
    t0 = time.monotonic()
    record = {"schema": SCHEMA, "started_utc": utc(), "label": args.label, "argv": sys.argv if argv is None else argv,
              "tool_revision": tool_revision(), "port": args.port, "baud": args.baud, "payload": payload_info,
              "addr": args.addr, "chunk": args.chunk, "faults": {k: sorted(v) for k, v in faults.items()},
              "design_hz": args.design_hz, "t0_note": "every time is host seconds after the start on one monotonic clock; "
              "a reply's t is when the reader thread read its last byte"}
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    status_code = 0
    if args.identity:
        record["identity_before"] = run_identity(args, t0, "before")
        temp = (record["identity_before"].get("xadc", {}).get("value") or {}).get("temp")
        if any(v.get("returncode") for v in record["identity_before"].values()) or temp is None:
            record["stopped"] = "openFPGALoader failed (cable not found?)"
            status_code = 2
        elif temp >= args.max_temp:
            record["stopped"] = f"die at {temp} C (limit {args.max_temp} C)"
            status_code = 2
    link = None
    if not status_code:
        try:
            link = Link(args.port, args.baud, t0)
            for _ in range(args.open_glitch):
                link.reopen()
            if args.bit:
                bit = Path(args.bit)
                record["bitstream"] = {"path": str(bit), "sha256": sha256(bit.read_bytes())}
                proc = subprocess.Popen([args.openfpgaloader, "-c", args.cable, str(bit)], stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True)
                output = proc.communicate()[0]
                record["bitstream"]["load_output"] = output.strip().splitlines()[-6:]
                record["bitstream"]["load_returncode"] = proc.returncode
                link.quiet(0.5)
                if proc.returncode:
                    raise RuntimeError("openFPGALoader failed")
            else:
                link.quiet(0.3)
            loader = Loader(link, args, rng)
            record["status_before"] = loader.status()
            if args.wait_calib and record["status_before"] and "calib" in record["status_before"] \
                    and not proto.decode_calib(record["status_before"]["calib"])["calib_complete"]:
                record["calib_polls"] = wait_calibrated(loader, args.wait_calib)
                record["status_before"] = record["calib_polls"][-1]["status"]
            record["transfer"] = transfer(loader, data, args.addr, args.chunk, faults, read_before=args.read_before,
                                          margin=args.margin)
            record["status_after"] = loader.status()
            if args.baud_try:
                record["baud_trials"] = baud_trials(loader, args, data)
        except Exception as error:  # noqa: BLE001 - recorded, run stopped
            record["stopped"] = f"{type(error).__name__}: {error}"
            status_code = 2
        finally:
            if link is not None:
                link.quiet(0.2)
                link.close()
                if link.reader_error:
                    record["reader_error"] = link.reader_error
    if args.identity and "identity_before" in record and status_code != 2:
        record["identity_after"] = run_identity(args, t0, "after")
    if link is not None:
        record["device_events"] = [e.as_dict() for e in link.events if e.kind != "readback"]
        record["device_hello"] = next((e.as_dict() for e in link.events if e.kind == "line" and e.tag == "H"), None)
        raw = bytes(link.rx)
        rx_path = out.with_name(out.name + ".rx.bin.gz")
        rx_path.write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))
        record["rx_raw"] = {"file": rx_path.name, "bytes": len(raw), "sha256_uncompressed": sha256(raw),
                            "tx_bytes": link.tx_bytes}
    record["ended_utc"] = utc()
    transfer_record = record.get("transfer") or {}
    checks = {"all_chunks_acked": transfer_record.get("acked") == transfer_record.get("chunks") and bool(transfer_record),
              "read_back_identical": bool(transfer_record.get("identical")),
              "status_read": record.get("status_before") is not None and record.get("status_after") is not None}
    if args.margin:
        checks["margins_unchanged"] = bool((transfer_record.get("margins") or {}).get("unchanged"))
    before, after = record.get("status_before"), record.get("status_after")
    if before and "calib" in before:
        # DDR3: calibrated before and after the transfer, the same calibration (no reset between:
        # the clock count at calib_complete is the same), and not one clock since it with
        # calib_complete low or a state other than 23 (counted by the device every clock).
        checks["calibration_held"] = bool(calib_ok(before) and calib_ok(after)
                                          and before["calib_clocks"] == after["calib_clocks"] != 0)
    if args.baud_try:
        trials = record.get("baud_trials") or []
        wanted = len([x for x in args.baud_try.split(",") if x.strip()])
        checks["baud_trials_all_ok"] = len(trials) == wanted and all(trial_ok(t) for t in trials)
    record["checks"] = checks
    record["pass"] = all(checks.values()) and status_code == 0
    out.write_text(json.dumps(record, indent=1, default=lambda b: b.hex() if isinstance(b, bytes) else str(b)) + "\n")
    summary = {k: transfer_record.get(k) for k in ("chunks", "acked", "payload_bytes", "load_s", "load_payload_Bps",
                                                   "readback_payload_Bps", "identical", "retransmits")}
    print(json.dumps({"pass": record["pass"], "stopped": record.get("stopped"), "checks": checks, **summary}, indent=1))
    if status_code:
        return status_code
    return 0 if record["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
