#!/usr/bin/env python3
"""Summary of the DDR3 read path's builds and board loads (issue #62), from the committed files.

Reads a seed sweep of tools/fpga-seed-sweep.py (per seed: Fmax, FASM and, in nextpnr-xilinx's
delay model, the clock-buffer -> fabric-inverter-LUT -> CK / DQS delays) and the capture
records of tools/fpga-ddr3-capture.py (capture.json or capture.json.gz), and writes one JSON:
per seed the delays and the ranking metric of the seed choice, per load what the record says
(calibration, the S lines before and after calibration complete, the reader's totals and
per-format ranges, the die temperature before and after), and every load in time order.
Nothing here is measured anew: each value is copied or counted from the files named in it.

  python3 tools/ddr3-reader-summary.py --sweep reports/fpga/ddr3-seed-sweep-<...>.json \\
      --build seed6=reports/fpga/ddr3-build-<...>-seed6 \\
      --loads seed6=reports/fpga/ddr3-reader-<...>-seed6 \\
      --netlist "<text>" --seed-choice "<text>" --output reports/fpga/ddr3-reader-summary-<...>.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = "trinity.ddr3-reader-summary.v2"
DONE_CALIBRATE = 23


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def utc_plus(t0: str, seconds: float) -> str:
    moment = dt.datetime.fromisoformat(t0.replace("Z", "+00:00")) + dt.timedelta(seconds=seconds)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def seed_delays(run: dict) -> dict:
    """CK and each lane's DQS OSERDES clock, from the clock buffer through the fabric LUT (ps)."""
    out = {"seed": run["seed"], "routed": run.get("routed", False), "fmax_mhz": None, "meets_83_33": False,
           "fasm_sha256": run.get("fasm_sha256")}
    for clock in run.get("clocks_routed") or []:
        if clock["clock"] == "clk_ctrl":
            out["fmax_mhz"] = clock["fmax_mhz"]
            out["meets_83_33"] = clock["verdict"] == "PASS"
    luts = run.get("luts_on_clock_nets") or []
    if len(luts) == 1:
        lut = luts[0]
        base, loads = lut["buffer_to_lut_ps"], lut["lut_to_load_ps"]
        ck = base + max(v for k, v in loads.items() if "OBUFDS" in k)
        lanes = sorted((int(re.search(r"genblk7\[(\d+)\]", k).group(1)), v) for k, v in loads.items()
                       if "OSERDESE2_dqs" in k)
        dqs = [base + v for _, v in lanes]
        skew = [ck - d for d in dqs]
        out.update({"inverter_lut": lut["bel"], "ck_ps": ck, "dqs_clk_ps_by_lane": dqs,
                    "ck_minus_dqs_ps_by_lane": skew, "largest_abs_ck_minus_dqs_ps": max(abs(s) for s in skew)})
    return out


def read_record(directory: Path) -> dict:
    for name in ("capture.json", "capture.json.gz"):
        path = directory / name
        if path.exists():
            data = path.read_bytes()
            return json.loads(gzip.decompress(data) if name.endswith(".gz") else data)
    raise FileNotFoundError(f"no capture.json(.gz) in {directory}")


