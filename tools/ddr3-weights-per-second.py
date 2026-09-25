#!/usr/bin/env python3
"""Weights per second from DDR3 (issue #65) from committed captures: per consumer, region and
format the statistic of #65, the ratio of the two formats against the ceiling, the memory-bound
verdict with what it rests on, and where every figure came from. Nothing is measured here.

  python3 tools/ddr3-weights-per-second.py \\
      --delivery reports/fpga/ddr3-reader-2026-09-24-42b6f5a9-x16-seed11 \\
      [--workload reports/fpga/matvec-ddr3-<date>-<id>/capture.json ...] \\
      --output reports/fpga/ddr3-weights-per-second-<date>.json
  python3 tools/ddr3-weights-per-second.py --check reports/fpga/ddr3-weights-per-second-<date>.json

Inputs:
- --delivery DIR (consumer (A), repeatable): the loads of one #62 read-path build
  (DIR/load<n>/capture.json[.gz] of tools/fpga-ddr3-capture.py, trinity.ddr3-capture.v1). Each
  load's UART transcript must have the sha256 its record gives, and decoded again here (without
  the host model, which the record's own decode ran on every run) it must give the record's
  counters for every run and the same calibration checks. Per run: the logical trits (line n)
  and the read cycles (line c: the first read request presented to the last read ack, inclusive).
- --workload FILE (consumer (B), repeatable): a record of tools/fpga-matvec-capture.py
  (trinity.fpga-ddr-capture.v1); the bytes it received (FILE.rx.bin.gz) must have the sha256 it
  gives. Per run: rows x cols and the cycles of Z line 4 (the first word taken to the last,
  inclusive).

Definitions (docs/hardware.md, "Weights per second from DDR3 (#65)"): weights per second of a
run = logical trits (padding lanes excluded) x f_ctrl / cycles, f_ctrl = 250/3 MHz (the board's
200 MHz oscillator through the DDR3 tops' PLL: arithmetic, not measured). The statistic, per
consumer, region and format, over the runs used: n, min, median, max, mean and sample s.d. A run
is used when it passed every check of its record (A: its own checks and the host model's counts,
dot product and checksum; B: status 0, every accumulator equal to the reference, the result
checksum) and the calibration is shown to hold around it (A: the load's last status line shows
calibration complete, state 23 and highest state 23, no return to IDLE since reset, and the
reset at the load; B: the status read after the format's runs); the others are listed with the
reason. The ratio is the dense5 median over the baseline2 median, with the bracket
min(dense5) / max(baseline2) .. max(dense5) / min(baseline2), against the ceiling 80 / 64 = 1.25.
Memory-bound: consumer stalls 0 in every run used of both formats; each row says what that count
rests on in its design.

--check recomputes the summary (from the inputs it names, unless others are given) and compares
it with the file, all but written_utc, tool_revision and argv. Exit status 0 when it matches
(without --check: when every input was consistent), 1 otherwise.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import importlib.util
import json
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = "trinity.ddr3-weights-per-second.v1"
F_CTRL_HZ = 250e6 / 3
TRITS_PER_WORD = {"dense5": 80, "baseline2": 64}
FORMATS = tuple(TRITS_PER_WORD)
CEILING = TRITS_PER_WORD["dense5"] / TRITS_PER_WORD["baseline2"]
VOLATILE = ("written_utc", "tool_revision", "argv")
# Counters of a #62 run that decoding the transcript again must reproduce.
COUNTERS_A = ("run", "pair", "format", "words", "cycles", "logical_trits", "consumer_stalls", "command_stalls",
              "wait_stalls", "cap_holds", "max_outstanding", "bad_words", "invalid_groups", "stray_acks")
CALIB_CHECKS_A = ("calib_complete", "done_calibrate", "no_return_to_idle", "reset_at_load")
Z_RAN = 0

DEFINITIONS = {
    "weights_per_s": "logical trits of the run (padding lanes excluded) x f_ctrl / cycles",
    "f_ctrl_hz": "250/3 MHz: the board's 200 MHz oscillator x 5 / (4 x 3) in the DDR3 tops' PLL (PLL_MULT 5, "
                 "DDR_DIV 3); arithmetic, not an instrument measurement",
    "cycles": {"A": "line c of the read path: clocks from the first read request presented to the last read ack, "
                    "inclusive (the first read's latency is in them)",
               "B": "Z line 4 of the matvec: clocks from the first word taken to the last, inclusive (the first "
                    "read's latency is in Z line 6, with SETUP and MASK, not in them)"},
    "statistic": "over the runs used: n, min, median, max, mean and sample s.d., every figure to 9 significant "
                 "digits",
    "ratio": "median(dense5) / median(baseline2) of the weights per second; bracket min(dense5) / max(baseline2) "
             ".. max(dense5) / min(baseline2)",
    "ceiling": "80 / 64 = 1.25 logical trits per 128-bit word, payload only (padding, scales and metadata count "
               "against it); x16 at 667 MT/s moves one 128-bit word per controller clock, so the peaks are 80 and "
               "64 trits per clock: 6.667 G (dense5) and 5.333 G (baseline2) weights per second",
    "memory_bound": "consumer stalls 0 in every run used of both formats: the consumer never held the bus off, so "
                    "the gap to one word per clock is the bus's (command and wait stalls, refresh, row changes)",
}
BASIS = {
    "A": "by construction: consumer (A)'s ready is the constant 1 (docs/hardware.md, 'Which counters are "
         "evidence'), so its stall count cannot be nonzero; that it keeps up rests on one word taken in every "
         "clock and on the build meeting 83.33 MHz. Its command stalls, wait stalls and cap holds split the gap",
    "B": "by construction in the DDR3 matvec build: the feed asks for the first word only after the matvec's "
         "in_ready rose, and in_ready stays high until the run's last word, so Z line 9 cannot count; that the "
         "matvec keeps up rests on one word taken in every clock of RUN and on the build meeting 83.33 MHz. The "
         "idle clocks (Z line 5) are the gap; they would also hold any clock in which the feed's cap of 64 "
         "outstanding requests held a request back, which the board does not report",
}
THESIS_A = ("delivery: shows whether DDR3 delivers and the decoders keep up; not by itself evidence for the "
            "thesis (#65)")
THESIS_B = "workload: the device matvec's weights per second, the ratio that tests the thesis (#65)"


def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def kept(path: Path) -> Path:
    """The file as committed: long captures are kept gzip-compressed as <name>.gz."""
    return path if path.exists() or not path.with_name(path.name + ".gz").exists() else path.with_name(path.name + ".gz")


def read_kept(path: Path) -> bytes:
    """A capture file's bytes as recorded (the sha256 in a record is the uncompressed file's)."""
    found = kept(path)
    data = found.read_bytes()
    return gzip.decompress(data) if found != path else data


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sig(x):
    """A figure to 9 significant digits, so that the summary is the same on every Python."""
    return x if isinstance(x, int) else float(f"{x:.9g}")


def spread(values):
    if not values:
        return None
    return {"n": len(values), "min": sig(min(values)), "median": sig(statistics.median(values)),
            "max": sig(max(values)), "mean": sig(statistics.fmean(values)),
            "sd": sig(statistics.stdev(values)) if len(values) > 1 else None}


def span(values):
    return {"min": min(values), "max": max(values)} if values else None


def tool_revision() -> dict:
    def git(*cmd):
        proc = subprocess.run(["git", "-C", str(ROOT), *cmd], capture_output=True, text=True)
        return proc.stdout.strip() if proc.returncode == 0 else None
    status = git("status", "--porcelain", "--untracked-files=no")
    return {"head": git("rev-parse", "HEAD"), "tracked_changes": None if status is None else bool(status)}


def capture_tool():
    spec = importlib.util.spec_from_file_location("fpga_ddr3_capture", ROOT / "tools/fpga-ddr3-capture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def transcript_entries(data: bytes) -> list[dict]:
    entries = []
    for raw in data.decode().splitlines():
        if raw.startswith("#") or not raw.strip():
            continue
        t_s, _utc, line = raw.split("\t", 2)
        entries.append({"t_s": float(t_s), "line": line})
    return entries


def format_stats(runs: list[dict], consumer: str) -> dict:
    used = [r for r in runs if r["excluded"] is None]
    out = {"runs": len(runs), "used": len(used),
           "excluded": [{k: r[k] for k in ("where", "run", "excluded")} for r in runs if r["excluded"] is not None],
           "logical_trits": sorted({r["trits"] for r in runs}), "words": sorted({r["words"] for r in runs}),
           "cycles": span([r["cycles"] for r in used]),
           "weights_per_s": spread([r["trits"] * F_CTRL_HZ / r["cycles"] for r in used]),
           "words_per_clock": spread([r["words"] / r["cycles"] for r in used]),
           "consumer_stalls_max": max((r["consumer_stalls"] for r in used), default=None)}
    if consumer == "A":
        out.update({"command_stalls": span([r["command_stalls"] for r in used]),
                    "wait_stalls": span([r["wait_stalls"] for r in used]),
                    "cap_holds_max": max((r["cap_holds"] for r in used), default=None),
                    "max_outstanding": max((r["max_outstanding"] for r in used), default=None)})
    else:
        out.update({"idle_clocks": span([r["idle_clocks"] for r in used]),
                    "idle_fraction": spread([r["idle_clocks"] / r["cycles"] for r in used]),
                    "latency": span([r["latency"] for r in used]),
                    "invalid_codes_max": max((r["invalid_codes"] for r in used), default=None)})
    return out


def finish_row(row: dict, runs: list[dict], consumer: str) -> dict:
    row["formats"] = {name: format_stats([r for r in runs if r["format"] == name], consumer)
                      for name in FORMATS if any(r["format"] == name for r in runs)}
    stats = {name: f["weights_per_s"] for name, f in row["formats"].items()}
    if all(stats.get(name) for name in FORMATS):
        d5, b2 = stats["dense5"], stats["baseline2"]
        row["ratio_dense5_baseline2"] = {"of_medians": sig(d5["median"] / b2["median"]),
                                         "bracket": [sig(d5["min"] / b2["max"]), sig(d5["max"] / b2["min"])],
                                         "ceiling": CEILING}
    else:
        row["ratio_dense5_baseline2"] = None
    used = [r for r in runs if r["excluded"] is None]
    both = all(row["formats"].get(name, {}).get("used") for name in FORMATS)
    row["memory_bound"] = {"verdict": both and all(r["consumer_stalls"] == 0 for r in used),
                           "consumer_stalls_max": max((r["consumer_stalls"] for r in used), default=None),
                           "basis": BASIS[consumer]}
    row["every_run_passed"] = all(r["passed"] for r in runs)
    return row


def delivery_row(directory: Path, tool) -> tuple[dict, list[str]]:
    """Consumer (A): the loads of one #62 read-path build."""
    problems, loads, runs, header = [], [], [], None
    for load_dir in sorted((p for p in directory.iterdir() if p.is_dir() and p.name.startswith("load")),
                           key=lambda p: (len(p.name), p.name)):
        record_file = kept(load_dir / "capture.json")
        record = json.loads(read_kept(load_dir / "capture.json"))
        decoded = record.get("decoded") or {}
        reader = decoded.get("reader")
        if record.get("schema") != "trinity.ddr3-capture.v1" or not reader:
            problems.append(f"{rel(load_dir)}: not a read-path capture")
            continue
        transcript = read_kept(load_dir / record["uart_transcript"]["file"])
        transcript_ok = sha256(transcript) == record["uart_transcript"]["sha256"]
        expect = record["expect"]
        again = tool.decode(transcript_entries(transcript), expect_build_id=expect["build_id"],
                            expect_lanes=expect["lanes"], expect_period_ps=expect["period_ps"],
                            load_end_s=record["run"].get("load_end_s"), nominal_hz=expect["nominal_hz"],
                            expect_pattern=expect.get("pattern"), expect_reader=expect.get("reader"),
                            reader_model_runs=0)
        counters_ok = ([{k: r.get(k) for k in COUNTERS_A} for r in (again.get("reader") or {}).get("runs", [])]
                       == [{k: r.get(k) for k in COUNTERS_A} for r in reader["runs"]])
        calibration = {k: decoded["checks"].get(k) for k in CALIB_CHECKS_A}
        calibration_again = {k: again["checks"].get(k) for k in CALIB_CHECKS_A}
        held = all(v is True for v in calibration.values()) and calibration == calibration_again
        if not transcript_ok:
            problems.append(f"{rel(load_dir)}: the transcript's sha256 is not the record's")
        elif not counters_ok:
            problems.append(f"{rel(load_dir)}: the transcript decoded again does not give the record's counters")
        if calibration != calibration_again:
            problems.append(f"{rel(load_dir)}: the transcript decoded again does not give the record's calibration")
        header = header or reader["header"]
        run_info = record.get("run") or {}
        loads.append({
            "load": load_dir.name, "label": record.get("label"), "t0_utc": run_info.get("t0_utc"),
            "capture": {"file": rel(record_file), "sha256": sha256(record_file.read_bytes())},
            "transcript": {"file": rel(kept(load_dir / record["uart_transcript"]["file"])),
                           "sha256": record["uart_transcript"]["sha256"], "equals_record": transcript_ok,
                           "counters_decoded_again_equal": counters_ok},
            "bitstream": {k: (record.get("bitstream") or {}).get(k)
                          for k in ("report", "report_sha256", "report_sha256_from_sync")},
            "build_id": expect.get("build_id"), "idcode": run_info.get("idcode"), "dna": run_info.get("dna"),
            "die_c_before_after": [(run_info.get("xadc_before") or {}).get("temp"),
                                   (run_info.get("xadc_after") or {}).get("temp")],
            "calibration": calibration, "calibration_held": held,
            "runs": len(reader["runs"]), "partial_run_at_end_not_counted": reader.get("partial_run_at_end") is not None})
        for r in reader["runs"]:
            reason = None
            if not held:
                reason = "the calibration was not shown to hold in this load"
            elif not (transcript_ok and counters_ok):
                reason = "the transcript does not give the record's counters"
            elif not r.get("pass"):
                reason = "failed: " + ", ".join(k for k, v in sorted(r.get("checks", {}).items()) if v is False)
            runs.append({"where": load_dir.name, "run": r["run"], "format": r["format_name"],
                         "trits": r["logical_trits"], "words": r["words"], "cycles": r["cycles"],
                         "consumer_stalls": r["consumer_stalls"], "command_stalls": r["command_stalls"],
                         "wait_stalls": r["wait_stalls"], "cap_holds": r["cap_holds"],
                         "max_outstanding": r["max_outstanding"], "passed": bool(r.get("pass")),
                         "excluded": reason})
    if not loads:
        problems.append(f"{rel(directory)}: no read-path loads")
    header = header or {}
    row = {"consumer": "A", "source": rel(directory), "what": THESIS_A,
           "design": "t27/rtl/fpga_ddr3_reader.t27 (#62), make -C fpga/ax7203 ddr3-bit DDR3_APP=reader",
           "region": {"logical_trits_per_run": header.get("trits"), "seed": header.get("seed"),
                      "cap": header.get("cap"),
                      "note": "trits from the reader's generator (a new key per pair of runs), written by each "
                              "run's fill before its read; both formats of a pair carry the same trits, from "
                              "burst 0"},
           "loads": loads}
    return finish_row(row, runs, "A"), problems


