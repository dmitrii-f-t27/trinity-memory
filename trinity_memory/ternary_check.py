"""Ternary Check: the compatibility matrix of issue #32 on real public checkpoints.

Orchestration and rendering only. For every cached real tensor and every
storage format, the t27 code decides the cell (t27/matrix.t27 over the readers
and writers of t27/formats.t27): published bytes are decoded and compared with
the tensor's reference, other formats are written and read back by t27 (a t27
round trip, not third-party evidence) or by the pinned llama.cpp reference
quantizers (tests/upstream/run-llamacpp-matrix.sh), and a format that cannot
hold the tensor is `not-representable` with its reasons (provenance
`not_written`: t27 decides before anything is written). This module chooses
tensors, moves bytes between the fixture cache and those functions, and writes
reports/ternary-check.json (schema trinity.ternary-check.v1,
schemas/ternary-check.v1.schema.json), reports/ternary-check.html and one
minimal reproduction per mismatch under reports/ternary-check/repro/. The
reports are deterministic; the run's timings go to build/ternary-check/run.json.

    make ternary-check                              # fetch, build, run, write reports/ (one command)
    python3 -m trinity_memory.ternary_check         # from the caches into build/ternary-check.json, .html, repro/
                                                    # (needs build/fixtures, build/t27 and build/upstream/matrix:
                                                    # make ternary-check makes them; else exit 2, no traceback)
    python3 -m trinity_memory.ternary_check --write # the same into reports/ (the committed files)
    python3 -m trinity_memory.ternary_check --check # recompute, compare with the committed files
"""
from __future__ import annotations
import argparse
import array
from collections.abc import Mapping
from datetime import datetime, timezone
import gc
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import time

from . import fixtures as fx
from . import formats as f

SCHEMA = "trinity.ternary-check.v1"
REPRO_SCHEMA = "trinity.ternary-check.repro.v1"
ROOT = Path(__file__).resolve().parent.parent
REPORT = Path("reports") / "ternary-check.json"
HTML = Path("reports") / "ternary-check.html"
REPRO = Path("reports") / "ternary-check" / "repro"
UPSTREAM = ROOT / "build" / "upstream" / "matrix"
UPSTREAM_LOCK = ROOT / "tests" / "upstream" / "llama.cpp.lock.json"
RUN = ROOT / "build" / "ternary-check" / "run.json"
SAMPLES = 4


class _Files(Mapping):
    """Label -> fixtures.Remote. Revisions and file sizes come from
    fixtures/manifest.json, which is read on first access, so importing this
    module does not need the manifest (it ships with a source checkout, not
    with the wheel)."""

    def __init__(self, files: dict):
        self._files, self._remotes = files, {}

    def __getitem__(self, label):
        if label not in self._remotes:
            self._remotes[label] = fx.remote(*self._files[label])
        return self._remotes[label]

    def __iter__(self):
        return iter(self._files)

    def __len__(self):
        return len(self._files)


BITNET = _Files({
    "packed": ("microsoft/bitnet-b1.58-2B-4T", "model.safetensors"),
    "bf16": ("microsoft/bitnet-b1.58-2B-4T-bf16", "model.safetensors"),
    "gguf": ("microsoft/bitnet-b1.58-2B-4T-gguf", "ggml-model-i2_s.gguf"),
})
BITNET_TENSORS = [("model.layers.0.self_attn.q_proj.weight", "blk.0.attn_q.weight"),
                  ("model.layers.0.mlp.down_proj.weight", "blk.0.ffn_down.weight")]

BONSAI = _Files({
    "PTQ1_0": ("prism-ml/Ternary-Bonsai-2-27B-gguf", "Ternary-Bonsai-2-27B-PTQ1_0.gguf"),
    "PQ2_0": ("prism-ml/Ternary-Bonsai-2-27B-gguf", "Ternary-Bonsai-2-27B-PQ2_0.gguf"),
    "Q2_0": ("prism-ml/Ternary-Bonsai-2-27B-gguf-dev", "Ternary-Bonsai-2-27B-Q2_0-prism-fork-required.gguf"),
    "mlx": ("prism-ml/Ternary-Bonsai-2-27B-mlx-2bit", "model.safetensors"),
})
BONSAI_TENSORS = [("blk.0.ffn_down.weight", "language_model.model.layers.0.mlp.down_proj")]

# MLX group and ONNX block size of the round trips: 128, the group of the
# published Bonsai MLX file (a power of two, as MatMulNBits requires).
MLX_GROUP = ONNX_BLOCK = 128

