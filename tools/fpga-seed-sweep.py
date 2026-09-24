#!/usr/bin/env python3
"""Summarise a nextpnr-xilinx placer/seed sweep of one netlist (issue #60).

  python3 tools/fpga-seed-sweep.py --top tms_ddr3_ax7203 --commit f07e91cd --label "nextpnr-xilinx 0.9.7, x16" \
      --netlist build/fpga/ddr3-x16/tms_ddr3_ax7203.json --output reports/fpga/ddr3-seed-sweep-....json \
      build/fpga/ddr3-x16/sweep/*.log

`make -C fpga/ax7203 ddr3-sweep` runs every seed of DDR3_SEEDS without the seed search's
early stop and calls this. Each LOG is one nextpnr run; its FASM is the file with the same
stem next to it, and a stem of the form <placer>_seed<n> names placer and seed. Per run:
the routed Fmax per clock (the analysis after the router, not the placer's estimate), the
routed critical path (start and end net, LUT and carry stages, logic and routing ns), the
utilisation, the LUT1 cells the packer created, the PLLE2 tables checked against Vivado's
(tools/fpga-build-report.py), the IOLOGIC clock inputs fed from the fabric, and the sha256
of the log and the FASM. Neither file is copied.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("fpga_build_report", ROOT / "tools/fpga-build-report.py")
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)


def critical_path(text):
    """The last critical-path report after the router: start and end net, stage counts, ns split."""
    marks = [text.rfind(m) for m in ("Router2 time", "Routing complete")]
    tail = text[max(marks):] if max(marks) >= 0 else text
    starts = [m.start() for m in re.finditer(r"Critical path report for clock '([^']+)'", tail)]
    if not starts:
        return None
    block = tail[starts[-1]:]
    end = re.search(r"([\d.]+) ns logic, ([\d.]+) ns routing", block)
    if end:
        block = block[:end.end()]
    clock = re.match(r"Critical path report for clock '([^']+)'", block).group(1)
    sources = re.findall(r"^Info:\s+[\d.]+\s+[\d.]+\s+Source (\S+)\.(\S+)\s*$", block, re.M)
    nets = re.findall(r"^Info:\s+[\d.]+\s+[\d.]+\s+Net (\S+)", block, re.M)
    return {"clock": clock,
            "start": f"{sources[0][0]}.{sources[0][1]}" if sources else None,
            "first_net": nets[0] if nets else None,
            "last_net": nets[-1] if nets else None,
            "lut_stages": sum(1 for _, pin in sources if pin in ("O5", "O6")),
            "carry_stages": sum(1 for _, pin in sources if pin.startswith("CO") or re.fullmatch(r"O\[?[0-3]\]?", pin)),
            "logic_ns": float(end.group(1)) if end else None,
            "routing_ns": float(end.group(2)) if end else None}


def packed_lut1(text):
    block = re.search(r"Created \d+ SLICE_LUTX cells from:\n((?:Info:\s+\d+x \S+\n)+)", text)
    if not block:
        return None
    m = re.search(r"(\d+)x LUT1\b", block.group(1))
    return int(m.group(1)) if m else 0


def summarise(log):
    text = log.read_text(errors="replace")
    stem = log.stem
    m = re.fullmatch(r"(\w+?)_seed(\d+)", stem)
    run = {"run": stem, "placer": m.group(1) if m else None, "seed": int(m.group(2)) if m else None,
           "log_sha256": report.sha256(log), "routed": "Router2 time" in text or "Routing complete" in text}
    run["clocks_routed"] = report.routed_clocks(text)
    util = report.nextpnr_summary(text)["utilisation"]
    run["utilisation"] = {k: util[k]["used"] for k in ("SLICE_LUTX", "SLICE_FFX", "CARRY4") if k in util}
    run["packed_lut1"] = packed_lut1(text)
    run["critical_path"] = critical_path(text)
    fasm = log.with_suffix(".fasm")
    if fasm.is_file():
        fasm_text = fasm.read_text(errors="replace")
        run["fasm_sha256"] = report.sha256(fasm)
        run["pll"] = [{k: p.get(k) for k in ("clkfbout_mult", "lktable", "table", "result", "note") if p.get(k) is not None}
                      for p in report.pll_check(fasm_text)]
        run["iologic_clock_from_fabric"] = len(report.fabric_clocks(fasm_text)["iologic_clock_from_fabric"])
    return run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("logs", nargs="+", type=Path, help="nextpnr logs, one per run")
    parser.add_argument("--output", required=True, type=Path, help="summary JSON to write")
    parser.add_argument("--label", default="", help="tool and variant")
    parser.add_argument("--commit", default="", help="BUILD_ID of the netlist")
    parser.add_argument("--top", default="tms_ddr3_ax7203")
    parser.add_argument("--netlist", type=Path, help="the netlist every run placed (hashed)")
    parser.add_argument("--chipdb", type=Path, help="the chip database every run read (hashed)")
    parser.add_argument("--note", default="", help="free text: where and how the runs were made")
    args = parser.parse_args(argv)
    runs = [summarise(log) for log in sorted(args.logs, key=lambda p: (len(p.stem), p.stem))]
    fmax = [c["fmax_mhz"] for r in runs for c in r["clocks_routed"] if c["clock"] == "clk_ctrl"]
    met = [r["run"] for r in runs if r["clocks_routed"] and all(c["verdict"] == "PASS" for c in r["clocks_routed"])]
    record = {"schema": "trinity.fpga-seed-sweep.v1",
              "written_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
              "label": args.label, "commit": args.commit, "top": args.top, "note": args.note,
              "netlist_sha256": report.sha256(args.netlist) if args.netlist and args.netlist.is_file() else None,
              "chipdb_sha256": report.sha256(args.chipdb) if args.chipdb and args.chipdb.is_file() else None,
              "summary": {"runs": len(runs), "routed": sum(r["routed"] for r in runs),
                          "met_every_clock": met,
                          "clk_ctrl_fmax_mhz": [min(fmax), max(fmax)] if fmax else None},
              "runs": runs}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    print(json.dumps(record["summary"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
