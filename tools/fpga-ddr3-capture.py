#!/usr/bin/env python3
"""Load the AX7203 DDR3 bitstream into SRAM and record its status lines with the identity around them.

One run, in this order, every step with host UTC and monotonic times:

1. IDCODE (`openFPGALoader --detect`), DNA (`--read-dna`) and XADC (`--read-xadc`)
   before the load; stop when the die is at or above --max-temp.
2. The UART recorder starts: every line gets the host time at which its newline
   arrived, so a design's first `H` line is not lost to a port opened late.
3. `make ddr3-flash` (SRAM only, never builds; the bitstream must match the
   report by whole-file sha256 or by its sha256 from the sync word), its whole
   output kept; with --no-load this step is skipped and the resident design is read.
4. --seconds of capture after the load returned, then XADC again.

The report decodes the `H` line (build id, byte lanes, DDR3 clock period) and the
`S` lines of t27/rtl/fpga_ddr3_status.t27: calib_complete, the calibration state,
the highest state since reset, the returns to IDLE since reset (the netlist names
them `recals`; both a wrong self-test read and a failed alignment return the
controller to IDLE) and the low 40 bits of the controller clocks since reset. From
the host times it fits the controller clock, derives when the design left reset
and so how many times the 40-bit count wrapped (every 2^40 clocks, 3.665 h at
83.33 MHz), and compares the reset with the end of the load. Without a load in
the same run the wrap count is unknown and the report says so.

What the lines can and cannot show: after DONE_CALIBRATE (state 23) UberDDR3
checks no data (its self-test runs once, inside calibration), so the returns to
IDLE can change only during calibration. A later line shows that the design has
not been reset since, not that memory kept data.

A build with the pattern test (PATTERN_TEST 1, t27/rtl/fpga_ddr3_pattern.t27; the
report's variant names it) also sends G, K and L lines once after calibration and
W, R, E, M, F lines after every pass of its write / read-back / compare over the
burst addresses (true data, then the complement), T when its watchdog stopped it
and Z after its last round. They are decoded into `pattern`: every pass with its
wrong bursts, 64-bit words and bits, the DQ bits ever wrong, the first failing
burst, and its write and read phase clocks. The MB/s derived from those clocks is
the pattern test's own throughput (in-order bursts, one request per clock at
most, refresh and row changes included), not the stage-2 measurement. The test
checks only what it read back: the passes decoded, over the bursts it covered.

  ../.venv-hw/bin/python tools/fpga-ddr3-capture.py --port /dev/cu.usbserial-110 \\
      --bit build/fpga/ddr3-x16/tms_ddr3_ax7203.bit \\
      --report reports/fpga/ddr3-build-2026-09-24-f07e91cd-x16/build.json \\
      --output-dir reports/fpga/ddr3-bringup-2026-09-24-f07e91cd-x16 --seconds 60
  python3 tools/fpga-ddr3-capture.py --decode reports/fpga/.../uart.tsv --report .../build.json

Exit status 0 when every check passes, 1 otherwise, 2 when the run was stopped
(temperature, a tool failed, the port could not be opened).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = "trinity.ddr3-capture.v1"
LINE = re.compile(r"^([HSGKLWREMFTZ])([0-9a-f]{8})([0-9a-f]{10})$")
PATTERN_TAGS = "GKLWREMFTZ"
PASS_ORDER = "WREMF"
NONE32 = 0xFFFFFFFF
WRAP = 1 << 40
DONE_CALIBRATE = 23


def utc(ts: float | None = None) -> str:
    moment = dt.datetime.fromtimestamp(time.time() if ts is None else ts, dt.timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_line(text: str):
    """One H or S line as a dict, None when the text is not one."""
    match = LINE.match(text)
    if not match:
        return None
    tag, a, b = match.group(1), int(match.group(2), 16), int(match.group(3), 16)
    if tag in PATTERN_TAGS:
        return {"tag": tag, "a": a, "b": b}
    if tag == "H":
        return {"tag": "H", "build_id": f"{a:08x}", "format": b >> 32, "byte_lanes": (b >> 24) & 0xFF,
                "ddr3_period_ps": b & 0xFFFFFF}
    return {"tag": "S", "calib_complete": a >> 24, "state": (a >> 16) & 0x1F, "highest": (a >> 8) & 0x1F,
            "returns_to_idle": a & 0xFF, "clocks_low40": b}


def fit_clock(points):
    """Least-squares slope of clocks over host seconds, from (host_s, clocks) pairs, and its
    standard error from the residuals (None for either when it cannot be computed)."""
    if len(points) < 2:
        return None, None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None, None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    if len(points) < 3:
        return slope, None
    rss = sum((y - my - slope * (x - mx)) ** 2 for x, y in zip(xs, ys))
    return slope, (rss / (len(points) - 2) / sxx) ** 0.5


def decode_pattern(lines, *, hz=None, lanes=None) -> dict:
    """The pattern test's lines (parsed, with t_s) of one reset, in arrival order."""
    header, passes, problems = {}, [], []
    timeout = done = None
    block = None
    for ln in lines:
        tag, a, b = ln["tag"], ln["a"], ln["b"]
        if tag == "G":
            header.update({"bursts": a, "format": b >> 32, "byte_lanes": (b >> 24) & 0xFF,
                           "rounds_configured": b & 0xFFFFFF, "t_s": ln["t_s"]})
        elif tag == "K":
            header.update({"seed": a, "calib_complete_clocks_low40": b})
        elif tag == "L":
            header.update({"hold_clocks": a, "watchdog_clocks": b})
        elif tag in PASS_ORDER:
            want = PASS_ORDER[len(block["_seen"])] if block else "W"
            if tag != want:
                problems.append({"t_s": ln["t_s"], "problem": f"{tag} line where {want} was expected"})
                block = None
                if tag != "W":
                    continue
            if tag == "W":
                block = {"round": a >> 1, "pass": a & 1, "data": "complement" if a & 1 else "true",
                         "write_clocks": b, "_seen": "W", "_rp": a}
            elif tag == "R":
                if a != block["_rp"]:
                    problems.append({"t_s": ln["t_s"], "problem": f"R line for pass {a:#x} in the block of {block['_rp']:#x}"})
                block.update({"read_clocks": b, "_seen": block["_seen"] + "R"})
            elif tag == "E":
                block.update({"bad_bursts": a, "bit_errors": b, "_seen": block["_seen"] + "E"})
            elif tag == "M":
                block.update({"bad_words_64bit": a, "dq_fail_mask": f"{b:#010x}",
                              "dq_failed": [j for j in range(32) if b >> j & 1], "_seen": block["_seen"] + "M"})
            else:
                block.update({"first_fail_burst": None if a == NONE32 else a,
                              "first_fail_word": (b >> 32) if a != NONE32 else None,
                              "first_fail_xor_low32": f"{b & NONE32:#010x}" if a != NONE32 else None,
                              "reported_t_s": ln["t_s"]})
                block.pop("_seen")
                block.pop("_rp")
                passes.append(block)
                block = None
        elif tag == "T":
            timeout = {"acks": a, "read_phase": bool(b >> 36 & 1), "pass": b >> 32 & 1, "round": b & NONE32,
                       "t_s": ln["t_s"]}
        elif tag == "Z":
            done = {"rounds": a, "bad_bursts_all_passes": b, "t_s": ln["t_s"]}
    partial = None
    if block:
        partial = {k: v for k, v in block.items() if not k.startswith("_")}
    lanes = header.get("byte_lanes") or lanes
    bursts = header.get("bursts")
    region = bursts * 8 * lanes if bursts is not None and lanes else None
    for p in passes:
        p["clean"] = p["bad_bursts"] == 0 and p["bit_errors"] == 0 and p["bad_words_64bit"] == 0
        if hz and region:
            for phase in ("write", "read"):
                seconds = p[f"{phase}_clocks"] / hz
                p[f"{phase}_s"] = round(seconds, 6)
                p[f"pattern_test_{phase}_MBps"] = round(region / seconds / 1e6, 1) if seconds else None
    rounds = {}
    for p in passes:
        rounds.setdefault(p["round"], set()).add(p["pass"])
    complete = sorted(r for r, got in rounds.items() if got == {0, 1})
    hold = header.get("hold_clocks")
    min_sep = hold + bursts - 1 if hold is not None and bursts else None
    return {
        "header": header,
        "bytes_per_burst": 8 * lanes if lanes else None,
        "region_bytes": region,
        "passes": passes,
        "partial_pass_at_end": partial,
        "problems": problems,
        "timeout": timeout,
        "done": done,
        "totals": {"passes": len(passes), "clean_passes": sum(p["clean"] for p in passes),
                   "rounds_complete": len(complete),
                   "bad_bursts": sum(p["bad_bursts"] for p in passes),
                   "bit_errors": sum(p["bit_errors"] for p in passes),
                   "bytes_written_and_read_back": len(passes) * region if region else None},
        "min_write_to_read_clocks": min_sep,
        "min_write_to_read_s": round(min_sep / hz, 6) if min_sep is not None and hz else None,
        "notes": ["pattern_test_*_MBps: region bytes over the phase's controller clocks (first request "
                  "presented to last ack), with the in-order single-master access of this test, refresh and "
                  "row changes included; it is not the stage-2 throughput measurement",
                  "min_write_to_read: every burst's read request is accepted at least hold + bursts - 1 "
                  "controller clocks after its write request (all writes are acknowledged before the first "
                  "read, at most one request per clock); no longer retention is claimed",
                  "a pass covers the burst addresses 0 .. bursts-1 once for write and once for read-back; "
                  "the counts are per pass, bursts and 64-bit words counted once however many bits are wrong"],
    }