# Matrix columns: (id, t27 format id, name, group argument, upstream writer).
COLUMNS = [
    ("hf_packed", f.HF_PACKED, "HF packed uint8 + bf16 weight_scale", 0, None),
    ("i2_s", f.I2_S, "I2_S (bitnet.cpp)", 0, None),
    ("ptq1_0", f.PTQ1_0, "PTQ1_0 (PrismML)", 0, None),
    ("pq2_0", f.PQ2_0, "PQ2_0 (PrismML)", 0, None),
    ("q2_0", f.Q2_0, "Q2_0, group 64 (llama.cpp)", 0, None),
    ("mlx_2bit", f.LINEAR2, "MLX 2-bit affine, group 128", MLX_GROUP, None),
    ("tq1_0", f.TQ1_0, "TQ1_0 (llama.cpp)", 0, None),
    ("tq2_0", f.TQ2_0, "TQ2_0 (llama.cpp)", 0, None),
    ("q1_0", f.Q1_0, "Q1_0, binary (llama.cpp)", 0, None),
    ("onnx_2bit", f.ONNX2, "ONNX MatMulNBits bits=2, block 128", ONNX_BLOCK, None),
    ("tq1_0_llamacpp", f.TQ1_0, "TQ1_0 by llama.cpp quantize_row_tq1_0_ref", 0, "tq1_0"),
    ("tq2_0_llamacpp", f.TQ2_0, "TQ2_0 by llama.cpp quantize_row_tq2_0_ref", 0, "tq2_0"),
]
KIND_NAME = {f.F16: "F16", f.BF16: "BF16", f.F32: "F32"}
PROVENANCE = {
    "reference": "the published bytes this tensor's other cells are compared with: third-party evidence",
    "published": "bytes of a pinned public checkpoint, decoded by t27: third-party evidence",
    "t27_round_trip": ("written by the t27 encoder and read back by the t27 decoder: shows that the "
                       "format can hold the tensor; not third-party evidence"),
    "upstream_encoder": ("written by the pinned llama.cpp reference quantizer from the dequantized "
                         "tensor, read by the t27 decoder: third-party evidence"),
    "not_written": ("nothing is written: t27 (tmx_representable) decides from the reference trits and scales "
                    "that the format cannot hold the tensor, so neither the t27 encoder nor an upstream "
                    "writer runs; not third-party evidence"),
    "derived": ("trits computed by t27 from published bf16 master weights with the transformers WeightQuant "
                "rule; not a storage format"),
}
REASON_UNITS = {
    "shape": "the format's geometry does not tile the tensor's shape",
    "binary_only": "weights equal to 0",
    "code_outside": "weights outside the format's codes",
    "group_scales_differ": "stored scale groups that span reference scales of different values",
    "scale_precision": "reference scale words without an exact word of the format's scale type",
}
METADATA_NOTE = {
    "gguf": ("the tensor's GGUF info record (24 bytes + name + 8 bytes per dimension) and the alignment padding "
             "after its data (t27 tmx_gguf_metadata_bytes); the GGUF header and key-value section belong to the "
             "whole file (file overhead, not counted per tensor)"),
    "safetensors": ("a safetensors tensor has no record or padding of its own; the JSON header belongs to the "
                    "whole file (file overhead, not counted per tensor)"),
    "none": "bare tensor bytes as the writer returns them, in no container: no metadata share",
}
EXPLANATIONS = {
    "scale_bf16_rounding": ("every differing scale pair is a bf16 word and an f32 word, and the bf16 word is "
                            "the f32 word rounded to nearest-even (item 1 of docs/ternary-check.md)"),
    "tie_split": ("every differing trit is at a weight whose bf16 master value is +-0.5 x weight_scale, and "
                  "the packed checkpoint stores both 0 and +-1 for that value, so no rule applied to the "
                  "published bf16 values gives the packed trits (item 2 of docs/ternary-check.md)"),
    "scale_zero_weights": ("scales differ only at weights whose trit is 0 in both tensors, so every weight "
                           "dequantizes to the same value: the llama.cpp TQ1_0/TQ2_0 quantizers store d = 0 for a "
                           "block of zero weights"),
    "unexplained": "the difference is not one of the explained patterns",
}


def _mx():
    from . import matrix
    return matrix


def _sha(data) -> str:
    return hashlib.sha256(bytes(data)).hexdigest()


def _source(remote, begin=None, end=None) -> dict:
    out = {"repo": remote.repo, "revision": remote.revision, "file": remote.filename}
    if begin is not None:
        out["range"] = [begin, end]
    return out


def _flags(*dicts) -> dict:
    merged = {token: 0 for token in f.FLAGS}
    for d in dicts:
        for token, value in d.items():
            merged[token] += value
    return merged


def _scale_words(values) -> list:
    return [int(w) for w in values]


def _i32_le(values, count: int) -> bytes:
    data = array.array("i", bytes(memoryview(values).cast("B"))[: 4 * count])
    if sys.byteorder != "little":
        data.byteswap()
    return data.tobytes()


# ---- loading the real tensors ------------------------------------------------------

def _gguf_record(info) -> dict:
    """What the metadata share of a GGUF tensor depends on (read by the t27 GGUF reader)."""
    return {"name_size": int(info.name_size), "dims": int(info.dims), "alignment": int(info.alignment)}


class Stored:
    """One published form of a tensor: decoded values and scale words, plus
    what the report says about its bytes."""

    def __init__(self, column, values, scale_words, kind, group, code_bytes, stored_bytes, source, flags,
                 outside, extra=None):
        self.column, self.values, self.words, self.kind, self.group = column, values, scale_words, kind, group
        self.code_bytes, self.stored_bytes, self.source, self.flags = code_bytes, stored_bytes, source, flags
        self.outside, self.extra = outside, extra or {}


class Tensor:
    def __init__(self, tid, model, family, names, rows, cols):
        self.id, self.model, self.family, self.names = tid, model, family, names
        self.rows, self.cols, self.count = rows, cols, rows * cols
        self.stored: dict[str, callable] = {}
        self.reference = None
        self.master = None
        self.runtime = None


