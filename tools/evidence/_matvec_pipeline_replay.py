#!/usr/bin/env python3
"""Offline replay of the retained AX7203 matvec compute-pipeline captures (#88).

Replays reports/fpga/matvec-compute-{baseline,pipeline}-2026-09-28-d5.uart/run-NN.txt
(24 raw UART captures, source commit d375d7ff) without hardware or network:

  * every capture's sha256 and size equal what its measurement JSON recorded;
  * the CRC-checked loader acknowledgement, 320 indexed Y lines and the seven
    summary lines parse strictly (exact 20-byte framing, no stray bytes), and the
    summary decodes through tools/matvec_device_model.decode_run_summary;
  * every Y accumulator (sign-extended 40 bits) equals the reference accumulator
    of its row, read from reports/fpga/matvec-pipeline-reference-y320.json;
  * cycles, words, bad words and stray acknowledgements are recomputed from the
    raw `c`, `n`, `u` lines and must equal the JSON runs[] entries; the speedup
    is recomputed from the raw cycle counts as integers.

The reference file is DERIVED data: `--regenerate-reference` rebuilds it from the
pinned BitNet fixture ranges through tools/fpga-matvec-run.py chunk_reference
(which checks the full 2560-row accumulator sha256 against the committed golden
reports/ternary-check/matvec-2026-09-23.json). That step needs the cached fixture
ranges (build/fixtures, not committed) and is not part of the CI replay; the
replay only checks the committed reference's own sha256_le against the golden's
first eight accumulators and against the captures.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import matvec_device_model as model  # noqa: E402
import uart_loader_protocol as proto  # noqa: E402

FPGA = Path("reports/fpga")
VARIANTS = {"baseline": "matvec-compute-baseline-2026-09-28-d5",
            "pipeline": "matvec-compute-pipeline-2026-09-28-d5"}
REFERENCE = FPGA / "matvec-pipeline-reference-y320.json"
GOLDEN = Path("reports/ternary-check/matvec-2026-09-23.json")
ROWS = 320
LINE = 20
SUMMARY_TAGS = "dcownuz"
RUNS = 12
WEIGHTS = ROWS * 2560
WORDS = WEIGHTS // 80
CTRL_HZ = 60_000_000


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def signed40(value: int) -> int:
    return value - (1 << 40) if value >= (1 << 39) else value


def sha256_le(values) -> str:
    digest = hashlib.sha256()
    for v in values:
        digest.update(struct.pack("<q", v))
    return digest.hexdigest()


def parse_capture(data: bytes):
    """Strict parse: one loader ack, then 320 y lines in order, then d c o w n u z."""
    if len(data) != LINE * (1 + ROWS + len(SUMMARY_TAGS)):
        raise ValueError("unexpected capture length %d" % len(data))
    lines = []
    for n in range(0, len(data), LINE):
        raw = data[n:n + LINE]
        if raw[19:20] != b"\n":
            raise ValueError("line not newline-terminated")
        lines.append(raw)
    if not any(e.kind == "line" and e.tag == "A" and e.check_ok and e.resp()["seq"] == 1
               and e.resp()["cmd"] == proto.CMD_LOAD for e in proto.StreamDecoder().feed(data)):
        raise ValueError("no CRC-checked load acknowledgement")
    if lines[0][:1] != b"A":
        raise ValueError("first line is not the acknowledgement")
    decoded = []
    for raw in lines[1:]:
        text = raw[1:19].decode("ascii")
        if raw[:1].decode("ascii") not in "y" + SUMMARY_TAGS or not all(c in "0123456789abcdef" for c in text):
            raise ValueError("bad line")
        decoded.append((raw[:1].decode("ascii"), int(text[:8], 16), int(text[8:], 16)))
    if [t for t, _, _ in decoded] != ["y"] * ROWS + list(SUMMARY_TAGS):
        raise ValueError("unexpected line order")
    if [a for t, a, _ in decoded[:ROWS]] != list(range(ROWS)):
        raise ValueError("Y indices are not 0..319 in order")
    y = [signed40(b) for _, _, b in decoded[:ROWS]]
    counters = model.decode_run_summary(decoded[ROWS:])
    return y, counters


def replay(root: Path = ROOT) -> dict:
    reference = json.loads((root / REFERENCE).read_text())
    ref_y = reference["accumulators"]
    golden = json.loads((root / GOLDEN).read_text())
    record = next(t for t in golden["tensors"] if t["tensor"].get("hf") == reference["tensor"]
                  and "layers.0." in reference["tensor"])
    first = record["formats"]["hf_packed"]["accumulators"]["first"]
    if len(ref_y) != ROWS or ref_y[:len(first)] != first:
        raise ValueError("reference differs from the committed golden accumulators")
    if sha256_le(ref_y) != reference["sha256_le"]:
        raise ValueError("reference hash mismatch")
    out = {"captures": 0, "rows": 0, "bad_rows": 0, "cycles": {}, "reports": {}}
    for variant, stem in VARIANTS.items():
        report = json.loads((root / FPGA / (stem + ".json")).read_text())
        if report["chunk"]["rows"] != ROWS or report["chunk"]["weights"] != WEIGHTS:
            raise ValueError("unexpected chunk")
        if report["clock"]["ctrl_hz"] != CTRL_HZ:
            raise ValueError("unexpected controller clock")
        want_flag = 1 if variant == "pipeline" else 0
        if "compute_pipeline %d," % want_flag not in report["read_discipline"]:
            raise ValueError("compute_pipeline setting does not match the variant")
        captures, runs = report["uart_captures"], report["runs"]
        if len(captures) != RUNS or len(runs) != RUNS:
            raise ValueError("expected 12 runs")
        cycles = set()
        for capture, run in zip(captures, runs):
            data = (root / FPGA / capture["file"].split("/", 1)[0] / Path(capture["file"]).name).read_bytes()
            if len(data) != capture["bytes"] or sha(data) != capture["sha256"]:
                raise ValueError("capture sha256 differs from its record: " + capture["file"])
            y, c = parse_capture(data)
            n = capture["run"]
            if (run["run"] != n or c["device_run"] != n or c["runs_done"] != n or c["words"] != WORDS
                    or c["bad_words"] or c["stray_acks"] or c["total_bad"]):
                raise ValueError("counter mismatch in " + capture["file"])
            for key in ("words", "cycles", "bad_words", "stray_acks", "polls", "command_stalls",
                        "wait_stalls", "consumer_stalls", "max_outstanding"):
                if run[key] != c[key]:
                    raise ValueError("%s differs from raw line in %s" % (key, capture["file"]))
            bad = sum(1 for a, b in zip(y, ref_y) if a != b)
            out["captures"] += 1
            out["rows"] += ROWS
            out["bad_rows"] += bad
            cycles.add(c["cycles"])
        if len(cycles) != 1:
            raise ValueError("cycle counts differ between runs of " + variant)
        out["cycles"][variant] = cycles.pop()
        out["reports"][variant] = report["statistic"]["median"]
    base, pipe = out["cycles"]["baseline"], out["cycles"]["pipeline"]
    out["ratio_ppm"] = base * 1_000_000 // pipe
    for variant, cyc in out["cycles"].items():  # recompute weights/s from raw cycles, compare to the report
        if round(WEIGHTS * CTRL_HZ / cyc, 1) != out["reports"][variant]:
            raise ValueError("weights/s differs from raw cycles: " + variant)
    return out


def regenerate_reference(root: Path = ROOT) -> None:
    import importlib.util
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location("fpga_matvec_run", root / "tools/fpga-matvec-run.py")
    run_tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run_tool)
    _, _, y, _ = run_tool.chunk_reference(ROWS)
    golden = json.loads((root / GOLDEN).read_text())
    record = next(t for t in golden["tensors"] if t["tensor"].get("hf") == run_tool.TENSORS["q_proj"])
    doc = {"schema": "trinity.matvec-pipeline-reference.v1",
           "derived_by": "python3 tools/evidence/_matvec_pipeline_replay.py --regenerate-reference",
           "tensor": record["tensor"]["hf"], "rows": "0-319",
           "source": record["formats"]["hf_packed"]["source"],
           "activations": "trinity_memory.matvec.activations(2560)",
           "full_tensor_sha256_le_checked_against": record["formats"]["hf_packed"]["accumulators"]["sha256_le"],
           "sha256_le": sha256_le(y), "accumulators": [int(v) for v in y]}
    (root / REFERENCE).write_text(json.dumps(doc, indent=1) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--regenerate-reference", action="store_true")
    args = ap.parse_args()
    if args.regenerate_reference:
        regenerate_reference()
        return 0
    r = replay()
    print(json.dumps({k: v for k, v in r.items() if k != "reports"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
