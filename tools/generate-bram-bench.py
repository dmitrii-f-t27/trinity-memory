#!/usr/bin/env python3
"""Specialize the block-RAM trit packing engine per format and write the manifest.

t27/rtl/bram_trit_engine.t27 is the one hand-written engine; its array bound must
be a literal constant, so this tool writes one copy per format with FORMAT,
LANES, WORDS and SEED substituted (and the module renamed), exactly as
tools/generate-t27-storage.py specializes the storage capacity. The manifest
holds what every engine must report, computed by tools/bram_trit_model.py, and
the block-RAM arithmetic of the three layouts.

With --rom-trits the stores hold a fixed tensor instead of the LFSR stream:
t27/rtl/bram_trit_rom.t27 is specialized with the tensor's words as array
initializers (they become the RAMB36E1 INIT/INITP contents) and renamed to the
engine module of each format, so the adapter is unchanged. The file holds one
signed byte per trit (-1, 0, +1), as tools/extract-bram-trits.py writes it; its
JSON sidecar (the same name with .json) is copied into the manifest.

  python3 tools/generate-bram-bench.py --trits 1013760 --output-dir build/fpga/bram
  python3 tools/generate-bram-bench.py --rom-trits build/fpga/rom/trits.bin --output-dir build/fpga/bram-rom
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
ROM_TEMPLATE = ROOT / "t27" / "rtl" / "bram_trit_rom.t27"
SOURCES = ["t27/rtl/bram_trit_engine.t27", "t27/rtl/bram_trit_codec.t27", "t27/rtl/fpga_bram_bench.t27",
           "tools/bram_trit_model.py", "tools/generate-bram-bench.py"]
MODULES = {model.FMT_B2: "TrinityBramTritEngineB2T27", model.FMT_D5: "TrinityBramTritEngineD5T27",
           model.FMT_D5D2: "TrinityBramTritEngineD5D2T27"}
RAMB36_WORDS_36 = 1024   # a RAMB36E1 in its 1K x 36 configuration
BANK_WORDS = 16384       # words per engine bank (14-bit address, see bram_trit_engine.t27)
BANKS = 4


def bank_sizes(words: int) -> list[int]:
    if words > BANKS * BANK_WORDS:
        raise SystemExit(f"{words} words do not fit {BANKS} banks of {BANK_WORDS}")
    return [min(BANK_WORDS, max(0, words - bank * BANK_WORDS)) for bank in range(BANKS)]
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
    for bank, size in enumerate(bank_sizes(words)):
        text = substitute(text, rf"^const B{bank}_WORDS: u32 = \d+;$", f"const B{bank}_WORDS: u32 = {max(1, size)};")
    return text


def initializer(bank: int, words: list[int]) -> str:
    """The initialized declaration of one bank, eight words per line."""
    rows = [", ".join(str(w) for w in words[i:i + 8]) for i in range(0, len(words), 8)]
    return (f"var mem_b{bank}: [B{bank}_WORDS]u64 = [B{bank}_WORDS]u64{{\n    "
            + ",\n    ".join(rows) + "\n};")


def specialize_rom(fmt: int, words: list[int], lanes_check: int, pipe: int = 0) -> str:
    text = ROM_TEMPLATE.read_text(encoding="utf-8")
    text = substitute(text, r"^module TrinityBramTritRomT27;$", f"module {MODULES[fmt]};")
    text = substitute(text, r"^const FORMAT: u32 = \d+;$", f"const FORMAT: u32 = {fmt};")
    text = substitute(text, r"^const LANES: u32 = \d+;$", f"const LANES: u32 = {model.LANES[fmt]};")
    text = substitute(text, r"^const WORDS: u32 = \d+;$", f"const WORDS: u32 = {len(words)};")
    text = substitute(text, r"^const LANES_CHECK: u64 = \d+;$", f"const LANES_CHECK: u64 = {lanes_check};")
    text = substitute(text, r"^const PIPE: u32 = \d+;$", f"const PIPE: u32 = {pipe};")
    for bank, size in enumerate(bank_sizes(len(words))):
        text = substitute(text, rf"^const B{bank}_WORDS: u32 = \d+;$", f"const B{bank}_WORDS: u32 = {max(1, size)};")
        chunk = words[bank * BANK_WORDS: bank * BANK_WORDS + size] or [0]
        # A function replacement: the declaration is inserted as it is, without escapes.
        text = substitute_with(text, rf"^var mem_b{bank}: \[B{bank}_WORDS\]u64 = .*;$", initializer(bank, chunk))
    return text


def substitute_with(text: str, pattern: str, replacement: str) -> str:
    new, count = re.subn(pattern, lambda _: replacement, text, flags=re.M)
    if count != 1:
        raise SystemExit(f"template: expected one match of {pattern!r}, found {count}")
    return new


def read_trits(path: Path, count: int | None) -> list[int]:
    data = path.read_bytes()
    trits = [b - 256 if b > 127 else b for b in data]
    if any(t not in (-1, 0, 1) for t in trits):
        raise SystemExit(f"{path}: every byte must be -1, 0 or +1")
    if count is not None:
        if count > len(trits):
            raise SystemExit(f"{path}: {len(trits)} trits, fewer than --trits {count}")
        trits = trits[:count]
    return trits


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--trits", type=int, default=None,
                        help="trits stored by every engine (a multiple of 1980 = lcm(18, 20, 22)); "
                             "default 1013760, or the whole --rom-trits file")
    parser.add_argument("--rom-trits", type=Path, default=None,
                        help="store this fixed tensor (one signed byte per trit) as array initializers")
    parser.add_argument("--pipe", type=int, choices=(0, 1), default=0,
                        help="with --rom-trits: 1 adds a register after the decoder and after the per-word sums")
    parser.add_argument("--seed", type=lambda v: int(v, 0), default=model.DEFAULT_SEED)
    parser.add_argument("--output-dir", default=str(ROOT / "build" / "fpga" / "bram"))
    args = parser.parse_args()
    trits = read_trits(args.rom_trits, args.trits) if args.rom_trits else None
    if args.trits is None:
        args.trits = len(trits) if trits is not None else 1013760
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
        if trits is None:
            path.write_text(specialize(fmt, words, args.seed), encoding="utf-8")
            result = model.engine_results(fmt, words, args.seed, verify=False)
        else:
            result = model.rom_results(fmt, trits, pipe=args.pipe)
            stored = [word for _, word in model.words_of_trits(fmt, trits)]
            path.write_text(specialize_rom(fmt, stored, result["lanes_check"], args.pipe), encoding="utf-8")
        result.update({
            "module": MODULES[fmt], "source": path.name,
            "used_bits_per_word": 36 if fmt != model.FMT_D5 else 32,
            "physical_bits_per_trit": 36 / lanes,
            "payload_bits_per_trit": {model.FMT_B2: 2.0, model.FMT_D5: 1.6, model.FMT_D5D2: 36 / 22}[fmt],
            "bank_words": bank_sizes(words),
            "ramb36_at_1k_x_36": sum(math.ceil(size / RAMB36_WORDS_36) for size in bank_sizes(words)),
            "trits_per_ramb36": RAMB36_WORDS_36 * lanes,
        })
        engines.append(result)
    same = {(e["pos"], e["neg"], e["dot"]) for e in engines}
    if len(same) != 1:
        raise SystemExit("model: the three formats disagree on the stored trits")
    manifest = {
        "schema": "trinity.fpga-bram-bench.v1",
        "mode": "rom" if trits is not None else "lfsr",
        "trits": args.trits, "seed": None if trits is not None else args.seed, "word_bits": 36,
        "activation_rule": "(global trit index mod 8) + 1",
        "trit_stream": ("fixed tensor stored as array initializers (see rom); trit i is lane i mod LANES of word i / LANES"
                        if trits is not None else
                        "64-bit Fibonacci LFSR, feedback s0^s1^s3^s4 into bit 63; trit i = stream bits 2i+1:2i, 11 reads as 0"),
        "engines": engines,
        "report_format": 1,
        "sources": {name: sha256(ROOT / name) for name in SOURCES + (["t27/rtl/bram_trit_rom.t27"] if trits is not None else [])},
        "entropy_bits_per_trit": math.log2(3),
    }
    if trits is not None:
        sidecar = args.rom_trits.with_suffix(".json")
        manifest["rom"] = {"trits_file": args.rom_trits.name, "trits_sha256": sha256(args.rom_trits),
                           "trits_used": args.trits,
                           "source": json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else None}
    (out / "bram_bench_manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    for e in engines:
        print(f"{e['name']:>3}: {e['lanes']} trits/word, {e['words']} words, >= {e['ramb36_at_1k_x_36']} RAMB36, "
              f"{e['physical_bits_per_trit']:.3f} bits/trit; +1 {e['pos']} -1 {e['neg']} dot {e['dot']}")


if __name__ == "__main__":
    main()