def workload_row(path: Path) -> tuple[dict, list[str]]:
    """Consumer (B): one record of tools/fpga-matvec-capture.py."""
    problems, runs = [], []
    record = json.loads(read_kept(path))
    if record.get("schema") != "trinity.fpga-ddr-capture.v1":
        return {"consumer": "B", "source": rel(path), "formats": {}}, [f"{rel(path)}: not a matvec capture"]
    rx = record.get("rx") or {}
    rx_path = path.with_name(rx["file"]) if rx.get("file") else None
    rx_ok = bool(rx_path and rx_path.exists() and sha256(gzip.decompress(rx_path.read_bytes())) == rx.get("sha256"))
    if not rx_ok:
        problems.append(f"{rel(path)}: the received bytes are missing or not the record's sha256")
    chunk = record.get("chunk") or {}
    rows, cols = chunk.get("rows") or [0, 0], chunk.get("cols") or 0
    if not chunk.get("reference_matches_report"):
        problems.append(f"{rel(path)}: the chunk's reference does not match the committed matvec report")
    if rows[1] <= rows[0] or cols <= 0:
        problems.append(f"{rel(path)}: the record names no chunk rows and columns")
    for name, entry in (record.get("formats") or {}).items() if rows[1] > rows[0] and cols > 0 else ():
        held = entry.get("calib_held") is True
        for i, run in enumerate(entry.get("runs", [])):
            z = run.get("z") or {}
            passed = bool(run.get("status") == Z_RAN and run.get("equal_reference") and run.get("checksum_ok")
                          and z.get("cycles"))
            reason = None
            if not held:
                reason = "the calibration was not shown to hold after this format's runs"
            elif not rx_ok:
                reason = "the received bytes are missing or not the record's sha256"
            elif not passed:
                reason = (f"status {run.get('status')}, accumulators equal {run.get('equal_reference')}, "
                          f"checksum {run.get('checksum_ok')}")
            runs.append({"where": name, "run": i, "format": name, "trits": (rows[1] - rows[0]) * cols,
                         "words": z.get("words"), "cycles": z.get("cycles"), "idle_clocks": z.get("idle_clocks"),
                         "latency": z.get("latency"), "invalid_codes": z.get("invalid_codes"),
                         "consumer_stalls": z.get("consumer_stalls"), "passed": passed, "excluded": reason})
    before, after = record.get("identity_before") or {}, record.get("identity_after") or {}

    def value(block, key):
        return (block.get(key) or {}).get("value")
    row = {"consumer": "B", "source": rel(path), "what": THESIS_B,
           "design": "t27/rtl/fpga_ddr3_matvec.t27 fed by t27/rtl/fpga_matvec_feed.t27 (#64), make -C fpga/ax7203 "
                     "ddr3-bit DDR3_APP=matvec",
           "region": {"tensor": chunk.get("tensor"), "rows": rows, "cols": cols,
                      "logical_trits_per_run": (rows[1] - rows[0]) * cols,
                      "reference_full_sha256": chunk.get("reference_full_sha256"),
                      "note": "the #64 chunk, loaded once per format (read back and compared)"},
           "loads": [{"capture": {"file": rel(path), "sha256": sha256(kept(path).read_bytes())},
                      "rx": {"file": rel(rx_path) if rx_path else None, "sha256": rx.get("sha256"),
                             "equals_record": rx_ok},
                      "started_utc": record.get("started_utc"), "bitstream": record.get("bitstream"),
                      "device": record.get("device"), "idcode": value(before, "idcode"), "dna": value(before, "dna"),
                      "die_c_before_after": [(value(before, "xadc") or {}).get("temp"),
                                             (value(after, "xadc") or {}).get("temp")],
                      "calibration_held": {name: e.get("calib_held") for name, e in (record.get("formats") or {}).items()}}]}
    return finish_row(row, runs, "B"), problems


