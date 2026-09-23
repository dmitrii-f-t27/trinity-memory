#!/usr/bin/env python3
"""Capture the AX7203 trace player's UART report and compare it with the reference.

Reads the 19-byte lines emitted by fpga/ax7203/tms_trace_player.v (from a serial
port, or from a file written by tests/tb_fpga_trace_player.v), checks every `C`
line against the expected word of build/fpga/tms_trace_manifest.json, and writes
a JSON report (`trinity.fpga-capture.v1`). Exit status 1 on any mismatch, any
missing vector, or a device-side mismatch count that disagrees with the host.

  python3 tools/fpga-capture.py --port /dev/cu.usbserial-10 --output reports/fpga/capture.json
  python3 tools/fpga-capture.py --from-file build/fpga/sim_capture.txt
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
LINE = re.compile(r"^([HVCEKFWRTXAD])([0-9a-f]{8})([0-9a-f]{10})$")


def parse(text):
    """Return (runs, bad_lines). A run starts at an H line."""
    runs, bad = [], []
    current = None
    for raw in text.decode("ascii", "replace").split("\n"):
        if not raw:
            continue
        match = LINE.match(raw)
        if not match:
            bad.append(raw)
            continue
        tag, a, b = match.group(1), int(match.group(2), 16), match.group(3)
        if tag == "H":
            current = {"build_id": f"{a:08x}", "format": int(b[0:2], 16), "cycles": int(b[2:6], 16),
                       "vectors_announced": int(b[6:10], 16), "vectors": [], "free": {}, "done": None,
                       "workload": {"frames": None, "results": {}, "totals": {}}, "edge": {}}
            runs.append(current)
            continue
        if current is None:
            bad.append(raw)
            continue
        if tag == "V":
            current["vectors"].append({"index": a, "cycles": int(b, 16), "observed": [], "device_mismatches": None, "counters": []})
        elif tag in ("C", "E", "K") and not current["vectors"]:
            bad.append(raw)   # a per-vector line before any vector: a damaged stream
        elif tag == "C":
            current["vectors"][-1]["observed"].append((a, b))
        elif tag == "E":
            current["vectors"][-1]["device_mismatches"] = int(b, 16)
        elif tag == "K":
            current["vectors"][-1]["counters"].append((a, int(b, 16)))
        elif tag == "F":
            current["free"][a] = int(b, 16)
        elif tag == "W":
            current["workload"]["frames"] = int(b, 16)
        elif tag == "R":
            value = int(b, 16) & 0xffffffff
            current["workload"]["results"][a] = value - (1 << 32) if value >= (1 << 31) else value
        elif tag == "T":
            current["workload"]["totals"][a] = int(b, 16)
        elif tag == "X":
            value = int(b, 16)
            current["edge"].setdefault(a, {})["label"] = value & 3
            current["edge"][a]["ambiguous"] = bool((value >> 2) & 1)
            current["edge"][a]["ticks"] = value >> 3
        elif tag == "A":
            value = int(b, 16) & 0xffffffff
            current["edge"].setdefault(a // 4, {}).setdefault("accumulators", {})[a % 4] = value - (1 << 32) if value >= (1 << 31) else value
        elif tag == "D":
            current["done"] = {"stepped_mismatches": a, "free_mismatches": int(b, 16)}
    return runs, bad


def compare(run, manifest):
    report, ok = [], True
    expected_vectors = manifest["vectors"]
    if run["vectors_announced"] != len(expected_vectors) or run["cycles"] != manifest["total_cycles"]:
        ok = False
    for entry in expected_vectors:
        got = next((v for v in run["vectors"] if v["index"] == entry["index"]), None)
        item = {"index": entry["index"], "id": entry["id"], "kind": entry["kind"], "cycles": entry["cycles"],
                "captured": got is not None, "host_mismatches": None, "device_mismatches": None,
                "free_mismatches": run["free"].get(entry["index"]), "counters": None, "first_mismatch": None}
        if got is None:
            ok = False
            report.append(item)
            continue
        observed = {c: w for c, w in got["observed"]}
        mismatches = 0
        for cycle, expected in enumerate(entry["expected"]):
            word = observed.get(cycle)
            if word != expected:
                mismatches += 1
                if item["first_mismatch"] is None:
                    item["first_mismatch"] = {"cycle": cycle, "expected": expected, "observed": word}
        item["host_mismatches"] = mismatches
        item["device_mismatches"] = got["device_mismatches"]
        names = manifest["counters"][entry["kind"]]
        item["counters"] = {names[i].split(":")[0]: value for i, value in got["counters"] if i < len(names)}
        if (mismatches or got["cycles"] != entry["cycles"] or len(observed) != entry["cycles"]
                or got["device_mismatches"] != mismatches or item["free_mismatches"] != 0):
            ok = False
        report.append(item)
    if run["done"] is None:
        ok = False
    return ok, report


TOTALS = ["ticks from the first start to the last result consumed", "beats fired", "results delivered",
          "activation stalls", "ticks of the load phase"]


def compare_workload(run, manifest):
    spec = manifest.get("workload")
    got = run["workload"]
    if not spec:
        return None, True
    results = [got["results"].get(i) for i in range(spec["frames"])]
    totals = {TOTALS[i] if i < len(TOTALS) else str(i): v for i, v in sorted(got["totals"].items())}
    ok = got["frames"] == spec["frames"] and results == spec["expected_results"] and len(got["totals"]) >= 5
    beats = got["totals"].get(1, 0)
    ticks = got["totals"].get(0, 0)
    return {"frames": spec["frames"], "words": spec["words"], "results_match": results == spec["expected_results"],
            "results": results, "expected_results": spec["expected_results"], "totals": totals,
            "beats_per_tick": (beats / ticks) if ticks else None, "ticks_per_frame": (ticks / spec["frames"]) if spec["frames"] else None,
            "rule": {"trits": spec["trit_rule"], "activations": spec["activation_rule"]}}, ok


def compare_edge(run, manifest):
    spec = manifest.get("edge")
    got = run["edge"]
    if not spec:
        return None, True
    rows, ok = [], True
    for fixture in spec["fixtures"]:
        seen = got.get(fixture["index"])
        acc = [seen["accumulators"].get(r) for r in range(3)] if seen and "accumulators" in seen else None
        item = {"index": fixture["index"], "id": fixture["id"], "expected": {"label": fixture["label"], "label_name": fixture["label_name"],
                "ambiguous": fixture["ambiguous"], "accumulators": fixture["accumulators"]},
                "device": None if seen is None else {"label": seen.get("label"), "ambiguous": seen.get("ambiguous"),
                                                      "ticks": seen.get("ticks"), "accumulators": acc}}
        expected_label = 3 if fixture["ambiguous"] else fixture["label"]
        item["match"] = bool(seen and seen.get("label") == expected_label and seen.get("ambiguous") == fixture["ambiguous"]
                             and acc == fixture["accumulators"])
        ok = ok and item["match"]
        rows.append(item)
    return {"fixtures": rows, "labels": spec["labels"], "all_match": ok,
            "ticks": [r["device"]["ticks"] for r in rows if r["device"]]}, ok


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--no-trigger", action="store_true", help="do not send a byte to restart the run")
    parser.add_argument("--settle", type=float, default=0.5, help="seconds to wait after opening the port before triggering")
    parser.add_argument("--quiet", type=float, default=0.5, help="then wait until the line has been silent this long")
    parser.add_argument("--trigger-byte", type=lambda v: int(v, 0), default=0xFF,
                        help="byte sent to start a run (default 0xff: one falling edge, so one run)")
    parser.add_argument("--from-file", help="parse a previously captured byte stream instead of a port")
    parser.add_argument("--manifest", default=str(ROOT / "build" / "fpga" / "tms_trace_manifest.json"))
    parser.add_argument("--output", help="write the JSON report here")
    parser.add_argument("--raw", help="also save the raw byte stream here")
    parser.add_argument("--label", default="", help="free-text provenance (board, bitstream, commit)")
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    if args.from_file:
        data = Path(args.from_file).read_bytes()
        source = {"file": args.from_file}
    elif args.port:
        data = read_port(args.port, args.baud, args.timeout, not args.no_trigger, args.settle, args.quiet, args.trigger_byte)
        source = {"port": args.port, "baud": args.baud}
    else:
        parser.error("give --port or --from-file")
    if args.raw:
        Path(args.raw).write_bytes(data)
    runs, bad = parse(data)
    if not runs:
        print("no run header in the capture", file=sys.stderr)
        sys.exit(1)
    run = next((r for r in runs if r["done"]), runs[0])
    ok, report = compare(run, manifest)
    workload, workload_ok = compare_workload(run, manifest)
    edge, edge_ok = compare_edge(run, manifest)
    ok = ok and workload_ok and edge_ok
    summary = {
        "schema": "trinity.fpga-capture.v1",
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": source, "label": args.label, "build_id": run["build_id"],
        "manifest_source": manifest["source"], "runs_in_capture": len(runs), "unparsed_lines": len(bad),
        "device_totals": run["done"], "vectors": report,
        "workload": workload, "edge": edge,
        "result": "PASS" if ok else "FAIL",
        "evidence": "fpga" if ok and args.port else "capture-file",
    }
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    width = max(len(v["id"]) for v in report)
    print(f"{'vector':{width}}  kind     cycles  host  dev  free  counters")
    for v in report:
        counters = " ".join(f"{val}" for val in (v["counters"] or {}).values())
        print(f"{v['id']:{width}}  {v['kind']:8} {v['cycles']:6}  {v['host_mismatches']!s:>4}  {v['device_mismatches']!s:>3}  "
              f"{v['free_mismatches']!s:>4}  {counters}")
    if workload:
        print(f"workload: {workload['frames']} frames x {workload['words']} beats, results {'match' if workload['results_match'] else 'DIFFER'}, "
              f"totals {workload['totals']}, beats/tick {workload['beats_per_tick']}")
    if edge:
        for row in edge["fixtures"]:
            d = row["device"] or {}
            print(f"edge {row['id']:28} label {d.get('label')}/{row['expected']['label'] if not row['expected']['ambiguous'] else 3} "
                  f"acc {d.get('accumulators')} ticks {d.get('ticks')} {'ok' if row['match'] else 'MISMATCH'}")
    print(f"result {summary['result']}: {len(report)} vectors, device totals {run['done']}, unparsed {len(bad)}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
