#!/usr/bin/env python3
"""Migration differential tests: native t27 RTL against unchanged v0.2 fixtures.

This Python file is test orchestration, not the new hardware implementation.
It reuses the preserved legacy software oracle and drives actual Icarus. It is
explicitly retained migration infrastructure until a native test runner exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.test_dot_rtl import overflow_suite, protocol_suite
from scripts.test_rtl import expected_tables


def execute(command: list[str], cwd: Path) -> str:
    process = subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=180)
    if process.returncode:
        raise RuntimeError(f"command failed ({process.returncode}): {command}\n"
                           f"{process.stdout}\n{process.stderr}")
    return process.stdout


def run_checks(generated: Path, compiler: str | None = None) -> dict:
    for tool in ("iverilog", "vvp"):
        if not shutil.which(tool):
            raise RuntimeError(f"Required actual RTL simulator is missing: {tool}")
    generated = generated.resolve()
    generated.mkdir(parents=True, exist_ok=True)
    specs = sorted((ROOT / "t27/rtl").glob("*.t27"))
    spec_test_logs = {}
    with tempfile.TemporaryDirectory(prefix="trinity-native-t27-rtl-") as temporary:
        work = Path(temporary)
        for spec in specs:
            output = generated / f"{spec.stem}.v"
            if compiler:
                source = execute([compiler, "gen-verilog", str(spec)], work)
                if "ENTRY POINT REFUSED" in source or "NO DATA PORTS" in source:
                    raise RuntimeError(f"t27 compiler refused a data entry point: {spec}")
                output.write_text(source, encoding="ascii")
                spec_test_logs[spec.name] = execute([compiler, "icarus-simulate", str(spec)], work)
            elif not output.is_file():
                raise RuntimeError(f"Missing generated RTL: {output}; generate from {spec}")

        native_decoders = [generated / f"{name}_decoder.v" for name in ("dense5", "baseline5", "sparse41")]
        for name, values in expected_tables().items():
            (work / f"{name}_expected.mem").write_text(
                "".join(f"{value:03x}\n" for value in values), encoding="ascii")
        decoder_tb = (ROOT / "rtl/tb/tb_decoders.sv").read_text(encoding="ascii")
        for name in ("dense5", "baseline5", "sparse41"):
            decoder_tb = decoder_tb.replace(f"ternary_{name}_decoder", f"ternary_{name}_decoder_t27")
        (work / "tb_decoders.sv").write_text(decoder_tb, encoding="ascii")
        execute(["iverilog", "-g2012", "-s", "tb_decoders", "-o", str(work / "decoders.vvp"),
                 *map(str, native_decoders), str(ROOT / "rtl/t27/decoders.v"),
                 str(work / "tb_decoders.sv")], work)
        decoder_log = execute(["vvp", str(work / "decoders.vvp")], work)

        # Preserve every storage assertion; only select the native adapters.
        stream_tb = (ROOT / "rtl/tb/tb_streams.sv").read_text(encoding="ascii")
        for name in ("dense5", "baseline5"):
            stream_tb = stream_tb.replace(f"ternary_{name}_stream", f"ternary_{name}_stream_t27")
        # Reuse the checker verbatim across every supported logical size, which
        # also exercises all five possible final lane counts and address widths.
        stream_tb = stream_tb[:stream_tb.index("module tb_streams;")] + "\n".join([
            "module tb_streams;", "wire [319:0] done;",
            *(f"stream_checker #(.TRIT_COUNT({count})) check{count} (.done(done[{count - 1}]));"
              for count in range(1, 321)),
            "initial begin wait (&done); $display(\"PASS all 320 supported stream sizes\"); $finish; end",
            "initial begin #100000; $fatal(1, \"stream test timeout\"); end",
            "endmodule", "`default_nettype wire", ""])
        (work / "tb_streams.sv").write_text(stream_tb, encoding="ascii")
        execute(["iverilog", "-g2012", "-s", "tb_streams", "-o", str(work / "streams.vvp"),
                 str(generated / "stream_storage.v"), str(generated / "stream_view.v"),
                 str(ROOT / "rtl/t27/streams.v"), str(work / "tb_streams.sv")], work)
        stream_log = execute(["vvp", str(work / "streams.vvp")], work)

        # The existing API expects four filenames. Supply only native generated
        # code and the wiring adapter; no legacy decoder/datapath is compiled.
        resource = work / "resources"
        (resource / "generated").mkdir(parents=True)
        (resource / "trinity_dot_stream.v").write_text(
            (generated / "dot_stream.v").read_text(encoding="ascii") + "\n" +
            (ROOT / "rtl/t27/dot_stream.v").read_text(encoding="ascii"), encoding="ascii")
        (resource / "tb_dot_stream.v").write_text(
            (ROOT / "rtl/tb_dot_stream.v").read_text(encoding="ascii")
            .replace("trinity_dot_stream #(", "trinity_dot_stream_t27 #("), encoding="ascii")
        shutil.copyfile(generated / "dense5_decoder.v", resource / "generated/ternary_dense5_decoder.v")
        shutil.copyfile(generated / "baseline5_decoder.v", resource / "ternary_baseline5_decoder.v")
        previous_root = os.environ.get("TRINITY_MEMORY_RTL_ROOT")
        os.environ["TRINITY_MEMORY_RTL_ROOT"] = str(resource)
        try:
            suites = [suite(codec) for codec in ("dense5", "baseline2")
                      for suite in (protocol_suite, overflow_suite)]
        finally:
            if previous_root is None:
                os.environ.pop("TRINITY_MEMORY_RTL_ROOT", None)
            else:
                os.environ["TRINITY_MEMORY_RTL_ROOT"] = previous_root

        limit_logs = []
        for dense in (0, 1):
            executable = work / f"limits{dense}.vvp"
            execute(["iverilog", "-g2012", "-s", "tb_t27_dot_limits",
                     f"-Ptb_t27_dot_limits.DENSE5={dense}", "-o", str(executable),
                     str(generated / "dot_stream.v"), str(ROOT / "rtl/t27/dot_stream.v"),
                     str(ROOT / "tests/t27_rtl_dot_limits.sv")], work)
            limit_logs.append(execute(["vvp", str(executable)], work))

        rejected = []
        for width in (1, 33):
            testbench = work / f"invalid_width{width}.sv"
            testbench.write_text("module tb_invalid;\n"
                f"trinity_dot_stream_t27 #(.ACC_WIDTH({width})) dut ();\n"
                "initial begin #1; $fatal(1, \"invalid parameter was accepted\"); end\n"
                "endmodule\n", encoding="ascii")
            executable = work / f"invalid_width{width}.vvp"
            execute(["iverilog", "-g2012", "-s", "tb_invalid", "-o", str(executable),
                     str(generated / "dot_stream.v"), str(ROOT / "rtl/t27/dot_stream.v"),
                     str(testbench)], work)
            result = subprocess.run(["vvp", str(executable)], cwd=work, text=True, capture_output=True)
            if result.returncode == 0 or "supports ACC_WIDTH=2..32 only" not in result.stdout:
                raise RuntimeError(f"Unsupported ACC_WIDTH={width} was not explicitly rejected")
            rejected.append({"ACC_WIDTH": width, "log": result.stdout})

        for name, parameters, expected in [
            ("ternary_stream_storage_t27", ".WORDS(0)", "requires CODE_WIDTH=8/10"),
            ("ternary_stream_storage_t27", ".WORDS(65)", "requires CODE_WIDTH=8/10"),
            ("ternary_stream_storage_t27", ".CODE_WIDTH(7)", "requires CODE_WIDTH=8/10"),
            ("ternary_stream_storage_t27", ".WORDS(8), .ADDR_WIDTH(2)", "requires CODE_WIDTH=8/10"),
            ("ternary_dense5_stream_t27", ".TRIT_COUNT(0)", "require TRIT_COUNT=1..320"),
            ("ternary_dense5_stream_t27", ".TRIT_COUNT(321)", "require TRIT_COUNT=1..320"),
            ("ternary_baseline5_stream_t27", ".TRIT_COUNT(12), .WORDS(2)", "require TRIT_COUNT=1..320"),
        ]:
            testbench = work / "invalid_storage.sv"
            testbench.write_text("module tb_invalid;\n"
                f"{name} #({parameters}) dut ();\n"
                "initial begin #1; $fatal(1, \"invalid parameter was accepted\"); end\n"
                "endmodule\n", encoding="ascii")
            executable = work / "invalid_storage.vvp"
            execute(["iverilog", "-g2012", "-s", "tb_invalid", "-o", str(executable),
                     str(generated / "stream_storage.v"), str(generated / "stream_view.v"),
                     str(ROOT / "rtl/t27/streams.v"), str(testbench)], work)
            result = subprocess.run(["vvp", str(executable)], cwd=work, text=True, capture_output=True)
            if result.returncode == 0 or expected not in result.stdout:
                raise RuntimeError(f"Unsupported {name} {parameters} was not explicitly rejected")
            rejected.append({"module": name, "parameters": parameters, "log": result.stdout})

    return {
        "evidence": "native-t27-generated-rtl-simulation",
        "legacy_rtl_compiled": False,
        "legacy_test_fixtures_reused": True,
        "spec_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in specs},
        "generated_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in sorted(generated.glob("*.v"))},
        "embedded_spec_tests": spec_test_logs,
        "decoder_log": decoder_log,
        "stream_log": stream_log,
        "storage_trit_counts_checked": list(range(1, 321)),
        "protocol_suites": suites,
        "five_bit_logs": limit_logs,
        "unsupported_parameter_rejections": rejected,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", "--t27c", dest="t27c",
                        default=os.environ.get("T27C") or os.environ.get("TRINITY_T27C"),
                        help="Generate RTL and run embedded spec tests using this compiler")
    parser.add_argument("--generated-dir", type=Path, default=ROOT / "build/t27/rtl")
    parser.add_argument("--output", type=Path, default=ROOT / "build/t27/rtl-validation.json")
    args = parser.parse_args()
    try:
        report = run_checks(args.generated_dir, args.t27c)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(report["decoder_log"].strip())
    print("PASS native t27 storage: all TRIT_COUNT=1..320, unchanged stream checker")
    for log in report["five_bit_logs"]:
        print(log.strip())
    print("PASS native t27 dot: unchanged 32/12-bit scoreboards, 5-bit limits, X rejection")
    print(f"Evidence: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
