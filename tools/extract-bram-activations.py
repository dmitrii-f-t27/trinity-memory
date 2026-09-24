#!/usr/bin/env python3
"""Write BitNet b1.58 2B4T's int8 input to the layer-0 attention projections for one
token, for the block-RAM matrix-vector bench (tools/generate-bram-bench.py --matvec-act).

The token's embedding row and the layer-0 input RMSNorm weight come from the pinned
packed checkpoint over range reads (about 10 KB). The steps follow the model:
h = the embedding row (bf16); RMSNorm in float32, h * rsqrt(mean(h^2) + eps), rounded
to bf16 and multiplied by the norm weight with the product rounded to bf16; then the
absmax activation quantization of transformers' BitNet in float32 (scale = 127 / max|x|,
round half to even, clamp to [-128, 127]). Float32 is emulated with round-trips through
struct; the sum of squares may differ from PyTorch's in the last bit, which does not
matter here because the device result is compared with the host's product of the same
vector. Output: one signed byte per element and a JSON sidecar (the output name with .json).

  python3 tools/extract-bram-activations.py --token 128000 --output build/fpga/rom/act.bin
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from trinity_memory import fixtures as fx  # noqa: E402
from trinity_memory import formats as f  # noqa: E402
from trinity_memory.ternary_check import BITNET, _source  # noqa: E402

EPS = 1e-5          # rms_norm_eps of the model's config.json


def f32(x: float) -> float:
    return struct.unpack("<f", struct.pack("<f", x))[0]


def bf16_bits_to_float(bits: int) -> float:
    return struct.unpack("<f", struct.pack("<I", bits << 16))[0]


def to_bf16(x: float) -> float:
    bits = struct.unpack("<I", struct.pack("<f", x))[0]
    rounded = (bits + 0x7FFF + ((bits >> 16) & 1)) >> 16
    return bf16_bits_to_float(rounded & 0xFFFF)


def tensor_range(remote, name: str):
    status, info, _ = remote.header_walk(lambda p: f.safetensors_find(p, name))
    if status != 0:
        raise f.FormatError(status)
    return info


def bf16_values(data: bytes) -> list[float]:
    return [bf16_bits_to_float(w) for w in struct.unpack(f"<{len(data) // 2}H", data)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--token", type=int, default=128000, help="token id (128000 is <|begin_of_text|>)")
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "fpga" / "rom" / "act.bin")
    args = parser.parse_args()
    remote = BITNET["packed"]
    emb = tensor_range(remote, "model.embed_tokens.weight")
    norm = tensor_range(remote, "model.layers.0.input_layernorm.weight")
    vocab, hidden = emb.d0, emb.d1
    if not 0 <= args.token < vocab:
        parser.error(f"token must be in [0, {vocab})")
    row_begin = emb.begin + args.token * hidden * 2
    h = bf16_values(remote.read(row_begin, row_begin + hidden * 2))
    w = bf16_values(remote.read(norm.begin, norm.end))
    if len(w) != hidden:
        raise SystemExit(f"norm weight has {len(w)} elements, expected {hidden}")

    variance = f32(sum(f32(x * x) for x in h) / hidden)
    inv = f32(1.0 / math.sqrt(f32(variance + EPS)))
    normed = [to_bf16(f32(x * inv)) for x in h]
    y = [to_bf16(f32(a * b)) for a, b in zip(w, normed)]
    peak = max(abs(v) for v in y)
    scale = f32(127.0 / max(peak, 1e-5))
    q = [max(-128, min(127, round(f32(v * scale)))) for v in y]   # round() is half to even

    args.output.parent.mkdir(parents=True, exist_ok=True)
    data = bytes(v & 0xFF for v in q)
    args.output.write_bytes(data)
    sidecar = {
        "schema": "trinity.bram-activations.v1", "model": "BitNet b1.58 2B4T",
        "token": args.token, "elements": hidden,
        "steps": "embedding row (bf16) -> RMSNorm (float32, eps 1e-5, bf16) * input_layernorm weight (bf16) "
                 "-> absmax int8 (scale = 127 / max|x|, round half to even, clamp [-128, 127])",
        "source": _source(remote),
        "ranges": {"embedding_row": [row_begin, row_begin + hidden * 2], "input_layernorm": [norm.begin, norm.end]},
        "rms_variance": variance, "peak": peak, "scale": scale,
        "counts": {"zero": q.count(0), "min": min(q), "max": max(q)},
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    args.output.with_suffix(".json").write_text(json.dumps(sidecar, indent=1) + "\n", encoding="utf-8")
    print(f"token {args.token}: {hidden} int8 activations (min {min(q)}, max {max(q)}, {q.count(0)} zeros), "
          f"scale {scale:.6g}; {args.output}")


if __name__ == "__main__":
    main()
