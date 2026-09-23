"""Real-layer int8-activation matvec on decoded weights (issue #33); I/O glue only.

The activations, the decoders, the integer products, the float steps and every
comparison run in generated t27 code (t27/matvec.t27, t27/formats.t27,
t27/compute.t27, t27/random.t27). This module moves bytes between the fixture
cache and those functions, hashes the resulting vectors and writes the report
reports/ternary-check/matvec-2026-09-23.json. Statuses and their tokens are the
reject classes of trinity_memory.formats (specs/formats/OWNERS.md).

    python3 -m trinity_memory.matvec --check           # recompute, compare with the committed report
    python3 -m trinity_memory.matvec --output PATH     # write the report
"""
from __future__ import annotations
import argparse
import array
import ctypes as C
import gc
import hashlib
import json
from pathlib import Path
import sys
import time

from . import _native as n
from . import fixtures as fx
from . import formats as f
from . import ternary_check as tc

SCHEMA = "trinity.ternary-check.matvec.v1"
ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "ternary-check" / "matvec-2026-09-23.json"
SEED = 27
GROUP = 64
F16_SHIFT = 24
FIRST = 8
I8 = C.POINTER(C.c_int8)
USED = ("length", "capacity", "code", "format", "scale_nonfinite")


def _check(status):
    if status < 0:
        raise f.FormatError(status)
    return status


def activations(count: int, seed: int = SEED):
    """int8 ctypes array: CPython random.Random(seed).randint(-128, 127), count draws."""
    x = (C.c_int8 * max(1, count))()
    _check(n.call("tmv_activations", C.c_int32, [C.c_int64, I8, n.SZ], seed, x, count))
    return x


def matvec(values, rows: int, cols: int, x, group: int = GROUP):
    """(partials, y): i64 partial sums per group of `group` columns and their row sums."""
    groups = cols // group if group else 0
    row64, x64 = (C.c_int64 * cols)(), (C.c_int64 * cols)()
    partials, y = (C.c_int64 * max(1, rows * groups))(), (C.c_int64 * rows)()
    _check(n.call("tmv_matvec", C.c_int32,
                  [n.I32, n.SZ, n.SZ, I8, n.SZ, n.I64, n.I64, n.SZ, n.I64, n.SZ, n.I64, n.SZ],
                  values, rows, cols, x, group, row64, x64, cols, partials, rows * groups, y, rows))
    return partials, y


def scale_value(word: int, kind: int) -> float:
    """A scale word of kind F16, BF16 or F32 as an exact f64 (t27 tf_scale_value)."""
    return n.call("tf_scale_value", C.c_double, [C.c_uint32, C.c_int32], word, kind)


def max_abs(values, count: int) -> int:
    return _check(n.call("tmv_max_abs", C.c_int64, [n.I64, n.SZ], values, count))


def mismatches_i64(a, b, count: int):
    first = (C.c_int64 * 1)()
    total = n.call("tmv_mismatches_i64", C.c_int64, [n.I64, n.I64, n.SZ, n.I64], a, b, count, first)
    return total, first[0]


def mismatches_f64(a, b, count: int):
    first = (C.c_int64 * 1)()
    total = n.call("tmv_mismatches_f64", C.c_int64, [n.F64, n.F64, n.SZ, n.I64], a, b, count, first)
    return total, first[0]


def difference(a, b, count: int):
    """(a - b as an i32 ctypes array, number of nonzero differences)."""
    out = (C.c_int32 * max(1, count))()
    nonzero = _check(n.call("tmv_difference", C.c_int64, [n.I32, n.I32, n.SZ, n.I32, n.SZ], a, b, count, out, count))
    return out, nonzero


def rows_nonzero(values, rows: int, cols: int) -> int:
    return n.call("tmv_rows_nonzero", C.c_int64, [n.I32, n.SZ, n.SZ], values, rows, cols)


def residual(ya, yb, yd, count: int):
    first = (C.c_int64 * 1)()
    total = n.call("tmv_residual", C.c_int64, [n.I64, n.I64, n.I64, n.SZ, n.I64], ya, yb, yd, count, first)
    return total, first[0]


