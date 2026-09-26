#!/usr/bin/env python3
"""Board run of the DDR3 device matvec (issue #64 phase C): the stage-1 golden
chunk of BitNet b1.58 2B4T layer-0 q_proj loaded through the #63 UART loader,
one doorbell run, every report line captured, the Y accumulators compared
bit-exact with the t27 reference of reports/ternary-check/matvec-2026-09-23.json.

The payloads follow the module's own layout (t27/rtl/fpga_ddr3_matvec.t27, the
DDR3_MATVEC_* parameters baked into the bitstream):
  weights    rows x words_per_row words of dense5 device codes at WADDR,
             one raw byte stream, not a TMEM container (the loader's
             --payload writes bytes verbatim; --payload-tmem-trits would wrap
             them in a container the module does not expect);
  activations  cols int8 bytes at AADDR, column c at byte c;
  doorbell     16 bytes at DADDR: LE u128, bits 63:32 magic, 31:0 the run.

The reference is computed from the cached fixtures through trinity_memory
(the reproduce path of the golden JSON) and its full-tensor accumulators are
sha256-checked against that committed report before any chunk of it is used.

  python3 tools/fpga-matvec-run.py --port /dev/cu.usbserial-10 \\
      --output reports/fpga/matvec-capture-$(date +%Y-%m-%d).json

Exit status 0 only when every accumulator matches the reference bit-exact.
"""
from __future__ import annotations

import argparse
import json
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
import matvec_device_model as model  # noqa: E402

GOLDEN = ROOT / "reports/ternary-check/matvec-2026-09-23.json"
HF_NAME = "model.layers.0.self_attn.q_proj.weight"
MAGIC = 0x74337633
WORD = 16                      # the store and the module both count 16-byte words


def synthetic_chunk(rows: int, cols: int, seed: int):
    """A seeded random chunk and its device-model expectation, no fixtures."""
    import random
    rng = random.Random(seed)
    trits = [rng.choice((-1, 0, 1)) for _ in range(rows * cols)]
    acts = [rng.randint(-128, 127) for _ in range(cols)]
    lanes, subs = model.LANES[model.FMT_D5], model.subs_of(model.FMT_D5)
    wpr = cols // lanes
    words = [model.encode_word(model.FMT_D5, model.lane_word(trits, r * cols + w * lanes))
             for r in range(rows) for w in range(wpr)]
    act_words = [x for pair in model.act_words(acts) for x in pair]
    expected = []
    for r in range(rows):
        acc = 0
        for w in range(wpr):
            lane_bits, _ = model.decode_word(model.FMT_D5, *words[r * wpr + w])
            for s in range(subs):
                acc += model.dot_chunk(lane_bits, s * 8, act_words[w * subs + s])
        expected.append(acc)
    return trits, acts, expected, cols


def chunk_reference(rows: int):
    """The golden chunk from the cached fixtures: trits, activations, the
    accumulators of every row, sha-checked against the committed report."""
    from trinity_memory import fixtures as fx, formats as f, ternary_check as tc, matvec as mv
    info, dtype, packed = fx.safetensors_tensor(tc.BITNET["packed"], HF_NAME)
    if dtype != "U8":
        raise fx.FixtureError(f"{HF_NAME}: expected packed U8, got {dtype}")
    full_rows, cols = info.d0 * 4, info.d1
    values, _ = f.decode_hf_packed(packed, full_rows, cols)
    x = mv.activations(cols)
    _, y = mv.matvec(values, full_rows, cols, x)
    want = json.loads(GOLDEN.read_text())["tensors"][0]["formats"]["hf_packed"]["accumulators"]
    got = hashlib_sha256_le(y, full_rows)
    if got != want["sha256_le"]:
        raise SystemExit(f"reference mismatch: computed {got}, committed {want['sha256_le']}")
    trits = [values[i] for i in range(rows * cols)]
    return trits, [v for v in x][:cols], [y[i] for i in range(rows)], cols


def hashlib_sha256_le(values, count: int) -> str:
    import hashlib
    digest = hashlib.sha256()
    for i in range(count):
        digest.update(struct.pack("<q", values[i]))
    return digest.hexdigest()


