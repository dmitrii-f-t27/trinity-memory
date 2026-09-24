#!/usr/bin/env python3
"""Out-of-context estimates of the device matvec (issue #64): t27/rtl/fpga_ddr3_matvec.t27
in fpga/ax7203/matvec/tms_matvec_ooc.v, synthesised the way the DDR3 designs are (yosys
synth_xilinx -flatten -abc9 -arch xc7: carry chains and DSPs allowed) with the block RAM
library of the block-RAM designs (fpga/ax7203/brams_x36.txt, the 1K x 36 configuration the
board has verified), then placed and routed by nextpnr-xilinx (heap placer, router2, one run per
seed, --freq 83.33 for the DDR3 controller clock). Nothing here is a bitstream or a board
result: the cells are yosys's, the frequencies nextpnr's timing model's estimates.

  python3 tools/fpga-matvec-ooc.py --nextpnr PATH --chipdb PATH [--yosys yosys] [--seeds 1,2,3,4,5]
      [--output build/fpga/matvec-ooc]

Writes summary.json (cells, utilisation, the estimated Fmax of every seed before and after
routing, the critical path's end points, tool revisions, source hashes) next to the logs.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "fpga" / "ax7203" / "matvec" / "tms_matvec_ooc.v"
XDC = ROOT / "fpga" / "ax7203" / "matvec" / "tms_matvec_ooc.xdc"
BRAMS = ROOT / "fpga" / "ax7203" / "brams_x36.txt"
SOURCE = ROOT / "t27" / "rtl" / "fpga_ddr3_matvec.t27"
SYNTH = "synth_xilinx -flatten -abc9 -arch xc7 -top tms_matvec_ooc"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command, **options):
    return subprocess.run(command, capture_output=True, text=True, **options)


def place_and_route(nextpnr: str, chipdb: str, netlist: Path, out: Path, seed: int) -> dict:
    log = out / f"nextpnr_seed{seed}.log"
    command = [nextpnr, "--chipdb", chipdb, "--xdc", str(XDC), "--json", str(netlist), "--fasm", str(out / f"seed{seed}.fasm"),
               "--freq", "83.33", "--seed", str(seed), "--placer", "heap", "--router", "router2", "--timing-allow-fail"]
    result = run(command)
    log.write_text(result.stdout + result.stderr, encoding="utf-8")
    text = log.read_text(encoding="utf-8")
    fmax = [float(m) for m in re.findall(r"Max frequency for clock 'clk': ([0-9.]+) MHz", text)]
    utilisation = dict((name, int(used)) for name, used in re.findall(r"Info:\s+(\w+):\s+(\d+)/\s*\d+\s+\d+%", text)
                       if int(used))
    tail = text[text.rfind("Critical path report for clock 'clk'"):] if "Critical path report" in text else ""
    nets = re.findall(r"Net (mv\.[\w\[\]\.]+)", tail.split("Max frequency")[0]) if tail else []
    delay = re.search(r"([0-9.]+) ns logic, ([0-9.]+) ns routing", tail)
    (out / f"seed{seed}.fasm").unlink(missing_ok=True)     # large and not needed for an estimate
    return {"seed": seed, "returncode": result.returncode, "fmax_mhz_after_placement": fmax[0] if fmax else None,
            "fmax_mhz_after_routing": fmax[-1] if fmax else None, "pass_83_33": bool(fmax and fmax[-1] >= 83.33),
            "utilisation": utilisation, "critical_path_named_nets": nets[:4],
            "critical_path_ns": {"logic": float(delay.group(1)), "routing": float(delay.group(2))} if delay else None,
            "command": " ".join(command[:1] + ["..."] + command[7:])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--yosys", default="yosys")
    parser.add_argument("--nextpnr", required=True, help="nextpnr-xilinx (the DDR3 line, 0.9.7)")
    parser.add_argument("--chipdb", required=True)
    parser.add_argument("--seeds", default="1,2,3,4,5")
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "fpga" / "matvec-ooc")
    args = parser.parse_args()
    t27_root = os.environ.get("T27_ROOT")
    if not t27_root:
        parser.error("set T27_ROOT to the pinned compiler checkout (native/compiler.lock)")
    compiler = Path(t27_root) / "target" / "release" / "t27c"
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    verilog = out / "fpga_ddr3_matvec.v"
    generated = run([str(compiler), "gen-verilog", str(SOURCE)], check=True).stdout
    verilog.write_text(generated, encoding="ascii")
    script = (f"read_verilog -nomem2reg {verilog} {HARNESS}; {SYNTH} -run :map_memory; "
              f"memory_libmap -lib +/xilinx/lutrams_xc5v.txt -lib {BRAMS}; "
              "techmap -map +/xilinx/lutrams_xc5v_map.v -map +/xilinx/brams_xc6v_map.v; "
              f"{SYNTH} -run map_ffram:; tee -o {out / 'yosys_stat.txt'} stat; write_json {out / 'tms_matvec_ooc.json'}")
    synthesis = run([args.yosys, "-q", "-l", str(out / "yosys.log"), "-p", script])
    if synthesis.returncode:
        print(synthesis.stdout + synthesis.stderr, file=sys.stderr)
        return 1
    stat = (out / "yosys_stat.txt").read_text(encoding="utf-8")
    cells = dict((name, int(count)) for count, name in re.findall(r"^\s+(\d+)\s+([A-Z][A-Z0-9_]+)\s*$", stat, re.M))
    luts = sum(v for k, v in cells.items() if re.fullmatch(r"LUT[1-6]", k))
    ffs = sum(v for k, v in cells.items() if k.startswith("FD"))
    # The module alone as the top (every port kept): its cells without the harness.
    module_synth = SYNTH.replace("tms_matvec_ooc", "TrinityFpgaDdr3MatvecT27")
    alone = run([args.yosys, "-q", "-l", str(out / "yosys_module.log"), "-p",
                 f"read_verilog -nomem2reg {verilog}; {module_synth} -run :map_memory; "
                 f"memory_libmap -lib +/xilinx/lutrams_xc5v.txt -lib {BRAMS}; "
                 "techmap -map +/xilinx/lutrams_xc5v_map.v -map +/xilinx/brams_xc6v_map.v; "
                 f"{module_synth} -run map_ffram:; tee -o {out / 'yosys_stat_module.txt'} stat"])
    if alone.returncode:
        print(alone.stdout + alone.stderr, file=sys.stderr)
        return 1
    module_stat = (out / "yosys_stat_module.txt").read_text(encoding="utf-8")
    module_cells = dict((name, int(count)) for count, name in re.findall(r"^\s+(\d+)\s+([A-Z][A-Z0-9_]+)\s*$", module_stat, re.M))
    seeds = [int(s) for s in args.seeds.split(",") if s]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(seeds)) as pool:
        routes = list(pool.map(lambda s: place_and_route(args.nextpnr, args.chipdb, out / "tms_matvec_ooc.json", out, s), seeds))
    after = [r["fmax_mhz_after_routing"] for r in routes if r["fmax_mhz_after_routing"] is not None]
    summary = {
        "schema": "trinity.matvec-ooc.v1", "issue": "dmitrii-f-t27/trinity-memory#64",
        "what": "out-of-context estimates of t27/rtl/fpga_ddr3_matvec.t27 in fpga/ax7203/matvec/tms_matvec_ooc.v "
                "(the harness adds a 388-bit input shift register, an XOR reduction, IBUFDS and BUFG); "
                "synthesis cell counts and nextpnr timing-model estimates, not a bitstream and not a board result",
        "sources": {str(p.relative_to(ROOT)): sha256(p) for p in (SOURCE, HARNESS, XDC, BRAMS)},
        "generated_verilog_sha256": sha256(verilog),
        "tools": {"t27c": (ROOT / "native" / "compiler.lock").read_text().strip(),
                  "yosys": run([args.yosys, "-V"]).stdout.strip(),
                  "nextpnr": run([args.nextpnr, "--version"]).stdout.strip().splitlines()[:1] + run([args.nextpnr, "--version"]).stderr.strip().splitlines()[:1],
                  "chipdb": Path(args.chipdb).name, "synthesis": SYNTH + " with memory_libmap on brams_x36.txt"},
        "module_alone": {"cells": module_cells,
                         "luts": sum(v for k, v in module_cells.items() if re.fullmatch(r"LUT[1-6]", k)),
                         "flip_flops": sum(v for k, v in module_cells.items() if k.startswith("FD")),
                         "carry4": module_cells.get("CARRY4", 0), "dsp48e1": module_cells.get("DSP48E1", 0),
                         "ramb36e1": module_cells.get("RAMB36E1", 0),
                         "note": "the module as the synthesis top, every output a port; not placed"},
        "cells": cells, "luts": luts, "flip_flops": ffs, "carry4": cells.get("CARRY4", 0),
        "dsp48e1": cells.get("DSP48E1", 0), "ramb36e1": cells.get("RAMB36E1", 0), "ramb18e1": cells.get("RAMB18E1", 0),
        "routes": routes,
        "fmax_mhz_after_routing": {"min": min(after) if after else None, "max": max(after) if after else None,
                                   "seeds_passing_83_33": sum(r["pass_83_33"] for r in routes), "seeds": len(routes)},
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("luts", "flip_flops", "carry4", "dsp48e1", "ramb36e1", "fmax_mhz_after_routing")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
