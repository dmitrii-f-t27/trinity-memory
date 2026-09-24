#!/usr/bin/env python3
"""Write the FP8 E4M3 vector tables from the independent reference model (standard library only).

Tables (one hex word per line, $readmemh format) in kits/processorci/vectors/:
  e4m3_decode.hex        256 x 32-bit   decode of every code
  e4m3_bf16_sat.hex      65536 x 8-bit  encode of every BF16 pattern (input = bf16 << 16), saturating
  e4m3_bf16_nan.hex      65536 x 8-bit  same inputs, non-saturating (NaN on overflow)
  e4m3_edge_in.hex       N x 32-bit     rounding boundaries +-1 ulp, specials, binary32 subnormals
  e4m3_edge_sat.hex / e4m3_edge_nan.hex   expected codes for the edge inputs
  e4m3_rand_in.hex       65536 x 32-bit seeded xorshift32 binary32 patterns (seed 0x27272727)
  e4m3_rand_sat.hex / e4m3_rand_nan.hex   expected codes for the random inputs
and manifest.json with the SHA-256 and CRC-32 (zlib) of every table.
"""
import json, hashlib, zlib, struct, sys
from pathlib import Path
from fractions import Fraction
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import e4m3_ref as R
import ternary_ref as T
OUT = HERE.parent / "vectors"

def f32_of_fraction(v: Fraction) -> int:
    b = R.f32_bits(float(v)); assert R.bits_value(b) == v; return b

def edge_inputs():
    xs = set()
    vals = R.POS + [R.VIRTUAL_NEXT]
    for i in range(len(vals)):
        xs.add(f32_of_fraction(vals[i]) if vals[i] else 0)
        if i + 1 < len(vals):
            mid = (vals[i] + vals[i + 1]) / 2
            b = f32_of_fraction(mid)
            xs.update({b, b - 1, b + 1})
    # the overflow side of the grid and far outside it
    for v in (Fraction(512), Fraction(1000), Fraction(65504), Fraction(2) ** 127):
        b = f32_of_fraction(v); xs.update({b, b - 1, b + 1})
    xs.update({0x7F7FFFFF, 0x00000001, 0x007FFFFF, 0x00800000,       # FLT_MAX, binary32 subnormals, FLT_MIN
               0x7F800000, 0x7FC00000, 0x7F800001, 0x7FFFFFFF})      # inf, quiet NaN, signalling NaN, max NaN
    for v in (Fraction(1, 2 ** 11), Fraction(3, 2 ** 12)):
        b = f32_of_fraction(v); xs.update({b, b - 1, b + 1})
    both = sorted(xs | {x | 0x80000000 for x in xs})
    return both

def xorshift32(n, state=0x27272727):
    out = []
    for _ in range(n):
        state ^= (state << 13) & 0xFFFFFFFF; state ^= state >> 17; state ^= (state << 5) & 0xFFFFFFFF
        out.append(state)
    return out

def write(name, words, width):
    text = "".join(f"{w:0{width}x}\n" for w in words)
    (OUT / name).write_text(text)
    raw = b"".join(w.to_bytes(width // 2, "little") for w in words)
    return {"count": len(words), "width_bits": width * 4, "sha256_file": hashlib.sha256(text.encode()).hexdigest(),
            "crc32_le_words": f"{zlib.crc32(raw) & 0xFFFFFFFF:08x}"}

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    man = {"format": "OCP FP8 E4M3FN (float8_e4m3fn)", "rounding": "nearest, ties to even",
           "reference": "kits/processorci/model/e4m3_ref.py", "tables": {}}
    t = man["tables"]
    t["e4m3_decode.hex"] = write("e4m3_decode.hex", [R.decode(c) for c in range(256)], 8)
    bf = [b << 16 for b in range(65536)]
    t["e4m3_bf16_sat.hex"] = write("e4m3_bf16_sat.hex", [R.encode(b, True) for b in bf], 2)
    t["e4m3_bf16_nan.hex"] = write("e4m3_bf16_nan.hex", [R.encode(b, False) for b in bf], 2)
    edge = edge_inputs()
    t["e4m3_edge_in.hex"] = write("e4m3_edge_in.hex", edge, 8)
    (OUT / "e4m3_edge_count.vh").write_text(f"`define E4M3_EDGE_N {len(edge)}\n")
    t["e4m3_edge_sat.hex"] = write("e4m3_edge_sat.hex", [R.encode(b, True) for b in edge], 2)
    t["e4m3_edge_nan.hex"] = write("e4m3_edge_nan.hex", [R.encode(b, False) for b in edge], 2)
    rnd = xorshift32(65536)
    t["e4m3_rand_in.hex"] = write("e4m3_rand_in.hex", rnd, 8)
    t["e4m3_rand_sat.hex"] = write("e4m3_rand_sat.hex", [R.encode(b, True) for b in rnd], 2)
    t["e4m3_rand_nan.hex"] = write("e4m3_rand_nan.hex", [R.encode(b, False) for b in rnd], 2)
    t["ternary_dense5.hex"] = write("ternary_dense5.hex", [T.dense5(c) for c in range(256)], 4)
    t["ternary_baseline5.hex"] = write("ternary_baseline5.hex", [T.baseline5(c) for c in range(1024)], 4)
    t["ternary_sparse41.hex"] = write("ternary_sparse41.hex", [T.sparse41(c) for c in range(256)], 4)
    # Expected result blocks for the ProcessorCI harness (test id -> count, CRC-32 of the output bytes).
    tests = [(1, "e4m3_decode.hex"), (2, "e4m3_bf16_sat.hex"), (3, "e4m3_bf16_nan.hex"), (4, "e4m3_rand_sat.hex"),
             (5, "e4m3_rand_nan.hex"), (6, "ternary_dense5.hex"), (7, "ternary_baseline5.hex"), (8, "ternary_sparse41.hex")]
    man["harness"] = {"descriptor": {"word0": "test id", "word1": "count (tests 4 and 5)", "word2": "seed (tests 4 and 5)"},
                      "random_seed": "0x27272727", "random_count": 65536,
                      "tests": {str(i): {"table": name, "count": t[name]["count"], "crc32": t[name]["crc32_le_words"]} for i, name in tests}}
    words = []
    for i, name in tests:
        words += [t[name]["count"], int(t[name]["crc32_le_words"], 16)]
    (OUT / "harness_expected.hex").write_text("".join(f"{w:08x}\n" for w in words))
    (OUT / "manifest.json").write_text(json.dumps(man, indent=2) + "\n")
    print(f"wrote {len(t)} tables; edge inputs {len(edge)}")
if __name__ == "__main__":
    main()
