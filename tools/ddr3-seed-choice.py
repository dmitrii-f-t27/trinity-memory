#!/usr/bin/env python3
"""The seed of a DDR3 build to load, chosen from a seed sweep BEFORE any board load, by the
rule of #61/#62 (docs/hardware.md, "Placement of 42b6f5a9"): among the seeds whose routed
controller clock meets 83.33 MHz, the one whose larger |CK - DQS| over the byte lanes is
smallest, in nextpnr-xilinx's delay model (clock buffer -> the fabric inverter LUT -> the CK
OBUFDS and each lane's DQS OSERDES clock; tools/ddr3-reader-summary.py, seed_delays).

The rule is a correlation from #61's x32 placements and #62's x16 ones, not a timing analysis;
the output records every seed's delays and the ranking, and the time it was written, so that
it can be compared with the time of the first load. Nothing here is measured: every value is
read from the sweep (tools/fpga-seed-sweep.py).

  python3 tools/ddr3-seed-choice.py --sweep reports/fpga/ddr3-seed-sweep-<...>.json \\
      --output reports/fpga/ddr3-seed-choice-<...>.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def summary_tool():
    spec = importlib.util.spec_from_file_location("ddr3_reader_summary", ROOT / "tools/ddr3-reader-summary.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def choose(sweep: dict) -> dict:
    tool = summary_tool()
    seeds = [tool.seed_delays(run) for run in sweep["runs"]]
    ranked = sorted((s for s in seeds if s["meets_83_33"] and "largest_abs_ck_minus_dqs_ps" in s),
                    key=lambda s: (s["largest_abs_ck_minus_dqs_ps"], s["seed"]))
    return {"seeds": seeds, "seed_rank": [s["seed"] for s in ranked],
            "chosen_seed": ranked[0]["seed"] if ranked else None}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sweep", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--note", default="")
    args = parser.parse_args(argv)
    sweep = json.loads(args.sweep.read_text())
    result = choose(sweep)
    record = {
        "schema": "trinity.ddr3-seed-choice.v1",
        "written_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "sweep": rel(args.sweep), "sweep_written_at": sweep.get("written_at"), "commit": sweep.get("commit"),
        "netlist_sha256": sweep.get("netlist_sha256"),
        "rule": "among the seeds whose routed controller clock meets 83.33 MHz, the smallest largest "
                "|CK - DQS| over the byte lanes (nextpnr-xilinx's delay model); the #61/#62 rule, a "
                "correlation, not a timing analysis",
        "note": args.note, **result}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=1) + "\n")
    print(json.dumps({"chosen_seed": result["chosen_seed"], "seed_rank": result["seed_rank"]}))
    return 0 if result["chosen_seed"] is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
