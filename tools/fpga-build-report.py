#!/usr/bin/env python3
"""Turn a bitstream build (local `make -C fpga/ax7203 bit` or the `ax7203-trace-player`
CI artifact) into a provenance record under reports/fpga/.

  python3 tools/fpga-build-report.py --artifact-dir build/fpga/ci --commit c79a6b9 \
      --source "github actions run 34591728660" --output reports/fpga/build-2026-09-11-c79a6b9

Writes build.json (`trinity.fpga-build.v1`: part, toolchain lines, yosys cell counts,
nextpnr utilisation and clock estimates, seed, frame count, bitstream sha256) and copies
yosys_stat.txt and nextpnr.log next to it. Nothing here measures the board.

With --ddr3 (the UberDDR3 build of issue #60, `make -C fpga/ax7203 ddr3-report`) the
record also carries the tool revisions the Makefile wrote next to the build, the image
digest, the UberDDR3 commit, license and per-file sha256 (checked against the fetched
files), the routed Fmax per declared clock, the PLLE2 configuration decoded from the
FASM with its lock and loop-filter tables checked against Vivado's values for the
programmed CLKFBOUT_MULT, and the FASM's VREF lines. The bitstream and the FASM are
never copied, only hashed.
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
            "routed": "Routing complete" in text or "Design routed" in text or "Router2 time" in text}


def routed_clocks(text):
    """Fmax lines of the analysis of the routed design (after the router), not the placer's estimate."""
    marks = [text.rfind(m) for m in ("Router2 time", "Routing complete")]
    tail = text[max(marks):] if max(marks) >= 0 else ""
    return [{"clock": m.group(1), "fmax_mhz": float(m.group(2)), "verdict": m.group(3), "target_mhz": float(m.group(4))}
            for m in re.finditer(r"Max frequency for clock\s+'([^']+)':\s+([\d.]+) MHz \(([A-Z]+) at ([\d.]+) MHz\)", tail)]


# Vivado's PLLE2 lock (LKTABLE) and loop-filter (TABLE, BANDWIDTH OPTIMIZED) values per
# CLKFBOUT_MULT, harvested from Vivado golden bitstreams in openXC7/nextpnr-xilinx#138
# (merged 2026-08-11). Only the multipliers this repository's DDR3 flow can use are
# listed; any other MULT is reported as unchecked. nextpnr-xilinx 45a986b8 writes the
# MULT=8 pair for every PLL (xilinx/fasm.cc, FIXME), which this check flags.
PLLE2_VIVADO = {5: (0x73BE8FA401, 0x1EC), 8: (0xB5BE8FA401, 0x3B4)}