def scale_tensor(y, rows: int, word: int, kind: int):
    out = (C.c_double * rows)()
    _check(n.call("tmv_scale_tensor", C.c_int32, [n.I64, n.SZ, C.c_uint32, C.c_int32, n.F64, n.SZ],
                  y, rows, word, kind, out, rows))
    return out


def scale_groups(partials, rows: int, groups: int, scales, scale_count: int, kind: int, per_scale: int):
    out = (C.c_double * rows)()
    _check(n.call("tmv_scale_groups", C.c_int32,
                  [n.I64, n.SZ, n.SZ, f.U32, n.SZ, C.c_int32, n.SZ, n.F64, n.SZ],
                  partials, rows, groups, scales, scale_count, kind, per_scale, out, rows))
    return out


def exact_groups_f16(partials, rows: int, groups: int, scales, scale_count: int, per_scale: int):
    out = (C.c_int64 * rows)()
    _check(n.call("tmv_exact_groups_f16", C.c_int32, [n.I64, n.SZ, n.SZ, f.U32, n.SZ, n.SZ, n.I64, n.SZ],
                  partials, rows, groups, scales, scale_count, per_scale, out, rows))
    return out


def exact_mismatches(exact, shift: int, values, count: int):
    first = (C.c_int64 * 1)()
    total = n.call("tmv_exact_mismatches", C.c_int64, [n.I64, C.c_int32, n.F64, n.SZ, n.I64],
                   exact, shift, values, count, first)
    return total, first[0]


# ---- report rendering -------------------------------------------------------

def _le(values, count: int, code: str) -> bytes:
    data = array.array(code, bytes(memoryview(values).cast("B"))[: count * array.array(code).itemsize])
    if sys.byteorder != "little":
        data.byteswap()
    return data.tobytes()


def _vector(values, count: int, code: str) -> dict:
    return {"sha256_le": hashlib.sha256(_le(values, count, code)).hexdigest(),
            "first": [values[i] for i in range(min(FIRST, count))]}


def _differ(total_first) -> dict:
    total, first = total_first
    return {"differ": total, "first": first}


def _source(remote) -> dict:
    return {"repo": remote.repo, "revision": remote.revision, "file": remote.filename}


