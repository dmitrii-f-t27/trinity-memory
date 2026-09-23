#!/usr/bin/env python3
"""Turn a bitstream build (local `make -C fpga/ax7203 bit` or the `ax7203-trace-player`
CI artifact) into a provenance record under reports/fpga/.

  python3 tools/fpga-build-report.py --artifact-dir build/fpga/ci --commit c79a6b9 \
      --source "github actions run 34591728660" --output reports/fpga/build-2026-09-11-c79a6b9

Writes build.json (`trinity.fpga-build.v1`: part, toolchain lines, yosys cell counts,
nextpnr utilisation and clock estimates, seed, frame count, bitstream sha256) and copies
yosys_stat.txt and nextpnr.log next to it. Nothing here measures the board.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def yosys_cells(text):
    cells = {}
    for match in re.finditer(r"^\s+(\d+)\s+([A-Z][A-Z0-9_]+)\s*$", text, re.M):
        cells[match.group(2)] = int(match.group(1))
    total = re.search(r"Number of cells:\s+(\d+)", text)
    return {"total": int(total.group(1)) if total else None, "by_type": cells}


def nextpnr_summary(text):
    seed = re.search(r"--seed (\d+)", text)
    utilisation = {}
    for match in re.finditer(r"^\s*Info:\s+([A-Z][A-Z0-9_]+):\s+(\d+)/\s*(\d+)", text, re.M):
        utilisation[match.group(1)] = {"used": int(match.group(2)), "available": int(match.group(3))}
    clocks = []
    for match in re.finditer(r"Max frequency for clock\s+'([^']+)':\s+([\d.]+) MHz \(([A-Z]+) at ([\d.]+) MHz\)", text):
        clocks.append({"clock": match.group(1), "fmax_mhz": float(match.group(2)), "verdict": match.group(3),
                       "target_mhz": float(match.group(4))})
    version = re.search(r"nextpnr-xilinx\s+--\s+.*?\(Version ([^)]+)\)", text)
    return {"seed": int(seed.group(1)) if seed else None, "utilisation": utilisation, "clocks": clocks,
            "version": version.group(1) if version else None,
            "routed": "Routing complete" in text or "Design routed" in text}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--artifact-dir", required=True, help="directory holding the .bit, .fasm, yosys_stat.txt, nextpnr.log")
    parser.add_argument("--commit", required=True, help="commit the bitstream was built from")
    parser.add_argument("--source", default="", help="where the build ran (CI run id or 'local')")
    parser.add_argument("--part", default="xc7a200tfbg484-2")
    parser.add_argument("--top", default="tms_trace_player_ax7203")
    parser.add_argument("--flow", default="yosys synth_xilinx -flatten -abc9 -nocarry -nodsp -family xc7; nextpnr-xilinx --placer sa "
                        "--router router1 --timing-allow-fail; prjxray fasm2frames + xc7frames2bit (regymm/openxc7 image)",
                        help="free-text description of the flow that built this bitstream")
    parser.add_argument("--output", required=True, help="report directory to create")
    args = parser.parse_args()
    src = Path(args.artifact_dir)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": "trinity.fpga-build.v1",
        "written_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "commit": args.commit, "source": args.source, "part": args.part, "top": args.top,
        "flow": args.flow,
        "files": {},
    }
    for name in ("yosys_stat.txt", "nextpnr.log"):  # yosys.log is megabytes; its hash is enough
        if (src / name).is_file():
            shutil.copy(src / name, out / name)
            record["files"][name] = sha256(out / name)
    if (src / "yosys.log").is_file():
        record["files"]["yosys.log"] = sha256(src / "yosys.log")
    if (src / "yosys_stat.txt").is_file():
        record["yosys"] = yosys_cells((src / "yosys_stat.txt").read_text(errors="replace"))
    if (src / "nextpnr.log").is_file():
        record["nextpnr"] = nextpnr_summary((src / "nextpnr.log").read_text(errors="replace"))
    bit = src / f"{args.top}.bit"
    if bit.is_file():
        record["bitstream"] = {"file": bit.name, "bytes": bit.stat().st_size, "sha256": sha256(bit)}
    fasm = src / f"{args.top}.fasm"
    if fasm.is_file():
        record["fasm"] = {"lines": sum(1 for _ in open(fasm, "rb")), "sha256": sha256(fasm)}
    for name in ("sim_capture.json",):
        if (src / name).is_file():
            record["pre_silicon"] = json.loads((src / name).read_text())["result"]
    (out / "build.json").write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    print(json.dumps({k: v for k, v in record.items() if k in ("commit", "bitstream", "nextpnr", "yosys")}, indent=1)[:1500])


if __name__ == "__main__":
    main()
