#!/usr/bin/env python3
"""Replay the cycle traces of conformance/memory_stream_compute.json in Icarus.

Each `dot_trace` vector drives trinity_dot_stream_t27 and each `storage_trace`
vector drives the native storage stream through the trace testbenches in
tests/, comparing every listed output on every cycle. The generated RTL
directory must contain dot_stream.v, stream_storage.v and stream_view.v from
the pinned compiler (build/t27/rtl by default); the wiring adapters come from
rtl/t27/. Missing tools or a mismatch fail loudly; nothing is simulated in
software instead.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT_PATH = ROOT / "conformance" / "memory_stream_compute.json"


class TraceFailure(AssertionError):
    pass


def load_document():
    return json.loads(DOCUMENT_PATH.read_text(encoding="utf-8"))


def require_tools():
    missing = [tool for tool in ("iverilog", "vvp") if not shutil.which(tool)]
    if missing:
        raise TraceFailure(f"required simulator tools missing: {missing}")


def pack_activations(values):
    word = 0
    for lane, value in enumerate(values):
        word |= (value & 255) << (8 * lane)
    return word


def dot_stimulus_word(step):
    stimulus = step["stimulus"]
    word = stimulus["in_code"] & 0x3ff
    word |= pack_activations(stimulus["in_activations"]) << 10
    word |= (stimulus["in_mask"] & 31) << 50
    word |= (1 if stimulus["in_last"] else 0) << 55
    word |= (1 if stimulus["reset"] else 0) << 56
    word |= (1 if stimulus["in_valid"] else 0) << 57
    word |= (1 if stimulus["out_ready"] else 0) << 58
    return word


def dot_expected_word(step):
    expect = step["expect"]
    word = expect["out_result"] & 0xffffffff
    word |= (1 if expect["out_error"] else 0) << 32
    word |= (1 if expect["out_valid"] else 0) << 33
    word |= (1 if expect["in_ready"] else 0) << 34
    return word


def storage_stimulus_word(step):
    stimulus = step["stimulus"]
    word = 1 if stimulus["rst"] else 0
    word |= (1 if stimulus["start"] else 0) << 1
    word |= (1 if stimulus["load_en"] else 0) << 2
    word |= (stimulus["load_addr"] & 63) << 3
    word |= (stimulus["load_code"] & 0x3ff) << 9
    word |= (1 if stimulus.get("out_stall") else 0) << 19
    return word


def join_stimulus_word(step):
    """Joined path (issue #15): [0] rst, [1] start, [2] load_en, [8:3] load_addr, [18:9] load_code,
    [19] act_valid, [59:20] act_data (five int8 lanes), [60] out_ready."""
    stimulus = step["stimulus"]
    word = 1 if stimulus["rst"] else 0
    word |= (1 if stimulus["start"] else 0) << 1
    word |= (1 if stimulus["load_en"] else 0) << 2
    word |= (stimulus["load_addr"] & 63) << 3
    word |= (stimulus["load_code"] & 0x3ff) << 9
    word |= (1 if stimulus["act_valid"] else 0) << 19
    word |= pack_activations(stimulus["act_data"]) << 20
    word |= (1 if stimulus["out_ready"] else 0) << 60
    return word


def join_expected_word(step):
    """[31:0] result, [32] out_error, [33] out_valid, [34] act_ready, [35] load_ready, [36] busy, [37] beat."""
    expect = step["expect"]
    word = expect["out_result"] & 0xffffffff
    word |= (1 if expect["out_error"] else 0) << 32
    word |= (1 if expect["out_valid"] else 0) << 33
    word |= (1 if expect["act_ready"] else 0) << 34
    word |= (1 if expect["load_ready"] else 0) << 35
    word |= (1 if expect["busy"] else 0) << 36
    word |= (1 if expect["beat"] else 0) << 37
    return word


def storage_expected_word(step):
    expect = step["expect"]
    word = expect["out_trits"] & 0x3ff
    word |= (expect["out_lane_mask"] & 31) << 10
    word |= (1 if expect["out_code_valid"] else 0) << 15
    word |= (1 if expect["out_last"] else 0) << 16
    word |= (1 if expect["out_valid"] else 0) << 17
    word |= (1 if expect["busy"] else 0) << 18
    word |= (1 if expect["load_ready"] else 0) << 19
    return word


def write_mem(path, words):
    path.write_text("".join(f"{word:016x}\n" for word in words), encoding="ascii")


def simulate(work, name, sources, top, parameters, stimulus, expected):
    work = Path(work).resolve()
    executable = work / f"{name}.vvp"
    command = ["iverilog", "-g2012", "-s", top, "-o", str(executable)]
    for key, value in parameters.items():
        command.append(f"-P{top}.{key}={value}")
    command += [str(source) for source in sources]
    compiled = subprocess.run(command, capture_output=True, text=True, timeout=300)
    if compiled.returncode:
        raise TraceFailure(f"{name}: iverilog failed\n{compiled.stdout}\n{compiled.stderr}")
    result = subprocess.run(["vvp", str(executable), f"+stimulus={stimulus.name}", f"+expected={expected.name}"],
                            capture_output=True, text=True, timeout=300, cwd=work)
    if result.returncode or "TRACE_PASS" not in result.stdout:
        raise TraceFailure(f"{name}: simulation mismatch\n{result.stdout}\n{result.stderr}")
    return result.stdout


def replay(document, rtl_dir, work=None, only=None):
    """Return (dot traces run, storage traces run, join traces run, total cycles)."""
    require_tools()
    rtl_dir = Path(rtl_dir).resolve()
    for name in ("dot_stream.v", "stream_storage.v", "stream_view.v", "stream_join.v"):
        if not (rtl_dir / name).is_file():
            raise TraceFailure(f"generated RTL missing: {rtl_dir / name}")
    dot_sources = [rtl_dir / "dot_stream.v", ROOT / "rtl/t27/dot_stream.v", ROOT / "tests/tb_spec_dot_trace.v"]
    storage_sources = [rtl_dir / "stream_storage.v", rtl_dir / "stream_view.v", ROOT / "rtl/t27/streams.v",
                       ROOT / "tests/tb_spec_storage_trace.v"]
    join_sources = [rtl_dir / "stream_storage.v", rtl_dir / "stream_view.v", rtl_dir / "stream_join.v",
                    rtl_dir / "dot_stream.v", ROOT / "rtl/t27/streams.v", ROOT / "rtl/t27/dot_stream.v",
                    ROOT / "rtl/t27/stream_dot.v", ROOT / "tests/tb_spec_join_trace.v"]
    dots, storages, joins, cycles = 0, 0, 0, 0
    with tempfile.TemporaryDirectory(prefix="trinity-spec-traces-") as temporary:
        directory = (Path(work) if work else Path(temporary)).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        for vector in document["vectors"]:
            if only and vector["id"] not in only:
                continue
            if vector["kind"] == "dot_trace":
                steps = vector["cycles"]
                stimulus = directory / f"{vector['id']}.stim.mem"
                expected = directory / f"{vector['id']}.exp.mem"
                write_mem(stimulus, [dot_stimulus_word(step) for step in steps])
                write_mem(expected, [dot_expected_word(step) for step in steps])
                simulate(directory, vector["id"], dot_sources, "tb_spec_dot_trace",
                         {"DENSE5": 1 if vector["dense"] else 0, "ACC_WIDTH": vector["acc_width"], "CYCLES": len(steps)},
                         stimulus, expected)
                dots += 1
                cycles += len(steps)
            elif vector["kind"] == "storage_trace":
                steps = vector["cycles"]
                stimulus = directory / f"{vector['id']}.stim.mem"
                expected = directory / f"{vector['id']}.exp.mem"
                write_mem(stimulus, [storage_stimulus_word(step) for step in steps])
                write_mem(expected, [storage_expected_word(step) for step in steps])
                simulate(directory, vector["id"], storage_sources, "tb_spec_storage_trace",
                         {"DENSE5": 1 if vector["dense"] else 0, "TRIT_COUNT": vector["trit_count"], "CYCLES": len(steps)},
                         stimulus, expected)
                storages += 1
                cycles += len(steps)
            elif vector["kind"] == "join_trace":
                steps = vector["cycles"]
                stimulus = directory / f"{vector['id']}.stim.mem"
                expected = directory / f"{vector['id']}.exp.mem"
                write_mem(stimulus, [join_stimulus_word(step) for step in steps])
                write_mem(expected, [join_expected_word(step) for step in steps])
                simulate(directory, vector["id"], join_sources, "tb_spec_join_trace",
                         {"DENSE5": 1 if vector["dense"] else 0, "TRIT_COUNT": vector["trit_count"],
                          "ACC_WIDTH": vector["acc_width"], "CYCLES": len(steps)},
                         stimulus, expected)
                joins += 1
                cycles += len(steps)
    return dots, storages, joins, cycles


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rtl-dir", type=Path, default=ROOT / "build" / "t27" / "rtl")
    parser.add_argument("--work", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    document = load_document()
    dots, storages, joins, cycles = replay(document, args.rtl_dir, args.work)
    expected_dots = sum(1 for vector in document["vectors"] if vector["kind"] == "dot_trace")
    expected_storages = sum(1 for vector in document["vectors"] if vector["kind"] == "storage_trace")
    expected_joins = sum(1 for vector in document["vectors"] if vector["kind"] == "join_trace")
    if (dots, storages, joins) != (expected_dots, expected_storages, expected_joins):
        raise SystemExit(f"replayed {dots}/{expected_dots} dot, {storages}/{expected_storages} storage and "
                         f"{joins}/{expected_joins} join traces")
    summary = {"rtl_dir": str(args.rtl_dir), "dot_traces": dots, "storage_traces": storages, "join_traces": joins,
               "cycles": cycles, "evidence": "rtl-simulation", "simulator": "Icarus Verilog"}
    if args.output:
        args.output.write_text(json.dumps(summary, indent=1) + "\n")
    print(f"PASS spec stream traces in Icarus: {dots} dot traces, {storages} storage traces, {joins} join traces, "
          f"{cycles} compared cycles")


if __name__ == "__main__":
    main()
