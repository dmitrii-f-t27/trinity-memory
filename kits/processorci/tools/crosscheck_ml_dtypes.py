#!/usr/bin/env python3
"""Cross-check the E4M3 reference tables against Google's ml_dtypes (float8_e4m3fn, non-saturating).

Needs numpy and ml_dtypes (pip install numpy ml_dtypes); the kit itself does not. Compares every decode,
every BF16 encode, the edge inputs and the seeded binary32 inputs in vectors/ with ml_dtypes' own casts.
The saturating tables have no ml_dtypes counterpart and are covered by the reference model alone.
"""
import sys
from pathlib import Path
import numpy as np, ml_dtypes
V = Path(__file__).resolve().parents[1] / "vectors"
read = lambda name, dt: np.array([int(l, 16) for l in (V / name).read_text().split()], dtype=dt)
bad = 0
dec = np.arange(256, dtype=np.uint8).view(ml_dtypes.float8_e4m3fn).astype(np.float32).view(np.uint32)
n = int((dec != read("e4m3_decode.hex", np.uint32)).sum()); bad += n
print(f"decode, every code: {n} mismatches of 256")
for label, xs in (("every BF16", np.arange(65536, dtype=np.uint32) << 16),
                  ("edges", read("e4m3_edge_in.hex", np.uint32)), ("seeded binary32", read("e4m3_rand_in.hex", np.uint32))):
    ours = {"every BF16": "e4m3_bf16_nan.hex", "edges": "e4m3_edge_nan.hex", "seeded binary32": "e4m3_rand_nan.hex"}[label]
    ml = xs.view(np.float32).astype(ml_dtypes.float8_e4m3fn).view(np.uint8)
    n = int((ml != read(ours, np.uint8)).sum()); bad += n
    print(f"encode, {label}: {n} mismatches of {len(xs)}")
print(f"ml_dtypes {ml_dtypes.__version__}: {'ALL AGREE' if bad == 0 else f'{bad} DISAGREE'}")
sys.exit(1 if bad else 0)
