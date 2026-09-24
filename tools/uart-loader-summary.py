#!/usr/bin/env python3
"""Summary of the UART loader's board records (tools/fpga-uart-loader.py,
`trinity.uart-loader-capture.v2`) in one directory, in run order: per run the payload, the
address, chunks and attempts, the bytes the load changed, the margins, rates and latencies,
the retransmits by reason, the device's status before and after (on the DDR3 build: the
calibration word, the clock count at calib_complete, the clocks off state 23 since then, the
Wishbone master's and arbiter's counters), the H line and the die temperature. Nothing is
measured here: every value is copied or counted from the records named in it.

  python3 tools/uart-loader-summary.py reports/fpga/uart-loader-ddr3-<...>/ --output <...>.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import uart_loader_protocol as proto  # noqa: E402

KEEP = ("calib", "calib_clocks", "calib_lost_clocks", "frames_committed", "frames_duplicate", "bytes_committed",
        "readbacks", "nak_crc", "nak_timeout", "nak_not_ready", "nak_port", "ignored_bytes", "rx_framing_errors",
        "rx_false_starts", "wb_writes", "wb_reads", "wb_read_hits", "wb_acks_dropped", "wb_acks_stray",
        "wb_max_outstanding", "wb_read_latency_max", "wb_cmd_stalls", "arb_switches", "arb_stray", "build_id",
        "config")


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def status(s):
    if not s:
        return None
    out = {k: s[k] for k in KEEP if k in s}
    if "calib" in s:
        out["calib_decoded"] = proto.decode_calib(s["calib"])
    return out


def run_summary(path: Path) -> dict:
    r = json.loads(path.read_text())
    t = r.get("transfer") or {}
    before = t.get("store_before") or {}
    temps = [(r.get(k) or {}).get("xadc", {}).get("value", {}) or {} for k in ("identity_before", "identity_after")]
    return {
        "record": rel(path), "label": r.get("label"), "started_utc": r.get("started_utc"), "ended_utc": r.get("ended_utc"),
        "pass": r.get("pass"), "checks": r.get("checks"), "stopped": r.get("stopped"),
        "bitstream": r.get("bitstream"), "hello": r.get("device_hello"),
        "idcode": ((r.get("identity_before") or {}).get("idcode") or {}).get("value"),
        "dna": ((r.get("identity_before") or {}).get("dna") or {}).get("value"),
        "die_c_before_after": [x.get("temp") for x in temps], "vccint_before_after": [x.get("vccint") for x in temps],
        "payload": {k: (r.get("payload") or {}).get(k) for k in ("kind", "codec", "source", "bytes", "sha256", "trits",
                                                                  "counts")},
        "addr": r.get("addr"), "addr_hex": f"0x{r.get('addr', 0):08x}", "chunk": r.get("chunk"), "faults": r.get("faults"),
        "open_glitch": next((int(r["argv"][i + 1]) for i, a in enumerate(r.get("argv", [])) if a == "--open-glitch"), 0),
        "chunks": t.get("chunks"), "acked": t.get("acked"), "attempts": t.get("attempts"),
        "retransmits": t.get("retransmits"), "late_replies": t.get("late_replies"),
        "identical": t.get("identical"), "bytes_the_load_changes": before.get("bytes_the_load_changes"),
        "read_before_s": before.get("read_s"), "margins": t.get("margins"),
        "load_s": t.get("load_s"), "load_payload_Bps": t.get("load_payload_Bps"),
        "readback_s": t.get("readback_s"), "readback_payload_Bps": t.get("readback_payload_Bps"),
        "ideal_payload_Bps": t.get("ideal_payload_Bps"),
        "latency_s": t.get("latency_s"), "turnaround_s": t.get("turnaround_s"), "gap_s": t.get("gap_s"),
        "queued_s": t.get("queued_s"),
        "calib_polls": [{"t": p["t"], "calib": proto.decode_calib(p["status"]["calib"]) if p.get("status") else None}
                        for p in r.get("calib_polls", [])],
        "status_before": status(r.get("status_before")), "status_after": status(r.get("status_after")),
        "duplicate_replies": [c["duplicate"]["reply"] for c in t.get("per_chunk", []) if "duplicate" in c],
        "rx_raw": r.get("rx_raw"),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    records = sorted((p for p in args.directory.glob("run*.json")), key=lambda p: int(p.name[3:p.name.index("-")]))
    runs = [run_summary(p) for p in records]
    calib = sorted({(r["status_before"] or {}).get("calib_clocks") for r in runs}
                   | {(r["status_after"] or {}).get("calib_clocks") for r in runs})
    out = {"schema": "trinity.uart-loader-summary.v1",
           "written_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "directory": rel(args.directory), "runs": runs,
           "all_pass": all(r["pass"] for r in runs),
           "calib_clocks_seen": calib,
           "calibration_note": "calib_clocks is the device's clock count at the first calib_complete since reset; "
                               "one value in every status of every run means one calibration (no reset) held the "
                               "whole session; calib_lost_clocks counts every clock since then with calib_complete "
                               "low or a state other than 23"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps({"runs": len(runs), "all_pass": out["all_pass"], "calib_clocks_seen": calib}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
