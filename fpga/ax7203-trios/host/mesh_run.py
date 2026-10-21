#!/usr/bin/env python3
"""Drive or check the TriOS mesh UART front end with host/mesh_vectors.py.

  mesh_run.py sim-files OUTDIR           write steps.mem/req.mem for the testbench
  mesh_run.py sim-compare OUTDIR RESP    compare the testbench's response bytes with the vectors
  mesh_run.py serial PORT [--baud N]     run the vectors on the board, print and save the transcript

The host sends request bytes and compares response bytes with literals. It never
computes a routing or packet result.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mesh_vectors as mv  # noqa: E402


def sim_files(out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    ss = mv.steps()
    lines_steps = ["%02x\n%02x" % (0, len(mv.BOOT))]  # step 0: boot line, no request
    req = []
    for _, rq, rs in ss:
        assert len(rq) < 256 and len(rs) < 256
        lines_steps.append("%02x\n%02x" % (len(rq), len(rs)))
        req.extend(rq)
    (out / "steps.mem").write_text("\n".join(lines_steps) + "\n")
    (out / "req.mem").write_text("\n".join("%02x" % b for b in req) + "\n")
    (out / "nsteps.vh").write_text("localparam integer NSTEPS = %d;\nlocalparam integer NREQ = %d;\n" % (len(ss) + 1, len(req)))
    print("steps", len(ss) + 1, "request bytes", len(req))


def compare(got, label):
    ss = mv.steps()
    exp = [("boot line", b"", mv.BOOT)] + ss
    pos, bad, report = 0, 0, []
    for name, _, e in exp:
        g = got[pos:pos + len(e)]
        pos += len(e)
        ok = g == e
        bad += (not ok)
        report.append({"step": name, "ok": ok, "expected": e.hex(), "got": g.hex()})
    extra = got[pos:]
    if extra:
        bad += 1
        report.append({"step": "unexpected extra bytes", "ok": False, "expected": "", "got": extra.hex()})
    print("%s: %d/%d steps match, %d response bytes" % (label, len(exp) - bad, len(exp), len(got)))
    for r in report:
        if not r["ok"]:
            print("  MISMATCH", r["step"], "\n    expected", r["expected"], "\n    got     ", r["got"])
    return bad, report


def sim_compare(out, resp):
    got = bytes(int(x, 16) for x in Path(resp).read_text().split())
    bad, report = compare(got, "simulation")
    (Path(out) / "sim_result.json").write_text(json.dumps({"mismatches": bad, "steps": report}, indent=1))
    return 1 if bad else 0


def execute(link, ss):
    """Run every step on an open serial link; return the transcript (stops at the first timeout)."""
    transcript = []
    for name, rq, rs in ss:
        link.write(rq)
        buf = bytearray()
        deadline = time.monotonic() + 5.0
        while len(buf) < len(rs) and time.monotonic() < deadline:
            buf += link.read(len(rs) - len(buf))
        transcript.append({"step": name, "tx": rq.hex(), "rx": bytes(buf).hex()})
        if len(buf) < len(rs):
            print("TIMEOUT at step", name)
            break
    return transcript


def evaluate(ss, transcript, baud, out):
    bad, rows = 0, []
    for (name, _, e), t in zip(ss, transcript):
        ok = bytes.fromhex(t["rx"]) == e
        bad += (not ok)
        rows.append({**t, "expected": e.hex(), "ok": ok})
    complete = len(transcript) == len(ss)
    passed = sum(1 for r in rows if r["ok"])
    print("board: %d of %d observed steps match; %d of %d planned steps executed%s"
          % (passed, len(rows), len(transcript), len(ss), "" if complete else " (INCOMPLETE: the run stopped early)"))
    for r in rows:
        if not r["ok"]:
            print("  MISMATCH", r["step"], "\n    expected", r["expected"], "\n    got     ", r["rx"])
    Path(out).write_text(json.dumps({"baud": baud, "planned": len(ss), "executed": len(transcript), "matched": passed,
                                     "complete": complete, "transcript": rows}, indent=1))
    return 1 if bad or not complete else 0


def serial_run(port, baud, out):
    import serial
    ss = mv.steps()
    with serial.Serial(port, baud, timeout=0.2) as link:
        # Drop anything already there (a boot line from a configuration earlier than this run).
        time.sleep(0.3)
        link.reset_input_buffer()
        transcript = execute(link, ss)
    return evaluate(ss, transcript, baud, out)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("sim-files"); a.add_argument("out")
    b = sub.add_parser("sim-compare"); b.add_argument("out"); b.add_argument("resp")
    c = sub.add_parser("serial"); c.add_argument("port"); c.add_argument("--baud", type=int, default=115200); c.add_argument("--out", default="board_transcript.json")
    args = ap.parse_args()
    if args.cmd == "sim-files":
        sim_files(args.out)
    elif args.cmd == "sim-compare":
        sys.exit(sim_compare(args.out, args.resp))
    else:
        sys.exit(serial_run(args.port, args.baud, args.out))


if __name__ == "__main__":
    main()
