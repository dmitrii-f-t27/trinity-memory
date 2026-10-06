#!/usr/bin/env python3
"""Generate reports/prism_hadamard/inputs.json (issues #49 and #52).

Everything here comes from an oracle that is independent of the t27 code:

  rotation   the numerators come from a fast Walsh-Hadamard butterfly in plain
             Python integers (the spec sums H[k][j] directly), the divisor from
             an integer square root, the f64 bit patterns from exact Fractions;
  metadata   GGUF headers are written by the byte builder below, and the status
             each header must get is the rejection class its mutation was made
             to trigger (the fork's rules, listed in t27/live.t27), written next
             to the mutation, never read back from the t27 code.

tools/evidence/prism_hadamard.py then runs the t27 functions, generated to C by
the pinned compiler, over these vectors and over conformance/hadamard_rotate.json.

  python3 tools/generate-prism-hadamard-vectors.py [--check]
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import random
import struct
import sys
from fractions import Fraction

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "reports/prism_hadamard/inputs.json"

U32, I32, BOOL, STR, ARR = 4, 5, 7, 8, 9
OK, TYPE, MISSING, VERSION, BLOCK, TRANSFORM, SIGNS, ARCH, NAME, TENSOR, TIED = (
    0, -63, -64, -65, -66, -67, -68, -69, -70, -71, -88)
STATUS_NAMES = {0: "ok", -63: "TLV_ERR_HADAMARD_TYPE", -64: "TLV_ERR_HADAMARD_MISSING",
                -65: "TLV_ERR_HADAMARD_VERSION", -66: "TLV_ERR_HADAMARD_BLOCK",
                -67: "TLV_ERR_HADAMARD_TRANSFORM", -68: "TLV_ERR_HADAMARD_SIGNS",
                -69: "TLV_ERR_HADAMARD_ARCH", -70: "TLV_ERR_HADAMARD_NAME",
                -71: "TLV_ERR_HADAMARD_TENSOR", -88: "TLV_ERR_HADAMARD_TIED"}


# ---- rotation oracle -------------------------------------------------------

def fwht(values):
    """The unnormalised Walsh-Hadamard transform by the in-place butterfly."""
    a = list(values)
    h = 1
    while h < len(a):
        for i in range(0, len(a), 2 * h):
            for j in range(i, i + h):
                a[j], a[j + h] = a[j] + a[j + h], a[j] - a[j + h]
        h *= 2
    return a


def divisor(block):
    """sqrt(block) when it is an integer power of two, else 0 (unsupported)."""
    if block < 1 or block & (block - 1):
        return 0
    r = math.isqrt(block)
    return r if r * r == block else 0


def f64_hex(values):
    return struct.pack(f"<{len(values)}d", *values).hex()


def rotation_vector(vid, block, x, signs):
    numerators = fwht([s * v for s, v in zip(signs, x)])
    div = divisor(block)
    outputs = [Fraction(n, div) for n in numerators]
    floats = [float(f) for f in outputs]
    assert all(Fraction(a) == f for a, f in zip(floats, outputs)), "not exact in f64"
    return {"id": vid, "block": block, "x": x, "signs": signs, "numerators": numerators,
            "divisor": div, "output_f64_le": f64_hex(floats)}


def rotation_vectors():
    rng = random.Random(49052)
    rows = []
    for block in (16, 64, 256, 1024):
        shapes = {
            "random": [rng.randint(-1024, 1024) for _ in range(block)],
            "random2": [rng.randint(-1024, 1024) for _ in range(block)],
            "max": [1024] * block,
            "min": [-1024] * block,
            "impulse0": [1] + [0] * (block - 1),
            "impulse1": [0, 1] + [0] * (block - 2),
            "impulse_last": [0] * (block - 1) + [-1],
            "ramp": [i - block // 2 for i in range(block)],
        }
        runs, s = [], 1
        for i in range(block):
            if i % 7 == 0:
                s = -s
            runs.append(s)
        layouts = {"identity": [1] * block, "runs": runs,
                   "random": [rng.choice((-1, 1)) for _ in range(block)],
                   "alternating": [1 if i % 2 == 0 else -1 for i in range(block)]}
        for shape, x in shapes.items():
            for layout, signs in layouts.items():
                if layout != "identity" and shape not in ("random", "max", "impulse1", "ramp"):
                    continue
                rows.append(rotation_vector(f"b{block}-{shape}-{layout}", block, x, signs))
    return rows


def normalisation_vectors():
    blocks = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 0, 3, 12, 24, 48, 1000]
    return [{"block": b, "divisor": divisor(b)} for b in blocks]


# ---- GGUF header builder (little endian, version 3) ------------------------

def _str(s):
    raw = s.encode() if isinstance(s, str) else s
    return struct.pack("<Q", len(raw)) + raw


def kv(name, vtype, payload):
    return _str(name) + struct.pack("<I", vtype) + payload


def kv_u32(name, v): return kv(name, U32, struct.pack("<I", v))
def kv_bool(name, v): return kv(name, BOOL, bytes([1 if v else 0]))
def kv_str(name, v): return kv(name, STR, _str(v))


def kv_strs(name, values):
    return kv(name, ARR, struct.pack("<IQ", STR, len(values)) + b"".join(_str(v) for v in values))


def kv_ints(name, elem, values):
    return kv(name, ARR, struct.pack("<IQ", elem, len(values))
              + b"".join(struct.pack("<I", v & 0xffffffff) for v in values))


def tensor(name, ne0, ne1, offset):
    return _str(name) + struct.pack("<I", 2) + struct.pack("<QQ", ne0, ne1) + struct.pack("<IQ", 1, offset)


def gguf(keys, tensors, data_bytes=64):
    out = bytearray(struct.pack("<IIQQ", 0x46554747, 3, len(tensors), len(keys)))
    for k in keys:
        out += k
    for t in tensors:
        out += t
    while len(out) % 32:
        out.append(0)
    return bytes(out), len(out) + data_bytes


WEIGHT = "blk.0.attn_q.weight"


def header(arch="qwen35", version=("u32", 1), block=4, transform="normalized-sylvester-walsh-hadamard",
           axis="input-last-dimension", mode="identity", names=(WEIGHT,), names_kind="strings",
           widths=None, values=None, sign_elem=I32, tied=None, inverse=None, ne0=8, embd_ne0=8,
           output=False, drop_tensor=False, drop=(), keys_extra=(), prism=True):
    """The keys and tensors of a model the fork would rotate, with one change."""
    keys = [kv_str("general.architecture", arch)] if arch is not None else []
    if prism:
        if version is not None:
            keys.append(kv_u32("prism.hadamard.version", version[1]) if version[0] == "u32"
                        else kv("prism.hadamard.version", 10, struct.pack("<Q", version[1])))
        if tied is not None:
            keys.append(kv_bool("prism.hadamard.tied_output", tied) if tied != "u32"
                        else kv_u32("prism.hadamard.tied_output", 1))
        if "block_size" not in drop:
            keys.append(kv_u32("prism.hadamard.block_size", block))
        if "transform" not in drop:
            keys.append(kv_u32("prism.hadamard.transform", 1) if transform == "u32"
                        else kv_str("prism.hadamard.transform", transform))
        if "axis" not in drop:
            keys.append(kv_str("prism.hadamard.axis", axis))
        if "sign_mode" not in drop:
            keys.append(kv_u32("prism.hadamard.sign_mode", 1) if mode == "u32"
                        else kv_str("prism.hadamard.sign_mode", mode))
        if "weight_names" not in drop:
            keys.append(kv_ints("prism.hadamard.weight_names", I32, [1]) if names_kind == "ints"
                        else kv_strs("prism.hadamard.weight_names", list(names)))
        if widths is not None:
            keys.append(kv_ints("prism.hadamard.sign_widths", sign_elem, widths))
        if values is not None:
            keys.append(kv_ints("prism.hadamard.sign_values", sign_elem, values))
        if inverse is not None:
            keys.append(kv_strs("prism.hadamard.inverse_weight_names", list(inverse)))
    keys += list(keys_extra)
    tensors, offset = [], 0
    if not drop_tensor:
        seen = set()
        for n in names:
            if n in seen or n == "token_embd.weight":
                continue
            seen.add(n)
            tensors.append(tensor(n, ne0, 2, offset))
            offset += (ne0 * 4 + 31) // 32 * 32
    tensors.append(tensor("token_embd.weight", embd_ne0, 4, offset))
    offset += (embd_ne0 * 8 + 31) // 32 * 32
    if output:
        tensors.append(tensor("output.weight", embd_ne0, 4, offset))
        offset += (embd_ne0 * 8 + 31) // 32 * 32
    return gguf(keys, tensors, offset)


def metadata_cases():
    # expected_present: the fork reads tied_output (:1197) before the version, so
    # a tied_output of the wrong type is refused before a rotation is "present".
    # (id, expected status, kwargs). Order: models the contract accepts, then one
    # mutation per rejection class.
    pm = [1, -1, 1, 1, -1, -1, 1, -1]
    cases = [
        ("accept-identity-b4", OK, {}),
        ("accept-no-prism-keys", OK, {"prism": False}),
        ("accept-block-16", OK, {"block": 16, "ne0": 32}),
        ("accept-block-64", OK, {"block": 64, "ne0": 128}),
        ("accept-block-1024", OK, {"block": 1024, "ne0": 2048}),
        ("accept-two-weights", OK, {"names": (WEIGHT, "blk.1.ffn_down.weight")}),
        ("accept-explicit-int32", OK, {"mode": "explicit", "widths": [8], "values": pm}),
        ("accept-explicit-uint32", OK, {"mode": "explicit", "widths": [8], "values": pm, "sign_elem": U32}),
        ("accept-explicit-two-widths", OK, {"mode": "explicit", "widths": [8, 16], "values": pm + [1] * 16,
                                            "names": (WEIGHT, "blk.1.ffn_down.weight")} | {"ne0": 8}),
        ("accept-arch-llama", OK, {"arch": "llama"}),
        ("accept-arch-qwen3", OK, {"arch": "qwen3"}),
        ("accept-arch-qwen3moe", OK, {"arch": "qwen3moe"}),
        ("accept-arch-qwen35moe", OK, {"arch": "qwen35moe"}),
        ("accept-arch-qwen3next", OK, {"arch": "qwen3next"}),
        ("accept-version-2-tied", OK, {"version": ("u32", 2), "tied": True, "inverse": ("token_embd.weight",)}),
        ("accept-version-1-tied-false", OK, {"tied": False}),
        ("accept-identity-ignores-bad-sign-keys", OK, {"widths": [3], "sign_elem": 6}),
        ("accept-with-output-weight", OK, {"output": True}),
        # --- rejections, one class each
        ("reject-version-u64", TYPE, {"version": ("u64", 1)}),
        ("reject-transform-not-string", TYPE, {"transform": "u32"}),
        ("reject-sign-mode-not-string", TYPE, {"mode": "u32"}),
        ("reject-weight-names-not-strings", TYPE, {"names_kind": "ints"}),
        ("reject-tied-output-not-bool", TYPE, {"tied": "u32"}),
        ("reject-block-size-missing", MISSING, {"drop": ("block_size",)}),
        ("reject-transform-missing", MISSING, {"drop": ("transform",)}),
        ("reject-axis-missing", MISSING, {"drop": ("axis",)}),
        ("reject-sign-mode-missing", MISSING, {"drop": ("sign_mode",)}),
        ("reject-weight-names-missing", MISSING, {"drop": ("weight_names",)}),
        ("reject-explicit-without-sign-widths", MISSING, {"mode": "explicit", "values": pm}),
        ("reject-version-3", VERSION, {"version": ("u32", 3)}),
        ("reject-version-0", VERSION, {"version": ("u32", 0)}),
        ("reject-block-zero", BLOCK, {"block": 0}),
        ("reject-block-12", BLOCK, {"block": 12}),
        ("reject-block-24", BLOCK, {"block": 24, "ne0": 24}),
        ("reject-transform-name", TRANSFORM, {"transform": "sylvester"}),
        ("reject-axis-output", TRANSFORM, {"axis": "output"}),
        ("reject-sign-mode-random", TRANSFORM, {"mode": "random"}),
        ("reject-sign-width-not-multiple-of-block", SIGNS, {"mode": "explicit", "widths": [6], "values": [1] * 6}),
        ("reject-sign-width-zero", SIGNS, {"mode": "explicit", "widths": [0], "values": []}),
        ("reject-sign-width-negative", SIGNS, {"mode": "explicit", "widths": [-8], "values": pm}),
        ("reject-sign-value-2", SIGNS, {"mode": "explicit", "widths": [8], "values": [1, 1, 1, 2, 1, 1, 1, 1]}),
        ("reject-sign-values-short", SIGNS, {"mode": "explicit", "widths": [8], "values": pm[:7]}),
        ("reject-sign-values-long", SIGNS, {"mode": "explicit", "widths": [8], "values": pm + [1] * 4}),
        ("reject-sign-widths-empty", SIGNS, {"mode": "explicit", "widths": [], "values": pm}),
        ("reject-arch-gemma3", ARCH, {"arch": "gemma3"}),
        ("reject-arch-missing", ARCH, {"arch": None}),
        ("reject-names-empty", NAME, {"names": ()}),
        ("reject-name-token-embedding", NAME, {"names": ("token_embd.weight",)}),
        ("reject-name-duplicate", NAME, {"names": (WEIGHT, WEIGHT)}),
        ("reject-name-no-layer-number", NAME, {"names": ("blk..attn_q.weight",)}),
        ("reject-name-unknown-kind", NAME, {"names": ("blk.0.attn_norm.weight",)}),
        ("reject-name-is-not-a-tensor", TENSOR, {"drop_tensor": True}),
        ("reject-width-not-divisible-by-block", TENSOR, {"ne0": 10, "block": 4}),
        ("reject-no-sign-vector-for-width", TENSOR, {"mode": "explicit", "widths": [16], "values": [1] * 16}),
        ("reject-tied-with-output-weight", TIED, {"version": ("u32", 2), "tied": True, "output": True,
                                                  "inverse": ("token_embd.weight",)}),
        ("reject-version-2-without-tied-output", TIED, {"version": ("u32", 2)}),
        ("reject-tied-output-with-version-1", TIED, {"tied": True, "inverse": ("token_embd.weight",)}),
    ]
    rows = []
    for cid, expected, kwargs in cases:
        data, file_size = header(**kwargs)
        rows.append({"id": cid, "expected_status": expected, "expected_name": STATUS_NAMES[expected],
                     "expected_present": bool(kwargs.get("prism", True)) and kwargs.get("tied") != "u32", "file_size": file_size,
                     "header_hex": data.hex()})
    return rows


def document():
    return {
        "schema": "trinity.prism-hadamard-evidence.v1",
        "generator": "tools/generate-prism-hadamard-vectors.py",
        "issues": ["dmitrii-f-t27/trinity-memory#49", "dmitrii-f-t27/trinity-memory#52"],
        "note": "independent oracle (butterfly transform, integer sqrt, byte-level GGUF builder); "
                "tools/evidence/prism_hadamard.py runs the generated t27 code over these",
        "rotation": rotation_vectors(),
        "normalisation": normalisation_vectors(),
        "metadata": metadata_cases(),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    text = json.dumps(document(), indent=1) + "\n"
    if args.check:
        ok = OUT.is_file() and OUT.read_text() == text
        print("check:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    OUT.write_text(text)
    doc = json.loads(text)
    print(f"wrote {len(doc['rotation'])} rotation, {len(doc['normalisation'])} normalisation, "
          f"{len(doc['metadata'])} metadata vectors to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
