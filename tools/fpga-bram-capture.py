#!/usr/bin/env python3
"""Capture the AX7203 block-RAM trit packing bench and compare it with the model.

Reads the 20-byte report lines of fpga/ax7203/tms_bram_bench.v (from a serial
port, or from a file written by tests/tb_fpga_bram_bench.v), checks every engine
against the manifest of its build (written by tools/generate-bram-bench.py from
tools/bram_trit_model.py; build/fpga/bram-N for one layout N, build/fpga/bram
for all three, the default derived from --expect-formats) and writes a
`trinity.fpga-bram-capture.v1` report. Exit status 1 on any difference, any bad
word or invalid group, or engines that disagree on the stored trits.

  python3 tools/fpga-bram-capture.py --port /dev/cu.usbserial-110 --expect-formats 2 \
      --manifest build/fpga/bram-2/bram_bench_manifest.json --output build/fpga/bram-2/capture.json
  python3 tools/fpga-bram-capture.py --from-file build/fpga/bram-sim/sim_capture_1.txt \
      --manifest build/fpga/bram-sim/bram_bench_manifest.json --expect-formats 0,1,2
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from fpga_uart import read_port  # noqa: E402

LINE = re.compile(r"^([HPQSMINUZD])([0-9a-f]{8})([0-9a-f]{10})$")
FIELDS = {"P": "words", "Q": "write_ticks", "S": "read_ticks", "M": "bad_words", "I": "invalid_groups",
          "N": "counts", "U": "dot", "Z": "chk40"}
B40 = (1 << 40) - 1
CLOCK_HZ = 25_000_000   # one enabled tick per 40 ns in both clock variants


def parse(data: bytes):
    """Return (runs, bad_lines). A run starts at an H line and is complete at its D line."""
    runs, bad, current = [], [], None
    for raw in data.decode("ascii", "replace").split("\n"):
        if not raw:
            continue
        match = LINE.match(raw)
        if not match:
            bad.append(raw)
            continue
        tag, a, b = match.group(1), int(match.group(2), 16), int(match.group(3), 16)
        if tag == "H":
            current = {"run": a, "format": b >> 32, "engines_announced": b & 0xFFFF, "engines": {}, "done": None}
            runs.append(current)
            continue
        if current is None:
            bad.append(raw)
            continue
        if tag == "D":
            current["done"] = {"bad_words": a, "invalid_groups": b}
            continue
        if tag == "P":
            engine = a >> 16
            current["engines"][engine] = {"engine": engine, "format": (a >> 8) & 0xFF, "lanes": a & 0xFF, "words": b}
            continue
        entry = current["engines"].setdefault(a, {"engine": a})
        if tag == "N":
            entry["pos"], entry["neg"] = b >> 20, b & 0xFFFFF
        elif tag == "U":
            entry["dot"] = b - (1 << 40) if b >> 39 else b
        else:
            entry[FIELDS[tag]] = b
    return runs, bad


def compare(run, manifest, expect_formats=None):
    """Compare every engine the run reports (matched by format) with the manifest.

    A bitstream may carry all three layouts or one (ONLY in tms_bram_bench.v); the
    engines it announces must all be present and each must match its layout. With
    expect_formats the set of reported layouts must be exactly that set.
    """
    rows, ok = [], True
    reported = [e for e in run["engines"].values() if "format" in e]
    if (run["format"] != manifest["report_format"] or not reported
            or run["engines_announced"] != len(run["engines"]) or len(reported) != len(run["engines"])):
        ok = False
    if expect_formats is not None and sorted(e["format"] for e in reported) != sorted(expect_formats):
        ok = False
    by_format = {e["format"]: e for e in manifest["engines"]}
    for got in sorted(reported, key=lambda e: e["engine"]):
        expect = by_format.get(got["format"])
        if expect is None:
            ok = False
            continue
        index = got["engine"]
        want = {"format": expect["format"], "lanes": expect["lanes"], "words": expect["words"],
                "write_ticks": expect["write_ticks"], "read_ticks": expect["read_ticks"], "bad_words": 0,
                "invalid_groups": 0, "pos": expect["pos"], "neg": expect["neg"], "dot": expect["dot"],
                "chk40": expect["chk"] & B40}
        diffs = {k: {"expected": v, "device": got.get(k)} for k, v in want.items() if got.get(k) != v}
        ok = ok and not diffs
        rows.append({"engine": index, "format": expect["format"], "name": expect["name"], "match": not diffs, "differences": diffs,
                     "device": got, "expected": want,
                     "trits": expect["trits"], "physical_bits_per_trit": expect["physical_bits_per_trit"],
                     "ramb36_at_1k_x_36": expect["ramb36_at_1k_x_36"],
                     "trits_per_read_tick": (expect["trits"] / got["read_ticks"]) if got.get("read_ticks") else None,
                     "read_microseconds": (got["read_ticks"] / CLOCK_HZ * 1e6) if got.get("read_ticks") else None})
    same = {(r["device"].get("pos"), r["device"].get("neg"), r["device"].get("dot")) for r in rows}
    # With one engine there is nothing to agree with; its counts are checked against the model above.
    agree = (len(same) == 1) if len(rows) > 1 else None
    if run["done"] is None or run["done"]["bad_words"] or run["done"]["invalid_groups"]:
        ok = False
    return ok and agree is not False, rows, agree


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--settle", type=float, default=0.5)
    parser.add_argument("--quiet", type=float, default=0.5)
    parser.add_argument("--trigger-byte", type=lambda v: int(v, 0), default=0xFF)
    parser.add_argument("--no-trigger", action="store_true")
    parser.add_argument("--from-file")
    parser.add_argument("--manifest", help="default: build/fpga/bram-N/ for a single --expect-formats N, else build/fpga/bram/")
    parser.add_argument("--output")
    parser.add_argument("--raw")
    parser.add_argument("--label", default="")
    parser.add_argument("--expect-formats", type=lambda v: [int(x) for x in v.split(",") if x != ""],
                        help="comma-separated layouts the run must report, e.g. 0,1,2 or 2 (0 b2, 1 d5, 2 d5d2)")
    args = parser.parse_args()
    if args.manifest is None:
        single = args.expect_formats if args.expect_formats and len(args.expect_formats) == 1 else None
        args.manifest = str(ROOT / "build" / "fpga" / (f"bram-{single[0]}" if single else "bram") / "bram_bench_manifest.json")
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    if args.from_file:
        data, source = Path(args.from_file).read_bytes(), {"file": args.from_file}
    elif args.port:
        data = read_port(args.port, args.baud, args.timeout, not args.no_trigger, args.settle, args.quiet, args.trigger_byte)
        source = {"port": args.port, "baud": args.baud}
    else:
        parser.error("give --port or --from-file")
    if args.raw:
        Path(args.raw).write_bytes(data)
    runs, bad = parse(data)
    complete = [r for r in runs if r["done"] is not None]
    if not complete:
        print(f"no complete run in the capture ({len(runs)} headers, {len(bad)} unparsed lines)", file=sys.stderr)
        sys.exit(1)
    run = complete[0]
    ok, rows, agree = compare(run, manifest, args.expect_formats)
    summary = {
        "schema": "trinity.fpga-bram-capture.v1",
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": source, "label": args.label, "run": run["run"], "runs_in_capture": len(runs),
        "unparsed_lines": len(bad), "manifest": {"trits": manifest["trits"], "seed": manifest["seed"],
                                                  "sources": manifest["sources"]},
        "engines": rows, "engines_agree_on_trits": agree, "expected_formats": args.expect_formats,
        "device_totals": run["done"],
        "result": "PASS" if ok else "FAIL", "evidence": "fpga" if ok and args.port else "capture-file",
    }
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{'engine':6} {'trits/word':>10} {'words':>7} {'write':>7} {'read':>7} {'bad':>4} {'inv':>4} "
          f"{'+1':>8} {'-1':>8} {'dot':>8} {'>=RAMB36':>8} {'bits/trit':>9}  match")
    for r in rows:
        d = r["device"]
        print(f"{r['name']:6} {d.get('lanes', '?')!s:>10} {d.get('words', '?')!s:>7} {d.get('write_ticks', '?')!s:>7} "
              f"{d.get('read_ticks', '?')!s:>7} {d.get('bad_words', '?')!s:>4} {d.get('invalid_groups', '?')!s:>4} "
              f"{d.get('pos', '?')!s:>8} {d.get('neg', '?')!s:>8} {d.get('dot', '?')!s:>8} "
              f"{r['ramb36_at_1k_x_36']:>8} {r['physical_bits_per_trit']:>9.3f}  {'ok' if r['match'] else r['differences']}")
    print(f"result {summary['result']}: engines agree on the trits: {agree}; totals {run['done']}; unparsed {len(bad)}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
