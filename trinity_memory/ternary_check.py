"""Stage 1 compatibility matrix on real public checkpoints (orchestration only).

For each fixture tensor, every available storage format is decoded by the t27
readers (trinity_memory.formats) and compared trit for trit and scale for
scale. This module chooses tensors, moves bytes and writes the JSON report.
"""
from __future__ import annotations
from datetime import datetime, timezone
import gc
import json
from pathlib import Path
import time

from . import fixtures as fx
from . import formats as f

SCHEMA = "trinity.ternary-check.v1"
ROOT = Path(__file__).resolve().parent.parent

# Revisions and file sizes come from fixtures/manifest.json.
BITNET = {
    "packed": fx.remote("microsoft/bitnet-b1.58-2B-4T", "model.safetensors"),
    "bf16": fx.remote("microsoft/bitnet-b1.58-2B-4T-bf16", "model.safetensors"),
    "gguf": fx.remote("microsoft/bitnet-b1.58-2B-4T-gguf", "ggml-model-i2_s.gguf"),
}
BITNET_TENSORS = [("model.layers.0.self_attn.q_proj.weight", "blk.0.attn_q.weight"),
                  ("model.layers.0.mlp.down_proj.weight", "blk.0.ffn_down.weight")]

BONSAI = {
    "PTQ1_0": fx.remote("prism-ml/Ternary-Bonsai-2-27B-gguf", "Ternary-Bonsai-2-27B-PTQ1_0.gguf"),
    "PQ2_0": fx.remote("prism-ml/Ternary-Bonsai-2-27B-gguf", "Ternary-Bonsai-2-27B-PQ2_0.gguf"),
    "Q2_0": fx.remote("prism-ml/Ternary-Bonsai-2-27B-gguf-dev", "Ternary-Bonsai-2-27B-Q2_0-prism-fork-required.gguf"),
    "mlx": fx.remote("prism-ml/Ternary-Bonsai-2-27B-mlx-2bit", "model.safetensors"),
}
BONSAI_TENSORS = [("blk.0.ffn_down.weight", "language_model.model.layers.0.mlp.down_proj")]


def _source(remote):
    return {"repo": remote.repo, "revision": remote.revision, "file": remote.filename, "size": remote.size}


def _trits(a, b, count):
    total, first = f.mismatches(a, b, count)
    return {"mismatches": total, "first": first}


def bitnet_tensor(hf_name, gguf_name):
    started = time.time()
    info, dtype, packed = fx.safetensors_tensor(BITNET["packed"], hf_name)
    if dtype != "U8" or info.dims != 2:
        raise fx.FixtureError(f"{hf_name}: expected packed U8 [rows/4, cols], got {dtype} {info.shape}")
    rows, cols = info.d0 * 4, info.d1
    count = rows * cols
    _, scale_dtype, scale_bytes = fx.safetensors_tensor(BITNET["packed"], hf_name + "_scale")
    hf_scale, _ = f.words(scale_bytes, 2)
    hf_values, hf_outside = f.decode_hf_packed(packed, rows, cols)

    ginfo, stored = fx.gguf_tensor(BITNET["gguf"], gguf_name)
    if ginfo.tensor_type != 36 or [ginfo.d1, ginfo.d0] != [rows, cols]:
        raise fx.FixtureError(f"{gguf_name}: expected I2_S [{cols}, {rows}] (ne0 first), got {ginfo.tensor_type} {ginfo.shape}")
    i2s_values, i2s_scale, i2s_outside = f.decode_i2s(stored, count)
    trailer = f.i2s_trailer_nonzero(stored, count)

    binfo, bdtype, master = fx.safetensors_tensor(BITNET["bf16"], hf_name)
    if bdtype != "BF16" or binfo.shape != [rows, cols]:
        raise fx.FixtureError(f"{hf_name} (bf16): expected BF16 [{rows}, {cols}], got {bdtype} {binfo.shape}")
    bf_values, boundary, mean, s = f.absmean_bf16(master, count)

    i2s_word = (f.C.c_uint32 * 1)(i2s_scale)
    scale_i2s_vs_hf, _ = f.compare_scales(i2s_word, 1, f.F32, count, hf_scale, 1, f.BF16, count, count)
    result = {
        "model": "BitNet b1.58 2B4T",
        "tensor": {"hf": hf_name, "gguf": gguf_name, "shape": [rows, cols], "weights": count},
        "formats": {
            "hf_packed": {"source": _source(BITNET["packed"]), "stored_bytes": len(packed) + len(scale_bytes),
                          "bits_per_weight": (len(packed) + len(scale_bytes)) * 8 / count,
                          "outside_ternary": hf_outside,
                          "scale": {"dtype": scale_dtype, "word": hf_scale[0], "value": f.bf16_value(hf_scale[0])}},
            "I2_S": {"source": _source(BITNET["gguf"]), "stored_bytes": len(stored),
                     "bits_per_weight": len(stored) * 8 / count, "outside_ternary": i2s_outside,
                     "trailer_nonzero_bytes": trailer,
                     "scale": {"dtype": "F32", "word": i2s_scale, "value": f.f32_value(i2s_scale)}},
            "bf16_absmean": {"source": _source(BITNET["bf16"]), "stored_bytes": len(master),
                             "bits_per_weight": 16.0, "boundary_weights": boundary,
                             "scale": {"mean_abs": mean, "inverse": s}},
        },
        "comparisons": [
            {"a": "hf_packed", "b": "I2_S", "trits": _trits(hf_values, i2s_values, count),
             "scales_differ_weights": scale_i2s_vs_hf},
            {"a": "hf_packed", "b": "bf16_absmean", "trits": _trits(hf_values, bf_values, count)},
            {"a": "I2_S", "b": "bf16_absmean", "trits": _trits(i2s_values, bf_values, count)},
        ],
        "seconds": round(time.time() - started, 2),
    }
    del hf_values, i2s_values, bf_values, packed, stored, master
    gc.collect()
    return result