def load_summary(directory: Path) -> dict:
    record = read_record(directory)
    run, dec = record["run"], record["decoded"]
    final, reset = dec.get("final") or {}, dec.get("reset") or {}
    reset_s = reset.get("reset_s")
    since = [s for s in dec.get("status_lines", []) if reset_s is not None and s["t_s"] >= reset_s]
    after = [s for s in since if s["calib_complete"]]
    before = [s for s in since if not s["calib_complete"]]
    out = {
        "capture": rel(directory),
        "label": record.get("label"),
        "bitstream_sha256": (record.get("bitstream") or {}).get("report_sha256"),
        "t0_utc": run["t0_utc"],
        "load_end_utc": utc_plus(run["t0_utc"], run["load_end_s"]) if run.get("load_end_s") is not None else None,
        "seconds_captured": next((float(record["argv"][i + 1]) for i, a in enumerate(record.get("argv", []))
                                  if a == "--seconds"), None),
        "exit_status": record.get("exit_status"),
        "calib_complete": final.get("calib_complete"), "state": final.get("state"),
        "highest_state": final.get("highest"), "returns_to_idle": final.get("returns_to_idle"),
        "seconds_after_reset": reset.get("last_line_since_reset_s"),
        "s_lines_since_reset": len(since),
        "s_lines_before_calib_complete": len(before),
        "s_lines_after_calib_complete": len(after),
        "after_calib_all_done_calibrate": bool(after) and all(
            (s["state"], s["highest"], s["returns_to_idle"]) == (DONE_CALIBRATE, DONE_CALIBRATE, 0) for s in after)
        and not any(s["calib_complete"] == 0 for s in since if after and s["t_s"] > after[0]["t_s"]),
        "after_calib_span_s": round(after[-1]["t_s"] - after[0]["t_s"], 3) if after else None,
        "calib_complete_s_after_reset": (dec.get("calibration") or {}).get("calib_complete_between_s"),
        "clock_ppm_from_nominal": (dec.get("clock") or {}).get("ppm_from_nominal"),
        "die_c": [run.get("xadc_before", {}).get("temp"), run.get("xadc_after", {}).get("temp")],
        "vccint_v": [run.get("xadc_before", {}).get("vccint"), run.get("xadc_after", {}).get("vccint")],
    }
    reader = dec.get("reader")
    if reader:
        totals = dict(reader["totals"])
        runs = reader["runs"]
        out.update(totals)
        out["per_format"] = reader["per_format"]
        out["first_report_s_after_load_end"] = (round(runs[0]["reported_t_s"] - run["load_end_s"], 3)
                                               if runs and run.get("load_end_s") is not None else None)
        out["checks_failed"] = sorted({k for r in runs for k, v in r["checks"].items() if not v})
        out["dot_values"] = len({r["dot"] for r in runs})
        out["count_pairs"] = len({(r["plus"], r["minus"]) for r in runs})
        out["checksum_values"] = {name: len({r["checksum"] for r in runs if r["format_name"] == name})
                                  for name in reader["per_format"]}
    else:
        out["runs"] = 0
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sweep", type=Path, required=True)
    parser.add_argument("--build", action="append", default=[], help="label=report directory")
    parser.add_argument("--loads", action="append", default=[], help="label=directory holding load*/")
    parser.add_argument("--netlist", default="")
    parser.add_argument("--seed-choice", default="")
    parser.add_argument("--note", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sweep = json.loads(args.sweep.read_text())
    seeds = [seed_delays(r) for r in sorted(sweep["runs"], key=lambda r: r["seed"])]
    ranked = sorted((s for s in seeds if s["meets_83_33"] and "largest_abs_ck_minus_dqs_ps" in s),
                    key=lambda s: (s["largest_abs_ck_minus_dqs_ps"], s["seed"]))
    board, order = {}, []
    for spec in args.loads:
        label, directory = spec.split("=", 1)
        loads = [load_summary(d) for d in sorted(Path(directory).glob("load*/"))]
        board[label] = loads
        order += [{"build": label, "capture": x["capture"], "t0_utc": x["t0_utc"], "load_end_utc": x["load_end_utc"],
                   "die_c_before": x["die_c"][0], "calibrated": x["calib_complete"] == 1} for x in loads]
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    summary = {
        "schema": SCHEMA,
        "written_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "written_by": {"tool": "tools/ddr3-reader-summary.py", "repository_head": head},
        "netlist": args.netlist,
        "sweep": rel(args.sweep),
        "skew_note": "nextpnr-xilinx 0.9.7's delay model (the sweep's luts_on_clock_nets): clock buffer -> fabric "
                     "inverter LUT -> CK OBUFDS input (ck_ps, the later of its P and N inputs) and -> each lane's "
                     "DQS OSERDES CLK. Not measured on silicon.",
        "seed_rank_metric": "largest |CK - DQS| over the byte lanes (largest_abs_ck_minus_dqs_ps), among the seeds "
                            "whose controller clock meets 83.33 MHz; smaller first",
        "seed_rank": [s["seed"] for s in ranked],
        "seed_choice": args.seed_choice,
        "seeds": seeds,
        "builds": {label: rel(Path(d)) for label, d in (b.split("=", 1) for b in args.build)},
        "board": board,
        "load_order": sorted(order, key=lambda x: x["t0_utc"]),
        "notes": args.note,
    }
    args.output.write_text(json.dumps(summary, indent=1) + "\n")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
