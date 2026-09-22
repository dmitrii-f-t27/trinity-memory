#!/usr/bin/env python3
"""Specialize the block-RAM trit packing engine per format and write the manifest.

t27/rtl/bram_trit_engine.t27 is the one hand-written engine; its array bound must
be a literal constant, so this tool writes one copy per format with FORMAT,
LANES, WORDS and SEED substituted (and the module renamed), exactly as
tools/generate-t27-storage.py specializes the storage capacity. The manifest
holds what every engine must report, computed by tools/bram_trit_model.py, and
the block-RAM arithmetic of the three layouts.

  python3 tools/generate-bram-bench.py --trits 1013760 --output-dir build/fpga/bram
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import bram_trit_model as model  # noqa: E402

TEMPLATE = ROOT / "t27" / "rtl" / "bram_trit_engine.t27"
SOURCES = ["t27/rtl/bram_trit_engine.t27", "t27/rtl/bram_trit_codec.t27", "t27/rtl/fpga_bram_bench.t27",
           "tools/bram_trit_model.py", "tools/generate-bram-bench.py"]
MODULES = {model.FMT_B2: "TrinityBramTritEngineB2T27", model.FMT_D5: "TrinityBramTritEngineD5T27",
           model.FMT_D5D2: "TrinityBramTritEngineD5D2T27"}
RAMB36_WORDS_36 = 1024   # a RAMB36E1 in its 1K x 36 configuration
BANK_WORDS = 32768       # words per engine bank (15-bit address, see bram_trit_engine.t27)
RAMB36_BITS = 36 * 1024


def substitute(text: str, pattern: str, replacement: str) -> str:
    new, count = re.subn(pattern, replacement, text, flags=re.M)
    if count != 1:
        raise SystemExit(f"template: expected one match of {pattern!r}, found {count}")
    return new


def specialize(fmt: int, words: int, seed: int) -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    text = substitute(text, r"^module TrinityBramTritEngineT27;$", f"module {MODULES[fmt]};")
    text = substitute(text, r"^const FORMAT: u32 = \d+;$", f"const FORMAT: u32 = {fmt};")
    text = substitute(text, r"^const LANES: u32 = \d+;$", f"const LANES: u32 = {model.LANES[fmt]};")
    text = substitute(text, r"^const WORDS: u32 = \d+;$", f"const WORDS: u32 = {words};")
    text = substitute(text, r"^const SEED: u64 = \d+;$", f"const SEED: u64 = {seed};")
    text = substitute(text, r"^const LO_WORDS: u32 = \d+;$", f"const LO_WORDS: u32 = {min(words, BANK_WORDS)};")
    text = substitute(text, r"^const HI_WORDS: u32 = \d+;$", f"const HI_WORDS: u32 = {max(1, words - BANK_WORDS)};")
    return text


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--trits", type=int, default=1013760,
                        help="trits stored by every engine (a multiple of 1980 = lcm(18, 20, 22))")
    parser.add_argument("--seed", type=lambda v: int(v, 0), default=model.DEFAULT_SEED)
    parser.add_argument("--output-dir", default=str(ROOT / "build" / "fpga" / "bram"))
    args = parser.parse_args()
    if args.trits <= 0 or args.trits % 1980:
        parser.error("--trits must be a positive multiple of 1980")
    if not 0 < args.seed < (1 << 64):
        parser.error("--seed must be a nonzero 64-bit value")
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    engines = []
    for fmt in (model.FMT_B2, model.FMT_D5, model.FMT_D5D2):
        lanes = model.LANES[fmt]
        words = args.trits // lanes
        path = out / f"bram_trit_engine_{model.FORMAT_NAMES[fmt]}.t27"
        path.write_text(specialize(fmt, words, args.seed), encoding="utf-8")
        result = model.engine_results(fmt, words, args.seed, verify=False)
        result.update({
            "module": MODULES[fmt], "source": path.name,
            "used_bits_per_word": 36 if fmt != model.FMT_D5 else 32,
            "physical_bits_per_trit": 36 / lanes,
            "payload_bits_per_trit": {model.FMT_B2: 2.0, model.FMT_D5: 1.6, model.FMT_D5D2: 36 / 22}[fmt],
            "ramb36_at_1k_x_36": math.ceil(min(words, BANK_WORDS) / RAMB36_WORDS_36)
                                 + math.ceil(max(0, words - BANK_WORDS) / RAMB36_WORDS_36),
            "trits_per_ramb36": RAMB36_WORDS_36 * lanes,
        })
        engines.append(result)
    same = {(e["pos"], e["neg"], e["dot"]) for e in engines}
    if len(same) != 1:
        raise SystemExit("model: the three formats disagree on the stored trits")
    manifest = {
        "schema": "trinity.fpga-bram-bench.v1",
        "trits": args.trits, "seed": args.seed, "word_bits": 36,
        "activation_rule": "(global trit index mod 8) + 1",
        "trit_stream": "64-bit Fibonacci LFSR, feedback s0^s1^s3^s4 into bit 63; trit i = stream bits 2i+1:2i, 11 reads as 0",
        "engines": engines,
        "report_format": 1,
        "sources": {name: sha256(ROOT / name) for name in SOURCES},
        "entropy_bits_per_trit": math.log2(3),
    }
    (out / "bram_bench_manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    for e in engines:
        print(f"{e['name']:>3}: {e['lanes']} trits/word, {e['words']} words, >= {e['ramb36_at_1k_x_36']} RAMB36, "
              f"{e['physical_bits_per_trit']:.3f} bits/trit; +1 {e['pos']} -1 {e['neg']} dot {e['dot']}")


if __name__ == "__main__":
    main()
