#!/usr/bin/env python3
"""Single-executor SRAM load of the TriOS mesh bitstream and the UART vectors, with fail-closed guards.

  board_run.py --bit B --expect-sha256 HEX --ftdi-serial S --expect-dna-file F --port P --out DIR

Order: bitstream sha256 against the verified artifact, exclusive lock file, nobody holds the UART,
then the PREFLIGHT, each item of which must succeed and parse or nothing is loaded:
  - openFPGALoader --detect returns 0 and the IDCODE is the XC7A200T's (0x3636093 / 0x13636093);
  - --read-dna returns 0, parses, and equals the board DNA read from a private file (never in the repo);
  - --read-xadc returns 0 and yields a finite plausible temperature, recorded as temp_before_c.
Then the SRAM load (reversible: the configuration is lost at power-off; flash is never touched), the
boot line, the vectors, and a second XADC read: the die must be at or below --max-temp-after (a declared
test limit, not a vendor rating) after at most --cool-wait seconds of polling, else result "fail" with an
explicit thermal reason. temp_before_c is recorded; --max-temp-before is a declared emergency bound for
the reading taken before loading (the load replaces whatever design is running and lowers dissipation).
The raw log (board serial, DNA) goes to DIR/private/; DIR/board_summary.json carries no identifiers.
Needs pyserial and openFPGALoader. Run from one place only.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board_parse as bp  # noqa: E402
import mesh_run  # noqa: E402
import mesh_vectors as mv  # noqa: E402

LOCK = Path("/private/tmp/claude-501/ax7203-board.lock")
IDCODES = ("0x3636093", "0x13636093")


class Stop(Exception):
    pass


def run(cmd, log, timeout=120):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    text = p.stdout + p.stderr
    log.append({"cmd": cmd, "rc": p.returncode, "out": text.strip().splitlines()})
    return p.returncode, text


def read_xadc(loader, log):
    rc, text = run(loader + ["--read-xadc"], log)
    if rc != 0:
        raise Stop("--read-xadc failed (rc %d)" % rc)
    try:
        return bp.parse_xadc(text)
    except ValueError as e:
        raise Stop("XADC not readable: %s" % e)


def preflight(a, loader, log, summary):
    rc, text = run(loader + ["--detect"], log)
    if rc != 0:
        raise Stop("--detect failed (rc %d)" % rc)
    try:
        idc = bp.parse_idcode(text)
    except ValueError as e:
        raise Stop(str(e))
    summary["idcode"] = idc
    if idc not in IDCODES:
        raise Stop("unexpected IDCODE %s" % idc)
    rc, text = run(loader + ["--read-dna"], log)
    if rc != 0:
        raise Stop("--read-dna failed (rc %d)" % rc)
    try:
        dna = bp.parse_dna(text)
        expected = bp.parse_dna('{"dna": "%s"}' % Path(a.expect_dna_file).read_text().strip())
    except (ValueError, OSError) as e:
        raise Stop("DNA not verifiable: %s" % e)
    if dna != expected:
        raise Stop("board DNA does not match the expected board")
    summary["dna_matches_expected_board"] = True
    x = read_xadc(loader, log)
    summary["temp_before_c"] = x["temp"]
    if x["temp"] >= a.max_temp_before:
        raise Stop("die at %.1f C before the load (emergency bound %.1f C)" % (x["temp"], a.max_temp_before))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", required=True)
    ap.add_argument("--expect-sha256", required=True)
    ap.add_argument("--ftdi-serial", required=True)
    ap.add_argument("--expect-dna-file", required=True, help="private file with the board DNA (0x...)")
    ap.add_argument("--port", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cable", default="digilent_hs2")
    ap.add_argument("--max-temp-before", type=float, default=100.0, help="declared emergency bound for the pre-load reading, C")
    ap.add_argument("--max-temp-after", type=float, default=85.0, help="declared test limit after the run, C")
    ap.add_argument("--cool-wait", type=float, default=90.0, help="seconds to poll for the die to reach the after limit")
    ap.add_argument("--baud", type=int, default=115200)
    a = ap.parse_args()
    out = Path(a.out)
    (out / "private").mkdir(parents=True, exist_ok=True)
    log, summary = [], {"result": "not run"}
    code = 1
    locked = False
    try:
        sha = hashlib.sha256(Path(a.bit).read_bytes()).hexdigest()
        summary["bit_sha256_whole_file"] = sha
        if sha != a.expect_sha256:
            raise Stop("bitstream sha256 %s is not the verified artifact %s" % (sha, a.expect_sha256))
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise Stop("board lock %s exists: another executor owns the board" % LOCK)
        os.write(fd, ("trios-mesh board_run pid %d\n" % os.getpid()).encode()); os.close(fd); locked = True
        if not Path(a.port).exists():
            raise Stop("UART %s does not exist" % a.port)
        holders = subprocess.run(["lsof", a.port], capture_output=True, text=True).stdout.strip()
        if holders:
            raise Stop("UART %s is held by another process" % a.port)
        loader = ["openFPGALoader", "-c", a.cable, "--ftdi-serial", a.ftdi_serial]
        preflight(a, loader, log, summary)
        import serial
        with serial.Serial(a.port, a.baud, timeout=0.2) as link:
            link.reset_input_buffer()
            rc, text = run(loader + [a.bit], log, timeout=180)   # SRAM load only
            summary["load_rc"] = rc
            if rc != 0:
                raise Stop("loader failed (rc %d)" % rc)
            boot = bytearray()
            end = time.monotonic() + 5
            while time.monotonic() < end and not boot.endswith(b"\n"):
                boot += link.read(64)
            summary["boot_line"] = boot.decode("ascii", "replace").strip()
            summary["boot_line_ok"] = bytes(boot) == mv.BOOT
            ss = mv.steps()
            transcript = mesh_run.execute(link, ss)
        code = mesh_run.evaluate(ss, transcript, a.baud, str(out / "board_transcript.json"))
        summary["steps"] = {"planned": len(ss), "executed": len(transcript)}
        if not summary["boot_line_ok"]:
            code = 1
        # after-run temperature: a real value is required; poll while it cools
        deadline = time.monotonic() + a.cool_wait
        temps = []
        while True:
            t = read_xadc(loader, log)["temp"]
            temps.append(t)
            if t <= a.max_temp_after or time.monotonic() >= deadline:
                break
            time.sleep(5)
        summary["temp_after_c"] = temps[-1]
        summary["temp_samples_after_c"] = temps
        summary["max_temp_after_limit_c"] = a.max_temp_after
        if temps[-1] > a.max_temp_after:
            summary["thermal"] = "limit exceeded: %.1f C > declared test limit %.1f C after %.0f s" % (temps[-1], a.max_temp_after, a.cool_wait)
            code = 1
        else:
            summary["thermal"] = "within the declared test limit"
        summary["persistence"] = "SRAM only: configuration is lost at power-off; flash not touched"
    except Stop as e:
        summary["stopped_before_or_during"] = str(e)
        code = 1
    finally:
        (out / "private" / "loader_log.json").write_text(json.dumps(log, indent=1))
        summary["result"] = "pass" if code == 0 else "fail"
        (out / "board_summary.json").write_text(json.dumps(summary, indent=1))
        print(json.dumps(summary, indent=1))
        if locked:
            LOCK.unlink(missing_ok=True)
    sys.exit(code)


if __name__ == "__main__":
    main()