def outcome(row: dict) -> str:
    who = {"A": "Consumer (A), delivery", "B": "Consumer (B), the matvec"}[row["consumer"]]
    parts = []
    for name in FORMATS:
        f = row["formats"].get(name)
        if f and f["weights_per_s"]:
            w = f["weights_per_s"]
            parts.append(f"{name} {w['median'] / 1e9:.4f} G weights per second (median of {w['n']} runs, "
                         f"{w['min'] / 1e9:.4f}-{w['max'] / 1e9:.4f} G)")
        else:
            parts.append(f"{name} no run used")
    ratio = row["ratio_dense5_baseline2"]
    text = f"{who}, {row['source']}: " + "; ".join(parts)
    if ratio:
        text += (f"; dense5 / baseline2 {ratio['of_medians']:.5f} (bracket {ratio['bracket'][0]:.5f}-"
                 f"{ratio['bracket'][1]:.5f}) against the ceiling {CEILING}")
    text += "; memory-bound " + ("yes, by construction of the design" if row["memory_bound"]["verdict"] else "not shown")
    return text + "."


def summarize(delivery: list[Path], workload: list[Path]) -> tuple[dict, list[str]]:
    tool = capture_tool() if delivery else None
    rows, problems = [], []
    for directory in delivery:
        row, issues = delivery_row(directory, tool)
        rows.append(row)
        problems += issues
    for path in workload:
        row, issues = workload_row(path)
        rows.append(row)
        problems += issues
    not_measured = []
    if not any(r["consumer"] == "B" for r in rows):
        not_measured.append("consumer (B), the device matvec from DDR3: no board capture yet (the DDR3 matvec build "
                            "has not been built into a bitstream)")
    not_measured.append("consumer (A) on the #64 chunk: no build reads loaded weights through consumer (A); its "
                        "rows here are on the #62 reader's own generated region")
    summary = {
        "schema": SCHEMA, "issue": "#65", "written_by": "tools/ddr3-weights-per-second.py",
        "inputs": {"delivery": [rel(d) for d in delivery], "workload": [rel(w) for w in workload]},
        "f_ctrl_hz": F_CTRL_HZ, "definitions": DEFINITIONS,
        "ceiling": {"ratio": CEILING, "peak_weights_per_s": {name: sig(lanes * F_CTRL_HZ)
                                                             for name, lanes in TRITS_PER_WORD.items()}},
        "rows": rows, "outcome": [outcome(r) for r in rows], "not_measured": not_measured, "problems": problems}
    return summary, problems


