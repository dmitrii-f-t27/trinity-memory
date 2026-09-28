#!/usr/bin/env python3
"""Issue #65, consumer (B): the device matvec's weights per second from DDR3,
dense5 (1.6 bits/weight) vs baseline2 (2 bits/weight), with error bars.

The chunk is loaded once per format (the golden 320-row q_proj chunk through
the #63 loader); every run then rings the doorbell with a fresh counter and
reads the summary lines (d run, c words + cycles, o max outstanding +
command stalls, w issue-hold cycles + wait stalls, n bad words + consumer stalls, u act words,
z runs + total bad). Weights/s = logical trits x f_ctrl / cycles, per run;
the report carries min/median/max and mean +- sample s.d. (at least --runs
runs), the bitstream sha256 of the flashed build, IDCODE and DNA, and the
bit-exactness verdict of the first run's 320 Y lines against the reference
(computed from the cached fixtures, sha-tied to the committed golden report).

  python3 tools/fpga-ddr3-matvec-measure.py --port /dev/cu.usbserial-10 \
      --fmt d5 --build reports/fpga/ddr3-build-2026-09-27-twoclock-m6d5/build.json \
      --output reports/fpga/ddr3-matvec-measure-2026-09-27-d5.json

Exit status 0 when every run's Y lines are bit-exact and the statistic was
written; 1 otherwise; 2 when a board tool failed (then nothing is written).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("fpga_matvec_run", ROOT / "tools/fpga-matvec-run.py")
run_tool = importlib.util.module_from_spec(_spec)  # hyphen-named module
_spec.loader.exec_module(run_tool)
import matvec_device_model as model  # noqa: E402

SCHEMA = "trinity.fpga-ddr3-matvec-measure.v2"


def board_identity(cable: str) -> dict:
    out = {"cable": cable}
    for key, cmd in (("idcode", ["openFPGALoader", "--detect", "--cable", cable]),
                     ("dna", ["openFPGALoader", "--read-dna", "--cable", cable])):
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            lines = [l.strip() for l in (done.stdout + done.stderr).splitlines() if l.strip()]
            out[key] = lines if lines else f"exit {done.returncode}"
        except Exception as exc:  # noqa: BLE001 - the measurement may proceed without it
            out[key] = f"failed: {exc}"
    return out


def signed40(b: int) -> int:
    return b - (1 << 40) if b >= (1 << 39) else b


def one_run(port: str, baud: int, counter: int, timeout: float,
            raw: bytearray | None = None) -> list:
    """One doorbell run: decoded lines, optionally retaining every received byte."""
    import struct
    import uart_loader_protocol as proto
    import serial
    lines, buf = [], b""
    with serial.Serial(port, baud, timeout=0.2) as com:
        def read(count):
            data = com.read(count)
            if raw is not None:
                raw.extend(data)
            return data
        com.reset_input_buffer()
        for attempt in range(6):
            if attempt:
                time.sleep(0.05)
            com.write(proto.load_frame(1, 0x40 * 16,
                                       struct.pack("<QQ", (run_tool.MAGIC << 32) | counter, 0)))
            com.flush()
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                buf += read(256)
                if run_tool.load_acknowledged(buf):
                    break
            if run_tool.load_acknowledged(buf):
                break
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            buf += read(512)
            while len(buf) >= 20:
                line, buf = buf[:20], buf[20:]
                if line[19:20] != b"\n":
                    nl = buf.find(b"\n")
                    if nl >= 0:
                        buf = buf[nl + 1:]
                    continue
                tag = chr(line[0])
                if tag not in run_tool.LINE_TAGS:
                    continue
                lines.append((tag, int(line[1:9], 16), int(line[9:19], 16)))
                if tag == "z":
                    return lines
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", default="/dev/cu.usbserial-10")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--cable", default="digilent_hs2")
    ap.add_argument("--fmt", choices=("d5", "b2"), required=True)
    ap.add_argument("--rows", type=int, default=320)
    ap.add_argument("--runs", type=int, default=12)
    ap.add_argument("--design-hz", type=float, default=60000000.0)
    ap.add_argument("--build", required=True, help="build.json of the flashed bitstream")
    ap.add_argument("--work", default=str(ROOT / "build/fpga/matvec-run"))
    ap.add_argument("--output", required=True)
    ap.add_argument("--max-temp", type=float, default=70.0)
    args = ap.parse_args()

    fmt = model.FMT_D5 if args.fmt == "d5" else model.FMT_B2
    lanes = model.LANES[fmt]
    cols = 2560
    trits_total = args.rows * cols

    build = json.loads(Path(args.build).read_text())
    print(f"reference: computing from the cached fixtures ({args.fmt}) ...", flush=True)
    trits, acts, expected, _ = run_tool.chunk_reference(args.rows)
    import struct
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    stream = bytearray()
    for r in range(args.rows):
        for w in range(cols // lanes):
            lo, hi = model.encode_word(fmt, model.lane_word(trits, r * cols + w * lanes))
            stream += struct.pack("<QQ", lo, hi)
    weights = work / f"weights.{args.fmt}.bin"
    weights.write_bytes(stream)
    act_path = work / "activations.int8.bin"
    act_path.write_bytes(bytes(v & 255 for v in acts))

    print("load: weights + activations (once) ...", flush=True)
    run_tool.loader_load(args.port, args.baud, weights, 0x4000, work / f"{args.fmt}.w.json",
                         int(args.design_hz), 2048)
    run_tool.loader_load(args.port, args.baud, act_path, 0x1000, work / f"{args.fmt}.a.json",
                         int(args.design_hz), 2048)

    runs, first_y, captures = [], None, []
    output = Path(args.output)
    capture_dir = output.with_suffix(".uart")
    capture_dir.mkdir(parents=True, exist_ok=True)
    for n in range(1, args.runs + 1):
        raw = bytearray()
        lines = one_run(args.port, args.baud, n, 240.0, raw)
        raw_path = capture_dir / f"run-{n:02d}.txt"
        raw_path.write_bytes(raw)
        raw_path.with_suffix(".json").write_text(json.dumps(lines) + "\n")
        captures.append({"run": n, "file": f"{capture_dir.name}/{raw_path.name}",
                         "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        y = [signed40(b) for tag, a, b in lines if tag == "y"]
        try:
            counters = model.decode_run_summary(lines)
        except ValueError as exc:
            print(f"run {n}: {exc} — stopping", flush=True)
            return 1
        words, cycles = counters["words"], counters["cycles"]
        if (counters["device_run"] != n or counters["runs_done"] != n
                or words != trits_total // lanes or counters["bad_words"] or counters["stray_acks"]):
            print(f"run {n}: wrong run, word count or bad words — stopping", flush=True)
            return 1
        exact = (len(y) == args.rows and y == expected
                 and [a for tag, a, _ in lines if tag == "y"] == list(range(args.rows)))
        if first_y is None:
            first_y = {"y_lines": len(y), "bit_exact": exact,
                       "first": y[:4], "expected": expected[:4]}
        if not exact:
            print(f"run {n}: NOT bit-exact ({len(y)} lines) — stopping", flush=True)
            return 1
        wps = trits_total * args.design_hz / cycles if cycles else 0.0
        runs.append({"run": n, **counters, "weights_per_s": round(wps, 1)})
        print(f"run {n}: {words} words, {cycles} cycles, {wps/1e6:.3f} M weights/s", flush=True)

    vals = [r["weights_per_s"] for r in runs]
    stat = {"runs": len(vals), "min": min(vals), "median": statistics.median(vals),
            "max": max(vals), "mean": round(statistics.mean(vals), 1),
            "sample_sd": round(statistics.stdev(vals), 1) if len(vals) > 1 else 0.0}
    consumer_stalls = sum(r["consumer_stalls"] for r in runs)
    memory_bound = consumer_stalls == 0
    variant = build.get("ddr3", {}).get("variant", "")
    deferred = "defer_results 1" in variant
    explicit_timing = "defer_results " in variant
    timing = ("weight phase through consumer drain, including DRAIN_CLOCKS; activation prefetch excluded; "
              + ("Y output emitted after timing stops" if deferred else "UART Y output backpressure included")) \
        if explicit_timing else "legacy weight-phase counter; see source commit for timing boundaries"
    report = {
        "schema": SCHEMA, "written_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "issue": "dmitrii-f-t27/trinity-memory#65", "consumer": "B (device matvec of #64)",
        "format": {"name": "dense5" if fmt == model.FMT_D5 else "baseline2",
                   "bits_per_weight": 1.6 if fmt == model.FMT_D5 else 2.0,
                   "lanes_per_word": lanes},
        "clock": {"ctrl_hz": args.design_hz,
                  "source": "PLL 6/5 from the board's 200 MHz oscillator (derived, not instrument-measured)"},
        "chunk": {"rows": args.rows, "cols": cols, "weights": trits_total,
                  "model": "BitNet b1.58 2B4T layer-0 self_attn.q_proj rows 0-319",
                  "activations": "t27 tmv_activations seed 27",
                  "reference": "trinity_memory.matvec from the cached fixtures, sha-tied to "
                               "reports/ternary-check/matvec-2026-09-23.json"},
        "bitstream": {"sha256": build["bitstream"]["sha256"],
                      "routed_fmax_mhz": build["nextpnr"]["clocks_routed"][0]["fmax_mhz"],
                      "commit": build["commit"]},
        "board": board_identity(args.cable),
        "read_discipline": variant or "read settings not recorded by the build report",
        "timing_scope": timing,
        "first_run_verdict": first_y,
        "runs": runs,
        "uart_captures": captures,
        "statistic": stat,
        "memory_bound": {"consumer_stalls_zero": memory_bound,
                         "verdict": ("not established: Y output is deferred, but the matvec consumer and "
                                     "request policy can still limit throughput" if deferred else
                                     "not established: result-output isolation is not demonstrated")},
        "ceiling": {"arithmetic_weights_per_s": args.design_hz * lanes,
                    "note": "x16: 128 bits per controller clock, 80 dense5 or 64 baseline2 weights; "
                            "arithmetic only, not measured delivery"},
    }
    output.write_text(json.dumps(report, indent=1))
    print(json.dumps(stat, indent=1))
    print("memory-bound:", report["memory_bound"]["verdict"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
