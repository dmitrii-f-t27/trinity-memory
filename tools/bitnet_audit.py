#!/usr/bin/env python3
"""Standalone cross-check of the BitNet b1.58 2B4T observations in docs/ternary-check.md.

Python standard library only; no t27 build needed. It is an independent
check for third parties, not the product decoder (that is t27/formats.t27).
It reads file headers and small byte ranges of the three pinned Hugging Face
files over HTTP range requests (about 60 MB, mostly the two layer-0 bf16
tensors) and prints a JSON summary:

  scales    all ternary tensors: packed bf16 weight_scale vs the f32 scale after
            each I2_S tensor, and whether the bf16 value is the f32 value
            rounded to nearest-even bf16;
  trailers  nonzero bytes among the 28 that follow each I2_S scale, and the
            nearest earlier tensor in the file whose bytes at the same offset
            are identical (the quantizer reuses one output buffer);
  layer0    packed trits vs transformers WeightQuant on the bf16 master weights
            for layer 0 q_proj and down_proj, and how the packed file treats
            the weights whose |w| is exactly 0.5 * weight_scale.

Usage: python3 tools/bitnet_audit.py [--cache build/fixtures] > audit.json
With --cache, byte ranges already fetched by trinity_memory.fixtures are
read from disk instead of the network.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import statistics
import struct
import urllib.request

FILES = {
    "packed": ("microsoft/bitnet-b1.58-2B-4T", "04c3b9ad9361b824064a1f25ea60a8be9599b127", "model.safetensors"),
    "bf16": ("microsoft/bitnet-b1.58-2B-4T-bf16", "276681394656abdadb8e80e5b2c3db5e5d7fcaff", "model.safetensors"),
    "gguf": ("microsoft/bitnet-b1.58-2B-4T-gguf", "a1f2f1c765812aa8af3f6eda4a313707064bba15", "ggml-model-i2_s.gguf"),
}
GGUF_TO_HF = {"attn_q": "self_attn.q_proj", "attn_k": "self_attn.k_proj", "attn_v": "self_attn.v_proj",
              "attn_output": "self_attn.o_proj", "ffn_gate": "mlp.gate_proj", "ffn_up": "mlp.up_proj",
              "ffn_down": "mlp.down_proj"}


class Source:
    def __init__(self, key, cache):
        self.repo, self.revision, self.filename = FILES[key]
        self.url = f"https://huggingface.co/{self.repo}/resolve/{self.revision}/{self.filename}"
        self.cache = cache and Path(cache) / self.repo.replace("/", "--") / self.revision / self.filename

    def read(self, begin, end):
        if self.cache and self.cache.is_dir():
            prefix = self.cache / "prefix.bin"
            if prefix.is_file() and prefix.stat().st_size >= end:
                with open(prefix, "rb") as handle:
                    handle.seek(begin)
                    return handle.read(end - begin)
            for path in self.cache.glob("*-*.bin"):
                low, high = (int(x) for x in path.stem.split("-"))
                if low <= begin and end <= high:
                    with open(path, "rb") as handle:
                        handle.seek(begin - low)
                        return handle.read(end - begin)
        request = urllib.request.Request(self.url, headers={"Range": f"bytes={begin}-{end - 1}"})
        with urllib.request.urlopen(request, timeout=120) as response:
            data = response.read()
        assert len(data) == end - begin, (self.url, begin, end, len(data))
        return data


def safetensors_header(src):
    size = struct.unpack("<Q", src.read(0, 8))[0]
    header = json.loads(src.read(8, 8 + size))
    return {k: (v["dtype"], v["shape"], 8 + size + v["data_offsets"][0], 8 + size + v["data_offsets"][1])
            for k, v in header.items() if k != "__metadata__"}


def gguf_tensors(src, size=8 << 20):
    """(name, dims, ggml type, absolute offset) of every tensor record; the
    header prefix is read in growing chunks until it parses."""
    try:
        return _gguf_tensors(src.read(0, size))
    except (struct.error, IndexError, UnicodeDecodeError):
        return gguf_tensors(src, size * 2)


def _gguf_tensors(data):
    magic, version, tensors, keys = struct.unpack_from("<IIQQ", data, 0)
    assert magic == 0x46554747 and version == 3
    pos, alignment = 24, 32
    widths = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}

    def text(at):
        length = struct.unpack_from("<Q", data, at)[0]
        return data[at + 8: at + 8 + length].decode(), at + 8 + length

    def skip(at, vtype):
        if vtype in widths:
            return at + widths[vtype]
        if vtype == 8:
            return text(at)[1]
        element, count = struct.unpack_from("<IQ", data, at)
        at += 12
        if element in widths:
            return at + count * widths[element]
        for _ in range(count):
            at = skip(at, element)
        return at

    for _ in range(keys):
        key, pos = text(pos)
        vtype = struct.unpack_from("<I", data, pos)[0]
        if key == "general.alignment":
            alignment = struct.unpack_from("<I", data, pos + 4)[0]
        pos = skip(pos + 4, vtype)
    records = []
    for _ in range(tensors):
        name, pos = text(pos)
        dims = struct.unpack_from("<I", data, pos)[0]
        shape = struct.unpack_from(f"<{dims}Q", data, pos + 4)
        ggml_type, offset = struct.unpack_from("<IQ", data, pos + 4 + 8 * dims)
        pos += 4 + 8 * dims + 12
        records.append((name, list(shape), ggml_type, offset))
    start = (pos + alignment - 1) // alignment * alignment
    return [(n, s, t, start + o) for n, s, t, o in records]


def bf16_value(bits):
    return struct.unpack("<f", struct.pack("<I", bits << 16))[0]


def to_bf16_rne(value):
    bits = struct.unpack("<I", struct.pack("<f", value))[0]
    return (bits + 0x7FFF + ((bits >> 16) & 1)) >> 16


def scales_and_trailers(packed_header, gguf_records, gguf, packed):
    def one(record):
        name, shape, ggml_type, offset = record
        blk, layer, kind, _ = name.split(".")
        count = shape[0] * shape[1]
        tail = gguf.read(offset + count // 4, offset + count // 4 + 32)
        _, _, begin, end = packed_header[f"model.layers.{layer}.{GGUF_TO_HF[kind]}.weight_scale"]
        word = struct.unpack("<H", packed.read(begin, end))[0]
        f32 = struct.unpack("<f", tail[:4])[0]
        return {"tensor": name, "packed_bf16": bf16_value(word), "i2s_f32": f32,
                "bf16_is_rounded_f32": to_bf16_rne(f32) == word,
                "trailer_nonzero": sum(1 for b in tail[4:] if b)}

    records = [r for r in gguf_records if r[2] == 36]
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(one, records))
    ordered = sorted(gguf_records, key=lambda r: r[3])
    sizes = {0: 4, 1: 2}

    def stored(record):
        count = record[1][0] * (record[1][1] if len(record[1]) > 1 else 1)
        return count // 4 + 32 if record[2] == 36 else count * sizes.get(record[2], 0)

    def source(index):
        name, shape, _, offset = ordered[index]
        at = shape[0] * shape[1] // 4 + 4
        tail = gguf.read(offset + at, offset + at + 28)
        for earlier in reversed(ordered[:index]):
            if stored(earlier) >= at + 28:
                same = gguf.read(earlier[3] + at, earlier[3] + at + 28) == tail
                return name, earlier[0] if same else None
        return name, None

    indices = [i for i, r in enumerate(ordered) if r[2] == 36]
    with ThreadPoolExecutor(max_workers=8) as pool:
        sources = dict(pool.map(source, indices))
    for row in rows:
        row["trailer_equals_bytes_of"] = sources[row["tensor"]]
    rel = [abs(r["i2s_f32"] - r["packed_bf16"]) / r["packed_bf16"] for r in rows]
    return {"tensors": len(rows), "equal": sum(1 for r in rows if r["i2s_f32"] == r["packed_bf16"]),
            "bf16_is_rounded_f32": sum(1 for r in rows if r["bf16_is_rounded_f32"]),
            "relative_difference_median": statistics.median(rel), "relative_difference_max": max(rel),
            "trailers_with_nonzero_bytes": sum(1 for r in rows if r["trailer_nonzero"]),
            "trailers_equal_to_an_earlier_tensor": sum(1 for r in rows if r["trailer_equals_bytes_of"]),
            "rows": rows}


def layer0(packed_header, bf16_header, packed, master, name):
    dtype, shape, begin, end = packed_header[name]
    rows, cols = shape[0] * 4, shape[1]
    raw = packed.read(begin, end)
    _, _, sb, se = packed_header[name + "_scale"]
    scale = bf16_value(struct.unpack("<H", packed.read(sb, se))[0])
    _, _, mb, me = bf16_header[name]
    words = struct.unpack(f"<{rows * cols}H", master.read(mb, me))
    values = [bf16_value(w) for w in words]
    mean = sum(abs(v) for v in values) / len(values)
    inverse = 1.0 / max(mean, 1e-5)
    stride = rows // 4
    differ = at_half = at_half_nonzero = 0
    for i in range(rows):
        slot, r = divmod(i, stride)
        base = i * cols
        for k in range(cols):
            packed_trit = ((raw[r * cols + k] >> (2 * slot)) & 3) - 1
            x = values[base + k] * inverse
            online = 1 if x > 0.5 else (-1 if x < -0.5 else 0)
            if packed_trit != online:
                differ += 1
            if abs(values[base + k]) == 0.5 * scale:
                at_half += 1
                at_half_nonzero += packed_trit != 0
    return {"tensor": name, "weights": rows * cols, "weight_scale_bf16": scale, "mean_abs_bf16": mean,
            "trits_differ_from_weightquant": differ, "weights_at_half_scale": at_half,
            "of_which_packed_nonzero": at_half_nonzero}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--cache", help="directory of ranges cached by trinity_memory.fixtures")
    args = parser.parse_args()
    packed, master, gguf = (Source(k, args.cache) for k in ("packed", "bf16", "gguf"))
    packed_header, bf16_header = safetensors_header(packed), safetensors_header(master)
    gguf_records = gguf_tensors(gguf)
    report = {"sources": {k: dict(zip(("repo", "revision", "file"), v)) for k, v in FILES.items()},
              "scales": scales_and_trailers(packed_header, gguf_records, gguf, packed),
              "layer0": [layer0(packed_header, bf16_header, packed, master, n)
                         for n in ("model.layers.0.self_attn.q_proj.weight", "model.layers.0.mlp.down_proj.weight")]}
    print(json.dumps(report, indent=1))
