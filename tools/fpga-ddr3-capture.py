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

A #61 run (Python with pyserial; the x16 pattern-test build of 7deeef16, 60 s after the load):

  python tools/fpga-ddr3-capture.py --port /dev/cu.usbserial-110 \\
      --bit build/fpga/ddr3-x16-pattern/tms_ddr3_ax7203.bit \\
      --report reports/fpga/ddr3-build-2026-09-24-7deeef16-x16/build.json \\
      --output-dir reports/fpga/ddr3-bringup-<date>-7deeef16-x16-pattern/load<n> \\
      --seconds 60 --label "x16 pattern test, load <n>"
  python3 tools/fpga-ddr3-capture.py --decode reports/fpga/.../uart.tsv --report .../build.json
  python3 tools/fpga-ddr3-capture.py --redecode reports/fpga/.../load<n> --report .../build.json --reason "..."

A build with the read path (`define DDR3_READER, make ... DDR3_APP=reader; t27/rtl/fpga_ddr3_reader.t27,
issue #62; the report's variant names READER) sends q, k, v lines once after calibration and
thirteen lines per run (a f g c y p d n o w u s h: format, fill, read cycles and words, bus,
payload, padding and scale/metadata bytes, logical trits, bad words, invalid groups, command,
wait and consumer stalls, the most requests outstanding, the clocks the cap held a request, the
+1 and -1 counts, the dot product and the checksum), t when its watchdog stopped it and z after
its last run. They are decoded into `reader`: every run with its counters checked against each
other (bus bytes = words x 16, padding = bus - payload, ...) and against tools/ddr3_read_model.py
(words, bytes, trits, counts, dot product and checksum of the run's format and key), the pairs of
runs that carry the same trits in both formats, and per run the read's words per clock, which
is a raw read-path figure (cycles from the first read request to the last ack), not the
stage-2 weights-per-second comparison.

The record keeps the command line (`argv`) and the repository revision of the tool
(`tool_revision`: HEAD and whether tracked files differed from it).

Exit status 0 when every check passes, 1 otherwise, 2 when the run was stopped
(temperature, a tool failed, the port could not be opened).
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
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
LINE = re.compile(r"^([HSGKLWREMFTZqkvafgcypdnowushtz])([0-9a-f]{8})([0-9a-f]{10})$")
LINE_CHARS = 19
PATTERN_TAGS = "GKLWREMFTZ"
PASS_ORDER = "WREMF"
READER_TAGS = "qkvafgcypdnowushtz"
RUN_ORDER = "afgcypdnowush"
NONE32 = 0xFFFFFFFF
WRAP = 1 << 40
DONE_CALIBRATE = 23
CLOCK_TOLERANCE_PPM = 5000


def utc(ts: float | None = None) -> str:
    moment = dt.datetime.fromtimestamp(time.time() if ts is None else ts, dt.timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_line(text: str):
    """One H or S line as a dict, None when the text is not one."""
    match = LINE.match(text)
    if not match:
        return None
    tag, a, b = match.group(1), int(match.group(2), 16), int(match.group(3), 16)
    if tag in PATTERN_TAGS or tag in READER_TAGS:
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
    """The pattern test's lines (parsed, with t_s) of one reset, in arrival order.

    hz converts phase clocks into seconds and MB/s (the caller passes the nominal controller
    clock when it has one). Passes must come as round << 1 | pass = 0, 1, 2, ... without a gap
    or repeat (from 0 when the G line was seen). A last block cut off by the end of the capture
    is kept in partial_pass_at_end; its E and M counts, when they arrived, are final (the test
    sends them after the pass's compare) and count as wrong bits like a completed pass's."""
    header, passes, problems = {}, [], []
    timeout = done = None
    block = None
    last_rp = None
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
                want_rp = 0 if last_rp is None and "bursts" in header else (
                    (last_rp + 1) & NONE32 if last_rp is not None else None)
                if want_rp is not None and a != want_rp:
                    problems.append({"t_s": ln["t_s"], "problem": f"pass {a:#x} where {want_rp:#x} was expected "
                                                                  "(a pass missing or repeated)"})
                last_rp = a
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
        partial["clean_so_far"] = not (partial.get("bad_bursts") or partial.get("bit_errors")
                                       or partial.get("bad_words_64bit"))
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
        "clock_hz_used": hz,
        "notes": ["pattern_test_*_MBps: region bytes over the phase's controller clocks (first request "
                  "presented to last ack), with the in-order single-master access of this test, refresh and "
                  "row changes included; it is not the stage-2 throughput measurement",
                  "*_s, pattern_test_*_MBps and min_write_to_read_s convert clocks at clock_hz_used, the nominal "
                  "controller clock when the report gives one (the PLL output; the fit in `clock` is limited by "
                  "the host's arrival jitter)",
                  "min_write_to_read: a bound from the design, not a measurement: every burst's read request is "
                  "accepted at least hold + bursts - 1 controller clocks after its write request (all writes "
                  "are acknowledged before the first read, at most one request per clock); no longer retention "
                  "is claimed",
                  "a pass covers the burst addresses 0 .. bursts-1 once for write and once for read-back; "
                  "the counts are per pass, bursts and 64-bit words counted once however many bits are wrong"],
    }


def _signed64(x: int) -> int:
    return x - (1 << 64) if x >> 63 else x


def reader_model():
    sys.path.insert(0, str(ROOT / "tools"))
    import ddr3_read_model  # noqa: E402

    return ddr3_read_model


def decode_reader(lines, *, hz=None, model_runs=None) -> dict:
    """The read path's lines (parsed, with t_s) of one reset, in arrival order.

    Runs must come as 0, 1, 2, ... without a gap or repeat (from 0 when the q line was seen),
    each as the thirteen lines of RUN_ORDER. Every complete run is checked on its own (the
    counters add up, no bad word, no invalid group, no consumer stall) and, for the first
    model_runs runs (None: all), against tools/ddr3_read_model.py with the seed of the k line.
    hz converts clocks into seconds and MB/s (the nominal controller clock when known)."""
    model = reader_model()
    header, runs, problems = {}, [], []
    timeout = done = None
    block, last_run = None, None
    for ln in lines:
        tag, a, b = ln["tag"], ln["a"], ln["b"]
        if tag == "q":
            header.update({"trits": a, "format": b >> 32, "byte_lanes": (b >> 24) & 0xFF, "cap": b & 0xFFFFFF,
                           "t_s": ln["t_s"]})
        elif tag == "k":
            header.update({"seed": a, "calib_complete_clocks_low40": b})
        elif tag == "v":
            header.update({"runs_configured": a, "watchdog_clocks": b})
        elif tag in RUN_ORDER:
            want = RUN_ORDER[len(block["_seen"])] if block else "a"
            if tag != want:
                problems.append({"t_s": ln["t_s"], "problem": f"{tag} line where {want} was expected"})
                block = None
                if tag != "a":
                    continue
            if tag == "a":
                want_run = 0 if last_run is None and "trits" in header else (
                    (last_run + 1) & NONE32 if last_run is not None else None)
                if want_run is not None and a != want_run:
                    problems.append({"t_s": ln["t_s"], "problem": f"run {a} where {want_run} was expected "
                                                                  "(a run missing or repeated)"})
                last_run = a
                fmt = (b >> 32) & 1
                block = {"run": a, "pair": a >> 1, "format": fmt, "format_name": model.FORMAT_NAMES[fmt],
                         "lanes_per_word": (b >> 24) & 0xFF, "_seen": ""}
            elif tag == "f":
                block.update({"fill_words": a, "fill_cycles": b})
            elif tag == "g":
                block.update({"fill_max_outstanding": a, "fill_command_stalls": b})
            elif tag == "c":
                block.update({"words": a, "cycles": b})
            elif tag == "y":
                block.update({"consumer_stalls": a, "bus_bytes": b})
            elif tag == "p":
                block.update({"scale_metadata_bytes": a, "payload_bytes": b})
            elif tag == "d":
                block.update({"bad_words": a, "padding_bytes": b})
            elif tag == "n":
                block.update({"invalid_groups": a, "logical_trits": b})
            elif tag == "o":
                block.update({"max_outstanding": a, "command_stalls": b})
            elif tag == "w":
                block.update({"cap_holds": a, "wait_stalls": b})
            elif tag == "u":
                block.update({"minus": a, "plus": b})
            elif tag == "s":
                block.update({"dot": _signed64((a << 40) | b)})
            elif tag == "h":
                block.update({"checksum": f"{(a << 32) | b:#018x}", "reported_t_s": ln["t_s"]})
            block["_seen"] += tag
            if tag == "h":
                block.pop("_seen")
                runs.append(block)
                block = None
        elif tag == "t":
            timeout = {"acks": a, "read_phase": bool(b >> 36 & 1), "format": b >> 32 & 1, "run": b & NONE32,
                       "t_s": ln["t_s"]}
        elif tag == "z":
            done = {"runs": a, "bad_words_all_runs": b, "t_s": ln["t_s"]}
    trits, seed = header.get("trits"), header.get("seed")
    checked = 0
    for r in runs:
        fmt = r["format"]
        lanes = model.LANES[fmt]
        r["checks"] = {
            "lanes_per_word": r["lanes_per_word"] == lanes,
            "bus_bytes_is_words_x16": r["bus_bytes"] == r["words"] * model.WORD_BYTES,
            "padding_is_bus_minus_payload": r["padding_bytes"] == r["bus_bytes"] - r["payload_bytes"],
            "no_scale_metadata": r["scale_metadata_bytes"] == 0,
            "fill_words_equal_read_words": r["fill_words"] == r["words"],
            "no_bad_word": r["bad_words"] == 0,
            "no_invalid_group": r["invalid_groups"] == 0,
            "no_consumer_stall": r["consumer_stalls"] == 0,
            "cycles_at_least_words": r["cycles"] >= r["words"],
        }
        if trits is not None:
            r["checks"].update({
                "words_cover_the_region": r["words"] == model.words_of(fmt, trits),
                "logical_trits_are_the_region": r["logical_trits"] == trits,
                "payload_bytes_of_the_format": r["payload_bytes"] == model.payload_bytes(fmt, trits),
            })
        if trits is not None and seed is not None and (model_runs is None or checked < model_runs):
            want = model.expected_run(seed, r["run"], trits)
            got = {"words": r["words"], "bus_bytes": r["bus_bytes"], "payload_bytes": r["payload_bytes"],
                   "padding_bytes": r["padding_bytes"], "pos": r["plus"], "neg": r["minus"], "dot": r["dot"],
                   "chk": int(r["checksum"], 16)}
            r["model"] = {k: want[k] for k in got}
            r["checks"]["equals_host_model"] = got == r["model"]
            r["model"]["chk"] = f"{want['chk']:#018x}"
            checked += 1
        r["pass"] = all(r["checks"].values())
        r["words_per_clock"] = round(r["words"] / r["cycles"], 6) if r["cycles"] else None
        if hz and r["cycles"]:
            r["read_s"] = round(r["cycles"] / hz, 9)
            r["bus_MBps"] = round(r["bus_bytes"] / (r["cycles"] / hz) / 1e6, 1)
    partial = None
    if block:
        partial = {k: v for k, v in block.items() if not k.startswith("_")}
        partial["clean_so_far"] = not (partial.get("bad_words") or partial.get("invalid_groups")
                                       or partial.get("consumer_stalls"))
    pairs = []
    by_run = {r["run"]: r for r in runs}
    for r in runs:
        if r["format"] == 0 and r["run"] + 1 in by_run:
            other = by_run[r["run"] + 1]
            same = {k: r[k] == other[k] for k in ("logical_trits", "plus", "minus", "dot")}
            pairs.append({"pair": r["pair"], "runs": [r["run"], other["run"]], "same": same,
                          "same_logical_trits": all(same.values())})
    formats = {r["format"] for r in runs}
    per_format = {}
    for fmt in sorted(formats):
        mine = [r for r in runs if r["format"] == fmt]
        per_format[model.FORMAT_NAMES[fmt]] = {
            "runs": len(mine), "words": sorted({r["words"] for r in mine}),
            "cycles_min": min(r["cycles"] for r in mine), "cycles_max": max(r["cycles"] for r in mine),
            "words_per_clock_min": min(r["words_per_clock"] for r in mine),
            "words_per_clock_max": max(r["words_per_clock"] for r in mine),
            "command_stalls_max": max(r["command_stalls"] for r in mine),
            "wait_stalls_max": max(r["wait_stalls"] for r in mine),
            "max_outstanding_max": max(r["max_outstanding"] for r in mine),
            "cap_holds_max": max(r["cap_holds"] for r in mine),
            "fill_cycles_min": min(r["fill_cycles"] for r in mine),
            "fill_cycles_max": max(r["fill_cycles"] for r in mine)}
    return {
        "header": header,
        "runs": runs,
        "partial_run_at_end": partial,
        "problems": problems,
        "timeout": timeout,
        "done": done,
        "pairs": pairs,
        "per_format": per_format,
        "totals": {"runs": len(runs), "passing_runs": sum(r["pass"] for r in runs),
                   "model_checked_runs": checked, "bad_words": sum(r["bad_words"] for r in runs),
                   "invalid_groups": sum(r["invalid_groups"] for r in runs),
                   "consumer_stalls": sum(r["consumer_stalls"] for r in runs),
                   "complete_pairs": len(pairs)},
        "clock_hz_used": hz,
        "notes": ["cycles: controller clocks from the first read request presented to the last ack, inclusive; "
                  "words_per_clock = words / cycles, the raw read path of this in-order single master with "
                  "refresh and row changes included, not the stage-2 weights-per-second comparison (#65)",
                  "read_s and bus_MBps convert cycles at clock_hz_used, the nominal controller clock when the "
                  "report gives one",
                  "runs 2p and 2p+1 hold the same logical trits (key of pair p) as baseline2 and as dense5; "
                  "pairs[].same compares their logical trits, +1 and -1 counts and dot products",
                  "scale_metadata_bytes is 0 by construction: the region stores no scale and no metadata"],
    }


def decode(entries, *, expect_build_id=None, expect_lanes=None, expect_period_ps=None,
           load_end_s=None, nominal_hz=None, min_span_s=1.0, expect_pattern=None, expect_reader=None,
           reader_model_runs=None) -> dict:
    """Decode a timestamped transcript.

    entries: [{"t_s": host seconds on one monotonic clock, "line": text}] in arrival order.
    load_end_s: host seconds (same clock) at which the load returned, if this run loaded.
    expect_pattern: True when the build has the pattern test, False when it has not, None unknown.
    expect_reader: the same for the read path (#62); reader_model_runs limits its model check."""
    headers, status, other, pattern_lines, reader_lines, joined = [], [], [], [], [], []
    for entry in entries:
        parsed = parse_line(entry["line"])
        if parsed is None and len(entry["line"]) > LINE_CHARS:
            # A design that was sending when the load began leaves a cut-off line on the
            # port, and the new design's first line (its H line) is then appended to it:
            # the last LINE_CHARS characters are a whole line of their own.
            parsed = parse_line(entry["line"][-LINE_CHARS:])
            if parsed is not None:
                joined.append({"t_s": entry["t_s"], "line": entry["line"],
                               "recovered": entry["line"][-LINE_CHARS:]})
        if parsed is None:
            other.append({"t_s": entry["t_s"], "line": entry["line"]})
            continue
        parsed["t_s"] = entry["t_s"]
        if parsed["tag"] in READER_TAGS:
            reader_lines.append(parsed)
        elif parsed["tag"] in PATTERN_TAGS:
            pattern_lines.append(parsed)
        else:
            (headers if parsed["tag"] == "H" else status).append(parsed)
    # The S lines after the last H line belong to the design's current reset.
    last_h = headers[-1]["t_s"] if headers else None
    current = [s for s in status if last_h is None or s["t_s"] >= last_h]
    # Clocks rise monotonically within one reset; the 40-bit field wraps every 2^40 clocks (3.665 h at
    # 83.33 MHz), so a drop by more than half the range is a wrap and is unwrapped before the fit.
    unwrapped, add, prev = [], 0, None
    for s in current:
        if prev is not None and prev - s["clocks_low40"] > WRAP // 2:
            add += WRAP
        prev = s["clocks_low40"]
        unwrapped.append(s["clocks_low40"] + add)
    wraps_inside = add // WRAP
    points = [(s["t_s"], u) for s, u in zip(current, unwrapped)]
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
            wraps = [round(((s["t_s"] - load_end_s) * rate - u) / WRAP) for s, u in zip(current, unwrapped)]
            k = max(set(wraps), key=wraps.count)
            estimates = [s["t_s"] - (u + k * WRAP) / rate for s, u in zip(current, unwrapped)]
            reset_s = statistics.median(estimates)
            reset = {"wraps_of_the_40_bit_count": k + wraps_inside, "wraps_within_capture": wraps_inside,
                     "reset_s": round(reset_s, 3),
                     "reset_minus_load_end_s": round(reset_s - load_end_s, 3),
                     "last_line_since_reset_s": round(current[-1]["t_s"] - reset_s, 3)}
        else:
            reset = {"wraps_of_the_40_bit_count": None, "wraps_within_capture": wraps_inside,
                     "last_line_since_reset_s_if_no_wrap": round(unwrapped[-1] / rate, 3),
                     "note": "no load in this run: the time since reset is this value plus an unknown "
                             "multiple of 2^40 clocks (3.665 h at 83.33 MHz) before the first line"}
    final = current[-1] if current else None
    # Calibration as the coalesced lines show it: the first line of each state, and the
    # last line before calib_complete and the first one with it (the finish lies between).
    timeline, seen = [], set()
    for line, u in zip(current, unwrapped):
        if line["state"] not in seen:
            seen.add(line["state"])
            timeline.append({"state": line["state"], "first_seen_clocks": line["clocks_low40"],
                             "first_seen_s": round(u / rate, 4) if rate else None})
    before = [u for line, u in zip(current, unwrapped) if not line["calib_complete"]]
    after = [u for line, u in zip(current, unwrapped) if line["calib_complete"]]
    calibration = {
        "state_timeline": timeline,
        "last_line_before_calib_complete_clocks": before[-1] if before else None,
        "first_line_with_calib_complete_clocks": after[0] if after else None,
    }
    if rate and before and after:
        calibration["calib_complete_between_s"] = [round(before[-1] / rate, 4), round(after[0] / rate, 4)]
    current_pattern = [p for p in pattern_lines if last_h is None or p["t_s"] >= last_h]
    pattern = None
    if current_pattern or expect_pattern:
        pattern = decode_pattern(current_pattern, hz=nominal_hz or rate,
                                 lanes=headers[-1]["byte_lanes"] if headers else expect_lanes)
    has_pattern = bool(current_pattern)
    current_reader = [p for p in reader_lines if last_h is None or p["t_s"] >= last_h]
    reader = None
    if current_reader or expect_reader:
        reader = decode_reader(current_reader, hz=nominal_hz or rate, model_runs=reader_model_runs)
    has_reader = bool(current_reader)
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
        # The PLL makes the controller clock from a 200 MHz oscillator: a fit far from the nominal
        # rate means lines that do not belong together (or a count read wrongly), not a slow clock.
        "clock_near_nominal": (abs(clock["ppm_from_nominal"]) <= CLOCK_TOLERANCE_PPM)
        if clock["ppm_from_nominal"] is not None else None,
        # Pattern test: present exactly when the build has it; every pass reported in order and
        # without a wrong bit; no watchdog stop; at least one round (true and complement pass).
        "pattern_present": (has_pattern == bool(expect_pattern)) if expect_pattern is not None else None,
        "pattern_header": bool(pattern["header"].get("bursts") is not None) if pattern else None,
        "pattern_lanes": (pattern["header"].get("byte_lanes") == headers[-1]["byte_lanes"])
        if pattern and headers and "byte_lanes" in pattern["header"] else None,
        "pattern_well_formed": not pattern["problems"] if pattern else None,
        "pattern_no_wrong_bits": (all(p["clean"] for p in pattern["passes"]) and pattern["timeout"] is None
                                  and (pattern["partial_pass_at_end"] or {}).get("clean_so_far", True))
        if pattern else None,
        "pattern_round_complete": pattern["totals"]["rounds_complete"] >= 1 if pattern else None,
    }
    if reader is not None or expect_reader is not None:
        # Read path (#62), only in records of builds that name it (earlier records keep their
        # checks): present exactly when the build has it; runs in order; every run's counters
        # add up, no bad word, invalid group or consumer stall, the host model's results; no
        # watchdog stop; at least one run of each format; both formats of a pair the same trits.
        checks.update({
            "reader_present": (has_reader == bool(expect_reader)) if expect_reader is not None else None,
            "reader_header": bool(reader["header"].get("trits") is not None) if reader else None,
            "reader_lanes": (reader["header"].get("byte_lanes") == headers[-1]["byte_lanes"])
            if reader and headers and "byte_lanes" in reader["header"] else None,
            "reader_well_formed": not reader["problems"] if reader else None,
            "reader_runs_pass": (all(r["pass"] for r in reader["runs"]) and reader["timeout"] is None
                                 and (reader["partial_run_at_end"] or {}).get("clean_so_far", True))
            if reader else None,
            "reader_both_formats": len(reader["per_format"]) == 2 if reader else None,
            "reader_pairs_same_trits": all(p["same_logical_trits"] for p in reader["pairs"]) if reader else None,
        })
    passed = all(v for v in checks.values() if v is not None)
    result = {"header": headers, "status_lines": status, "unparsed_lines": other, "joined_lines": joined,
              "final": final,
              "clock": clock, "reset": reset, "calibration": calibration, "checks": checks, "pass": passed}
    if pattern is not None:
        result["pattern"] = pattern
    if reader is not None:
        result["reader"] = reader
    return result


# UberDDR3 79d8fd3e with `define UART_DEBUG_BIST` (the UART_DEBUG_BIST build of #61): every message is
# its 100-character uart_text buffer sent from the top, so text shorter than 100 characters arrives
# after NUL padding. The phases of BIST_MODE 1, in order (L3199-3353), then the count of correct reads
# as four raw bytes, most significant first. A wrong read sets wrong_read_data, and from then on the
# controller sends RESET reports (L3410-3440) instead of finishing: wrong_read_data itself is printed
# only in them, so a capture without RESET means it stayed 0 for as long as the capture ran.
BIST_PHASES = [b"DONE BURST WRITE (", b"DONE BURST READ: BIST_MODE=", b"DONE RANDOM WRITE: BIST_MODE=",
               b"DONE RANDOM READ: BIST_MODE=", b"DONE ALTERNATING WRITE-READ", b"DONE BIST_MODE="]
BIST_COUNT_MARK = b"correct_read_data=\n\n"
# BIST_MODE 1 checks 2^23 burst reads, 2^24 random reads and 2^23 - 1 alternating reads (the last
# alternating write is not read back): tools/uberddr3-bist-model.py `operations`.
BIST1_CHECKED_READS = (1 << 23) + (1 << 24) + (1 << 23) - 1


def decode_bist_debug(raw: bytes, entries, *, load_end_s=None, expect_reads=BIST1_CHECKED_READS) -> dict:
    """The UART_DEBUG_BIST text of one load (raw bytes as received, entries with host times).

    The design loaded before may still be sending when the port opens (this build repeats its final
    message), so the load's own text starts at its first phase message, which a design prints once
    per calibration; everything is read from there on."""
    first = raw.find(BIST_PHASES[0])
    own = raw[first:] if first >= 0 else raw
    after = [e for e in entries if load_end_s is None or e["t_s"] >= load_end_s]
    start, lookup = 0, 0
    phases, order_ok = [], True
    for mark in BIST_PHASES:
        at = own.find(mark, start)
        if at < 0:
            order_ok = False
            break
        end = own.find(b"\n", at)
        text = own[at:end if end >= 0 else len(own)].decode("ascii", "replace")
        t_s = None
        for k in range(lookup, len(after)):
            if text[:24] in after[k]["line"]:
                t_s, lookup = after[k]["t_s"], k + 1
                break
        phases.append({"message": text, "t_s": t_s,
                       "since_load_end_s": round(t_s - load_end_s, 3) if t_s is not None and load_end_s is not None
                       else None})
        start = at + len(mark)
    counts, at = [], own.find(BIST_COUNT_MARK)
    while at >= 0 and len(own) >= at + len(BIST_COUNT_MARK) + 4:
        counts.append(int.from_bytes(own[at + len(BIST_COUNT_MARK):at + len(BIST_COUNT_MARK) + 4], "big"))
        at = own.find(BIST_COUNT_MARK, at + 1)
    count = counts[0] if counts else None
    resets = own.count(b"RESET")
    last_t = entries[-1]["t_s"] if entries else None
    done_t = phases[-1]["t_s"] if len(phases) == len(BIST_PHASES) else None
    checks = {
        "all_phases_in_order": order_ok and len(phases) == len(BIST_PHASES),
        "correct_read_data_reported": count is not None,
        "correct_read_data_as_modelled": count == expect_reads if count is not None else False,
        "correct_read_data_same_in_every_repeat": len(set(counts)) == 1 if counts else False,
        "no_reset_report": resets == 0,
        "phases_after_load": all(p["since_load_end_s"] is not None and p["since_load_end_s"] > 0 for p in phases)
        if load_end_s is not None else None,
    }
    return {
        "phases": phases,
        "correct_read_data": count,
        "done_message_repeats": len(counts),
        "expected_correct_read_data": expect_reads,
        "reset_reports": resets,
        "bytes_before_this_load_text": first if first >= 0 else None,
        "capture_after_done_s": round(last_t - done_t, 3) if done_t is not None and last_t is not None else None,
        "checks": checks,
        "pass": all(v for v in checks.values() if v is not None),
        "notes": ["correct_read_data counts the self-test reads whose data matched (L3638-3650); it is never "
                  "reset, not even by a recalibration",
                  "wrong_read_data is printed only in RESET reports, which the controller sends from the first "
                  "wrong read on; no RESET report in the capture means wrong_read_data stayed 0 while it ran",
                  "the DONE message comes from FINISH_READ, in the clock that sets final_calibration_done "
                  "(= o_calib_complete); on the board it then repeated about every 0.12 s with the same count",
                  "the expected count is the BIST_MODE 1 model's number of checked reads; the self-test covers "
                  "3/4 of the bursts and cannot see some address faults (docs/hardware.md, self-test coverage)"],
    }


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
    uart_debug_bist = "UART_DEBUG_BIST 1" in ddr3.get("variant", "")
    # The read path (#62) names itself READER in the variant; earlier builds have none.
    reader = "READER (" in ddr3.get("variant", "")
    return {"build_id": ddr3.get("netlist_build_id"), "lanes": lanes, "pattern": pattern,
            "uart_debug_bist": uart_debug_bist, "reader": reader,
            "period_ps": round(ddr_clock["period_ns"] * 1000) if ddr_clock.get("period_ns") else None,
            "nominal_hz": round(1e9 / ctrl_clock["period_ns"], 1) if ctrl_clock.get("period_ns") else None}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_kept(path: Path) -> bytes:
    """A capture file as recorded: long captures are committed gzip-compressed as <name>.gz
    (the record's sha256 values are those of the uncompressed file)."""
    if not path.exists() and path.with_name(path.name + ".gz").exists():
        return gzip.decompress(path.with_name(path.name + ".gz").read_bytes())
    return path.read_bytes()


def write_kept(path: Path, data: bytes) -> None:
    """Write a capture file back the way it is kept: <name>.gz (gzip -n: no name, no time) when only
    that exists, otherwise the plain file."""
    if not path.exists() and path.with_name(path.name + ".gz").exists():
        path.with_name(path.name + ".gz").write_bytes(gzip.compress(data, compresslevel=9, mtime=0))
    else:
        path.write_bytes(data)


def tool_revision() -> dict:
    """The repository revision this tool ran from, and whether tracked files differed from it."""
    def git(*cmd):
        proc = subprocess.run(["git", "-C", str(ROOT), *cmd], capture_output=True, text=True)
        return proc.stdout.strip() if proc.returncode == 0 else None
    status = git("status", "--porcelain", "--untracked-files=no")
    return {"head": git("rev-parse", "HEAD"), "tracked_changes": None if status is None else bool(status),
            "tool_changed": None if status is None else any(line.endswith("tools/fpga-ddr3-capture.py")
                                                            for line in status.splitlines())}


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
    parser.add_argument("--redecode", type=Path, metavar="CAPTURE_DIR",
                        help="decode the transcript of a committed capture again with this decoder and rewrite "
                             "its `decoded` (the earlier checks and the reason are kept in `redecoded`)")
    parser.add_argument("--reason", default="", help="with --redecode: why the capture is decoded again")
    parser.add_argument("--port")
    parser.add_argument("--baud", type=int, help="default 115200, or 9600 for a UART_DEBUG_BIST build")
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

    if args.redecode:
        path = args.redecode / "capture.json"
        record = json.loads(read_kept(path))
        entries = []
        for raw in read_kept(args.redecode / record["uart_transcript"]["file"]).decode().splitlines():
            if raw.startswith("#") or not raw.strip():
                continue
            t_s, _utc, line = raw.split("\t", 2)
            entries.append({"t_s": float(t_s), "line": line})
        rec_expect = record["expect"]
        if rec_expect.get("uart_debug_bist"):
            raw_bytes = read_kept(args.redecode / record["uart_transcript"]["raw_file"])
            result = decode_bist_debug(raw_bytes, entries, load_end_s=record["run"].get("load_end_s"))
            before = record.get("decoded_bist", {})
            record.setdefault("redecoded", []).append({
                "utc": utc(), "reason": args.reason, "tool_revision": tool_revision(),
                "checks_before": before.get("checks"),
                "pass_before": before.get("pass"), "exit_status_before": record.get("exit_status")})
            record["decoded_bist"] = result
            if record.get("exit_status") in (0, 1):
                record["exit_status"] = 0 if result["pass"] else 1
            write_kept(path, (json.dumps(record, indent=1) + "\n").encode())
            print(json.dumps({"pass": result["pass"], "checks": result["checks"],
                              "correct_read_data": result["correct_read_data"]}, indent=1))
            return 0 if result["pass"] else 1
        result = decode(entries, expect_build_id=rec_expect["build_id"], expect_lanes=rec_expect["lanes"],
                        expect_period_ps=rec_expect["period_ps"], load_end_s=record["run"].get("load_end_s"),
                        nominal_hz=rec_expect["nominal_hz"], expect_pattern=rec_expect.get("pattern"),
                        expect_reader=rec_expect.get("reader"))
        before = record.get("decoded", {})
        record.setdefault("redecoded", []).append({
            "utc": utc(), "reason": args.reason, "tool_revision": tool_revision(),
            "checks_before": before.get("checks"),
            "pass_before": before.get("pass"), "exit_status_before": record.get("exit_status")})
        record["decoded"] = result
        if record.get("exit_status") in (0, 1):
            record["exit_status"] = 0 if result["pass"] else 1
        write_kept(path, (json.dumps(record, indent=1) + "\n").encode())
        print(json.dumps({"pass": result["pass"], "checks": result["checks"], "final": result["final"]}, indent=1))
        return 0 if result["pass"] else 1

    if args.decode:
        entries = []
        for raw in read_kept(args.decode).decode().splitlines():
            if raw.startswith("#") or not raw.strip():
                continue
            t_s, _utc, line = raw.split("\t", 2)
            entries.append({"t_s": float(t_s), "line": line})
        result = decode(entries, expect_build_id=expect["build_id"], expect_lanes=expect["lanes"],
                        expect_period_ps=expect["period_ps"], load_end_s=args.load_end_s,
                        nominal_hz=expect["nominal_hz"], expect_pattern=expect.get("pattern"),
                        expect_reader=expect.get("reader"))
        print(json.dumps(result, indent=1))
        return 0 if result["pass"] else 1

    if not args.port or not args.output_dir or (not args.no_load and not args.bit):
        parser.error("a board run needs --port, --output-dir and --bit (or --no-load)")
    if args.baud is None:
        args.baud = 9600 if expect["uart_debug_bist"] else 115200
    revision = tool_revision()
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
        "tool_revision": revision,
        "argv": [repo_relative(a) for a in sys.argv],
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
    if status == 0 and expect["uart_debug_bist"]:
        # Every byte received; lines of the design loaded before (115200 baud read at 9600) are noise.
        record["decoded_bist"] = decode_bist_debug(raw, entries, load_end_s=run.get("load_end_s"))
        status = 0 if record["decoded_bist"]["pass"] else 1
    elif status == 0:
        record["decoded"] = decode(entries, expect_build_id=expect["build_id"], expect_lanes=expect["lanes"],
                                   expect_period_ps=expect["period_ps"], load_end_s=run.get("load_end_s"),
                                   nominal_hz=expect["nominal_hz"], expect_pattern=expect.get("pattern"),
                        expect_reader=expect.get("reader"))
        status = 0 if record["decoded"]["pass"] else 1
    record["exit_status"] = status
    (out / "capture.json").write_text(json.dumps(record, indent=1) + "\n")
    summary = record.get("decoded", {})
    if "decoded_bist" in record:
        print(json.dumps({"exit_status": status, "stopped": run.get("stopped"),
                          **{k: v for k, v in record["decoded_bist"].items() if k != "notes"},
                          "xadc_before": run.get("xadc_before", {}).get("temp"),
                          "xadc_after": run.get("xadc_after", {}).get("temp")}, indent=1))
        return status
    print(json.dumps({"exit_status": status, "stopped": run.get("stopped"), "checks": summary.get("checks"),
                      "final": summary.get("final"), "clock": summary.get("clock"), "reset": summary.get("reset"),
                      "pattern_totals": (summary.get("pattern") or {}).get("totals"),
                      "reader_totals": (summary.get("reader") or {}).get("totals"),
                      "reader_per_format": (summary.get("reader") or {}).get("per_format"),
                      "xadc_before": run.get("xadc_before", {}).get("temp"),
                      "xadc_after": run.get("xadc_after", {}).get("temp")}, indent=1))
    return status


if __name__ == "__main__":
    sys.exit(main())
