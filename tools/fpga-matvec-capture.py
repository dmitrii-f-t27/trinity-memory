#!/usr/bin/env python3
"""Weights per second from DDR3 through the device matvec (issue #65, consumer (B)).

The DDR3 matvec build (make -C fpga/ax7203 ddr3-bit DDR3_APP=matvec; docs/bridge.md, "The DDR3
matvec build") runs the #64 chunk, BitNet b1.58 2B4T layer-0 q_proj rows 0-319 (2,560 columns),
in dense5 and baseline2, --runs times each on data loaded once; this tool records every run's
counters, the device's Z lines, with the statistic of #65:

  python3 tools/fpga-matvec-capture.py --port /dev/cu.usbserial-110 --identity \\
      --build-report reports/fpga/ddr3-build-<date>-<id>-x16-matvec-seed<n>/build.json \\
      --runs 10 --output reports/fpga/matvec-ddr3-<date>-<id>/capture.json

Per format: the row-padded image of the chunk is loaded into DDR3 with L frames, every chunk read
back and compared (tools/fpga-uart-loader.py's transfer), the activations sent with X frames, then
the M runs. Each run's Y lines are compared with t27/matvec.t27's first accumulators (the chunk's
reference) and its eleven Z lines kept: words, cycles (clocks from the first word taken to the
last), idle clocks (clocks of that span with no word offered: the bus left the consumer waiting),
latency, invalid codes, stray words, consumer stalls, and the result checksum (recomputed here
from the Y lines).

Definitions (#65): weights per second of a run = rows x cols (logical trits, padding lanes
excluded) x f_ctrl / cycles, where f_ctrl is the controller clock derived from the board's 200 MHz
oscillator (--design-hz, 250/3 MHz = 83.33 MHz for PLL MULT 5 and DDR_DIV 3: arithmetic, not an
instrument measurement). Memory-bound: the consumer never held the bus off (consumer stalls 0 in
every run of both formats); in this build that count is 0 by construction (the feed asks for the
first word only after in_ready rose, which stays high until the run's last word), so the verdict
states the design, and the matvec takes a word in every clock of RUN, so every idle clock of the
span is the bus's (or the feed's cap, which the board does not report). The arithmetic ceiling of
the ratio dense5 / baseline2 is 80 / 64 = 1.25 (payload only; the row padding counts against it,
and there is none at 2,560 columns). Statistic
per format: min / median / max and mean +- sample s.d. over the runs that ran (status 0) with every
accumulator equal to the reference; runs after which the calibration had dropped, or was not shown
to hold (the status read after each format), are reported and excluded with the reason.

The record (trinity.fpga-ddr-capture.v1, --output) holds the tool's argv and revision, the
bitstream's sha256 and BUILD_ID from its build record, IDCODE, DNA and the die temperature before
and after (--identity: openFPGALoader), the chunk's reference hash (its whole-q_proj sha256 must
equal reports/ternary-check/matvec-2026-09-23.json), each format's upload and every run with host
times, the statistics and the verdicts; the bytes received are kept next to it
(<output>.rx.bin.gz). Exit status 0 when every run of every format ran and matched the reference,
1 otherwise, 2 when the run was stopped (temperature, port, identity).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))
import bridge_link_protocol as blink  # noqa: E402
import uart_loader_protocol as proto  # noqa: E402

SCHEMA = "trinity.fpga-ddr-capture.v1"
DESIGN_HZ = 250e6 / 3                          # 200 MHz x 5 / (4 x 3): the DDR3 builds' controller clock
FORMATS = {"dense5": 1, "baseline2": 0}
INDEX_ACT, INDEX_MATVEC = blink.CMD_INDEX[blink.CMD_ACT], blink.CMD_INDEX[blink.CMD_MATVEC]


def loader_tool():
    spec = importlib.util.spec_from_file_location("fpga_uart_loader_tool", ROOT / "tools/fpga-uart-loader.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


UL = loader_tool()


def resp(event) -> dict:
    return {"seq": event.a >> 24, "index": (event.a >> 17) & 7, "reason": proto.REASONS[(event.a >> 20) & 15]
            if ((event.a >> 20) & 15) < len(proto.REASONS) else "?", "tag": event.tag, "t": event.t}


class Matvec:
    """X and M frames on the loader tool's Link and Loader (their seq counter, deadlines, reader
    thread and retransmission rules)."""

    def __init__(self, loader):
        self.loader, self.link = loader, loader.link

    def _answer(self, seq, index, until):
        def match(event):
            return (event.kind == "line" and event.check_ok and event.tag in "AN" and event.a >> 24 == seq
                    and (event.a >> 17) & 7 == index and (event.a >> 16) & 1)
        return self.link.take(match, until)

    def act(self, block: int, data: bytes) -> dict:
        """One X frame (at most 51 blocks); retransmitted after a nak or no answer."""
        record = {"block": block, "len": len(data), "attempts": []}
        for attempt in range(self.loader.args.max_attempts):
            if attempt:
                self.loader.guard()
            seq = self.loader.next_seq()
            frame = blink.act_frame(seq, block, data)
            start, queued, _ = self.link.write(frame)
            event = self._answer(seq, INDEX_ACT, self.loader.deadline(start, queued, len(frame) + proto.LINE_BYTES))
            record["attempts"].append({"seq": seq, "start": round(start, 6),
                                       "reply": None if event is None else resp(event)})
            if event is not None and event.tag == "A":
                record["acked"] = True
                return record
        record["acked"] = False
        return record

    def run(self, addr: int, rows: int, cols: int, fmt: int) -> dict:
        """One M frame and its Y and Z lines; retransmitted while the device answers not_ready
        or nothing (an M that was acknowledged is not sent again: it ran)."""
        record = {"attempts": []}
        for attempt in range(self.loader.args.max_attempts):
            if attempt:
                self.loader.guard()
            seq = self.loader.next_seq()
            frame = blink.matvec_frame(seq, addr, rows, cols, fmt)
            start, queued, _ = self.link.write(frame)
            event = self._answer(seq, INDEX_MATVEC, self.loader.deadline(start, queued, len(frame) + proto.LINE_BYTES))
            record["attempts"].append({"seq": seq, "start": round(start, 6),
                                       "reply": None if event is None else resp(event)})
            if event is None or event.tag == "N":
                continue
            lines = rows + blink.Z_COUNT
            until = event.t + UL.wire_s(lines * proto.LINE_BYTES, self.link.baud) + self.loader.args.ack_timeout + 1.0
            ys, zs = {}, {}
            while len(zs) < blink.Z_COUNT:
                line = self.link.take(lambda e, s=seq: e.kind == "line" and e.check_ok and e.tag in "YZ"
                                      and e.a >> 24 == s, until)
                if line is None:
                    break
                if line.tag == "Y":
                    ys[line.a & 0xFFFF] = blink.signed32(line.v)
                else:
                    zs[blink.Z_NAMES[line.a & 0xFFFF]] = line.v
            record.update({"seq": seq, "ack_t": event.t, "end_t": self.link.now(), "y": [ys[i] for i in sorted(ys)],
                           "y_rows": sorted(ys) == list(range(len(ys))), "z": zs,
                           "complete": len(zs) == blink.Z_COUNT})
            return record
        record["complete"] = False
        return record


def spread(values):
    if not values:
        return None
    out = {"n": len(values), "min": min(values), "median": statistics.median(values), "max": max(values),
           "mean": statistics.fmean(values)}
    out["sd"] = statistics.stdev(values) if len(values) > 1 else None
    return out


def parse(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", required=True)
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--build-report", type=Path, help="build.json of the loaded bitstream (sha256, BUILD_ID)")
    p.add_argument("--design-hz", type=float, default=DESIGN_HZ, help="f_ctrl for weights per second")
    p.add_argument("--formats", default="dense5,baseline2")
    p.add_argument("--rows", type=int, default=320, help="the first N rows of the chunk (default all 320)")
    p.add_argument("--runs", type=int, default=10)
    p.add_argument("--region-dense5", type=lambda v: int(v, 0), default=0x0100_0000)
    p.add_argument("--region-baseline2", type=lambda v: int(v, 0), default=0x0B04_0000)
    p.add_argument("--chunk", type=int, default=proto.MAX_LEN)
    p.add_argument("--first-seq", type=int, default=1)
    p.add_argument("--ack-timeout", type=float, default=0.5)
    p.add_argument("--guard", type=float, default=0.15)
    p.add_argument("--max-attempts", type=int, default=6)
    p.add_argument("--identity", action="store_true", help="IDCODE, DNA and XADC before, XADC after (openFPGALoader)")
    p.add_argument("--max-temp", type=float, default=80.0)
    p.add_argument("--openfpgaloader", default="openFPGALoader")
    p.add_argument("--cable", default="digilent_hs2")
    p.add_argument("--label", default="")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args(argv)
    args.formats = [f for f in args.formats.split(",") if f]
    if not args.formats or any(f not in FORMATS for f in args.formats):
        p.error("--formats is a list of dense5 and baseline2")
    if not 1 <= args.rows <= 320 or args.runs < 1:
        p.error("--rows 1..320 and --runs >= 1")
    args.drain, args.garbage_bytes = False, 64                  # what the loader tool's Loader reads
    return args


def main(argv=None) -> int:
    args = parse(argv)
    import stage1_chunk
    chunk = stage1_chunk.chunk()
    rows, cols = args.rows, stage1_chunk.COLS
    trits, want = chunk["trits"][:rows * cols], list(chunk["y"])[:rows]
    t0 = time.monotonic()
    record = {"schema": SCHEMA, "started_utc": UL.utc(), "label": args.label,
              "argv": sys.argv if argv is None else ["tools/fpga-matvec-capture.py", *argv],
              "tool_revision": UL.tool_revision(), "port": args.port, "baud": args.baud, "design_hz": args.design_hz,
              "consumer": "B: the device matvec (t27/rtl/fpga_ddr3_matvec.t27) fed from DDR3 by t27/rtl/fpga_matvec_feed.t27",
              "chunk": {"tensor": stage1_chunk.TENSOR, "rows": [0, rows], "cols": cols, "logical_trits": rows * cols,
                        "activations": f"tmv_activations seed {stage1_chunk.SEED} (t27/matvec.t27)",
                        "reference_full_sha256": chunk["full_sha256"],
                        "reference_matches_report": chunk["full_sha256"] == chunk["report_sha256"]},
              "definitions": {
                  "weights_per_s": "rows x cols x design_hz / cycles (Z line 4); design_hz from the 200 MHz oscillator "
                                   "and the PLL settings, not measured",
                  "memory_bound": "consumer stalls (Z line 9) 0 in every run of both formats: the matvec never held "
                                  "the bus off, so the idle clocks (Z line 5) are the bus's; 0 by construction in "
                                  "this build (the feed asks only after in_ready rose)",
                  "ceiling": "dense5 / baseline2 at most 80 / 64 = 1.25 (arithmetic)"},
              "t0_note": "host times are seconds after the start on one monotonic clock"}
    if args.build_report:
        build = json.loads(args.build_report.read_text())
        record["bitstream"] = {"build_report": str(args.build_report), "sha256": build["bitstream"]["sha256"],
                               "build_id": build["commit"][:8]}
    code = 0
    if args.identity:
        record["identity_before"] = UL.run_identity(args, t0, "before")
        temp = (record["identity_before"].get("xadc", {}).get("value") or {}).get("temp")
        if any(v.get("returncode") for v in record["identity_before"].values()) or temp is None:
            record["stopped"] = "openFPGALoader failed (cable not found?)"
            code = 2
        elif temp >= args.max_temp:
            record["stopped"] = f"die at {temp} C (limit {args.max_temp} C)"
            code = 2
    link = None
    if not code:
        link = UL.Link(args.port, args.baud, t0)
        with link.cond:
            link.decoder = blink.LinkDecoder()          # the loader's lines plus Y and Z
        loader = UL.Loader(link, args, None)
        mv = Matvec(loader)
        try:
            status = loader.status()
            record["status_before"] = status
            protocol = None if status is None else status["config"] >> 24
            record["device"] = {"protocol": protocol, "build_id": None if status is None else f"{status['build_id']:08x}",
                                "calib_ok": UL.calib_ok(status)}
            if status is None or protocol is None or protocol < blink.PROTO:
                record["stopped"] = f"no protocol-{blink.PROTO} status from the device"
                code = 2
            elif "bitstream" in record and record["device"]["build_id"] != record["bitstream"]["build_id"]:
                record["stopped"] = "the device's build id is not the build record's"
                code = 2
            formats = {}
            for name in args.formats if not code else []:
                fmt = FORMATS[name]
                addr = getattr(args, f"region_{name}")
                image = blink.image(trits, rows, cols, fmt)
                acts = blink.act_image(chunk["x"], cols, fmt)
                entry = {"fmt": fmt, "region": addr, "image_bytes": len(image),
                         "image_sha256": hashlib.sha256(image).hexdigest(),
                         "words": rows * blink.words_per_row(cols, fmt)}
                upload = UL.transfer(loader, image, addr, args.chunk, {})
                entry["upload"] = {k: upload[k] for k in ("chunks", "acked", "identical", "first_mismatch", "load_s",
                                                          "load_payload_Bps", "readback_s", "retransmits", "attempts")}
                entry["activations"] = [mv.act(b, acts[b * blink.ACT_BLOCK_BYTES:(b + blink.ACT_FRAME_BLOCKS)
                                                     * blink.ACT_BLOCK_BYTES])
                                        for b in range(0, len(acts) // blink.ACT_BLOCK_BYTES, blink.ACT_FRAME_BLOCKS)]
                runs = []
                if upload["identical"] and all(a["acked"] for a in entry["activations"]):
                    for _ in range(args.runs):
                        run = mv.run(addr, rows, cols, fmt)
                        z = run.get("z", {})
                        run["status"] = z.get("status")
                        run["equal_reference"] = run.get("y") == want
                        run["mismatching_rows"] = [i for i, (a, b) in enumerate(zip(run.get("y", []), want)) if a != b][:16]
                        run["checksum_ok"] = run.get("complete") and blink.result_checksum(run.get("y", [])) == z.get("checksum")
                        if z.get("cycles"):
                            run["weights_per_s"] = rows * cols * args.design_hz / z["cycles"]
                            run["words_per_clock"] = z["words"] / z["cycles"]
                            run["idle_fraction"] = z["idle_clocks"] / z["cycles"]
                        run.pop("y", None)
                        runs.append(run)
                entry["runs"] = runs
                after = loader.status()
                entry["status_after"] = after
                entry["calib_held"] = UL.calib_ok(after)
                good = [r for r in runs if r["status"] == blink.Z_RAN and r["equal_reference"] and r["checksum_ok"]]
                # Runs count only when the status after them shows the calibration held (not read: excluded too).
                entry["excluded"] = ([] if entry["calib_held"] else
                                     [{"runs": "all", "reason": "the calibration had dropped by the status after them"
                                       if entry["calib_held"] is False else "no status after them showed the calibration"}])
                usable = good if entry["calib_held"] else []
                entry["weights_per_s"] = spread([r["weights_per_s"] for r in usable])
                entry["words_per_clock"] = spread([r["words_per_clock"] for r in usable])
                entry["idle_fraction"] = spread([r["idle_fraction"] for r in usable])
                entry["all_runs_exact"] = len(good) == len(runs) == args.runs
                formats[name] = entry
                if not entry["all_runs_exact"]:
                    code = max(code, 1)
            record["formats"] = formats
            if {"dense5", "baseline2"} <= formats.keys() and all(formats[f]["weights_per_s"] for f in ("dense5", "baseline2")):
                d5, b2 = formats["dense5"]["weights_per_s"]["median"], formats["baseline2"]["weights_per_s"]["median"]
                record["ratio_dense5_baseline2"] = {"of_medians": d5 / b2, "ceiling": 80 / 64}
            stalls = [r["z"].get("consumer_stalls") for f in formats.values() for r in f["runs"] if r.get("z")]
            record["memory_bound"] = bool(stalls) and all(s == 0 for s in stalls)
        finally:
            link.close()
            out_rx = args.output.with_name(args.output.name + ".rx.bin.gz")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            out_rx.write_bytes(gzip.compress(bytes(link.rx), mtime=0))
            record["rx"] = {"file": out_rx.name, "bytes": len(link.rx), "sha256": hashlib.sha256(bytes(link.rx)).hexdigest()}
    if args.identity and code != 2:
        record["identity_after"] = UL.run_identity(args, t0, "after")
    record["ended_utc"] = UL.utc()
    record["exit_status"] = code
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=1) + "\n")
    for name, entry in record.get("formats", {}).items():
        w = entry["weights_per_s"]
        print(f"{name}: {len(entry['runs'])} runs, exact {entry['all_runs_exact']}, weights/s median "
              f"{w['median'] / 1e9:.3f} G" if w else f"{name}: no usable run")
    print(f"record: {args.output} (exit {code})")
    return code


if __name__ == "__main__":
    sys.exit(main())