def bonsai_tensor(gguf_name, mlx_prefix):
    started = time.time()
    formats, runtime, reference = {}, {}, None
    comparisons, scales = [], {}
    for label, fmt in (("PTQ1_0", f.PTQ1_0), ("PQ2_0", f.PQ2_0), ("Q2_0", f.Q2_0)):
        remote = BONSAI[label]
        info, stored = fx.gguf_tensor(remote, gguf_name)
        if f.format_of_ggml(info.tensor_type, info.prism) != fmt:
            raise fx.FixtureError(f"{remote.key}: {gguf_name} has ggml type {info.tensor_type}")
        rows, cols = info.d1, info.d0
        count = rows * cols
        values, words, outside = f.decode_blocks(fmt, stored, count)
        per, _ = f.block_geometry(fmt)
        runtime[label] = {"prism_metadata": bool(info.prism)}
        formats[label] = {"source": _source(remote), "stored_bytes": len(stored),
                          "bits_per_weight": len(stored) * 8 / count, "outside_ternary": outside,
                          "scale": {"dtype": "F16", "group": per, "count": len(words)}}
        scales[label] = ((f.C.c_uint32 * len(words))(*words), len(words), f.F16, per)
        if reference is None:
            reference = (label, values, rows, cols, count)
        else:
            comparisons.append({"a": reference[0], "b": label, "trits": _trits(reference[1], values, count)})
            del values
    label0, ref_values, rows, cols, count = reference

    mlx = BONSAI["mlx"]
    winfo, wdtype, wbytes = fx.safetensors_tensor(mlx, mlx_prefix + ".weight")
    sinfo, sdtype, sbytes = fx.safetensors_tensor(mlx, mlx_prefix + ".scales")
    _, bdtype, bbytes = fx.safetensors_tensor(mlx, mlx_prefix + ".biases")
    if wdtype != "U32" or winfo.shape != [rows, cols // 16]:
        raise fx.FixtureError(f"MLX {mlx_prefix}: expected U32 [{rows}, {cols // 16}], got {wdtype} {winfo.shape}")
    kind = f.KIND_OF_DTYPE[sdtype]
    width = 2 if kind in (f.F16, f.BF16) else 4
    mlx_scales, scount = f.words(sbytes, width)
    mlx_biases, _ = f.words(bbytes, width)
    group = count // scount
    mlx_values, mlx_outside = f.decode_linear2(wbytes, rows, cols, cols // 4, 1)
    not_ternary = f.affine_not_ternary(mlx_scales, mlx_biases, scount, kind)
    formats["mlx_2bit"] = {"source": _source(mlx), "stored_bytes": len(wbytes) + len(sbytes) + len(bbytes),
                           "bits_per_weight": (len(wbytes) + len(sbytes) + len(bbytes)) * 8 / count,
                           "outside_ternary": mlx_outside,
                           "scale": {"dtype": sdtype, "group": group, "count": scount,
                                     "groups_where_bias_is_not_minus_scale": not_ternary}}
    scales["mlx_2bit"] = (mlx_scales, scount, kind, group)
    comparisons.append({"a": label0, "b": "mlx_2bit", "trits": _trits(ref_values, mlx_values, count)})
    for comparison in comparisons:
        a, b = scales[comparison["a"]], scales[comparison["b"]]
        comparison["scales_differ_weights"] = f.compare_scales(*a, *b, count)[0]
    result = {
        "model": "Ternary Bonsai 2 27B",
        "tensor": {"gguf": gguf_name, "mlx": mlx_prefix, "shape": [rows, cols], "weights": count},
        "formats": formats,
        "comparisons": comparisons,
        "runtime_requirements": runtime,
        "seconds": round(time.time() - started, 2),
    }
    del ref_values, mlx_values
    gc.collect()
    return result


def run(output: Path | None = None):
    checks = [bitnet_tensor(*pair) for pair in BITNET_TENSORS]
    checks += [bonsai_tensor(*pair) for pair in BONSAI_TENSORS]
    report = {"schema": SCHEMA, "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "checks": checks}
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=1) + "\n")
    return report


if __name__ == "__main__":
    print(json.dumps(run(ROOT / "build" / "ternary-check.json"), indent=1))
