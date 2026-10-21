#!/usr/bin/env python3
"""Single-executor SRAM load of the TriOS mesh bitstream and the UART vectors, with guards.

  board_run.py --bit trios_soc_ax7203.bit --expect-sha256 HEX --ftdi-serial S --port /dev/cu.usbserial-N --out DIR

Order: lock (exclusive file), nobody holds the UART, IDCODE, DNA and die temperature before,
SRAM load (reversible: configuration is lost at power-off; flash is never touched), boot line,
the vectors of mesh_vectors.py, temperature after, lock released. The raw log (with the board
serial and DNA) goes to DIR/private/, the summary DIR/board_summary.json carries no identifiers.
Needs pyserial and openFPGALoader. Run this from one place only.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mesh_run  # noqa: E402
import mesh_vectors as mv  # noqa: E402

LOCK = Path("/private/tmp/claude-501/ax7203-board.lock")


def run(cmd, log, timeout=120):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    log.append({"cmd": cmd, "rc": p.returncode, "out": (p.stdout + p.stderr).strip().splitlines()})
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", required=True)
    ap.add_argument("--expect-sha256", required=True)
    ap.add_argument("--ftdi-serial", required=True)
    ap.add_argument("--port", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cable", default="digilent_hs2")
    ap.add_argument("--max-temp", type=float, default=70.0)
    ap.add_argument("--baud", type=int, default=115200)
    a = ap.parse_args()
    out = Path(a.out)
    (out / "private").mkdir(parents=True, exist_ok=True)
    log, summary = [], {"bit_sha256": None, "steps": None}

    sha = hashlib.sha256(Path(a.bit).read_bytes()).hexdigest()
    summary["bit_sha256_whole_file"] = sha
    if sha != a.expect_sha256:
        sys.exit("bitstream sha256 %s is not the verified artifact %s" % (sha, a.expect_sha256))
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        sys.exit("board lock %s exists: another executor owns the board" % LOCK)
    os.write(fd, ("trios-mesh board_run pid %d\n" % os.getpid()).encode()); os.close(fd)
    code = 1
    try:
        holders = subprocess.run(["lsof", a.port], capture_output=True, text=True).stdout.strip()
        if holders:
            sys.exit("UART %s is held by:\n%s" % (a.port, holders))
        loader = ["openFPGALoader", "-c", a.cable, "--ftdi-serial", a.ftdi_serial]
        p = run(loader + ["--detect"], log)
        m = re.search(r"idcode\s+(0x[0-9a-fA-F]+)", p.stdout + p.stderr)
        summary["idcode"] = m.group(1) if m else None
        if p.returncode or summary["idcode"] not in ("0x3636093", "0x13636093"):
            sys.exit("unexpected JTAG chain: %s" % summary["idcode"])
        run(loader + ["--read-dna"], log)
        x = run(loader + ["--read-xadc"], log)
        mt = re.search(r"['\"]?temp['\"]?\s*:\s*([0-9.]+)", x.stdout + x.stderr)
        summary["temp_before_c"] = float(mt.group(1)) if mt else None
        if summary["temp_before_c"] is not None and summary["temp_before_c"] >= a.max_temp:
            sys.exit("die at %.1f C before the load" % summary["temp_before_c"])
        import serial
        with serial.Serial(a.port, a.baud, timeout=0.2) as link:
            link.reset_input_buffer()
            load = run(loader + [a.bit], log, timeout=180)   # SRAM load only
            summary["load_rc"] = load.returncode
            if load.returncode:
                sys.exit("loader failed: %s" % load.stdout[-500:] + load.stderr[-500:])
            boot = bytearray()
            end = time.monotonic() + 5
            while time.monotonic() < end and not boot.endswith(b"\n"):
                boot += link.read(64)
            summary["boot_line"] = boot.decode("ascii", "replace").strip()
            summary["boot_line_ok"] = bytes(boot).endswith(mv.BOOT) or bytes(boot) == mv.BOOT
            ss = mv.steps()
            transcript = mesh_run.execute(link, ss)
        code = mesh_run.evaluate(ss, transcript, a.baud, str(out / "board_transcript.json"))
        summary["steps"] = {"planned": len(ss), "executed": len(transcript)}
        x2 = run(loader + ["--read-xadc"], log)
        mt = re.search(r"['\"]?temp['\"]?\s*:\s*([0-9.]+)", x2.stdout + x2.stderr)
        summary["temp_after_c"] = float(mt.group(1)) if mt else None
        summary["persistence"] = "SRAM only: configuration is lost at power-off; flash not touched"
        if not summary["boot_line_ok"]:
            code = 1
    finally:
        (out / "private" / "loader_log.json").write_text(json.dumps(log, indent=1))
        summary["result"] = "pass" if code == 0 else "fail"
        (out / "board_summary.json").write_text(json.dumps(summary, indent=1))
        print(json.dumps(summary, indent=1))
        LOCK.unlink(missing_ok=True)
    sys.exit(code)


if __name__ == "__main__":
    main()
