#!/usr/bin/env python3
"""Write build/trios/build_report.json: hashes, pins and figures of one bitstream build."""
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

b = Path("build/trios")
fw = Path("fpga/ax7203-trios")


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def tail_util(log):
    out = {}
    for m in re.finditer(r"^Info:\s+(\w+):\s+(\d+)/\s*(\d+)\s+(\d+)%", Path(log).read_text(errors="replace"), re.M):
        out[m.group(1)] = {"used": int(m.group(2)), "of": int(m.group(3)), "percent": int(m.group(4))}
    return out


def stat_cells(stat):
    cells = {}
    for m in re.finditer(r"^\s+(\d+)\s+(\S+)\s*$", Path(stat).read_text(errors="replace"), re.M):
        cells[m.group(2)] = int(m.group(1))
    return cells


chosen = (b / "nextpnr.log")
rep = {
    "schema": "trinity.fpga-build.v1/trios-mesh-ax7203",
    "source_commit": sys.argv[1],
    "image": sys.argv[2],
    "part": "xc7a200tfbg484-2",
    "clock_mhz": 25,
    "cpu": {"core": "PicoRV32 RV32I (no M/C/counters/IRQ), YosysHQ/picorv32 ef203c2b0a3fb793280f5114941416c425c5b461",
            "picorv32_v_sha256": sha(fw / "third_party/picorv32.v")},
    "payload": {"trios_mesh_source": "gHashTag/trios 70882158c9ff07c1157ff8a1a50ce39ee172fb3c crates/trios-mesh",
                "vendor_sha256_list_sha256": sha(fw / "fw/VENDOR-SHA256.txt"),
                "firmware_elf_sha256": sha(b / "firmware.elf"), "firmware_bin_sha256": sha(b / "firmware.bin"),
                "cargo_lock_sha256": sha(fw / "fw/Cargo.lock")},
    "bitstream": {"bit_sha256_whole_file": sha(b / "trios_soc_ax7203.bit"), "fasm_sha256": sha(b / "trios_soc_ax7203.fasm"),
                  "frames_sha256": sha(b / "trios_soc_ax7203.frames"), "bytes": (b / "trios_soc_ax7203.bit").stat().st_size},
    "xdc_sha256": sha(fw / "trios_soc_ax7203.xdc"),
    "yosys_cells": stat_cells(b / "yosys_stat.txt"),
    "nextpnr_utilisation": tail_util(chosen),
    "timing": json.loads((b / "timing.json").read_text()),
}
(b / "build_report.json").write_text(json.dumps(rep, indent=1))
print(json.dumps({k: rep[k] for k in ("bitstream", "timing")}, indent=1))