def fasm_features(text, prefix):
    """{tile: {feature: value}} for FASM lines of one site type, vectors as integers, bits as 1."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^(\S+)\.%s\.(\S+?)(?:\[(\d+):(\d+)\])?(?:\s*=\s*(\d+)'b([01]+))?\s*$" % re.escape(prefix), line)
        if not m:
            continue
        name = m.group(2) + (f"[{m.group(3)}:{m.group(4)}]" if m.group(3) else "")
        out.setdefault(m.group(1), {})[name] = int(m.group(6), 2) if m.group(6) else 1
    return out


def pll_divider(features, name):
    if features.get(f"{name}_{'DIVCLK' if name == 'DIVCLK' else 'CLKOUT2'}_NO_COUNT[0]"):
        return 1
    stem = "DIVCLK_DIVCLK" if name == "DIVCLK" else f"{name}_CLKOUT1"
    return features.get(f"{stem}_HIGH_TIME[5:0]", 0) + features.get(f"{stem}_LOW_TIME[5:0]", 0)


def pll_check(fasm_text):
    plls = []
    for tile, f in sorted(fasm_features(fasm_text, "PLLE2_ADV").items()):
        mult = pll_divider(f, "CLKFBOUT")
        lk, table = f.get("LKTABLE[39:0]"), f.get("TABLE[9:0]")
        expected = PLLE2_VIVADO.get(mult)
        entry = {"tile": tile, "clkfbout_mult": mult, "divclk_divide": pll_divider(f, "DIVCLK"),
                 "clkout_divide": {f"CLKOUT{i}": pll_divider(f, f"CLKOUT{i}") for i in range(6)
                                   if f.get(f"CLKOUT{i}_CLKOUT1_OUTPUT_ENABLE[0]")},
                 "clkout_phase_mux": {f"CLKOUT{i}": f.get(f"CLKOUT{i}_CLKOUT1_PHASE_MUX[2:0]", 0) for i in range(6)
                                      if f.get(f"CLKOUT{i}_CLKOUT1_OUTPUT_ENABLE[0]")},
                 "filtreg1": f.get("FILTREG1_RESERVED[11:0]"),
                 "lktable": f"0x{lk:010X}" if lk is not None else None,
                 "table": f"0x{table:03X}" if table is not None else None}
        if expected is None:
            entry["result"] = f"UNCHECKED: no Vivado reference value listed for CLKFBOUT_MULT={mult}"
        else:
            entry["vivado_lktable"], entry["vivado_table"] = f"0x{expected[0]:010X}", f"0x{expected[1]:03X}"
            entry["result"] = "PASS" if (lk, table) == expected else "FAIL"
            wrong = [m for m, v in PLLE2_VIVADO.items() if m != mult and (lk, table) == v]
            if wrong:
                entry["note"] = f"tables are Vivado's CLKFBOUT_MULT={wrong[0]} values"
        plls.append(entry)
    return plls


def declared_clocks(xdc_text):
    return [{"name": m.group(2) or m.group(3), "period_ns": float(m.group(1)), "target": m.group(3)}
            for m in re.finditer(r"^create_clock\s+-period\s+([\d.]+)(?:\s+-name\s+(\S+))?\s+\[get_(?:nets|ports)\s+(\S+?)\]",
                                 xdc_text, re.M)]


def read_first(path):
    return path.read_text(errors="replace").strip() if path.is_file() else None


def parse_lock(path):
    lock = {"files": {}}
    for line in path.read_text().splitlines():
        parts = line.split()
        if not parts or parts[0].startswith("#"):
            continue
        if parts[0] == "file":
            lock["files"][parts[2]] = parts[1]
        else:
            lock[parts[0]] = parts[1]
    return lock


def ddr3_record(src, args):
    """Provenance and checks of the UberDDR3 DDR3 build (issue #60)."""
    record = {"variant": args.variant}
    record["toolchain"] = {
        "yosys": (read_first(src / "yosys_version.txt") or "").splitlines()[:1],
        "nextpnr_xilinx": (read_first(src / "nextpnr_revision.txt") or "").splitlines(),
        "prjxray": read_first(src / "prjxray_revision.txt"),
        "prjxray_db": read_first(src / "prjxray_db_revision.txt"),
        "image": args.image_digest or None,
    }
    lock = parse_lock(Path(args.lock))
    files = {}
    for rel, want in lock["files"].items():
        got = sha256(Path(args.uberddr3_dir) / rel) if (Path(args.uberddr3_dir) / rel).is_file() else None
        files[rel] = {"sha256": want, "verified": got == want}
    record["uberddr3"] = {"repository": lock.get("repository"), "commit": lock.get("commit"),
                          "license": lock.get("license"), "files": files,
                          "committed_here": False, "lock_sha256": sha256(Path(args.lock))}
    record["nextpnr_choice"] = read_first(src / "nextpnr_choice.txt")
    if args.chipdb and Path(args.chipdb).is_file():
        record["chipdb"] = {"file": Path(args.chipdb).name, "bytes": Path(args.chipdb).stat().st_size,
                            "sha256": sha256(Path(args.chipdb))}
    warn = read_first(src / "yosys_conflicting_drivers.txt")
    record["yosys_conflicting_driver_warnings"] = int(warn) if warn and warn.isdigit() else None
    xdc = src / f"{args.top}.xdc"
    declared = declared_clocks(xdc.read_text()) if xdc.is_file() else []
    if xdc.is_file():
        record["xdc_sha256"] = sha256(xdc)
    routed = routed_clocks((src / "nextpnr.log").read_text(errors="replace")) if (src / "nextpnr.log").is_file() else []
    by_clock = {c["clock"]: c for c in routed}
    record["clocks"] = [dict(c, fmax_mhz=by_clock[c["name"]]["fmax_mhz"], verdict=by_clock[c["name"]]["verdict"])
                        if c["name"] in by_clock else
                        dict(c, fmax_mhz=None, verdict="not timed: nextpnr reports no register-to-register path in this domain")
                        for c in declared]
    fasm = src / f"{args.top}.fasm"
    if fasm.is_file():
        text = fasm.read_text(errors="replace")
        record["pll_check"] = pll_check(text)
        record["vref"] = [line for line in text.splitlines() if ".VREF." in line]
    return record


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
    parser.add_argument("--ddr3", action="store_true", help="add the DDR3 (UberDDR3) provenance and checks")
    parser.add_argument("--uberddr3-dir", default=str(ROOT / "build/uberddr3"), help="the fetched UberDDR3 files")
    parser.add_argument("--lock", default=str(ROOT / "fpga/ax7203/ddr3/uberddr3.lock"), help="UberDDR3 lock file")
    parser.add_argument("--image-digest", default="", help="digest of the toolchain image (prjxray)")
    parser.add_argument("--variant", default="", help="free-text variant description")
    parser.add_argument("--chipdb", default="", help="chip database nextpnr read (hashed into the DDR3 record)")
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
    if args.ddr3:
        record["ddr3"] = ddr3_record(src, args)
        if (src / "nextpnr.log").is_file():
            record["nextpnr"]["clocks_routed"] = routed_clocks((src / "nextpnr.log").read_text(errors="replace"))
    for name in ("sim_capture.json",):
        if (src / name).is_file():
            record["pre_silicon"] = json.loads((src / name).read_text())["result"]
    (out / "build.json").write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    print(json.dumps({k: v for k, v in record.items() if k in ("commit", "bitstream", "nextpnr", "yosys")}, indent=1)[:1500])
    if args.ddr3:
        print(json.dumps({k: record["ddr3"].get(k) for k in ("clocks", "pll_check", "vref")}, indent=1))


if __name__ == "__main__":
    main()
