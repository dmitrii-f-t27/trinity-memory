"""Write the real tensors used by the llama.cpp issue 15193 harness to files.

I/O glue only: the byte ranges come from the pinned checkpoints of
trinity_memory.ternary_check through trinity_memory.fixtures, which reads the
build/fixtures cache (or fetches the same ranges) and checks every range
against fixtures/manifest.lock.json. The C harness decodes the bytes.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from trinity_memory import fixtures as fx  # noqa: E402
from trinity_memory import formats as f  # noqa: E402
from trinity_memory.ternary_check import BITNET, BONSAI  # noqa: E402

BITNET_LAYER0 = {"q_proj": ("model.layers.0.self_attn.q_proj.weight", 2560, 2560),
                 "down_proj": ("model.layers.0.mlp.down_proj.weight", 2560, 6912)}
BONSAI_LAYER0 = ("blk.0.ffn_down.weight", 17408, 5120)   # ne0 (row length), ne1 (rows)


def write(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for label, (name, rows, cols) in BITNET_LAYER0.items():
        info, dtype, packed = fx.safetensors_tensor(BITNET["packed"], name)
        if dtype != "U8" or [info.d0 * 4, info.d1] != [rows, cols]:
            raise fx.FixtureError(f"{name}: expected packed U8 [{rows // 4}, {cols}], got {dtype} {info.shape}")
        _, scale_dtype, scale = fx.safetensors_tensor(BITNET["packed"], name + "_scale")
        if scale_dtype != "BF16" or len(scale) != 2:
            raise fx.FixtureError(f"{name}_scale: expected one BF16 value, got {scale_dtype}, {len(scale)} bytes")
        (directory / f"bitnet-{label}.packed").write_bytes(packed)
        (directory / f"bitnet-{label}.scale").write_bytes(scale)
    name, ne0, ne1 = BONSAI_LAYER0
    info, stored = fx.gguf_tensor(BONSAI["PTQ1_0"], name)
    if f.format_of_ggml(info.tensor_type, info.prism) != f.PTQ1_0 or [info.d0, info.d1] != [ne0, ne1]:
        raise fx.FixtureError(f"{name}: expected PTQ1_0 [{ne0}, {ne1}], got type {info.tensor_type} {info.shape}")
    (directory / "bonsai-ffn_down.ptq1_0").write_bytes(stored)
    print(f"wrote BitNet 2B4T layer-0 q_proj and down_proj, Bonsai 27B blk.0.ffn_down to {directory}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    write(parser.parse_args(argv).output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
