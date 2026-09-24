#!/usr/bin/env python3
"""Small records for the UART loader's build reports (issue #63), from files a build
leaves in build/fpga/ (which git does not keep):

  rams NETLIST.json          block-RAM cells of a yosys netlist with their port widths
                             (RAMB36E1 1K x 36 has READ/WRITE_WIDTH 36; 32K x 1 has 1)
  placements DIR [DIR ...]   per build directory: the nextpnr logs (placer, seed, whether
                             it routed, the last router2 line, the routed Fmax lines) and
                             the sha256 of the FASM it left; with two or more FASMs, how
                             many lines of each differ from the first

  python3 tools/fpga-loader-evidence.py rams build/fpga/loader-clk1-div217/tms_uart_loader_ax7203.json
  python3 tools/fpga-loader-evidence.py placements build/fpga/loader-clk1-div217 build/fpga/loader-seed4

Writes JSON to stdout (or --output). Nothing here measures the board.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sys
from pathlib import Path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def param(value):
    if isinstance(value, str) and value and set(value) <= {"0", "1"}:
        return int(value, 2)
    return value


def rams(netlist: Path) -> dict:
    data = json.loads(netlist.read_text())
    groups: collections.Counter = collections.Counter()
    for module in data["modules"].values():
        for cell in module["cells"].values():
            if cell["type"].startswith("RAMB"):
                widths = tuple(sorted((k, param(v)) for k, v in cell["parameters"].items() if "WIDTH" in k))
                groups[(cell["type"], widths)] += 1
    return {"netlist": str(netlist), "netlist_sha256": sha256(netlist),
            "groups": [{"type": t, "count": n, **dict(w)} for (t, w), n in sorted(groups.items())]}


def placements(dirs: list[Path]) -> dict:
    out = []
    for d in dirs:
        entry = {"dir": str(d), "runs": []}
        for log in sorted(d.glob("nextpnr_*_seed*.log")):
            text = log.read_text(errors="replace")
            m = re.match(r"nextpnr_(\w+)_seed(\d+)\.log", log.name)
            router = [line.strip() for line in text.splitlines() if "iter=" in line and "overused=" in line]
            entry["runs"].append({
                "log": log.name, "placer": m.group(1), "seed": int(m.group(2)),
                # The Makefile's rule: a FASM and no "Failed to find a route"; router2 ends at overused=0.
                "routed": bool(router) and " overused=0 " in router[-1] and "Failed to find a route" not in text,
                "last_router_line": router[-1] if router else None,
                "max_frequency_lines": [line.strip() for line in text.splitlines() if "Max frequency for clock" in line],
            })
        fasm = next(iter(d.glob("*.fasm")), None)
        if fasm is not None:
            entry["fasm"] = {"file": fasm.name, "sha256": sha256(fasm)}
        out.append(entry)
    fasms = [Path(e["dir"]) / e["fasm"]["file"] for e in out if "fasm" in e]
    record = {"builds": out}
    if len(fasms) >= 2:
        # Lines of each FASM that the first one does not have (as multisets).
        base = collections.Counter(fasms[0].read_text().splitlines())
        record["fasm_lines_differing_from_first"] = []
        for f in fasms[1:]:
            other = collections.Counter(f.read_text().splitlines())
            record["fasm_lines_differing_from_first"].append({
                "dir": str(f.parent), "only_in_first": sum((base - other).values()),
                "only_in_this": sum((other - base).values()), "lines_first": sum(base.values()),
                "lines_this": sum(other.values())})
    return record


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("what", choices=("rams", "placements"))
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    record = rams(Path(args.paths[0])) if args.what == "rams" else placements([Path(p) for p in args.paths])
    text = json.dumps(record, indent=1) + "\n"
    if args.output:
        Path(args.output).write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