def decode(entries, *, expect_build_id=None, expect_lanes=None, expect_period_ps=None,
           load_end_s=None, nominal_hz=None, min_span_s=1.0, expect_pattern=None) -> dict:
    """Decode a timestamped transcript.

    entries: [{"t_s": host seconds on one monotonic clock, "line": text}] in arrival order.
    load_end_s: host seconds (same clock) at which the load returned, if this run loaded.
    expect_pattern: True when the build has the pattern test, False when it has not, None unknown."""
    headers, status, other, pattern_lines = [], [], [], []
    for entry in entries:
        parsed = parse_line(entry["line"])
        if parsed is None:
            other.append({"t_s": entry["t_s"], "line": entry["line"]})
            continue
        parsed["t_s"] = entry["t_s"]
        if parsed["tag"] in PATTERN_TAGS:
            pattern_lines.append(parsed)
        else:
            (headers if parsed["tag"] == "H" else status).append(parsed)
    # The S lines after the last H line belong to the design's current reset.
    last_h = headers[-1]["t_s"] if headers else None
    current = [s for s in status if last_h is None or s["t_s"] >= last_h]
    # Clocks rise monotonically within one reset (a wrap inside a short capture is not expected).
    points = [(s["t_s"], s["clocks_low40"]) for s in current]
    span = points[-1][0] - points[0][0] if len(points) >= 2 else 0.0
    hz, hz_err = fit_clock(points) if span >= min_span_s else (None, None)
    clock = {"nominal_hz": nominal_hz, "lines": len(points), "span_s": round(span, 3),
             "measured_hz": round(hz, 1) if hz else None,
             "standard_error_hz": round(hz_err, 1) if hz_err else None,
             "ppm_from_nominal": round((hz / nominal_hz - 1) * 1e6, 1) if hz and nominal_hz else None,
             "note": "fit of the S lines' clock count over the host arrival times; the arrival "
                     "jitter of the USB serial bridge (milliseconds) bounds its precision"}
    rate = hz or nominal_hz
    reset = {}
    if rate and current:
        # When the design left reset, per line, in host seconds; the median is robust to arrival jitter.
        if load_end_s is not None:
            wraps = [round(((s["t_s"] - load_end_s) * rate - s["clocks_low40"]) / WRAP) for s in current]
            k = max(set(wraps), key=wraps.count)
            estimates = [s["t_s"] - (s["clocks_low40"] + k * WRAP) / rate for s in current]
            reset_s = statistics.median(estimates)
            reset = {"wraps_of_the_40_bit_count": k, "reset_s": round(reset_s, 3),
                     "reset_minus_load_end_s": round(reset_s - load_end_s, 3),
                     "last_line_since_reset_s": round(current[-1]["t_s"] - reset_s, 3)}
        else:
            reset = {"wraps_of_the_40_bit_count": None,
                     "last_line_since_reset_s_if_no_wrap": round(current[-1]["clocks_low40"] / rate, 3),
                     "note": "no load in this run: the time since reset is this value plus an unknown "
                             "multiple of 2^40 clocks (3.665 h at 83.33 MHz)"}
    final = current[-1] if current else None
    # Calibration as the coalesced lines show it: the first line of each state, and the
    # last line before calib_complete and the first one with it (the finish lies between).
    timeline, seen = [], set()
    for line in current:
        if line["state"] not in seen:
            seen.add(line["state"])
            timeline.append({"state": line["state"], "first_seen_clocks": line["clocks_low40"],
                             "first_seen_s": round(line["clocks_low40"] / rate, 4) if rate else None})
    before = [line for line in current if not line["calib_complete"]]
    after = [line for line in current if line["calib_complete"]]
    calibration = {
        "state_timeline": timeline,
        "last_line_before_calib_complete_clocks": before[-1]["clocks_low40"] if before else None,
        "first_line_with_calib_complete_clocks": after[0]["clocks_low40"] if after else None,
    }
    if rate and before and after:
        calibration["calib_complete_between_s"] = [round(before[-1]["clocks_low40"] / rate, 4),
                                                   round(after[0]["clocks_low40"] / rate, 4)]
    current_pattern = [p for p in pattern_lines if last_h is None or p["t_s"] >= last_h]
    pattern = None
    if current_pattern or expect_pattern:
        pattern = decode_pattern(current_pattern, hz=rate,
                                 lanes=headers[-1]["byte_lanes"] if headers else expect_lanes)
    has_pattern = bool(current_pattern)
    checks = {
        "header_line": bool(headers) if load_end_s is not None else None,
        "build_id": (headers[-1]["build_id"] == expect_build_id) if headers and expect_build_id else None,
        "byte_lanes": (headers[-1]["byte_lanes"] == expect_lanes) if headers and expect_lanes else None,
        "ddr3_period_ps": (headers[-1]["ddr3_period_ps"] == expect_period_ps) if headers and expect_period_ps else None,
        "calib_complete": bool(final and final["calib_complete"] == 1),
        "done_calibrate": bool(final and final["state"] == DONE_CALIBRATE and final["highest"] == DONE_CALIBRATE),
        "no_return_to_idle": bool(final and final["returns_to_idle"] == 0),
        # Before the design's H line the port may carry a partial line (opened mid-line) or
        # noise while the FPGA is configured; after it every line must parse.
        "no_unparsed_lines_after_header": not [o for o in other if last_h is None or o["t_s"] >= last_h],
        # The design leaves reset once configuration is done, a little before
        # openFPGALoader returns; a reset far from the load means another one happened.
        "reset_at_load": (-3.0 <= reset["reset_minus_load_end_s"] <= 3.0)
        if "reset_minus_load_end_s" in reset else None,
        # Pattern test: present exactly when the build has it; every pass reported in order and
        # without a wrong bit; no watchdog stop; at least one round (true and complement pass).
        "pattern_present": (has_pattern == bool(expect_pattern)) if expect_pattern is not None else None,
        "pattern_header": bool(pattern["header"].get("bursts") is not None) if pattern else None,
        "pattern_lanes": (pattern["header"].get("byte_lanes") == headers[-1]["byte_lanes"])
        if pattern and headers and "byte_lanes" in pattern["header"] else None,
        "pattern_well_formed": not pattern["problems"] if pattern else None,
        "pattern_no_wrong_bits": (all(p["clean"] for p in pattern["passes"]) and pattern["timeout"] is None)
        if pattern else None,
        "pattern_round_complete": pattern["totals"]["rounds_complete"] >= 1 if pattern else None,
    }
    passed = all(v for v in checks.values() if v is not None)
    result = {"header": headers, "status_lines": status, "unparsed_lines": other, "final": final,
              "clock": clock, "reset": reset, "calibration": calibration, "checks": checks, "pass": passed}
    if pattern is not None:
        result["pattern"] = pattern
    return result


