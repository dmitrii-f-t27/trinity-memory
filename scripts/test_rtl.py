#!/usr/bin/env python3
"""Cross-check generated decoders and synchronous streams with Icarus Verilog."""

from __future__ import annotations

import itertools
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


def expected_tables() -> dict[str, list[int]]:
    """Enumerate logical states independently of the RTL generator's division loop."""
    from trinity_memory.codecs import pack, unpack

    tables = {
        "dense5": [0] * 256,
        "sparse41": [0] * 16,
        "baseline5": [0] * 1024,
    }
    lane_bits = {-1: 2, 0: 0, 1: 1}
    for values in itertools.product((-1, 0, 1), repeat=5):
        output = sum(lane_bits[trit] << (2 * lane) for lane, trit in enumerate(values))
        code = sum((trit + 1) * (3**lane) for lane, trit in enumerate(values))
        payload = pack(list(values), codec="dense5")
        if payload != bytes([code]) or unpack(payload, 5, codec="dense5") != list(values):
            raise AssertionError(f"Python dense5 format disagrees at {values}")
        tables["dense5"][code] = (1 << 10) | output
        tables["baseline5"][output] = (1 << 10) | output
    for values in itertools.product((-1, 0, 1), repeat=4):
        nonzero = [(lane, value) for lane, value in enumerate(values) if value]
        if len(nonzero) > 1:
            continue
        code = 0 if not nonzero else 1 + 2 * nonzero[0][0] + (nonzero[0][1] == 1)
        output = sum(lane_bits[trit] << (2 * lane) for lane, trit in enumerate(values))
        payload = pack(list(values), codec="sparse41")
        if payload != bytes([code]) or unpack(payload, 4, codec="sparse41") != list(values):
            raise AssertionError(f"Python sparse41 format disagrees at {values}")
        tables["sparse41"][code] = (1 << 8) | output
    return tables


def main() -> int:
    for tool in ("iverilog", "vvp"):
        if shutil.which(tool) is None:
            print(f"Required simulator tool not found: {tool}. Install Icarus Verilog.", file=sys.stderr)
            return 2
    subprocess.run([sys.executable, str(ROOT / "scripts/generate_rtl.py"), "--check"], check=True)
    tables = expected_tables()
    sources = sorted((ROOT / "rtl").glob("*.v")) + sorted((ROOT / "rtl/generated").glob("*.v"))
    with tempfile.TemporaryDirectory(prefix="trinity-rtl-") as temporary:
        directory = Path(temporary)
        for name, values in tables.items():
            (directory / f"{name}_expected.mem").write_text("".join(f"{value:03x}\n" for value in values))
        for testbench in ("tb_decoders", "tb_streams"):
            simulation = directory / testbench
            subprocess.run(
                ["iverilog", "-g2012", "-Wall", "-s", testbench, "-o", str(simulation),
                 *map(str, sources), str(ROOT / "rtl/tb" / f"{testbench}.sv")],
                check=True, cwd=directory,
            )
            subprocess.run(["vvp", str(simulation)], check=True, cwd=directory)
    print("PASS RTL/Python format agreement: 243 dense5 and 9 sparse41 valid logical states")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
