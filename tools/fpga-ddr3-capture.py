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
checks no data (its self-test runs once, inside calibration) and the user port
of this build is idle, so the returns to IDLE can change only during
calibration. A later line shows that the design has not been reset since, not
that memory kept data.

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
LINE = re.compile(r"^([HS])([0-9a-f]{8})([0-9a-f]{10})$")
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


def decode(entries, *, expect_build_id=None, expect_lanes=None, expect_period_ps=None,
           load_end_s=None, nominal_hz=None, min_span_s=1.0) -> dict:
    """Decode a timestamped transcript.

    entries: [{"t_s": host seconds on one monotonic clock, "line": text}] in arrival order.
    load_end_s: host seconds (same clock) at which the load returned, if this run loaded."""
    headers, status, other = [], [], []
    for entry in entries:
        parsed = parse_line(entry["line"])
        if parsed is None:
            other.append({"t_s": entry["t_s"], "line": entry["line"]})
            continue
        parsed["t_s"] = entry["t_s"]
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
    }
    passed = all(v for v in checks.values() if v is not None)
    return {"header": headers, "status_lines": status, "unparsed_lines": other, "final": final,
            "clock": clock, "reset": reset, "calibration": calibration, "checks": checks, "pass": passed}


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
    return {"build_id": ddr3.get("netlist_build_id"), "lanes": lanes,
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
                        nominal_hz=expect["nominal_hz"])
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
                                   nominal_hz=expect["nominal_hz"])
        status = 0 if record["decoded"]["pass"] else 1
    record["exit_status"] = status
    (out / "capture.json").write_text(json.dumps(record, indent=1) + "\n")
    summary = record.get("decoded", {})
    print(json.dumps({"exit_status": status, "stopped": run.get("stopped"), "checks": summary.get("checks"),
                      "final": summary.get("final"), "clock": summary.get("clock"), "reset": summary.get("reset"),
                      "xadc_before": run.get("xadc_before", {}).get("temp"),
                      "xadc_after": run.get("xadc_after", {}).get("temp")}, indent=1))
    return status


if __name__ == "__main__":
    sys.exit(main())