def _run_format(values, rows, cols, x) -> tuple:
    partials, y = matvec(values, rows, cols, x)
    entry = {"status": "ok",
             "accumulators": {**_vector(y, rows, "q"), "max_abs": max_abs(y, rows)},
             "partials": {"group": GROUP, "count": rows * (cols // GROUP),
                          "sha256_le": _vector(partials, rows * (cols // GROUP), "q")["sha256_le"],
                          "max_abs": max_abs(partials, rows * (cols // GROUP))}}
    return entry, partials, y


def bitnet_tensor(hf_name: str, gguf_name: str) -> dict:
    info, dtype, packed = fx.safetensors_tensor(tc.BITNET["packed"], hf_name)
    if dtype != "U8" or info.dims != 2:
        raise fx.FixtureError(f"{hf_name}: expected packed U8 [rows/4, cols], got {dtype} {info.shape}")
    rows, cols = info.d0 * 4, info.d1
    count = rows * cols
    _, scale_dtype, scale_bytes = fx.safetensors_tensor(tc.BITNET["packed"], hf_name + "_scale")
    hf_scale, _ = f.words(scale_bytes, 2)
    hf_values, _ = f.decode_hf_packed(packed, rows, cols)
    ginfo, stored = fx.gguf_tensor(tc.BITNET["gguf"], gguf_name)
    if ginfo.tensor_type != 36 or [ginfo.d1, ginfo.d0] != [rows, cols]:
        raise fx.FixtureError(f"{gguf_name}: expected I2_S [{cols}, {rows}] (ne0 first), got {ginfo.tensor_type} {ginfo.shape}")
    i2s_values, i2s_word, _ = f.decode_i2s(stored, count)
    binfo, bdtype, master = fx.safetensors_tensor(tc.BITNET["bf16"], hf_name)
    if bdtype != "BF16" or binfo.shape != [rows, cols]:
        raise fx.FixtureError(f"{hf_name} (bf16): expected BF16 [{rows}, {cols}], got {bdtype} {binfo.shape}")
    bf_values, _, _, _ = f.absmean_bf16(master, count)
    del packed, stored, master

    x = activations(cols)
    formats, runs = {}, {}
    for label, values, word, kind, dtype_name, source in (
            ("hf_packed", hf_values, hf_scale[0], f.BF16, scale_dtype, tc.BITNET["packed"]),
            ("I2_S", i2s_values, i2s_word, f.F32, "F32", tc.BITNET["gguf"]),
            ("bf16_absmean", bf_values, None, None, None, tc.BITNET["bf16"])):
        entry, partials, y = _run_format(values, rows, cols, x)
        entry = {"source": _source(source), **entry}
        if word is not None:
            yf = scale_tensor(y, rows, word, kind)
            entry["float_step"] = {"formula": "y_int[r] * weight_scale, one f64 product per row, exact",
                                   "scale": {"dtype": dtype_name, "word": word, "value": scale_value(word, kind)},
                                   **_vector(yf, rows, "d")}
            runs[label] = (partials, y, yf)
        else:
            entry["derived"] = ("absmean ternarization of the bf16 master weights by t27 tf_absmean_bf16 "
                                "(transformers WeightQuant in f64); not a published storage format")
            entry["float_step"] = None
            runs[label] = (partials, y, None)
        formats[label] = entry
    groups = cols // GROUP
    hf, i2, bf = runs["hf_packed"], runs["I2_S"], runs["bf16_absmean"]
    trits = f.mismatches(hf_values, i2s_values, count)
    same = {"a": "hf_packed", "b": "I2_S", "trits": _differ(trits),
            "accumulator_rows": _differ(mismatches_i64(hf[1], i2[1], rows)),
            "partials": _differ(mismatches_i64(hf[0], i2[0], rows * groups)),
            "float_step_rows": _differ(mismatches_f64(hf[2], i2[2], rows)),
            "float_step_note": ("equal integers times two different scale values, the bf16 weight_scale and the "
                                "f32 I2_S scale (item 1 of docs/ternary-check.md); the products are exact, so "
                                "exactly the rows with y_int = 0 agree")}
    d, nonzero = difference(hf_values, bf_values, count)
    _, yd = matvec(d, rows, cols, x)
    derived = {"a": "hf_packed", "b": "bf16_absmean", "trits": _differ(f.mismatches(hf_values, bf_values, count)),
               "accumulator_rows": _differ(mismatches_i64(hf[1], bf[1], rows)),
               "partials": _differ(mismatches_i64(hf[0], bf[0], rows * groups)),
               "difference_tensor": {"nonzero": nonzero, "rows_with_nonzero": rows_nonzero(d, rows, cols),
                                     "y_difference": _vector(yd, rows, "q"),
                                     "rows_where_y_a_minus_y_b_is_not_D_x": _differ(residual(hf[1], bf[1], yd, rows))}}
    result = {"model": "BitNet b1.58 2B4T",
              "tensor": {"hf": hf_name, "gguf": gguf_name, "shape": [rows, cols], "weights": count},
              "x": {"count": cols, **_vector(x, cols, "b")},
              "formats": formats, "comparisons": [same, derived]}
    del hf_values, i2s_values, bf_values, d, runs
    gc.collect()
    return result


def bonsai_tensor(gguf_name: str, mlx_prefix: str) -> dict:
    formats, reference, comparisons = {}, None, []
    rows = cols = None
    loaded = []
    for label, fmt in (("PTQ1_0", f.PTQ1_0), ("PQ2_0", f.PQ2_0), ("Q2_0", f.Q2_0)):
        remote = tc.BONSAI[label]
        info, stored = fx.gguf_tensor(remote, gguf_name)
        if f.format_of_ggml(info.tensor_type, info.prism) != fmt:
            raise fx.FixtureError(f"{remote.key}: {gguf_name} has ggml type {info.tensor_type}")
        rows, cols = info.d1, info.d0
        per, _ = f.block_geometry(fmt)
        loaded.append((label, remote, lambda fmt=fmt, stored=stored, count=rows * cols: f.decode_blocks(fmt, stored, count),
                       per, bool(info.prism)))
    count = rows * cols
    mlx = tc.BONSAI["mlx"]
    winfo, wdtype, wbytes = fx.safetensors_tensor(mlx, mlx_prefix + ".weight")
    _, sdtype, sbytes = fx.safetensors_tensor(mlx, mlx_prefix + ".scales")
    _, bdtype, bbytes = fx.safetensors_tensor(mlx, mlx_prefix + ".biases")
    if wdtype != "U32" or winfo.shape != [rows, cols // 16] or sdtype != "F16" or bdtype != "F16":
        raise fx.FixtureError(f"MLX {mlx_prefix}: expected U32 [{rows}, {cols // 16}] with F16 scales and biases")
    mlx_scales, scount = f.words(sbytes, 2)
    mlx_biases, _ = f.words(bbytes, 2)
    not_ternary = f.affine_not_ternary(mlx_scales, mlx_biases, scount, f.F16)

    def mlx_decode():
        values, outside = f.decode_linear2(wbytes, rows, cols, cols // 4, 1)
        return values, mlx_scales, outside
    loaded.append(("mlx_2bit", mlx, mlx_decode, count // scount, None))

    x = activations(cols)
    groups = cols // GROUP
    for label, remote, decode, per, prism in loaded:
        values, words, _ = decode()
        if not isinstance(words, C.Array):
            words = (C.c_uint32 * len(words))(*words)
        scale_count = len(words)
        entry, partials, y = _run_format(values, rows, cols, x)
        del values
        per_scale = per // GROUP
        yf = scale_groups(partials, rows, groups, words, scale_count, f.F16, per_scale)
        exact = exact_groups_f16(partials, rows, groups, words, scale_count, per_scale)
        entry = {"source": _source(remote), **entry,
                 "float_step": {"scale": {"dtype": "F16", "group": per, "count": scale_count,
                                          "partials_per_scale": per_scale},
                                "f64": _vector(yf, rows, "d"),
                                "exact_times_2_24": _vector(exact, rows, "q"),
                                "rows_where_f64_is_not_exact": _differ(exact_mismatches(exact, F16_SHIFT, yf, rows))}}
        if prism is not None:
            entry["prism_metadata"] = prism
        else:
            entry["float_step"]["groups_where_bias_is_not_minus_scale"] = not_ternary
        formats[label] = entry
        if reference is None:
            reference = (label, partials, y, yf, exact)
        else:
            comparisons.append({"a": reference[0], "b": label,
                                "partials": _differ(mismatches_i64(reference[1], partials, rows * groups)),
                                "accumulator_rows": _differ(mismatches_i64(reference[2], y, rows)),
                                "float_step_rows": _differ(mismatches_f64(reference[3], yf, rows)),
                                "exact_rows": _differ(mismatches_i64(reference[4], exact, rows))})
        gc.collect()
    result = {"model": "Ternary Bonsai 2 27B",
              "tensor": {"gguf": gguf_name, "mlx": mlx_prefix, "shape": [rows, cols], "weights": count},
              "x": {"count": cols, **_vector(x, cols, "b")},
              "formats": formats, "comparisons": comparisons}
    gc.collect()
    return result


ARITHMETIC = {
    "activations": ("x[i] = CPython random.Random(27).randint(-128, 127), drawn in index order by t27 "
                    "tmv_activations (t27/random.t27); the vector for n columns is the first n draws"),
    "accumulators": ("W is the decoded [out_features, in_features] trit matrix (row r, column k); "
                     "p[r][j] = sum over k in group j (64 columns) of W[r][k] * x[k], each one tm_dot_i64 "
                     "call (t27/compute.t27) on the row widened to i64; y_int[r] = sum_j p[r][j] in i64. "
                     "64 divides every scale group here (64 for Q2_0, 128 for PTQ1_0, PQ2_0 and MLX)"),
    "bitnet_float_step": ("y[r] = y_int[r] * weight_scale in f64: bf16 weight_scale for the transformers packed "
                          "checkpoint, the f32 scale after the I2_S codes for bitnet.cpp. transformers 2c4914fb "
                          "src/transformers/integrations/bitnet.py:292, AutoBitLinear.forward (config "
                          "linear_class autobitlinear, quantization_mode offline): output = output * "
                          "self.weight_scale, a multiplication; BitLinear (:181) divides but is not the class "
                          "this checkpoint uses. |y_int| < 2^29 and a scale has at most 24 significant bits, "
                          "so each product is exact and order-free"),
    "bonsai_float_step": ("y[r] = sum over j ascending of f16(scale of partial j) * p[r][j], summed in f64 "
                          "left to right from 0; each product is exact (|p| < 2^29), the running sum rounds to "
                          "nearest-even when it needs more than 53 bits. Exact integer form: "
                          "sum_j (f16 * 2^24) * p[r][j] in checked i64, the exact value times 2^24. "
                          "For MLX (w = q * scale + bias) this equals the affine result only in groups where "
                          "bias = -scale, counted by groups_where_bias_is_not_minus_scale"),
    "activation_scale": ("x stands for quantized activations with one scale per token: the model output is "
                         "the float step divided by that scale (transformers ActQuant, bitnet.py:235-236: "
                         "scale = 127 / max|x|), a common factor for every format of the same tensor, "
                         "so it is left out. How each runtime quantizes activations is not modeled"),
    "hadamard": ("the Bonsai GGUF files carry prism.hadamard.* metadata (prism_metadata true): the PrismML "
                 "runtime applies a Walsh-Hadamard rotation at inference. It is a runtime transform, not "
                 "storage: the stored trits and scales are identical in all four formats, and the products "
                 "here use them as stored, without the rotation, so y is not the model's layer output"),
}


def run() -> dict:
    lock = (ROOT / "native" / "compiler.lock").read_text().strip()
    tensors = [bitnet_tensor(*pair) for pair in tc.BITNET_TENSORS]
    tensors += [bonsai_tensor(*pair) for pair in tc.BONSAI_TENSORS]
    x = activations(17408)
    return {"schema": SCHEMA,
            "issue": "dmitrii-f-t27/trinity-memory#33",
            "reproduce": "python3 -m trinity_memory.matvec --check",
            "compiler": {"repo": "gHashTag/t27", "commit": lock},
            "fixtures": "fixtures/manifest.json, consumer layer0_matvec",
            "activations": {"generator": "cpython-random.Random(seed).randint(-128,127)", "seed": SEED,
                            "count": 17408, "dtype": "int8", **_vector(x, 17408, "b")},
            "arithmetic": ARITHMETIC,
            "statuses": {token: status for status, token in f.TOKENS.items() if token in USED},
            "tensors": tensors}


def dumps(report: dict) -> str:
    return json.dumps(report, indent=1) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m trinity_memory.matvec", description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, help="write the report here")
    parser.add_argument("--check", action="store_true", help=f"compare with {REPORT.relative_to(ROOT)}")
    args = parser.parse_args(argv)
    started = time.time()
    text = dumps(run())
    # Run metadata stays out of the report, whose content must be reproducible.
    print(f"matvec report computed in {time.time() - started:.1f} s", file=sys.stderr)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    if args.check:
        if REPORT.read_text() != text:
            print(f"{REPORT.relative_to(ROOT)} differs from the recomputed report", file=sys.stderr)
            return 1
        print(f"{REPORT.relative_to(ROOT)} reproduced", file=sys.stderr)
    if not args.output and not args.check:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
