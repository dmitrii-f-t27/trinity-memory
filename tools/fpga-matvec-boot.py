"""Hash-checked SRAM load and retained boot evidence for the AX7203 matvec."""
import argparse
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import matvec_device_model as model

spec = importlib.util.spec_from_file_location("ddr_capture", ROOT / "tools/fpga-ddr3-capture.py")
capture = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = capture
spec.loader.exec_module(capture)
parser = argparse.ArgumentParser()
parser.add_argument("--report", type=Path, required=True)
parser.add_argument("--bit", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--cols", type=int, default=2560)
parser.add_argument("--rows", type=int, default=320)
parser.add_argument("--fmt", type=int, default=1)
parser.add_argument("--cap", type=int, default=4)
parser.add_argument("--port", default="/dev/cu.usbserial-10")
parser.add_argument("--cable", default="digilent_hs2")
parser.add_argument("--seconds", type=float, default=15.0)
parser.add_argument("--max-temp", type=float, default=80.0)
parser.add_argument("--ffn", action="store_true", help="check the full-layer FFN ready header instead of matvec")
args = parser.parse_args()
if args.output.exists():
    parser.error("output already exists; use a fresh directory to preserve earlier evidence")
report = json.loads(args.report.read_text())
if report["bitstream"]["sha256"] != hashlib.sha256(args.bit.read_bytes()).hexdigest():
    parser.error("bitstream hash differs from the build report")
if not report["nextpnr"]["clocks_routed"] or any(
        c["verdict"] != "PASS" for c in report["nextpnr"]["clocks_routed"]):
    parser.error("the build report does not confirm routed timing")
if args.ffn and 'FFN (' not in report['ddr3']['variant']:
    parser.error('--ffn requires a declared FFN build')
os.environ["DDR3_APP"] = "ffn" if args.ffn else "matvec"
run_args = SimpleNamespace(loader="openFPGALoader", cable=args.cable,
    port=args.port, baud=115200, max_temp=args.max_temp,
    settle=1.0, seconds=args.seconds, no_load=False, bit=str(args.bit.resolve()))
run, status = capture.board_run(run_args, capture.expectations(report), args.report)
raw = run.pop("uart_raw", b"")
args.output.mkdir(parents=True, exist_ok=True)
(args.output / "uart-raw.txt").write_bytes(raw)
lines = []
for entry in run.get("uart_entries", []):
    text = entry["line"]
    if len(text) == 19:
        try:
            lines.append((text[0], int(text[1:9], 16), int(text[9:19], 16)))
        except ValueError:
            pass
heads = [line for line in lines if line[0] in "qkv"]
hello = [line for line in lines if line[0] == "H"]
build_id = report["ddr3"]["netlist_build_id"]
build_id = int(build_id, 16) if isinstance(build_id, str) else build_id
checks = {"board_steps": status == 0,
          "hello_build_id": bool(hello) and hello[-1][1] == build_id,
          "matvec_header": heads == model.head_lines(args.cols, args.rows, args.fmt, args.cap, 8192)}
if args.ffn:
    checks.pop('matvec_header')
    checks['ffn_header'] = [line for line in lines if line[0] == 'F'] == [('F', 2560, 6912)]
record = {"build_report": os.path.relpath(args.report.resolve(), ROOT),
          "bitstream_sha256": report["bitstream"]["sha256"], "checks": checks,
          "uart_sha256": hashlib.sha256(raw).hexdigest(), "lines": lines, "run": run}
(args.output / "boot.json").write_text(json.dumps(record, indent=1) + "\n")
print(json.dumps({"checks": checks, "lines": lines, "status": status,
                  "temperature": run.get("xadc_after")}, indent=1))
raise SystemExit(0 if all(checks.values()) else 1)
