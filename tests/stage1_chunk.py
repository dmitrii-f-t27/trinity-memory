"""The #64 chunk: BitNet b1.58 2B4T layer-0 q_proj rows 0-319 and its reference accumulators.

The trits come from the pinned packed Hugging Face checkpoint through the t27 decoders
(trinity_memory.formats), as tools/extract-bram-trits.py takes them; the activations are
tmv_activations(seed 27) of t27/matvec.t27; the reference is t27/matvec.t27's product of the
whole [2560, 2560] q_proj, whose sha256 must equal the committed report
reports/ternary-check/matvec-2026-09-23.json (hf_packed accumulators), and the chunk's
reference is its first 320 accumulators. Fixture bytes come from the cache only
(TRINITY_FIXTURES_OFFLINE=1); a missing cache file raises fixtures.CacheMiss, which the
tests turn into a skip (or a failure with TRINITY_REQUIRE_CACHED=1). The result is kept in
memory for the test process.
"""
from __future__ import annotations

import ctypes as C
import hashlib
import json
import os
import struct
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "ternary-check" / "matvec-2026-09-23.json"
TENSOR = "model.layers.0.self_attn.q_proj.weight"
ROWS, COLS, SEED = 320, 2560, 27


@lru_cache(maxsize=1)
def chunk():
    """dict: trits (list of 819,200 ints), x (2,560 ints), y (320 ints), full_sha256 (of all 2,560)."""
    os.environ.setdefault("TRINITY_FIXTURES_OFFLINE", "1")
    from trinity_memory import fixtures as fx
    from trinity_memory import formats as f
    from trinity_memory import matvec as mv
    from trinity_memory.ternary_check import BITNET
    info, dtype, packed = fx.safetensors_tensor(BITNET["packed"], TENSOR)
    rows, cols = info.d0 * 4, info.d1
    assert (dtype, rows, cols) == ("U8", 2560, 2560), (dtype, rows, cols)
    values, outside = f.decode_hf_packed(packed, rows, cols)
    assert outside == 0
    x = mv.activations(cols, SEED)
    _, y = mv.matvec((C.c_int32 * len(values))(*values), rows, cols, x, 64)
    full = list(y)
    digest = hashlib.sha256(struct.pack(f"<{len(full)}q", *full)).hexdigest()
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    expected = report["tensors"][0]["formats"]["hf_packed"]["accumulators"]["sha256_le"]
    assert report["tensors"][0]["tensor"]["hf"] == TENSOR
    return {"trits": list(values[:ROWS * COLS]), "x": list(x), "y": full[:ROWS], "full_sha256": digest,
            "report_sha256": expected, "first8": report["tensors"][0]["formats"]["hf_packed"]["accumulators"]["first"]}