def bitnet_tensor(hf_name: str, gguf_name: str) -> Tensor:
    packed_remote, gguf_remote, bf16_remote = BITNET["packed"], BITNET["gguf"], BITNET["bf16"]
    info, dtype, packed = fx.safetensors_tensor(packed_remote, hf_name)
    if dtype != "U8" or info.dims != 2:
        raise fx.FixtureError(f"{hf_name}: expected packed U8 [rows/4, cols], got {dtype} {info.shape}")
    rows, cols = info.d0 * 4, info.d1
    count = rows * cols
    packed_begin = info.begin
    sinfo, scale_dtype, scale_bytes = fx.safetensors_tensor(packed_remote, hf_name + "_scale")
    if scale_dtype != "BF16" or len(scale_bytes) != 2:
        raise fx.FixtureError(f"{hf_name}_scale: expected one BF16 value")
    hf_word = scale_bytes[0] | scale_bytes[1] << 8
    ginfo, i2s_bytes = fx.gguf_tensor(gguf_remote, gguf_name)
    if ginfo.tensor_type != 36 or [ginfo.d1, ginfo.d0] != [rows, cols]:
        raise fx.FixtureError(f"{gguf_name}: expected I2_S [{cols}, {rows}] (ne0 first), got {ginfo.tensor_type} {ginfo.shape}")
    i2s_begin = ginfo.data_start + ginfo.offset
    binfo, bdtype, master = fx.safetensors_tensor(bf16_remote, hf_name)
    if bdtype != "BF16" or binfo.shape != [rows, cols]:
        raise fx.FixtureError(f"{hf_name} (bf16): expected BF16 [{rows}, {cols}], got {bdtype} {binfo.shape}")
    short = hf_name.split(".")[-2]
    tensor = Tensor(f"bitnet-{short}", "BitNet b1.58 2B4T", "bitnet", {"hf": hf_name, "gguf": gguf_name}, rows, cols)

    def hf():
        values, outside = f.decode_hf_packed(packed, rows, cols)
        return Stored("hf_packed", values, [hf_word], f.BF16, 0, packed, len(packed) + len(scale_bytes),
                      [_source(packed_remote, packed_begin, packed_begin + len(packed)),
                       _source(packed_remote, sinfo.begin, sinfo.end)],
                      _flags({"outside_ternary": outside}, f.scales_check([hf_word], f.BF16)), outside,
                      {"scale_bytes": scale_bytes.hex(), "begin": packed_begin, "container": "safetensors"})

    def i2s():
        values, word, outside = f.decode_i2s(i2s_bytes, count)
        return Stored("i2_s", values, [word], f.F32, 0, i2s_bytes, len(i2s_bytes),
                      [_source(gguf_remote, i2s_begin, i2s_begin + len(i2s_bytes))],
                      f.i2s_flags(i2s_bytes, count), outside,
                      {"begin": i2s_begin, "scale_bytes": i2s_bytes[count // 4: count // 4 + 4].hex(),
                       "container": "gguf", "gguf": _gguf_record(ginfo)})

    tensor.stored = {"hf_packed": hf, "i2_s": i2s}
    tensor.reference = "hf_packed"
    tensor.master = {"bytes": master, "begin": binfo.begin, "source": _source(bf16_remote, binfo.begin, binfo.end)}
    return tensor


def bonsai_tensor(gguf_name: str, mlx_prefix: str) -> Tensor:
    loaded, prism = {}, {}
    rows = cols = None
    for label, fmt in (("PTQ1_0", f.PTQ1_0), ("PQ2_0", f.PQ2_0), ("Q2_0", f.Q2_0)):
        remote = BONSAI[label]
        info, stored = fx.gguf_tensor(remote, gguf_name)
        if f.format_of_ggml(info.tensor_type, info.prism) != fmt:
            raise fx.FixtureError(f"{remote.key}: {gguf_name} has ggml type {info.tensor_type}")
        rows, cols = info.d1, info.d0
        loaded[label] = (fmt, remote, stored, info.data_start + info.offset, _gguf_record(info))
        prism[label] = bool(info.prism)
    count = rows * cols
    mlx = BONSAI["mlx"]
    winfo, wdtype, wbytes = fx.safetensors_tensor(mlx, mlx_prefix + ".weight")
    sinfo, sdtype, sbytes = fx.safetensors_tensor(mlx, mlx_prefix + ".scales")
    binfo, bdtype, bbytes = fx.safetensors_tensor(mlx, mlx_prefix + ".biases")
    if wdtype != "U32" or winfo.shape != [rows, cols // 16] or sdtype != "F16" or bdtype != "F16":
        raise fx.FixtureError(f"MLX {mlx_prefix}: expected U32 [{rows}, {cols // 16}] with F16 scales and biases")
    group = count // (len(sbytes) // 2)
    tensor = Tensor("bonsai-ffn_down", "Ternary Bonsai 2 27B", "bonsai", {"gguf": gguf_name, "mlx": mlx_prefix},
                    rows, cols)

    def gguf_form(label, column):
        fmt, remote, stored, begin, record = loaded[label]

        def load():
            values, words, outside = f.decode_blocks(fmt, stored, count)
            per, _ = f.block_geometry(fmt)
            return Stored(column, values, words, f.F16, per, stored, len(stored),
                          [_source(remote, begin, begin + len(stored))], f.block_flags(fmt, stored, count), outside,
                          {"begin": begin, "container": "gguf", "gguf": record})
        return load

    def mlx_form():
        values, outside = f.decode_mlx2(wbytes, rows, cols, group)
        scales, _ = f.words(sbytes, 2)
        biases, _ = f.words(bbytes, 2)
        return Stored("mlx_2bit", values, _scale_words(scales), f.F16, group, wbytes,
                      len(wbytes) + len(sbytes) + len(bbytes),
                      [_source(mlx, winfo.begin, winfo.end), _source(mlx, sinfo.begin, sinfo.end),
                       _source(mlx, binfo.begin, binfo.end)],
                      _flags({"outside_ternary": outside}, f.affine_check(scales, biases, f.F16)), outside,
                      {"begin": winfo.begin, "container": "safetensors"})

    tensor.stored = {"ptq1_0": gguf_form("PTQ1_0", "ptq1_0"), "pq2_0": gguf_form("PQ2_0", "pq2_0"),
                     "q2_0": gguf_form("Q2_0", "q2_0"), "mlx_2bit": mlx_form}
    tensor.reference = "ptq1_0"
    tensor.runtime = {"hadamard_rotation": all(prism.values()),
                      "evidence": "every GGUF file of this tensor declares prism.* metadata (prism.hadamard.*)",
                      "note": ("the PrismML runtime rotates activations at inference; the rotation is not storage, "
                               "so it is no reason for a cell: every cell compares stored trits and scales")}
    return tensor


# ---- cells ---------------------------------------------------------------------------

def _scales_entry(words, kind, group, result, explain) -> dict:
    return {"dtype": KIND_NAME[kind], "group": group, "count": len(words),
            "first_word": words[0] if words else None, "words_sha256_le": _sha(b"".join(w.to_bytes(4, "little") for w in words)),
            "differ": result["scales_differ"], "first": result["scales_first"],
            "explanation": explain.get(result["scales_explain"])}


def _trits_entry(count, result, explain) -> dict:
    return {"compared": count, "differ": result["trits_differ"], "first": result["trits_first"],
            "explanation": explain.get(result["trits_explain"])}


def _bits(stored_bytes: int, count: int, container: str, record: dict | None = None) -> dict:
    """Bits per weight of the stored bytes, and with the tensor's metadata share (t27 arithmetic)."""
    mx = _mx()
    extra = 0 if record is None else mx.gguf_metadata_bytes(record["name_size"], record["dims"], stored_bytes,
                                                            record["alignment"])
    metadata = {"container": container, "bytes": extra,
                "bits_per_weight": mx.bits_per_weight(stored_bytes + extra, count), "note": METADATA_NOTE[container]}
    if record is not None:
        metadata["record"] = record
    return {"stored_bytes": stored_bytes, "bits_per_weight": mx.bits_per_weight(stored_bytes, count),
            "metadata": metadata}


def _cell_id(tensor, column) -> str:
    return f"{tensor.id}--{column}"


def _reasons(reasons, tensor, fmt, group, ref) -> list:
    """Reasons with the number of units they could affect: 1 shape, the weights, the stored scale groups of
    the format, the reference scale words."""
    mx = _mx()
    total = {"shape": 1, "binary_only": tensor.count, "code_outside": tensor.count,
             "group_scales_differ": mx.scale_count(tensor.rows, tensor.cols, mx.scale_group(fmt, group)),
             "scale_precision": len(ref.words)}
    return [{"reason": token, "count": n, "of": total[token], "first": first, "unit": REASON_UNITS[token]}
            for token, n, first in reasons]


def _upstream_stored(tensor, column, fmt, ext, upstream_dir: Path) -> Stored:
    """The tensor as the pinned llama.cpp quantizer stored it (tests/upstream/run-llamacpp-matrix.sh)."""
    path = upstream_dir / f"{tensor.id}.{ext}"
    if not path.is_file():
        raise fx.CacheMiss(f"{path} is missing: run sh tests/upstream/run-llamacpp-matrix.sh (it fetches the "
                              f"pinned llama.cpp sources and stores the BitNet tensors with its quantizers)")
    data = path.read_bytes()
    values, words, outside = f.decode_blocks(fmt, data, tensor.count)
    per, _ = f.block_geometry(fmt)
    source = [{"upstream": "llama.cpp", "writer": f"quantize_row_{ext}_ref",
               "file": f"build/upstream/matrix/{tensor.id}.{ext}", "range": [0, len(data)]}]
    return Stored(column, values, words, f.F16, per, data, len(data), source, f.block_flags(fmt, data, tensor.count),
                  outside, {"begin": 0})


def tensor_cells(tensor: Tensor, upstream_dir: Path, repro: dict) -> tuple[list, dict]:
    """All cells of one tensor, the reference entry, and (for BitNet) the derived absmean cell."""
    mx = _mx()
    rows, cols, count = tensor.rows, tensor.cols, tensor.count
    ref = tensor.stored[tensor.reference]()
    reference = {"column": tensor.reference, "source": ref.source,
                 "trits_sha256_le": _sha(_i32_le(ref.values, count)),
                 "scale": {"dtype": KIND_NAME[ref.kind], "group": ref.group, "count": len(ref.words),
                           "first_word": ref.words[0]}}
    cells, t27_bytes = [], {}
    for column, fmt, _, group, upstream in COLUMNS:
        cell = {"id": _cell_id(tensor, column), "tensor": tensor.id, "format": column}
        stored = None
        if column in tensor.stored:
            stored = ref if column == tensor.reference else tensor.stored[column]()
            cell["provenance"] = "reference" if column == tensor.reference else "published"
        elif upstream is not None:
            # llama.cpp writes only what t27 finds representable; the rest is decided before writing.
            primary, reasons = mx.representable(fmt, rows, cols, group, ref.values, ref.words, ref.kind, ref.group)
            if primary is None:
                stored = _upstream_stored(tensor, column, fmt, upstream, upstream_dir)
                cell["provenance"] = "upstream_encoder"
            else:
                cell.update({"provenance": "not_written", "evidence": "t27", "status": "not-representable",
                             "reasons": _reasons(reasons, tensor, fmt, group, ref),
                             "note": "decided by t27 before writing; the llama.cpp quantizer is not run"})
        else:
            result, reasons, written, words = mx.round_trip(fmt, rows, cols, group, ref.values, ref.words,
                                                            ref.kind, ref.group)
            t27_bytes[column] = written
            cell.update({"provenance": "t27_round_trip", "evidence": "t27", "status": mx.STATUS[result["status"]]})
            if result["status"] == mx.NOT_REPRESENTABLE:
                cell.update({"provenance": "not_written", "reasons": _reasons(reasons, tensor, fmt, group, ref),
                             "note": "decided by t27 before writing; the t27 encoder is not run"})
            else:
                if result["status"] == mx.MISMATCH:
                    raise RuntimeError(f"{cell['id']}: a t27 round trip differs from its reference; "
                                       f"see the t27 encoder and decoder of this format")
                cell.update({"trits": _trits_entry(count, result, mx.EXPLAIN),
                             "scales": _scales_entry(words, mx.scale_kind(fmt), mx.scale_group(fmt, group),
                                                     result, mx.EXPLAIN),
                             "outside_ternary": result["outside"], **_bits(result["stored_bytes"], count, "none"),
                             "bytes_sha256": _sha(written)})
        if stored is not None:
            # Bytes written by a third party: published, or by the llama.cpp quantizer.
            result = mx.result_dict(mx.compare(ref.values, stored.values, rows, cols, ref.words, ref.kind, ref.group,
                                               stored.words, stored.kind, stored.group))
            differ, first = mx.reencode(fmt, rows, cols, group, stored.values, stored.words, stored.code_bytes)
            cell.update({"evidence": "third-party", "source": stored.source,
                         "status": mx.STATUS[result["status"]],
                         "trits": _trits_entry(count, result, mx.EXPLAIN),
                         "scales": _scales_entry(stored.words, stored.kind, stored.group, result, mx.EXPLAIN),
                         "flags": stored.flags,
                         **_bits(stored.stored_bytes, count, stored.extra.get("container", "none"),
                                 stored.extra.get("gguf")),
                         "t27_reencode": {"differ_bytes": differ, "first": first,
                                          "note": "the t27 writer given the decoded trits and scale words, "
                                                  "against the stored code bytes"}})
            if upstream is not None:
                same, same_first = mx.bytes_differ(stored.code_bytes, t27_bytes[upstream])
                cell["bytes_sha256"] = _sha(stored.code_bytes)
                cell["t27_round_trip_bytes"] = {
                    "differ_bytes": same, "first": same_first,
                    "note": f"against the bytes of cell {_cell_id(tensor, upstream)}, where the t27 writer is "
                            f"given the reference scale for every block"}
            if result["status"] == mx.MISMATCH:
                if result["trits_differ"] == 0 and len(ref.words) == 1 and len(stored.words) == 1:
                    repro[cell["id"]] = _scale_repro(tensor, ref, stored, fmt, group, result)
                else:
                    repro[cell["id"]] = _generic_repro(tensor, ref, stored, fmt, group, result)
                cell["repro"] = str(REPRO / f"{cell['id']}.json")
        cells.append(cell)
        del stored
        gc.collect()
    derived = None
    if tensor.master is not None:
        derived = _absmean_cell(tensor, ref, repro)
    del ref
    gc.collect()
    return cells, reference, derived


def _absmean_cell(tensor, ref, repro) -> dict:
    mx = _mx()
    rows, cols, count = tensor.rows, tensor.cols, tensor.count
    master = tensor.master["bytes"]
    values, boundary, mean, s = f.absmean_bf16(master, count)
    result_raw = mx.compare(ref.values, values, rows, cols)
    ties = mx.explain_ties(master, count, ref.words[0], ref.values, values, result_raw)
    result = mx.result_dict(result_raw)
    i2s = tensor.stored["i2_s"]()
    against_i2s_raw = mx.compare(i2s.values, values, rows, cols)
    mx.explain_ties(master, count, ref.words[0], i2s.values, values, against_i2s_raw)
    against_i2s = mx.result_dict(against_i2s_raw)
    packed_i2s = mx.result_dict(mx.compare(ref.values, i2s.values, rows, cols))
    tie_value = f.bf16_value(ties["tie_word"])
    cell = {"id": _cell_id(tensor, "bf16_absmean"), "tensor": tensor.id, "format": "bf16_absmean",
            "provenance": "derived", "evidence": "derived",
            "derivation": ("transformers WeightQuant on the bf16 master weights: s = 1 / max(mean|w|, 1e-5), "
                           "round(w * s) half to even, clamp to [-1, 1] (t27 tf_absmean_bf16, f64)"),
            "source": tensor.master["source"],
            "status": mx.STATUS[result["status"]],
            "trits": _trits_entry(count, result, mx.EXPLAIN),
            "scales": {"compared": False, "mean_abs": mean, "s": s,
                       "note": "the derivation has no stored scale word; the packed weight_scale is compared in the "
                               "reference cell"},
            "boundary_weights": boundary,
            "ties": {"tie_word": ties["tie_word"], "tie_value": tie_value, "tie_times_s": tie_value * s,
                     "positive": {"packed_nonzero": ties["pos_nonzero"], "packed_zero": ties["pos_zero"]},
                     "negative": {"packed_nonzero": ties["neg_nonzero"], "packed_zero": ties["neg_zero"]},
                     "differ_at_ties": ties["differ_at_ties"], "differ_elsewhere": ties["differ_elsewhere"],
                     "first_elsewhere": ties["first_elsewhere"], "derived_nonzero_at_ties": ties["derived_nonzero"]}}
    triple = {"tensor": tensor.id,
              "bf16_absmean_vs_hf_packed": _trits_entry(count, result, mx.EXPLAIN),
              "hf_packed_vs_i2_s": _trits_entry(count, packed_i2s, mx.EXPLAIN),
              "bf16_absmean_vs_i2_s": _trits_entry(count, against_i2s, mx.EXPLAIN),
              "holds": {"packed_equals_i2_s": packed_i2s["trits_differ"] == 0,
                        "absmean_equals_packed": result["trits_differ"] == 0}}
    cell["triple_check"] = triple
    if result["status"] == mx.MISMATCH:
        repro[cell["id"]] = _tie_repro(tensor, ref, i2s, values, cell, mean, s)
        cell["repro"] = str(REPRO / f"{cell['id']}.json")
    del values, i2s
    return cell


# ---- minimal reproductions ------------------------------------------------------------

def _code_at(stored: Stored, fmt: int, group: int, tensor: Tensor, index: int) -> dict:
    """Where t27 reads weight `index` (tmx_locate) and the trit it decodes there."""
    mx = _mx()
    at = mx.locate(fmt, tensor.rows, tensor.cols, group, index)
    byte = stored.code_bytes[at["byte"]]
    where = {2: "lowest bit of the 2-bit code", 1: "bit", 0: "base-3 digit (most significant first)"}
    out = {"file_offset": stored.extra["begin"] + at["byte"], "byte_hex": f"{byte:02x}",
           "position": at["position"], "position_is": where[at["width"]], "trit": stored.values[index]}
    if at["scale_byte"] >= 0:
        width = 4 if fmt == f.I2_S else 2
        out["scale_file_offset"] = stored.extra["begin"] + at["scale_byte"]
        out["scale_bytes_hex"] = bytes(stored.code_bytes[at["scale_byte"]: at["scale_byte"] + width]).hex()
    return out


def _weight(tensor: Tensor, index: int) -> dict:
    return {"index": index, "row": index // tensor.cols, "col": index % tensor.cols}


def _scale_repro(tensor, ref, stored, fmt, group, result) -> dict:
    mx = _mx()
    word = stored.words[0]
    first = result["scales_first"]
    sample = [dict(_weight(tensor, i), reference=_code_at(ref, f.HF_PACKED, 0, tensor, i),
                   stored=_code_at(stored, fmt, group, tensor, i)) for i in range(first, first + 2)]
    return {"schema": REPRO_SCHEMA, "cell": _cell_id(tensor, stored.column), "status": "mismatch",
            "component": "scales", "explanation": mx.EXPLAIN[result["scales_explain"]],
            "explanation_text": EXPLANATIONS[mx.EXPLAIN[result["scales_explain"]]],
            "weights": {"compared": tensor.count, "scales_differ": result["scales_differ"], "first": first,
                        "trits_differ": result["trits_differ"]},
            "reference": {"column": tensor.reference, "source": ref.source[1],
                          "bytes_hex": ref.extra["scale_bytes"], "word": ref.words[0], "dtype": KIND_NAME[ref.kind],
                          "value": f.bf16_value(ref.words[0])},
            "stored": {"column": stored.column, "source": stored.source[0],
                     "file_offset": stored.extra["begin"] + tensor.count // 4, "bytes_hex": stored.extra["scale_bytes"],
                     "word": word, "dtype": KIND_NAME[stored.kind], "value": f.f32_value(word),
                     "bf16_rounding": mx.bf16_round(word)},
            "sample_weights": sample,
            "check": ("read the two scale words at the offsets above: the bf16 word equals the f32 word rounded to "
                      "nearest-even (t27 tmx_bf16_round), and the codes of the sampled weights give the same trits")}


def _generic_repro(tensor, ref, stored, fmt, group, result) -> dict:
    """First differing weights of a published cell with their bytes and scale words."""
    mx = _mx()
    ref_fmt = next(c[1] for c in COLUMNS if c[0] == tensor.reference)
    ref_group = next(c[3] for c in COLUMNS if c[0] == tensor.reference)
    indices = mx.mismatch_indices(ref.values, stored.values, tensor.count, SAMPLES)
    if result["scales_first"] >= 0:
        indices = sorted(set(indices + [result["scales_first"]]))

    def weight(i):
        return dict(_weight(tensor, i), reference=_code_at(ref, ref_fmt, ref_group, tensor, i),
                    stored=_code_at(stored, fmt, group, tensor, i),
                    reference_scale=ref.words[mx.scale_index(i, tensor.cols, ref.group)],
                    stored_scale=stored.words[mx.scale_index(i, tensor.cols, stored.group)])
    return {"schema": REPRO_SCHEMA, "cell": _cell_id(tensor, stored.column), "status": "mismatch",
            "component": "trits" if result["trits_differ"] else "scales",
            "explanation": mx.EXPLAIN[result["trits_explain"] or result["scales_explain"]],
            "explanation_text": EXPLANATIONS[mx.EXPLAIN[result["trits_explain"] or result["scales_explain"]]],
            "weights": {"compared": tensor.count, "trits_differ": result["trits_differ"],
                        "trits_first": result["trits_first"], "scales_differ": result["scales_differ"],
                        "scales_first": result["scales_first"]},
            "reference": {"column": tensor.reference, "source": ref.source},
            "stored": {"column": stored.column, "source": stored.source},
            "sample_weights": [weight(i) for i in indices]}


def _tie_repro(tensor, ref, i2s, derived, cell, mean, s) -> dict:
    mx = _mx()
    master = tensor.master["bytes"]
    tie = cell["ties"]["tie_word"]

    def master_at(i):
        word = master[2 * i] | master[2 * i + 1] << 8
        return {"file_offset": tensor.master["begin"] + 2 * i, "bytes_hex": master[2 * i: 2 * i + 2].hex(),
                "word": word, "value": f.bf16_value(word)}

    def weight(i):
        return dict(_weight(tensor, i), master=master_at(i), packed=_code_at(ref, f.HF_PACKED, 0, tensor, i),
                    i2_s=_code_at(i2s, f.I2_S, 0, tensor, i), absmean_trit=derived[i])

    first = mx.mismatch_indices(ref.values, derived, tensor.count, SAMPLES)
    same_value = {}
    for sign, word in (("+", tie), ("-", tie | 0x8000)):
        for label, trit in (("packed_nonzero", 1 if sign == "+" else -1), ("packed_zero", 0)):
            same_value[f"{sign}tie_{label}"] = [weight(i) for i in mx.word_indices(master, tensor.count, word,
                                                                                  ref.values, trit, 2)]
    return {"schema": REPRO_SCHEMA, "cell": cell["id"], "status": "mismatch", "component": "trits",
            "explanation": cell["trits"]["explanation"], "explanation_text": EXPLANATIONS[cell["trits"]["explanation"]],
            "weights": {"compared": tensor.count, "trits_differ": cell["trits"]["differ"],
                        "first": cell["trits"]["first"]},
            "weight_scale": {"word": ref.words[0], "value": f.bf16_value(ref.words[0]), "source": ref.source[1]},
            "tie": {"word": tie, "value": cell["ties"]["tie_value"], "times_s": cell["ties"]["tie_times_s"]},
            "absmean": {"mean_abs": mean, "s": s},
            "counts_at_ties": {"positive": cell["ties"]["positive"], "negative": cell["ties"]["negative"],
                               "differ_elsewhere": cell["ties"]["differ_elsewhere"]},
            "first_mismatches": [weight(i) for i in first],
            "same_bf16_value_both_trits": same_value,
            "check": ("the listed weights share one bf16 master word, yet the packed checkpoint (and I2_S) store "
                      "+-1 for some and 0 for others; WeightQuant gives 0 for all of them (|w * s| < 0.5)")}


# ---- the report ----------------------------------------------------------------------

def _upstream_meta() -> dict:
    lock = json.loads(UPSTREAM_LOCK.read_text())
    wanted = ("quantize_row_tq1_0_ref", "dequantize_row_tq1_0", "nearest_int")
    functions = [f"{e['file']}:{e['first']}-{e['last']}" for e in lock["extracts"]
                 if any(e["first_line"].startswith(p) or p in e["first_line"] for p in wanted)]
    return {"repo": lock["repo"], "commit": lock["commit"], "lock": "tests/upstream/llama.cpp.lock.json",
            "files": {path: meta["sha256"] for path, meta in sorted(lock["files"].items())},
            "functions": functions,
            "writer": "tests/upstream/encode_llamacpp_tq.c, built by tests/upstream/run-llamacpp-matrix.sh",
            "input": ("w = t * weight_scale in float: t decoded from the HF packed checkpoint by t27, weight_scale "
                      "its bf16 value (exact in f16); one quantize call per row"),
            "applies_to": "BitNet tensors; Bonsai tensors are not representable in TQ1_0 or TQ2_0"}


def _taxonomy() -> dict:
    mx = _mx()
    return {"cell_status": {token: code for code, token in mx.STATUS.items()},
            "explanations": {token: {"code": code, "text": EXPLANATIONS[token]}
                             for code, token in mx.EXPLAIN.items() if token},
            "reasons": {token: {"code": slot, "unit": REASON_UNITS[token]} for slot, token in enumerate(mx.REASONS)},
            "provenance": PROVENANCE,
            "errors": {token: status for status, token in sorted(f.TOKENS.items(), reverse=True)},
            "flags": {token: slot for slot, token in enumerate(f.FLAGS)},
            "source": "t27/matrix.t27 (TMX_*) and t27/formats.t27 (TF_ERR_*, TF_FLAG_*); specs/formats/OWNERS.md"}


def _formats() -> list:
    mx = _mx()
    out = []
    for column, fmt, name, group, upstream in COLUMNS:
        out.append({"id": column, "name": name, "t27_format": fmt, "scale_dtype": KIND_NAME[mx.scale_kind(fmt)],
                    "scale_group": mx.scale_group(fmt, group), "group_argument": group,
                    "writer": f"llama.cpp quantize_row_{upstream}_ref" if upstream else "t27"})
    return out


def _summary(cells, derived) -> dict:
    by_status, by_family, by_provenance = {}, {}, {}
    for cell in cells:
        by_status[cell["status"]] = by_status.get(cell["status"], 0) + 1
        by_provenance[cell["provenance"]] = by_provenance.get(cell["provenance"], 0) + 1
    t27_format = {column: fmt for column, fmt, _, _, _ in COLUMNS}
    for cell in cells:
        family = cell["tensor"].split("-")[0]
        entry = by_family.setdefault(family, {"columns": 0, "storage_formats": 0, "third_party_formats": set()})
        if cell["evidence"] == "third-party":
            entry["third_party_formats"].add(cell["format"])
    for family, entry in by_family.items():
        columns = {c["format"] for c in cells if c["tensor"].startswith(family)}
        entry["columns"] = len(columns)
        entry["storage_formats"] = len({t27_format[column] for column in columns})
        entry["third_party_formats"] = sorted(entry["third_party_formats"])
    return {"tensors": len({c["tensor"] for c in cells}), "columns": len(COLUMNS),
            "storage_formats": len(set(t27_format.values())), "cells": len(cells),
            "status": dict(sorted(by_status.items())), "provenance": dict(sorted(by_provenance.items())),
            "families": by_family, "derived_cells": len(derived),
            "mismatches_with_repro": sum(1 for c in cells + derived if c["status"] == "mismatch" and "repro" in c)}


def run(upstream_dir: Path = UPSTREAM) -> tuple[dict, dict]:
    """(report, {cell id: repro}); every range read is in fixtures/manifest.json."""
    from . import matvec as mv
    tensors_out, cells, derived, repro = [], [], [], {}
    loaders = [lambda pair=pair: bitnet_tensor(*pair) for pair in BITNET_TENSORS]
    loaders += [lambda pair=pair: bonsai_tensor(*pair) for pair in BONSAI_TENSORS]
    for load in loaders:
        tensor = load()
        tensor_cells_, reference, absmean = tensor_cells(tensor, upstream_dir, repro)
        entry = {"id": tensor.id, "model": tensor.model, "family": tensor.family, "names": tensor.names,
                 "shape": [tensor.rows, tensor.cols], "weights": tensor.count, "reference": reference}
        if tensor.runtime:
            entry["runtime_requirements"] = tensor.runtime
        tensors_out.append(entry)
        cells += tensor_cells_
        if absmean:
            derived.append(absmean)
        del tensor
        gc.collect()
    lock = (ROOT / "native" / "compiler.lock").read_text().strip()
    report = {"schema": SCHEMA,
              "issue": "dmitrii-f-t27/trinity-memory#32",
              "reproduce": "make ternary-check (python3 -m trinity_memory.ternary_check --check compares)",
              "compiler": {"repo": "gHashTag/t27", "commit": lock},
              "fixtures": "fixtures/manifest.json, consumer ternary_check",
              "run_metadata": "excluded: timings and the host of a run are written to build/ternary-check/run.json",
              "comparison": ("trits compared value by value; scales compared weight by weight as exact values "
                             "(an f16 and a bf16 word of the same number are equal)"),
              "taxonomy": _taxonomy(),
              "upstream": {"llama.cpp": _upstream_meta()},
              "formats": _formats(),
              "tensors": tensors_out,
              "cells": cells,
              "derived": derived,
              "summary": _summary(cells, derived),
              "matvec": mv.run()}
    return report, repro


def dumps(value) -> str:
    return json.dumps(value, indent=1) + "\n"


def outputs(report: dict, repro: dict) -> dict:
    """Relative path -> text of every file the run writes into the repository."""
    from . import ternary_check_html as html
    files = {str(REPORT): dumps(report), str(HTML): html.render(report)}
    for cell_id, content in sorted(repro.items()):
        files[str(REPRO / f"{cell_id}.json")] = dumps(content)
    return files


def write(files: dict, directory: Path) -> None:
    """Writes the files under `directory` (their paths below reports/), and removes repro files of cells that no
    longer mismatch."""
    for path, text in files.items():
        target = directory / Path(path).relative_to("reports")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    repro_dir = directory / REPRO.relative_to("reports")
    wanted = {(directory / Path(path).relative_to("reports")).name for path in files if path.startswith(str(REPRO))}
    for stale in repro_dir.glob("*.json"):
        if stale.name not in wanted:
            os.remove(stale)


def check(files: dict) -> list:
    """Committed files that differ from `files`, or that exist without a counterpart."""
    bad = [path for path, text in files.items() if not (ROOT / path).is_file() or (ROOT / path).read_text() != text]
    committed = {str(p.relative_to(ROOT)) for p in (ROOT / REPRO).glob("*.json")}
    return bad + sorted(committed - set(files))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m trinity_memory.ternary_check", description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--output", type=Path, default=ROOT / "build",
                        help="directory for ternary-check.json, ternary-check.html and ternary-check/repro/ "
                             "(default build/)")
    target.add_argument("--write", action="store_true", help="write the committed files under reports/")
    target.add_argument("--check", action="store_true", help="compare with the committed files under reports/")
    parser.add_argument("--upstream", type=Path, default=UPSTREAM, help="the llama.cpp-encoded tensors")
    args = parser.parse_args(argv)
    started = time.time()
    try:
        report, repro = run(args.upstream)
    except (fx.FixtureError, f.FormatError) as error:
        print(f"ternary check: {error}", file=sys.stderr)
        if isinstance(error, fx.CacheMiss):
            print("ternary check: make ternary-check fetches the fixtures, builds t27 and runs the llama.cpp "
                  "writers before this step (OFFLINE=1 for the caches only)", file=sys.stderr)
        return 2
    files = outputs(report, repro)
    seconds = round(time.time() - started, 2)
    RUN.parent.mkdir(parents=True, exist_ok=True)
    RUN.write_text(dumps({"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "seconds": seconds,
                          "host": {"system": platform.system(), "machine": platform.machine(),
                                   "python": platform.python_version()},
                          "excluded_from": [str(REPORT), str(HTML), str(REPRO)]}))
    print(f"ternary check computed in {seconds} s: {report['summary']['cells']} cells, "
          f"{report['summary']['status']}", file=sys.stderr)
    if args.check:
        bad = check(files)
        for path in bad:
            print(f"{path} differs from the recomputed report", file=sys.stderr)
        if not bad:
            print(f"{len(files)} committed files reproduced: {', '.join(sorted(files))}", file=sys.stderr)
        return 1 if bad else 0
    directory = ROOT / "reports" if args.write else args.output
    write(files, directory)
    print(f"wrote {len(files)} files under {directory}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
