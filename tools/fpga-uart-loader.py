#!/usr/bin/env python3
"""Host side of the AX7203 UART loader (issue #63): load a file into the board's
block-RAM store chunk by chunk with stop-and-wait, read it back and compare, with
optional fault injection, and write a `trinity.uart-loader-capture.v1` record.

Protocol: docs/uart-loader.md, in Python tools/uart_loader_protocol.py (shared
with tests/test_uart_loader.py). The design: fpga/ax7203/tms_uart_loader.v.

One run, in this order, every step timed on one monotonic clock:

1. With --identity: IDCODE, DNA and XADC through openFPGALoader (read only);
   stop when the die is at or above --max-temp.
2. The port opens (and, with --open-glitch N, closes and opens N more times: the
   glitch a port can put on RX when it opens). With --bit, the bitstream is loaded
   into SRAM (never flash) while the port records, so the design's H line is kept.
3. Status (the device's counters before the run).
4. Load: --payload (a file) or --payload-tmem-trits (a file of signed-byte trits,
   packed into a TMEM v1 dense5 container, docs/format.md) goes to --addr in
   chunks of --chunk bytes. Each chunk is one load frame; the host waits for the
   ack with its sequence number and retransmits after a nak or when no ack came
   within the frame's wire time plus --ack-timeout, after a quiet time that lets
   the device drop a partial frame. Fault injection, on the first transmission of
   the chunks listed: --corrupt (one payload bit flipped), --drop (one byte left
   out), --abort (half the frame, then the port is closed and opened again: a host
   that restarts mid-transfer), --garbage (random bytes on the line before the
   frame), --duplicate (the chunk sent again after its ack, as when an ack is lost).
5. Read-back of the whole range in --chunk-sized read frames, each CRC-checked
   and compared byte for byte with the payload.
6. Status again; with --identity, XADC after.
7. With --baud-try R1,R2,...: for each rate a 'B' frame switches the device, the
   host follows, and a load plus read-back of --baud-bytes runs at that rate; then
   the device is switched back (or, if the new rate failed, it returns by itself
   after its probation time, 3 s in the board build).

The record (--output, JSON) holds per chunk every attempt with its send and ack
times and the reply, the retransmits by reason, payload throughput of the load and
read-back phases, and the latency distribution; the raw bytes received are kept
next to it (<output>.rx.bin.gz, gzip -n) with their sha256.

  ../.venv-hw/bin/python tools/fpga-uart-loader.py --port /dev/cu.usbserial-110 \\
      --payload-tmem-trits build/fpga/loader-payload/qproj-rows0-319.trits --identity \\
      --output reports/fpga/uart-loader-<date>-<build>/run1.json

Exit status 0 when every chunk was acknowledged and read back identical, 1
otherwise, 2 when the run was stopped (temperature, port, openFPGALoader).
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
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
import uart_loader_protocol as proto  # noqa: E402

SCHEMA = "trinity.uart-loader-capture.v1"
DESIGN_HZ = 25_000_000
DEVICE_TIMEOUT_S = 1_250_000 / DESIGN_HZ      # the board build's inter-byte timeout (50 ms)
PROBATION_S = 75_000_000 / DESIGN_HZ          # the board build's baud probation (3 s)


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
    """The serial port with a timestamped decoder of what the device sends."""

    def __init__(self, port: str, baud: int, t0: float, opener=None):
        self.port_name, self.baud, self.t0 = port, baud, t0
        self.opener = opener or self._open_serial
        self.port = self.opener(port, baud)
        self.decoder = proto.StreamDecoder()
        self.rx = bytearray()
        self.events: list[proto.Event] = []
        self.unread: list[proto.Event] = []
        self.tx_bytes = 0

    @staticmethod
    def _open_serial(port, baud):
        import serial  # pyserial

        return serial.Serial(port, baud, timeout=0.002, rtscts=False, dsrdtr=False)

    def now(self) -> float:
        return time.monotonic() - self.t0

    def reopen(self, baud: int | None = None):
        self.port.close()
        if baud is not None:
            self.baud = baud
        self.port = self.opener(self.port_name, self.baud)

    def set_baud(self, baud: int):
        self.baud = baud
        self.port.baudrate = baud

    def write(self, data: bytes) -> tuple[float, float]:
        start = self.now()
        self.port.write(data)
        self.port.flush()
        self.tx_bytes += len(data)
        return start, self.now()

    def poll(self, seconds: float = 0.0) -> list[proto.Event]:
        """Read for up to `seconds` (at least once); return the new events."""
        end = time.monotonic() + seconds
        new: list[proto.Event] = []
        while True:
            chunk = self.port.read(max(1, getattr(self.port, "in_waiting", 0) or 1))
            if chunk:
                self.rx += chunk
                events = self.decoder.feed(chunk, round(self.now(), 6))
                new += events
            elif time.monotonic() >= end:
                break
        self.events += new
        self.unread += new
        return new

    def take(self, match, timeout: float) -> proto.Event | None:
        """The first unread event for which match(event) is true, waiting up to `timeout`."""
        end = time.monotonic() + timeout
        while True:
            for i, event in enumerate(self.unread):
                if match(event):
                    del self.unread[:i + 1]
                    return event
            if time.monotonic() >= end:
                return None
            self.poll(0.01)

    def quiet(self, seconds: float):
        """Stay silent for `seconds` and keep reading (the device drops a partial frame
        after its inter-byte timeout and may send a nak for it)."""
        self.poll(seconds)

    def close(self):
        self.port.close()


def wire_s(nbytes: int, baud: int) -> float:
    return nbytes * 10 / baud


class Loader:
    def __init__(self, link: Link, args, rng: random.Random):
        self.link, self.args, self.rng = link, args, rng
        self.seq = args.first_seq & 255
        self.log: list[dict] = []

    def next_seq(self) -> int:
        seq = self.seq
        self.seq = (self.seq + 1) & 255
        return seq

    def guard(self):
        self.link.quiet(max(self.args.guard, 2.5 * DEVICE_TIMEOUT_S))

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

    def status(self) -> dict | None:
        for _ in range(3):
            seq = self.next_seq()
            self.link.write(proto.status_frame(seq))
            lines = []
            end = time.monotonic() + wire_s(22 * 20 + 14, self.link.baud) + self.args.ack_timeout + 0.3
            while len(lines) < len(proto.STATUS_NAMES) and time.monotonic() < end:
                event = self.link.take(lambda e, s=seq: e.kind == "line" and e.tag == "C" and e.a >> 24 == s,
                                       max(0.0, end - time.monotonic()))
                if event is None:
                    break
                if event.check_ok:
                    lines.append(event)
            if len(lines) == len(proto.STATUS_NAMES):
                return proto.decode_status(lines)
            self.guard()
        return None

    def load_chunk(self, index: int, addr: int, payload: bytes, inject: set[str]) -> dict:
        seq = self.next_seq()
        frame = proto.load_frame(seq, addr, payload)
        record = {"index": index, "seq": seq, "addr": addr, "len": len(payload), "attempts": [], "injected": sorted(inject)}
        for attempt in range(self.args.max_attempts):
            tx, note = frame, None
            if attempt == 0 and "garbage" in inject:
                junk = bytes(self.rng.getrandbits(8) for _ in range(self.args.garbage_bytes))
                self.link.write(junk)
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
                start, written = self.link.write(frame[:k])
                self.link.reopen()
                note = f"first {k} of {len(frame)} bytes, then the port closed and opened again"
                reply = self.link.take(self._matches(seq, proto.CMD_LOAD), self.args.ack_timeout + 3 * DEVICE_TIMEOUT_S)
                record["attempts"].append({"start": round(start, 6), "written": round(written, 6), "note": note,
                                           "reply": self.reply_of(reply)})
                self.guard()
                continue
            start, written = self.link.write(tx)
            wait = wire_s(len(tx), self.link.baud) + self.args.ack_timeout
            reply = self.link.take(self._matches(seq, proto.CMD_LOAD), wait)
            entry = {"start": round(start, 6), "written": round(written, 6), "reply": self.reply_of(reply)}
            if note:
                entry["note"] = note
            record["attempts"].append(entry)
            if reply is not None and reply.tag == "A":
                record["acked"] = True
                record["latency_s"] = round(reply.t - start, 6)
                record["chunk_s"] = round(reply.t - record["attempts"][0]["start"], 6)
                record["reason"] = proto.REASONS[proto.decode_resp_word(reply.a)["reason"]]
                break
            self.guard()
        else:
            record["acked"] = False
        if record.get("acked") and "duplicate" in inject:
            start, written = self.link.write(frame)
            reply = self.link.take(self._matches(seq, proto.CMD_LOAD), wire_s(len(frame), self.link.baud) + self.args.ack_timeout)
            record["duplicate"] = {"start": round(start, 6), "reply": self.reply_of(reply)}
        return record

    @staticmethod
    def _matches(seq, cmd):
        def match(event):
            if event.kind != "line" or event.tag not in "AN" or not event.check_ok:
                return False
            word = proto.decode_resp_word(event.a)
            return word["seq_known"] and word["seq"] == seq and word["cmd"] == cmd
        return match

    def read_chunk(self, addr: int, length: int) -> dict:
        record = {"addr": addr, "len": length, "attempts": []}
        for _ in range(self.args.max_attempts):
            seq = self.next_seq()
            start, _written = self.link.write(proto.read_frame(seq, addr, length))
            wait = wire_s(length + 2 * proto.FRAME_OVERHEAD, self.link.baud) + self.args.ack_timeout

            def match(event, s=seq):
                if event.kind == "readback":
                    return event.seq == s
                return self._matches(s, proto.CMD_READ)(event)

            reply = self.link.take(match, wait)
            record["attempts"].append({"seq": seq, "start": round(start, 6), "reply": self.reply_of(reply)})
            if reply is not None and reply.kind == "readback" and reply.crc_ok and reply.addr == addr \
                    and len(reply.data) == length:
                record["latency_s"] = round(reply.t - start, 6)
                record["data"] = reply.data
                return record
            self.guard()
        return record

    def set_rate(self, baud: int) -> dict:
        divisor = round(DESIGN_HZ / baud)
        seq = self.next_seq()
        start, _ = self.link.write(proto.baud_frame(seq, divisor))
        reply = self.link.take(self._matches(seq, proto.CMD_BAUD), wire_s(34, self.link.baud) + self.args.ack_timeout)
        out = {"baud": baud, "divisor": divisor, "device_baud": DESIGN_HZ / divisor,
               "error_ppm": round((DESIGN_HZ / divisor / baud - 1) * 1e6), "reply": self.reply_of(reply)}
        if reply is None or reply.tag != "A":
            return out
        self.link.poll(wire_s(20, self.link.baud) + 0.02)     # the ack has left the device before it changes
        self.link.set_baud(baud)
        self.link.poll(0.05)
        out["switched"] = True
        return out


def load_payload(args) -> tuple[bytes, dict]:
    if args.payload_tmem_trits:
        path = Path(args.payload_tmem_trits)
        raw = path.read_bytes()
        trits = [b - 256 if b > 127 else b for b in raw]
        from trinity_memory import container

        data = container.encode_file(trits, "dense5")
        info = {"kind": "TMEM v1 dense5 container (docs/format.md) of the trits in the source file",
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
            continue
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


def transfer(loader: Loader, data: bytes, addr: int, chunk: int, faults: dict[str, set[int]]) -> dict:
    link = loader.link
    records = []
    t_start = link.now()
    for index, offset in enumerate(range(0, len(data), chunk)):
        inject = {name for name, chunks in faults.items() if index in chunks}
        records.append(loader.load_chunk(index, addr + offset, data[offset:offset + chunk], inject))
        if not records[-1].get("acked"):
            break
    t_load = link.now()
    reads, got = [], bytearray()
    for offset in range(0, len(data), chunk):
        n = min(chunk, len(data) - offset)
        record = loader.read_chunk(addr + offset, n)
        got += record.pop("data", b"\0" * 0)
        reads.append(record)
        if "latency_s" not in record:
            break
    t_read = link.now()
    mismatch = next((i for i, (a, b) in enumerate(zip(got, data)) if a != b), None)
    retransmits: dict[str, int] = {}
    for record in records:
        for attempt in record["attempts"][:-1] if record.get("acked") else record["attempts"]:
            reply = attempt.get("reply")
            reason = "no_reply" if reply is None else (reply.get("reason_name") or reply.get("tag"))
            retransmits[reason] = retransmits.get(reason, 0) + 1
    payload = sum(r["len"] for r in records if r.get("acked"))
    frame_s = [wire_s(r["len"] + proto.FRAME_OVERHEAD, link.baud) for r in records if r.get("acked")]
    latency = [r["latency_s"] for r in records if r.get("acked")]
    clean = [r["latency_s"] - f for r, f in zip([r for r in records if r.get("acked")], frame_s)
             if len(r["attempts"]) == 1]
    return {
        "chunks": len(records), "acked": sum(1 for r in records if r.get("acked")),
        "payload_bytes": payload, "load_s": round(t_load - t_start, 6),
        "load_payload_Bps": round(payload / (t_load - t_start), 1) if t_load > t_start else None,
        "readback_bytes": len(got), "readback_s": round(t_read - t_load, 6),
        "readback_payload_Bps": round(len(got) / (t_read - t_load), 1) if t_read > t_load else None,
        "identical": bytes(got) == data, "first_mismatch": mismatch,
        "retransmits": retransmits, "attempts": sum(len(r["attempts"]) for r in records),
        "latency_s": distribution(latency),
        "latency_note": "per chunk: from the host's write of the transmission that was acknowledged to the ack "
                        "line's arrival; chunk_s (per chunk) also counts the failed attempts before it",
        "chunk_s": distribution([r["chunk_s"] for r in records if r.get("acked")]),
        "turnaround_s": distribution(clean),
        "turnaround_note": "per chunk sent once: ack time minus the host's write start minus the frame's own wire "
                           "time (10 bits per byte at the port rate): device commit, the ack line (20 bytes), USB "
                           "and host latency",
        "ideal_payload_Bps": round(link.baud / 10 * chunk / (chunk + proto.FRAME_OVERHEAD), 1),
        "per_chunk": records, "reads": reads,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", required=True)
    parser.add_argument("--baud", type=int, default=115200)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--payload", help="file to load")
    src.add_argument("--payload-tmem-trits", help="signed-byte trits (tools/extract-bram-trits.py), sent as TMEM dense5")
    parser.add_argument("--length", type=int, default=0, help="load only the first N bytes")
    parser.add_argument("--addr", type=lambda x: int(x, 0), default=0)
    parser.add_argument("--chunk", type=int, default=proto.MAX_LEN)
    parser.add_argument("--first-seq", type=int, default=1)
    parser.add_argument("--ack-timeout", type=float, default=0.5, help="seconds after the frame's wire time")
    parser.add_argument("--guard", type=float, default=0.15, help="quiet seconds before a retransmission")
    parser.add_argument("--max-attempts", type=int, default=6)
    parser.add_argument("--corrupt", default="", help="chunk indices, comma-separated")
    parser.add_argument("--drop", default="")
    parser.add_argument("--abort", default="")
    parser.add_argument("--garbage", default="")
    parser.add_argument("--garbage-bytes", type=int, default=64)
    parser.add_argument("--duplicate", default="")
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
              "design_hz": DESIGN_HZ, "t0_note": "every time is host seconds after the start on one monotonic clock"}
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
                while proc.poll() is None:
                    link.poll(0.05)
                record["bitstream"]["load_output"] = proc.stdout.read().strip().splitlines()[-6:]
                record["bitstream"]["load_returncode"] = proc.returncode
                link.poll(0.5)
                if proc.returncode:
                    raise RuntimeError("openFPGALoader failed")
            else:
                link.poll(0.3)
            loader = Loader(link, args, rng)
            record["status_before"] = loader.status()
            record["transfer"] = transfer(loader, data, args.addr, args.chunk, faults)
            record["status_after"] = loader.status()
            trials = []
            for rate in [int(x) for x in args.baud_try.split(",") if x.strip()]:
                trial = loader.set_rate(rate)
                if trial.get("switched"):
                    trial["status"] = loader.status()
                    if trial["status"] is not None:
                        size = min(args.baud_bytes, len(data))
                        result = transfer(loader, data[:size], args.addr, args.chunk, {})
                        trial["transfer"] = {k: v for k, v in result.items() if k not in ("per_chunk", "reads")}
                        trial["back"] = loader.set_rate(args.baud)
                    if not trial.get("back", {}).get("switched"):
                        # The device returns to its default rate by itself after its probation time.
                        link.set_baud(args.baud)
                        link.quiet(PROBATION_S + 1.0)
                        trial["fell_back"] = True
                    trial["status_after"] = loader.status()
                trials.append(trial)
            if trials:
                record["baud_trials"] = trials
        except Exception as error:  # noqa: BLE001 - recorded, run stopped
            record["stopped"] = f"{type(error).__name__}: {error}"
            status_code = 2
        finally:
            if link is not None:
                link.poll(0.2)
                link.close()
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
    record["checks"] = checks
    record["pass"] = all(checks.values()) and status_code == 0
    out.write_text(json.dumps(record, indent=1, default=lambda b: b.hex() if isinstance(b, bytes) else str(b)) + "\n")
    summary = {k: transfer_record.get(k) for k in ("chunks", "acked", "payload_bytes", "load_s", "load_payload_Bps",
                                                   "readback_payload_Bps", "identical", "retransmits")}
    print(json.dumps({"pass": record["pass"], "stopped": record.get("stopped"), **summary}, indent=1))
    if status_code:
        return status_code
    return 0 if record["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