def comparable(summary: dict) -> dict:
    return {k: v for k, v in summary.items() if k not in VOLATILE}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--delivery", type=Path, action="append", default=[], help="a #62 read-path capture directory")
    p.add_argument("--workload", type=Path, action="append", default=[], help="a fpga-matvec-capture.py record")
    p.add_argument("--output", type=Path, help="write the summary here")
    p.add_argument("--check", type=Path, help="recompute and compare with this summary")
    args = p.parse_args(argv)
    committed = None
    if args.check:
        committed = json.loads(args.check.read_text())
        if not (args.delivery or args.workload):
            args.delivery = [ROOT / d for d in committed["inputs"]["delivery"]]
            args.workload = [ROOT / w for w in committed["inputs"]["workload"]]
    if not (args.delivery or args.workload):
        p.error("give --delivery or --workload (or --check with a summary that names its inputs)")
    if not (args.output or args.check):
        p.error("give --output or --check")
    summary, problems = summarize(args.delivery, args.workload)
    for line in summary["outcome"]:
        print(line)
    for line in problems:
        print(f"problem: {line}", file=sys.stderr)
    if args.check:
        same = comparable(summary) == comparable(committed)
        print(f"{args.check}: {'reproduced' if same else 'DIFFERS from the recomputed summary'}")
        return 0 if same and not problems else 1
    summary = {**summary, "written_utc": utc(), "tool_revision": tool_revision(),
               "argv": ["tools/ddr3-weights-per-second.py", *(sys.argv[1:] if argv is None else argv)]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=1) + "\n")
    print(f"summary: {args.output}")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
