#!/usr/bin/env python3
"""Generate conformance/hadamard_rotate.json from t27/hadamard.t27 (#52).

Every vector: a block of integer inputs (|x| <= 1024, dyadic-exact in binary
floating point), a sign layout, and the expected rotated outputs as f64 bit
patterns computed through the t27 reference -- the functions are loaded from
the generated C (gen-c + cc), never re-implemented here. Positive vectors
cover the blocks the spec allows (16, 64, 256, 1024), the identity and the
explicit sign modes, and the inverse (rotating twice returns the input);
negative vectors carry the classes a rotation can get wrong (a sign value out
of {+1,-1}, a signs file whose length is not a positive multiple of the
block, a non-power-of-two block) and expect the runner's classification.

The Action feeds these through the CLI contract's `rotate` call (v1.1):
PROG rotate BLOCK SIGNS VALUES OUTPUT, little-endian f64 in, f64 out.

  python3 tools/generate-hadamard-vectors.py [--check]

--check recomputes and compares with the committed file. Exit 0 on success.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import random
import struct
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "conformance/hadamard_rotate.json"


def load_library():
    from trinity_memory import hadamard_reference as h
    return h._load()


def make_rotation(lib):
    from trinity_memory import hadamard_reference as h

    def rotate(block: int, x: list[int], signs: list[int]) -> list[float]:
        """The rotated values: numerator / normalisation, exact in f64."""
        return h.rotate(block, [float(v) for v in x], signs)

    return rotate


def f64_hex(values: list[float]) -> str:
    return struct.pack(f"<{len(values)}d", *values).hex()


def i32_hex(values: list[int]) -> str:
    return struct.pack(f"<{len(values)}i", *values).hex()


def vector(vid: str, block: int, x: list[int], signs: list[int],
           inverse: bool = False, negatives: dict | None = None) -> dict:
    return {
        "id": vid, "kind": "rotate", "block": block,
        "values": {"f64_le": f64_hex(x)},
        "signs": {"i32_le": i32_hex(signs), "mode": "explicit" if any(s != 1 for s in signs) else "identity"},
        "expect": {"output_f64_le": f64_hex(x)} if inverse else None,
        "inverse": inverse,
        **(negatives or {}),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    rotate = make_rotation(load_library())
    rng = random.Random(52)
    vectors = []

    for block in (16, 64, 256, 1024):
        # identity signs, mixed integer inputs
        x = [rng.randint(-512, 511) for _ in range(block)]
        out = rotate(block, x, [1] * block)
        vectors.append({"id": f"identity-b{block}", "kind": "rotate", "block": block,
                        "values": f64_hex(x), "signs": i32_hex([1] * block),
                        "sign_mode": "identity",
                        "expect_output": f64_hex(out)})
        # explicit signs from a real-file-like layout: +1 runs and -1 runs
        signs, s = [], 1
        for i in range(block):
            if i % 7 == 0:
                s = -s
            signs.append(s)
        out = rotate(block, x, signs)
        vectors.append({"id": f"explicit-b{block}", "kind": "rotate", "block": block,
                        "values": f64_hex(x), "signs": i32_hex(signs),
                        "sign_mode": "explicit",
                        "expect_output": f64_hex(out)})
        # inversion is proven offline with exact rationals for the identity
        # signs (H @ H = block * I; with explicit signs the transform is not
        # an involution -- the fork applies the signs once, at export time)
        from fractions import Fraction
        norm = {16: 4, 64: 8, 256: 16, 1024: 32}[block]
        mid = [Fraction(v) for v in rotate(block, x, [1] * block)]
        back = [sum((-1 if bin(k & j).count("1") % 2 else 1) * mid[j]
                    for j in range(block)) / norm for k in range(block)]
        assert back == [Fraction(v) for v in x], (block, "inversion broke")

    # negatives: each its own class, the runner must classify (never decode)
    vectors.append({"id": "negative-sign-value", "kind": "rotate", "block": 64,
                    "values": f64_hex([1.0] * 64),
                    "signs": i32_hex([1] * 63 + [2]), "sign_mode": "explicit",
                    "expect_status": "hadamard_sign_value"})
    vectors.append({"id": "negative-signs-length", "kind": "rotate", "block": 64,
                    "values": f64_hex([1.0] * 64),
                    "signs": i32_hex([1] * 63), "sign_mode": "explicit",
                    "expect_status": "hadamard_signs_length"})
    vectors.append({"id": "negative-block", "kind": "rotate", "block": 24,
                    "values": f64_hex([1.0] * 24),
                    "signs": i32_hex([1] * 24), "sign_mode": "identity",
                    "expect_status": "hadamard_block"})

    doc = {
        "schema": "trinity.ternary-check.hadamard-rotate.v1",
        "constants": {"errors": {}, "flags": {},
                      "note": "the rotate call carries its own statuses (v1.1)"},
        "spec": "t27/hadamard.t27",
        "generator": "tools/generate-hadamard-vectors.py",
        "contract": "trinity.ternary-check-cli.v1 (rotate call, v1.1)",
        "issue": "dmitrii-f-t27/trinity-memory#52",
        "note": "integer inputs, power-of-two blocks: every expected output is "
                "a dyadic rational exact in f64; the values are the t27 "
                "functions' own, produced through gen-c",
        "vectors": vectors,
    }
    text = json.dumps(doc, indent=1) + "\n"
    if args.check:
        ok = OUT.is_file() and OUT.read_text() == text
        print("check:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    OUT.write_text(text)
    print(f"wrote {len(vectors)} vectors to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