def dense5_stream(trits, cols: int, rows: int) -> bytes:
    """The module's weight region: rows x (cols/80) words of dense5 codes."""
    out = bytearray()
    lanes = model.LANES[model.FMT_D5]
    for r in range(rows):
        for w in range(cols // lanes):
            lo, hi = model.encode_word(model.FMT_D5,
                                       model.lane_word(trits, r * cols + w * lanes))
            out += struct.pack("<QQ", lo, hi)
    return bytes(out)


def loader_load(port: str, baud: int, path: Path, addr_words: int, output: Path,
                design_hz: float) -> None:
    """One payload through the #63 loader at a word address (bytes on the wire)."""
    cmd = [sys.executable, str(ROOT / "tools/fpga-uart-loader.py"), "--port", port,
           "--baud", str(baud), "--payload", str(path), "--addr", str(addr_words * WORD),
           "--design-hz", str(design_hz), "--output", str(output)]
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit(f"loader failed for {path.name} at word {addr_words}:\n"
                         f"{done.stdout[-2000:]}\n{done.stderr[-2000:]}")


LINE_TAGS = set("qkvydcownuz t")


def capture(port: str, baud: int, timeout_s: float, stop_tag: str = "z"):
    """Read 20-byte report lines until the stop tag; -> [(tag, a, b)]."""
    import serial
    lines, buf, deadline = [], b"", time.monotonic() + timeout_s
    with serial.Serial(port, baud, timeout=0.5) as com:
        com.reset_input_buffer()
        while time.monotonic() < deadline:
            buf += com.read(256)
            while len(buf) >= 20:
                line, buf = buf[:20], buf[20:]
                if line[19:20] != b"\n":
                    nl = buf.find(b"\n")          # resynchronise on the next newline
                    if nl >= 0:
                        buf = buf[nl + 1:]
                    continue
                tag = chr(line[0])
                if tag not in LINE_TAGS:
                    continue
                a = int(line[1:9], 16)
                b = int(line[9:19], 16)
                lines.append((tag, a, b))
                if tag == stop_tag:
                    return lines
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", default="/dev/cu.usbserial-10")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--rows", type=int, default=320, help="chunk rows (the bitstream's DDR3_MATVEC_ROWS)")
    ap.add_argument("--run", type=int, default=1, help="doorbell run counter")
    ap.add_argument("--waddr", type=lambda s: int(s, 0), default=0x4000, help="weights word address")
    ap.add_argument("--aaddr", type=lambda s: int(s, 0), default=0x1000, help="activations word address")
    ap.add_argument("--daddr", type=lambda s: int(s, 0), default=0x40, help="doorbell word address")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--synthetic", type=int, default=None,
                    help="seeded random chunk instead of the golden fixture data")
    ap.add_argument("--design-hz", type=float, default=83333333.33,
                    help="controller clock of this bitstream (62.5 MHz for DDR3_DDR_DIV=4)")
    ap.add_argument("--work", default=str(ROOT / "build/fpga/matvec-run"))
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    if args.synthetic is not None:
        print(f"reference: synthetic chunk, seed {args.synthetic} ...", flush=True)
        trits, acts, expected, cols = synthetic_chunk(args.rows, 2560, args.synthetic)
    else:
        print("reference: computing from the cached fixtures ...", flush=True)
        trits, acts, expected, cols = chunk_reference(args.rows)
    weights = work / "weights.dense5.bin"
    weights.write_bytes(dense5_stream(trits, cols, args.rows))
    act_path = work / "activations.int8.bin"
    act_path.write_bytes(bytes(v & 255 for v in acts))
    doorbell = work / "doorbell.bin"
    doorbell.write_bytes(struct.pack("<QQ", (MAGIC << 32) | (args.run & 0xFFFFFFFF), 0))
    print(f"payloads: weights {weights.stat().st_size} B, acts {len(acts)} B, doorbell 16 B", flush=True)

    loads = work / "loads.json"
    print("load: weights ...", flush=True)
    loader_load(args.port, args.baud, weights, args.waddr, loads.with_suffix(".w.json"), args.design_hz)
    print("load: activations ...", flush=True)
    loader_load(args.port, args.baud, act_path, args.aaddr, loads.with_suffix(".a.json"), args.design_hz)
    print("capture: opening the line stream, then the doorbell ...", flush=True)
    import threading
    lines = []

    def run_capture():
        lines.extend(capture(args.port, args.baud, args.timeout))

    reader = threading.Thread(target=run_capture)
    reader.start()
    time.sleep(0.5)                 # let the capture settle before the run starts
    loader_load(args.port, args.baud, doorbell, args.daddr, loads.with_suffix(".d.json"), args.design_hz)
    reader.join()
    print(f"capture: {len(lines)} lines", flush=True)

    y = [(a, b) for tag, a, b in lines if tag == "y"]
    got = [b - (1 << 64) if b >= (1 << 63) else b for _, b in y]  # 40-bit signed in u64 lines
    mismatch = [i for i in range(min(len(got), len(expected)))
                if got[i] != expected[i]]
    verdict = {"rows": args.rows, "y_lines": len(y), "expected": len(expected),
               "bit_exact": len(y) == len(expected) and not mismatch,
               "first_mismatch": mismatch[:5],
               "summary_lines": [(t, a, b) for t, a, b in lines if t in "dcownuz"],
               "first_y": [(a, b) for a, b in y[:4]],
               "first_expected": expected[:4]}
    out = Path(args.output)
    out.write_text(json.dumps(verdict, indent=1))
    print(json.dumps(verdict, indent=1))
    return 0 if verdict["bit_exact"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
