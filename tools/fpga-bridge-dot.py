#!/usr/bin/env python3
"""compute.dot on the AX7203 through the Bridge's fpga backend (issue #64, its board part): the
stage-1 chunk, BitNet b1.58 2B4T layer-0 q_proj rows 0-319 (2,560 columns), in dense5 and
baseline2, against the DDR3 matvec build (make -C fpga/ax7203 ddr3-bit DDR3_APP=matvec), with a
record of every call.

  python3 tools/fpga-bridge-dot.py --port /dev/cu.usbserial-110 \\
      --build-report reports/fpga/ddr3-build-<date>-<id>-x16-matvec-seed<n>/build.json \\
      --idcode 0x13636093 --dna 0x00389c0c2d85e85c --output reports/fpga/bridge-dot-<date>-<id>

What it does: trinity_chipInfo (the Bridge asks the device for its status lines and checks the
protocol, at least 4, and the build id), then per codec a TensorPack of the chunk uploaded to the
Bridge and compute.dot with t27/matvec.t27's activations (tmv_activations, seed 27): the Bridge
uploads the image to the device's DDR3 (L frames, every chunk read back and compared), sends the
activations (X), runs the matvec (M) and returns the device's 320 accumulators with the evidence
(bitstream sha256, IDCODE, DNA and build id as configured, and the sha256 and size of every byte
the device sent in the call), its transfer counters and the device's Z counters, and its own
reference check (the exact sums in process). --repeat runs each dot again (the upload cache then
skips the upload). The record (record.json) holds every result as returned, host times, the
accumulators against the chunk's reference (the first 320 of t27/matvec.t27's product of the whole
q_proj, whose sha256 over all 2,560 must equal reports/ternary-check/matvec-2026-09-23.json) and
against the emulator backend's, and for every call the capture (<call>.capture.gz: the bytes the
device sent; their sha256 must equal the evidence's capture_sha256).

The chunk comes from the fixture cache (tools/fetch-fixtures.py; tests/stage1_chunk.py). The tool
touches only the serial port given; the board must already hold the bitstream (SRAM load, e.g.
make -C fpga/ax7203 ddr3-flash ...) and nothing else may use the port while it runs. Exit status 0
when every call succeeded and every accumulator equals the reference, 1 otherwise.
"""
from __future__ import annotations

import argparse
import datetime
import gzip
import hashlib
import json
import platform
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

SCHEMA = "trinity.bridge-fpga-dot.v1"
NAME = "q_proj_rows_0_319"


def utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def revision() -> str:
    try:
        head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, check=True).stdout.strip()
        return head + ("+dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def parse(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", required=True, help="the board's serial port (the CP2102N)")
    p.add_argument("--build-report", type=Path, help="build.json of the loaded bitstream: its sha256 and commit (BUILD_ID)")
    p.add_argument("--bitstream-sha256", help="instead of --build-report: the loaded .bit's sha256")
    p.add_argument("--build-id", help="instead of --build-report: its BUILD_ID (8 hex digits)")
    p.add_argument("--idcode", required=True, help="0x and 8 hex digits (see docs/bridge.md, IDCODE)")
    p.add_argument("--dna", required=True, help="0x and 16 hex digits (openFPGALoader --read-dna)")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--region", type=lambda v: int(v, 0), default=0x0100_0000,
                   help="DDR3 byte address of the image (16-byte aligned; default 0x01000000)")
    p.add_argument("--reply-timeout", type=float, default=0.5)
    p.add_argument("--quiet", type=float, default=0.15)
    p.add_argument("--attempts", type=int, default=8)
    p.add_argument("--codecs", default="dense5,baseline2")
    p.add_argument("--repeat", type=int, default=2, help="dot calls per codec (the later ones use the upload cache)")
    p.add_argument("--output", type=Path, required=True, help="directory for record.json and the captures")
    args = p.parse_args(argv)
    if args.build_report:
        report = json.loads(args.build_report.read_text())
        args.bitstream_sha256 = args.bitstream_sha256 or report["bitstream"]["sha256"]
        args.build_id = args.build_id or report["commit"][:8]
    if not (args.bitstream_sha256 and args.build_id):
        p.error("give --build-report, or --bitstream-sha256 and --build-id")
    if args.repeat < 1:
        p.error("--repeat must be at least 1")
    args.codecs = [c for c in args.codecs.split(",") if c]
    if not args.codecs or any(c not in ("dense5", "baseline2") for c in args.codecs):
        p.error("--codecs is a list of dense5 and baseline2")
    return args


