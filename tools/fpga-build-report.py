#!/usr/bin/env python3
"""Turn a bitstream build (local `make -C fpga/ax7203 bit` or the `ax7203-trace-player`
CI artifact) into a provenance record under reports/fpga/.

  python3 tools/fpga-build-report.py --artifact-dir build/fpga/ci --commit c79a6b9 \
      --source "github actions run 34591728660" --output reports/fpga/build-2026-09-11-c79a6b9

Writes build.json (`trinity.fpga-build.v1`: part, toolchain lines, yosys cell counts,
nextpnr utilisation and clock estimates, seed, frame count, bitstream sha256) and copies
yosys_stat.txt and nextpnr.log next to it. Nothing here measures the board.

Identity: xc7frames2bit writes the time it ran into the .bit header, so the whole-file
sha256 of a .bit names that one file and no rebuild reproduces it. `reproducible_identity`
holds what a rebuild with the same inputs and tools does reproduce: the FASM sha256, the
frames sha256 and the sha256 of the .bit from its sync word (0xAA995566) on.

With --ddr3 (the UberDDR3 build of issue #60, `make -C fpga/ax7203 ddr3-report`) the
record also carries the tool revisions the Makefile wrote next to the build, the image
digest, the UberDDR3 commit, license and per-file sha256 (checked against the fetched
files), the routed Fmax per declared clock, the PLLE2 configuration decoded from the
FASM with its lock and loop-filter tables checked against Vivado's values for the
programmed CLKFBOUT_MULT, and the FASM's VREF lines. It also records the BUILD_ID baked
into the netlist (and stops when it is not --commit), the source tree the synthesis ran on,
the inputs of the chip database, the LUT levels of the controller-clock paths into and out
of the PHY primitives (which nextpnr does not time), and the clock nets that leave the
global network through a fabric LUT (FASM pips, and the LUT's placement and delays from the
routed netlist and SDF when the build kept them). It stops with exit status 1 when a
fetched UberDDR3 file does not match the lock. The XDC nextpnr read is copied next to the
report; the bitstream and the FASM are never copied, only hashed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


SYNC_WORD = bytes.fromhex("AA995566")


def bitstream_identity(path):
    """Whole-file sha256, and the sha256 from the sync word on (the header before it holds
    xc7frames2bit's date and time), with the header's text fields."""
    data = path.read_bytes()
    record = {"file": path.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    at = data.find(SYNC_WORD)
    if at >= 0:
        record["sync_offset"] = at
        record["sha256_from_sync"] = hashlib.sha256(data[at:]).hexdigest()
        header = {}
        # Xilinx .bit header: 13-byte preamble, then fields 'a'..'d' (2-byte length, text), then 'e'.
        pos = 13
        while pos + 3 <= at and data[pos:pos + 1] in (b"a", b"b", b"c", b"d"):
            key = data[pos:pos + 1].decode()
            size = int.from_bytes(data[pos + 1:pos + 3], "big")
            header[key] = data[pos + 3:pos + 3 + size].rstrip(b"\0").decode(errors="replace")
            pos += 3 + size
        if header:
            record["header"] = header
    return record


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


def netlist_top(netlist, top):
    modules = netlist.get("modules", {})
    return modules.get(top) or next((m for m in modules.values() if m.get("attributes", {}).get("top")), None)


def netlist_build_id(module):
    """BUILD_ID of the top module as 8 hex digits (yosys writes parameters as bit strings)."""
    value = (module or {}).get("parameter_default_values", {}).get("BUILD_ID")
    if value is None:
        return None
    try:
        return f"{int(value, 2) if set(value) <= {'0', '1'} else int(value):08x}"
    except (TypeError, ValueError):
        return str(value)


PHY_PRIMITIVES = ("OSERDESE2", "ISERDESE2", "IDELAYE2", "IDELAYCTRL")
FLIP_FLOPS = ("FDRE", "FDSE", "FDCE", "FDPE")


def phy_path_levels(module):
    """Maximum LUT levels of the controller-clock paths nextpnr-xilinx does not time: from a
    flip-flop into each input port of the PHY primitives, and from a PHY primitive output
    into a flip-flop (CARRY4 and MUXF7/8 are passed through without counting a level)."""
    cells = module["cells"]
    driver = {}
    for name, cell in cells.items():
        for port, bits in cell["connections"].items():
            if cell.get("port_directions", {}).get(port) == "output":
                for bit in bits:
                    if isinstance(bit, int):
                        driver[bit] = (name, port)
    memo = {}

    def depth(bit, start):
        key = (bit, start)
        if key in memo:
            return memo[key]
        memo[key] = -1
        if bit not in driver:
            return -1
        cell = cells[driver[bit][0]]
        kind = cell["type"]
        if kind in FLIP_FLOPS:
            result = 0 if start == "ff" else -1
        elif kind in PHY_PRIMITIVES:
            result = 0 if start == "phy" else -1
        elif kind.startswith("LUT") or kind in ("INV", "MUXF7", "MUXF8", "CARRY4"):
            best = max((depth(b, start) for port, bits in cell["connections"].items()
                        if cell.get("port_directions", {}).get(port) == "input" for b in bits if isinstance(b, int)),
                       default=-1)
            result = -1 if best < 0 else best + (1 if kind.startswith("LUT") or kind == "INV" else 0)
        else:
            result = -1
        memo[key] = result
        return result

    into, out_of = defaultdict(int), defaultdict(int)
    for cell in cells.values():
        kind = cell["type"]
        if kind not in PHY_PRIMITIVES and kind not in FLIP_FLOPS:
            continue
        for port, bits in cell["connections"].items():
            if cell.get("port_directions", {}).get(port) != "input" or (kind in FLIP_FLOPS and port == "C"):
                continue
            for bit in bits:
                if not isinstance(bit, int):
                    continue
                if kind in PHY_PRIMITIVES:
                    level = depth(bit, "ff")
                    if level >= 0:
                        key = f"{kind}.{port.rstrip('0123456789') if port.startswith('CNTVALUEIN') else port}"
                        into[key] = max(into[key], level)
                else:
                    level = depth(bit, "phy")
                    if level >= 0:
                        out_of[f"{kind}.{port}"] = max(out_of[f"{kind}.{port}"], level)
    ff_to_ff = 0
    for cell in cells.values():
        if cell["type"] in FLIP_FLOPS:
            for port, bits in cell["connections"].items():
                if port != "C" and cell.get("port_directions", {}).get(port) == "input":
                    ff_to_ff = max([ff_to_ff] + [depth(b, "ff") for b in bits if isinstance(b, int)])
    return {"into_phy_max_lut_levels": dict(sorted(into.items())),
            "out_of_phy_max_lut_levels": dict(sorted(out_of.items())),
            "ff_to_ff_max_lut_levels": ff_to_ff,
            "note": "controller-clock paths into and out of OSERDESE2/ISERDESE2/IDELAYE2/IDELAYCTRL are not timed by "
                    "nextpnr-xilinx (their ports have no timing class, arch.cc getPortTimingClass -> TMG_IGNORE); "
                    "LUT levels counted on the yosys netlist, placement and routing not included"}


FABRIC_CLOCK_PIP = re.compile(r"^(\S+)\.(IOI_OCLKM?_[01]|IOI_ICLKM?_[01]|IOI_OCLKDIV_[01])\.(IOI_IMUX\S*)\s*$", re.M)
ROUTE_THROUGH = re.compile(r"^(RIOI\S*|LIOI\S*)\.(OLOGIC_Y[01])\.OMUX\.D1\s*$", re.M)


def unescape_sdf(name):
    return re.sub(r"\\(.)", r"\1", name)


def fabric_clocks(fasm_text, routed=None, sdf_text=None):
    """Clock nets that leave the global network through a fabric LUT.

    From the FASM: IOLOGIC clock inputs fed from the fabric (IOI_IMUX) instead of a leaf clock,
    and OLOGIC D1 route-throughs (an output driven from D1 without OSERDES/ODDR, e.g. an
    OBUFDS input). From the routed netlist (--write) when present: every LUT whose input is a
    BUFGCTRL output, with its placement and its loads. From the SDF (--sdf) when present:
    nextpnr's delays from the buffer to the LUT and from the LUT to each load, and the range of
    the delays from the same buffer to the IOLOGIC clock pins it drives directly."""
    record = {"iologic_clock_from_fabric": sorted(f"{m.group(1)}.{m.group(2)}.{m.group(3)}" for m in FABRIC_CLOCK_PIP.finditer(fasm_text)),
              "ologic_d1_route_through": sorted({f"{m.group(1)}.{m.group(2)}" for m in ROUTE_THROUGH.finditer(fasm_text)})}
    if routed is None:
        return record
    module = netlist_top(routed, "top")
    cells = module["cells"]
    names = {}
    for net, info in module.get("netnames", {}).items():
        for bit in info.get("bits", []):
            names.setdefault(bit, net)
    buffers = {}
    for name, cell in cells.items():
        if cell["type"] == "BUFGCTRL":
            for bit in cell["connections"].get("O", []):
                buffers[bit] = name
    loads = defaultdict(list)
    for name, cell in cells.items():
        for port, bits in cell["connections"].items():
            if cell.get("port_directions", {}).get(port) == "input":
                for bit in bits:
                    loads[bit].append((name, cell["type"], port))
    delays = defaultdict(dict)
    if sdf_text:
        for m in re.finditer(r"\(INTERCONNECT\s+(\S+)\s+(\S+)\s+\((\d+):(\d+):(\d+)\)", sdf_text):
            delays[unescape_sdf(m.group(1))][unescape_sdf(m.group(2))] = int(m.group(4))
    luts = []
    for name, cell in sorted(cells.items()):
        if cell["type"] != "SLICE_LUTX":
            continue
        for port, bits in sorted(cell["connections"].items()):
            if cell.get("port_directions", {}).get(port) != "input":
                continue
            for bit in bits:
                if bit not in buffers:
                    continue
                buffer = buffers[bit]
                entry = {"lut": name, "bel": cell.get("attributes", {}).get("NEXTPNR_BEL"),
                         "init": cell.get("parameters", {}).get("INIT"), "clock_net": names.get(bit),
                         "buffer": buffer, "input": port, "loads": {}}
                outs = [b for p, bs in cell["connections"].items()
                        if cell.get("port_directions", {}).get(p) == "output" for b in bs]
                for out in outs:
                    for load, kind, load_port in loads.get(out, []):
                        key = f"{kind}.{load_port}"
                        entry["loads"][key] = entry["loads"].get(key, 0) + 1
                if delays:
                    entry["buffer_to_lut_ps"] = delays.get(f"{buffer}/O", {}).get(f"{name}/{port}")
                    entry["lut_to_load_ps"] = {dst: ps for src, dsts in delays.items() if src.startswith(f"{name}/O")
                                               for dst, ps in sorted(dsts.items())}
                    direct = [ps for dst, ps in delays.get(f"{buffer}/O", {}).items()
                              if re.search(r"/(CLK|CLKB|CLKDIV|OCLK|OCLKB|C)$", dst) and not dst.startswith(name + "/")]
                    if direct:
                        entry["buffer_to_leaf_clock_pins_ps"] = [min(direct), max(direct)]
                luts.append(entry)
    # A LUT6 and a LUT5 in one LUT site share the pins A1-A5: a LUT whose loads are all in the
    # fabric can show a clock net only because its site partner (the inverter) uses that pin.
    sites = defaultdict(list)
    for entry in luts:
        if entry["bel"]:
            sites[entry["bel"][:-4]].append(entry)
    for entry in luts:
        partners = [e["lut"] for e in sites.get((entry["bel"] or "")[:-4], []) if e is not entry]
        if partners and all(k.startswith("SLICE_") for k in entry["loads"]):
            entry["note"] = (f"shares its LUT site's input pins with {partners[0]}; the clock reaches this LUT's "
                             "pin only through that shared pin")
    record["luts_on_clock_nets"] = luts
    return record


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
        inputs = read_first(Path(args.chipdb + ".inputs"))
        record["chipdb"]["inputs"] = inputs.splitlines() if inputs else None
    tree = read_first(src / "source_tree.txt")
    if tree:
        lines = tree.splitlines()
        record["source_tree"] = {"head": lines[0], "tracked_changes": lines[1:], "dirty": len(lines) > 1}
    else:
        record["source_tree"] = None
    warn = read_first(src / "yosys_conflicting_drivers.txt")
    record["yosys_conflicting_driver_warnings"] = int(warn) if warn and warn.isdigit() else None
    xdc = src / f"{args.top}.xdc"
    declared = declared_clocks(xdc.read_text()) if xdc.is_file() else []
    if xdc.is_file():
        record["xdc_sha256"] = sha256(xdc)
    routed = routed_clocks((src / "nextpnr.log").read_text(errors="replace")) if (src / "nextpnr.log").is_file() else []
    by_clock = {c["clock"]: c for c in routed}
    scope = ("register-to-register paths in the fabric only; controller-clock paths into and out of the PHY "
             "primitives (OSERDESE2, ISERDESE2, IDELAYE2, IDELAYCTRL) and the PHY's I/O timing are not analysed, "
             "see phy_paths")
    record["clocks"] = [dict(c, fmax_mhz=by_clock[c["name"]]["fmax_mhz"], verdict=by_clock[c["name"]]["verdict"], scope=scope)
                        if c["name"] in by_clock else
                        dict(c, fmax_mhz=None, verdict="not timed: nextpnr reports no register-to-register path in this "
                             "domain (its loads are PHY primitives, which have no timing class, or fabric LUTs on the clock net)")
                        for c in declared]
    netlist = src / f"{args.top}.json"
    if netlist.is_file():
        module = netlist_top(json.loads(netlist.read_text()), args.top)
        record["netlist_build_id"] = netlist_build_id(module)
        if module is not None:
            record["phy_paths"] = phy_path_levels(module)
    fasm = src / f"{args.top}.fasm"
    if fasm.is_file():
        text = fasm.read_text(errors="replace")
        record["pll_check"] = pll_check(text)
        record["vref"] = [line for line in text.splitlines() if ".VREF." in line]
        routed = src / f"{args.top}.routed.json"
        sdf = src / f"{args.top}.sdf"
        record["fabric_clocks"] = fabric_clocks(text, json.loads(routed.read_text()) if routed.is_file() else None,
                                                sdf.read_text(errors="replace") if sdf.is_file() else None)
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
        record["bitstream"] = bitstream_identity(bit)
    fasm = src / f"{args.top}.fasm"
    if fasm.is_file():
        record["fasm"] = {"lines": sum(1 for _ in open(fasm, "rb")), "sha256": sha256(fasm)}
    frames = src / f"{args.top}.frames"
    if frames.is_file():
        record["frames"] = {"lines": sum(1 for _ in open(frames, "rb")), "sha256": sha256(frames)}
    record["reproducible_identity"] = {
        "fasm_sha256": record.get("fasm", {}).get("sha256"),
        "frames_sha256": record.get("frames", {}).get("sha256"),
        "bitstream_sha256_from_sync": record.get("bitstream", {}).get("sha256_from_sync"),
        "note": "a rebuild with the same inputs and tools reproduces these; bitstream.sha256 covers the .bit header "
                "with xc7frames2bit's date and time and identifies only this one file (the one to flash)"}
    problems = []
    if args.ddr3:
        record["ddr3"] = ddr3_record(src, args)
        if (src / "nextpnr.log").is_file():
            record["nextpnr"]["clocks_routed"] = routed_clocks((src / "nextpnr.log").read_text(errors="replace"))
        xdc = src / f"{args.top}.xdc"
        if xdc.is_file():
            shutil.copy(xdc, out / xdc.name)
            record["files"][xdc.name] = sha256(out / xdc.name)
        bad = [rel for rel, f in record["ddr3"]["uberddr3"]["files"].items() if not f["verified"]]
        if bad:
            problems.append(f"UberDDR3 files do not match {args.lock}: {', '.join(bad)}")
        built = record["ddr3"].get("netlist_build_id")
        if built is not None and built != args.commit[:8].lower():
            problems.append(f"the netlist carries BUILD_ID {built}, not --commit {args.commit}: it was synthesized at "
                            "another commit (rebuild it, or pass that id)")
    for name in ("sim_capture.json",):
        if (src / name).is_file():
            record["pre_silicon"] = json.loads((src / name).read_text())["result"]
    if problems:
        record["problems"] = problems
    (out / "build.json").write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    print(json.dumps({k: v for k, v in record.items() if k in ("commit", "bitstream", "nextpnr", "yosys")}, indent=1)[:1500])
    if args.ddr3:
        print(json.dumps({k: record["ddr3"].get(k) for k in ("clocks", "pll_check", "vref")}, indent=1))
    for problem in problems:
        print(f"fpga-build-report: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