def parse_xadc(text: str) -> dict:
    """openFPGALoader --read-xadc prints banner lines, then an object with a trailing comma."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError(f"no XADC object in {text!r}")
    body = re.sub(r",\s*}", "}", text[start:end + 1])
    return json.loads(body)


def parse_dna(text: str) -> str:
    match = re.search(r'"dna"\s*:\s*"(0x[0-9a-fA-F]+)"', text)
    if not match:
        raise ValueError(f"no DNA in {text!r}")
    return match.group(1).lower()


def parse_idcode(text: str) -> str:
    match = re.search(r"idcode\s+(0x[0-9a-fA-F]+)", text)
    if not match:
        raise ValueError(f"no IDCODE in {text!r}")
    return match.group(1).lower()


class Recorder(threading.Thread):
    """Reads the UART from before the load until stopped; one entry per line."""

    def __init__(self, port: str, baud: int, t0: float):
        super().__init__(daemon=True)
        import serial  # pyserial

        self.link = serial.Serial(port, baud, timeout=0.02)
        self.t0 = t0
        self.raw = bytearray()
        self.entries = []
        self.stop_flag = threading.Event()
        self.error = None

    def run(self):
        pending = bytearray()
        try:
            while not self.stop_flag.is_set():
                chunk = self.link.read(4096)
                if not chunk:
                    continue
                now = time.monotonic()
                self.raw += chunk
                pending += chunk
                while b"\n" in pending:
                    line, _, rest = pending.partition(b"\n")
                    pending = bytearray(rest)
                    self.entries.append({"t_s": round(now - self.t0, 4), "utc": utc(),
                                         "line": line.decode("ascii", "replace").strip("\r")})
        except Exception as error:  # noqa: BLE001 - reported in the capture
            self.error = repr(error)
        finally:
            self.link.close()

    def stop(self):
        self.stop_flag.set()
        self.join(timeout=5)


def repo_relative(text: str) -> str:
    """Paths inside the repository as the repository sees them (the record is committed)."""
    return text.replace(str(ROOT) + "/", "")


def run_step(cmd, t0, cwd=None) -> dict:
    start_utc, start = utc(), time.monotonic()
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    end = time.monotonic()
    return {"cmd": [repo_relative(c) for c in cmd], "started_utc": start_utc, "ended_utc": utc(),
            "start_s": round(start - t0, 4), "end_s": round(end - t0, 4), "returncode": proc.returncode,
            "output": [repo_relative(line) for line in (proc.stdout + proc.stderr).strip().splitlines()]}


def expectations(report: dict) -> dict:
    ddr3 = report.get("ddr3", {})
    ddr_clock = next((c for c in ddr3.get("clocks", []) if c.get("name") == "clk_ddr"), {})
    ctrl_clock = next((c for c in ddr3.get("clocks", []) if c.get("name") == "clk_ctrl"), {})
    lanes = None
    match = re.search(r"BYTE_LANES (\d+)", ddr3.get("variant", ""))
    if match:
        lanes = int(match.group(1))
    # Builds before the pattern test name no PATTERN_TEST and have none.
    match = re.search(r"PATTERN_TEST (\d)", ddr3.get("variant", ""))
    pattern = bool(int(match.group(1))) if match else False
    return {"build_id": ddr3.get("netlist_build_id"), "lanes": lanes, "pattern": pattern,
            "period_ps": round(ddr_clock["period_ns"] * 1000) if ddr_clock.get("period_ns") else None,
            "nominal_hz": round(1e9 / ctrl_clock["period_ns"], 1) if ctrl_clock.get("period_ns") else None}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def board_run(args, expect, report_path: Path) -> tuple[dict, int]:
    loader = [args.loader, "-c", args.cable]
    t0 = time.monotonic()
    run = {"t0_utc": utc(), "t0_note": "every *_s value is host seconds after t0 on one monotonic clock",
           "steps": {}}

    def step(name, cmd, cwd=None):
        run["steps"][name] = run_step(cmd, t0, cwd)
        return run["steps"][name]

    idcode = step("idcode", loader + ["--detect"])
    dna = step("dna", loader + ["--read-dna"])
    xadc_before = step("xadc_before", loader + ["--read-xadc"])
    for name in ("idcode", "dna", "xadc_before"):
        if run["steps"][name]["returncode"] != 0:
            return run, 2
    run["idcode"] = parse_idcode("\n".join(idcode["output"]))
    run["dna"] = parse_dna("\n".join(dna["output"]))
    run["xadc_before"] = parse_xadc("\n".join(xadc_before["output"]))
    if run["xadc_before"]["temp"] >= args.max_temp:
        run["stopped"] = f"die at {run['xadc_before']['temp']} C before the load (limit {args.max_temp} C)"
        return run, 2
    recorder = Recorder(args.port, args.baud, t0)
    recorder.start()
    time.sleep(args.settle)
    load_end = None
    if not args.no_load:
        bit = Path(args.bit).resolve()
        load = step("load", ["make", "-s", "-C", str(ROOT / "fpga/ax7203"), "ddr3-flash",
                             f"DDR3_BUILD={bit.parent}", f"DDR3_FLASH_REPORT={report_path.resolve()}",
                             f"OPENFPGALOADER={args.loader}", f"DDR3_CABLE={args.cable}"])
        if load["returncode"] != 0:
            recorder.stop()
            run["uart_entries"], run["uart_raw"], run["uart_error"] = recorder.entries, bytes(recorder.raw), recorder.error
            return run, 2
        load_end = load["end_s"]
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline and recorder.is_alive():
        time.sleep(0.2)
    recorder.stop()
    run["uart_entries"], run["uart_raw"], run["uart_error"] = recorder.entries, bytes(recorder.raw), recorder.error
    xadc_after = step("xadc_after", loader + ["--read-xadc"])
    if xadc_after["returncode"] == 0:
        run["xadc_after"] = parse_xadc("\n".join(xadc_after["output"]))
    run["load_end_s"] = load_end
    if recorder.error:
        run["stopped"] = f"UART: {recorder.error}"
        return run, 2
    if run.get("xadc_after", {}).get("temp", 0) >= args.max_temp:
        run["stopped"] = f"die at {run['xadc_after']['temp']} C after the run (limit {args.max_temp} C)"
        return run, 2
    return run, 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--report", type=Path, required=True, help="build.json of the bitstream")
    parser.add_argument("--decode", type=Path, help="decode a uart.tsv of an earlier run instead of using the board")
    parser.add_argument("--load-end-s", type=float, help="with --decode: host seconds at which the load returned")
    parser.add_argument("--port")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--bit", help="the .bit to load (make ddr3-flash checks it against --report)")
    parser.add_argument("--no-load", action="store_true", help="read the resident design")
    parser.add_argument("--seconds", type=float, default=60.0, help="capture after the load returned")
    parser.add_argument("--settle", type=float, default=1.0, help="seconds between opening the port and the load")
    parser.add_argument("--max-temp", type=float, default=80.0)
    parser.add_argument("--loader", default="openFPGALoader")
    parser.add_argument("--cable", default="digilent_hs2")
    parser.add_argument("--label", default="")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    expect = expectations(report)

    if args.decode:
        entries = []
        for raw in args.decode.read_text().splitlines():
            if raw.startswith("#") or not raw.strip():
                continue
            t_s, _utc, line = raw.split("\t", 2)
            entries.append({"t_s": float(t_s), "line": line})
        result = decode(entries, expect_build_id=expect["build_id"], expect_lanes=expect["lanes"],
                        expect_period_ps=expect["period_ps"], load_end_s=args.load_end_s,
                        nominal_hz=expect["nominal_hz"], expect_pattern=expect.get("pattern"))
        print(json.dumps(result, indent=1))
        return 0 if result["pass"] else 1

    if not args.port or not args.output_dir or (not args.no_load and not args.bit):
        parser.error("a board run needs --port, --output-dir and --bit (or --no-load)")
    run, status = board_run(args, expect, args.report)
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    entries = run.pop("uart_entries", [])
    raw = run.pop("uart_raw", b"")
    tsv = out / "uart.tsv"
    tsv.write_text("# host seconds after t0\thost UTC at the newline\tline\n" +
                   "".join(f"{e['t_s']:.4f}\t{e['utc']}\t{e['line']}\n" for e in entries))
    (out / "uart-raw.txt").write_bytes(raw)
    bitstream = report.get("bitstream", {})
    record = {
        "schema": SCHEMA,
        "label": args.label,
        "tool": "tools/fpga-ddr3-capture.py",
        "board": {"dna": run.get("dna"), "idcode": run.get("idcode"), "uart": f"{args.port} {args.baud} 8N1"},
        "bitstream": None if args.no_load else {
            "file": str(Path(args.bit)), "report": str(args.report),
            "report_sha256": bitstream.get("sha256"), "report_sha256_from_sync": bitstream.get("sha256_from_sync"),
            "reproducible_identity": report.get("reproducible_identity"),
            "load_check": run.get("steps", {}).get("load", {}).get("output", [])[:1]},
        "expect": expect,
        "run": {k: v for k, v in run.items()},
        "uart_transcript": {"file": tsv.name, "sha256": sha256_file(tsv), "lines": len(entries),
                            "raw_file": "uart-raw.txt", "raw_sha256": hashlib.sha256(raw).hexdigest(),
                            "raw_bytes": len(raw)},
    }
    if status == 0:
        record["decoded"] = decode(entries, expect_build_id=expect["build_id"], expect_lanes=expect["lanes"],
                                   expect_period_ps=expect["period_ps"], load_end_s=run.get("load_end_s"),
                                   nominal_hz=expect["nominal_hz"], expect_pattern=expect.get("pattern"))
        status = 0 if record["decoded"]["pass"] else 1
    record["exit_status"] = status
    (out / "capture.json").write_text(json.dumps(record, indent=1) + "\n")
    summary = record.get("decoded", {})
    print(json.dumps({"exit_status": status, "stopped": run.get("stopped"), "checks": summary.get("checks"),
                      "final": summary.get("final"), "clock": summary.get("clock"), "reset": summary.get("reset"),
                      "pattern_totals": (summary.get("pattern") or {}).get("totals"),
                      "xadc_before": run.get("xadc_before", {}).get("temp"),
                      "xadc_after": run.get("xadc_after", {}).get("temp")}, indent=1))
    return status


if __name__ == "__main__":
    sys.exit(main())
