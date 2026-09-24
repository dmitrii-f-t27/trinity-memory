#!/usr/bin/env python3
"""Run the Trinity arithmetic harness on a ProcessorCI board and check every result against the reference.

Uses the ProcessorCI host library (LSC-Unicamp/processor_ci_communication, class ProcessorCIInterface) with
single-word reads and writes only. For each test: write the descriptor, clear the result block, run until the
harness touches END_ADDR, read the five result words and compare the count and CRC-32 with
kits/processorci/vectors/manifest.json (computed by the reference models in kits/processorci/model/).

Status: exercised in simulation only (sim/tb_harness.v models the controller's memory). It has not yet been
run against a physical ProcessorCI controller; the protocol calls follow processor_ci_tests' runner.

Example:
    PYTHONPATH=/path/to/processor_ci_communication python3 run_arith.py --port /dev/ttyUSB1 --junit results.xml
"""
import argparse, json, sys, time
from pathlib import Path
from xml.sax.saxutils import escape

DESC_ADDR, RESULT_ADDR, END_ADDR = 0x0000, 0x0100, 0x1FFC
MAGIC, DONE = 0x54524931, 0x600D
NAMES = {1: "e4m3 decode, every code", 2: "e4m3 encode, every BF16, saturating", 3: "e4m3 encode, every BF16, NaN on overflow",
         4: "e4m3 encode, seeded binary32, saturating", 5: "e4m3 encode, seeded binary32, NaN on overflow",
         6: "ternary dense5 decode, every byte", 7: "ternary baseline5 decode, every code", 8: "ternary sparse 4:1 decode, every byte"}

def word(iface, address):
    return int.from_bytes(iface.read_memory(address), byteorder="big")

def run_test(iface, test_id, manifest):
    h = manifest["harness"]
    exp = h["tests"][str(test_id)]
    iface.write_memory(DESC_ADDR + 0, test_id)
    iface.write_memory(DESC_ADDR + 4, h["random_count"])
    iface.write_memory(DESC_ADDR + 8, int(h["random_seed"], 16))
    for k in range(5):
        iface.write_memory(RESULT_ADDR + 4 * k, 0)
    t0 = time.time()
    reply = iface.execute_until_stop(stop_address=END_ADDR)
    got = [word(iface, RESULT_ADDR + 4 * k) for k in range(5)]
    ok = (got[0] == MAGIC and got[1] == test_id and got[2] == exp["count"]
          and got[3] == int(exp["crc32"], 16) and got[4] == DONE)
    detail = (f"magic={got[0]:08x} id={got[1]} outputs={got[2]} crc32={got[3]:08x} status={got[4]:04x}; "
              f"expected outputs={exp['count']} crc32={exp['crc32']}; controller reply={reply!r}")
    return ok, detail, time.time() - t0

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", required=True)
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--timeout", type=int, default=10, help="passed to ProcessorCIInterface.set_timeout")
    ap.add_argument("--tests", default="1,2,3,4,5,6,7,8")
    ap.add_argument("--junit")
    a = ap.parse_args()
    from core.serial import ProcessorCIInterface  # from processor_ci_communication
    manifest = json.loads((Path(__file__).resolve().parents[1] / "vectors/manifest.json").read_text())
    iface = ProcessorCIInterface(a.port, a.baud)
    iface.set_timeout(a.timeout)
    results = []
    for tid in [int(x) for x in a.tests.split(",")]:
        ok, detail, secs = run_test(iface, tid, manifest)
        print(f"{'PASS' if ok else 'FAIL'} test {tid} ({NAMES[tid]}): {detail}")
        results.append((tid, ok, detail, secs))
    iface.close()
    if a.junit:
        cases = "".join(
            f'<testcase classname="trinity_arith" name="{escape(NAMES[t])}" time="{s:.3f}">'
            + ("" if ok else f'<failure message="{escape(d)}"/>') + "</testcase>" for t, ok, d, s in results)
        fails = sum(not ok for _, ok, _, _ in results)
        Path(a.junit).write_text(f'<?xml version="1.0"?><testsuite name="trinity_arith" tests="{len(results)}" failures="{fails}">{cases}</testsuite>\n')
    return 0 if all(ok for _, ok, _, _ in results) else 1

if __name__ == "__main__":
    sys.exit(main())