def main(argv=None) -> int:
    args = parse(argv)
    import stage1_chunk
    from trinity_memory.bridge import BridgeClient, BridgeServer, FpgaDevice, check_identity
    from trinity_memory.tensorpack import Tensor, encode_tensors

    chunk = stage1_chunk.chunk()
    if chunk["full_sha256"] != chunk["report_sha256"]:
        raise SystemExit("the chunk's reference does not match reports/ternary-check/matvec-2026-09-23.json")
    rows, cols = stage1_chunk.ROWS, stage1_chunk.COLS
    want = list(chunk["y"])
    args.output.mkdir(parents=True, exist_ok=True)
    device = FpgaDevice(port=args.port, bitstream_sha256=args.bitstream_sha256, idcode=args.idcode, dna=args.dna,
                        build_id=args.build_id, baud=args.baud, reply_timeout=args.reply_timeout, quiet=args.quiet,
                        attempts=args.attempts, region=args.region)
    record = {"schema": SCHEMA, "argv": sys.argv if argv is None else ["tools/fpga-bridge-dot.py", *argv],
              "tool_revision": revision(), "host": {"platform": platform.platform(), "python": platform.python_version()},
              "device": {"port": args.port, "baud": args.baud, "region": args.region,
                         "evidence": {"bitstream_sha256": args.bitstream_sha256, "idcode": args.idcode,
                                      "dna": args.dna, "build_id": args.build_id}},
              "chunk": {"tensor": stage1_chunk.TENSOR, "rows": [0, rows], "cols": cols,
                        "activations": f"tmv_activations seed {stage1_chunk.SEED} (t27/matvec.t27)",
                        "reference": "first 320 accumulators of t27/matvec.t27's q_proj product",
                        "reference_full_sha256": chunk["full_sha256"], "reference_first8": want[:8]},
              "calls": [], "started_utc": utc()}
    ok = True

    def keep(call, server, result=None, error=None, started=None, ended=None, extra=None):
        nonlocal ok
        capture = server.last_capture()
        name = f"{len(record['calls']):02d}-{call}"
        (args.output / f"{name}.capture.gz").write_bytes(gzip.compress(capture, mtime=0))
        entry = {"call": call, "name": name, "started_utc": started, "ended_utc": ended,
                 "capture": {"file": f"{name}.capture.gz", "bytes": len(capture),
                             "sha256": hashlib.sha256(capture).hexdigest()}}
        if result is not None:
            entry["result"] = result
            evidence = result.get("evidence", {})
            entry["capture"]["equals_evidence"] = (evidence.get("capture_sha256") == entry["capture"]["sha256"]
                                                   and evidence.get("capture_bytes") == len(capture))
            ok = ok and entry["capture"]["equals_evidence"]
        if error is not None:
            entry["error"] = error
            ok = False
        entry.update(extra or {})
        record["calls"].append(entry)
        return entry

    with BridgeServer(backend="fpga", device=device, max_trits=1_000_000) as server:
        client = BridgeClient(server.url, timeout=600)
        started = utc()
        try:
            info = client.call("trinity_chipInfo")
            check_identity(info)
            keep("chip_info", server, result=info, started=started, ended=utc())
        except Exception as error:  # noqa: BLE001 - recorded, then the run stops
            keep("chip_info", server, error=f"{type(error).__name__}: {error}", started=started, ended=utc())
            (args.output / "record.json").write_text(json.dumps(record, indent=1) + "\n")
            print(f"chip_info failed: {error}", file=sys.stderr)
            return 1
        emulator = {}
        for codec in args.codecs:
            data = encode_tensors([Tensor(NAME, (rows, cols), tuple(chunk["trits"]), codec=codec)])
            with BridgeServer(max_trits=1_000_000) as reference_server:
                ref = BridgeClient(reference_server.url, timeout=600)
                emulator[codec] = ref.dot(ref.upload(data), NAME, chunk["x"])["accumulators"]
            handle = client.upload(data)
            for attempt in range(args.repeat):
                started, t0 = utc(), time.monotonic()
                try:
                    result = client.dot(handle, NAME, chunk["x"])
                except Exception as error:  # noqa: BLE001 - recorded, the next call goes on
                    keep(f"dot-{codec}", server, error=f"{type(error).__name__}: {error}", started=started, ended=utc())
                    continue
                seconds = time.monotonic() - t0
                acc = result["accumulators"]
                equal_ref, equal_emu = acc == want, acc == emulator[codec]
                ok = ok and equal_ref and equal_emu and result.get("hardware") is True
                entry = keep(f"dot-{codec}", server, result=result, started=started, ended=utc(),
                             extra={"codec": codec, "repeat": attempt, "host_seconds": round(seconds, 3),
                                    "accumulators_equal_reference": equal_ref,
                                    "accumulators_equal_emulator": equal_emu,
                                    "mismatching_rows": [i for i, (a, b) in enumerate(zip(acc, want)) if a != b][:32]})
                print(f"{codec} #{attempt}: {len(acc)} accumulators, reference {'equal' if equal_ref else 'DIFFERENT'}, "
                      f"emulator {'equal' if equal_emu else 'DIFFERENT'}, {seconds:.1f} s, capture "
                      f"{entry['capture']['bytes']} bytes")
    record["ended_utc"] = utc()
    record["all_equal"] = ok
    (args.output / "record.json").write_text(json.dumps(record, indent=1) + "\n")
    print(f"record: {args.output / 'record.json'} ({'all equal' if ok else 'NOT all equal'})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
