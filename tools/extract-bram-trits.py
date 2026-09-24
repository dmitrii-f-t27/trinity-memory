#!/usr/bin/env python3
"""Write rows of a real ternary tensor as one signed byte per trit, for the
block-RAM store (tools/generate-bram-bench.py --rom-trits).

The trits come from the pinned BitNet b1.58 2B4T checkpoints through the t27
decoders (t27/formats.t27, through trinity_memory.formats): the packed Hugging
Face tensor gives them, and the I2_S tensor of the GGUF must give the same trits
for every row written (exit status 1 otherwise). Rows are output rows of the
unpacked [rows, cols] matrix, row-major, so rows [first, first + count) are
trits [first * cols, (first + count) * cols). A JSON sidecar (the output name
with .json) records the sources, the rows and the counts.

  python3 tools/extract-bram-trits.py --rows 396 --output build/fpga/rom/trits.bin
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from trinity_memory import fixtures as fx  # noqa: E402
from trinity_memory import formats as f  # noqa: E402
from trinity_memory.ternary_check import BITNET, _source  # noqa: E402

TENSORS = {
    "q_proj": ("model.layers.0.self_attn.q_proj.weight", "blk.0.attn_q.weight"),
    "down_proj": ("model.layers.0.mlp.down_proj.weight", "blk.0.ffn_down.weight"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tensor", choices=sorted(TENSORS), default="q_proj", help="layer-0 projection")
    parser.add_argument("--first-row", type=int, default=0)
    parser.add_argument("--rows", type=int, default=396, help="396 rows of 2560 = 1 013 760 trits")
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "fpga" / "rom" / "trits.bin")
    args = parser.parse_args()

    hf_name, gguf_name = TENSORS[args.tensor]
    info, dtype, packed = fx.safetensors_tensor(BITNET["packed"], hf_name)
    if dtype != "U8" or info.dims != 2:
        raise SystemExit(f"{hf_name}: expected packed U8 [rows/4, cols], got {dtype}")
    rows, cols = info.d0 * 4, info.d1
    if args.first_row < 0 or args.rows <= 0 or args.first_row + args.rows > rows:
        parser.error(f"rows [{args.first_row}, {args.first_row + args.rows}) are outside [0, {rows})")
    values, outside = f.decode_hf_packed(packed, rows, cols)
    ginfo, stored = fx.gguf_tensor(BITNET["gguf"], gguf_name)
    if ginfo.tensor_type != 36 or [ginfo.d1, ginfo.d0] != [rows, cols]:
        raise SystemExit(f"{gguf_name}: expected I2_S [{cols}, {rows}] (ne0 first)")
    i2s, _, i2s_outside = f.decode_i2s(stored, rows * cols)

    lo, hi = args.first_row * cols, (args.first_row + args.rows) * cols
    trits, other = values[lo:hi], i2s[lo:hi]
    differ = sum(1 for a, b in zip(trits, other) if a != b)
    if outside or i2s_outside or differ:
        raise SystemExit(f"packed and I2_S disagree: {outside} and {i2s_outside} values outside the trits, "
                         f"{differ} of {hi - lo} trits differ")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    data = bytes(t & 0xFF for t in trits)
    args.output.write_bytes(data)
    sidecar = {
        "schema": "trinity.bram-trits.v1",
        "model": "BitNet b1.58 2B4T", "tensor": hf_name, "gguf_tensor": gguf_name,
        "shape": [rows, cols], "rows": [args.first_row, args.first_row + args.rows], "trits": hi - lo,
        "order": "row-major rows of the unpacked [rows, cols] matrix (Hugging Face unpack order)",
        "sources": {"hf_packed": _source(BITNET["packed"]), "I2_S": _source(BITNET["gguf"])},
        "i2s_mismatches": differ,
        "counts": {"+1": trits.count(1), "0": trits.count(0), "-1": trits.count(-1)},
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    args.output.with_suffix(".json").write_text(json.dumps(sidecar, indent=1) + "\n", encoding="utf-8")
    print(f"{hi - lo} trits of {hf_name} rows {args.first_row}..{args.first_row + args.rows - 1} "
          f"(+1 {sidecar['counts']['+1']}, 0 {sidecar['counts']['0']}, -1 {sidecar['counts']['-1']}); "
          f"I2_S agrees; {args.output}")


if __name__ == "__main__":
    main()
