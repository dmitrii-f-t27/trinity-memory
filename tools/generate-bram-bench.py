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

With --matvec-act as well, the d5d2 store is the matrix-vector kernel of
t27/rtl/bram_trit_matvec.t27: the trits are a matrix with one row per len(activations)
trits, stored column by column in groups of 22 rows, and the store computes y = W x
with the file's int8 activations (tools/extract-bram-activations.py) and checks the
result against the host's checksum.

  python3 tools/generate-bram-bench.py --trits 1013760 --output-dir build/fpga/bram
  python3 tools/generate-bram-bench.py --rom-trits build/fpga/rom/trits.bin --output-dir build/fpga/bram-rom
  python3 tools/generate-bram-bench.py --rom-trits build/fpga/rom/trits.bin --matvec-act build/fpga/rom/act.bin \
      --output-dir build/fpga/bram-matvec
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
MATVEC_TEMPLATE = ROOT / "t27" / "rtl" / "bram_trit_matvec.t27"
SOURCES = ["t27/rtl/bram_trit_engine.t27", "t27/rtl/bram_trit_codec.t27", "t27/rtl/fpga_bram_bench.t27",
           "tools/bram_trit_model.py", "tools/generate-bram-bench.py"]
MODULES = {model.FMT_B2: "TrinityBramTritEngineB2T27", model.FMT_D5: "TrinityBramTritEngineD5T27",
           model.FMT_D5D2: "TrinityBramTritEngineD5D2T27", model.FMT_D5P: "TrinityBramTritEngineD5PT27"}
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
    pair = fmt == model.FMT_D5P
    lanes, byte_lanes, lanes_out = (25, 20, model.PAIR_LANES) if pair else (model.LANES[fmt],) * 3
    text = substitute(text, r"^const FORMAT: u32 = \d+;$", f"const FORMAT: u32 = {fmt};")
    text = substitute(text, r"^const LANES: u32 = \d+;$", f"const LANES: u32 = {lanes};")
    text = substitute(text, r"^const BYTE_LANES: u32 = \d+;$", f"const BYTE_LANES: u32 = {byte_lanes};")
    text = substitute(text, r"^const LANES_OUT: u32 = \d+;$", f"const LANES_OUT: u32 = {lanes_out};")
    text = substitute(text, r"^const PAIR: u32 = \d+;$", f"const PAIR: u32 = {int(pair)};")
    text = substitute(text, r"^const WORDS: u32 = \d+;$", f"const WORDS: u32 = {len(words)};")
    text = substitute(text, r"^const LANES_CHECK: u64 = \d+;$", f"const LANES_CHECK: u64 = {lanes_check};")
    text = substitute(text, r"^const PIPE: u32 = \d+;$", f"const PIPE: u32 = {pipe};")
    for bank, size in enumerate(bank_sizes(len(words))):
        text = substitute(text, rf"^const B{bank}_WORDS: u32 = \d+;$", f"const B{bank}_WORDS: u32 = {max(1, size)};")
        chunk = words[bank * BANK_WORDS: bank * BANK_WORDS + size] or [0]
        # A function replacement: the declaration is inserted as it is, without escapes.
        text = substitute_with(text, rf"^var mem_b{bank}: \[B{bank}_WORDS\]u64 = .*;$", initializer(bank, chunk))
    return text


def specialize_matvec(words: list[int], act_words: list[int], rows: int, cols: int, y_check: int) -> str:
    text = MATVEC_TEMPLATE.read_text(encoding="utf-8")
    text = substitute(text, r"^module TrinityBramTritMatvecT27;$", f"module {MODULES[model.FMT_D5D2]};")
    for name, value in (("ROWS", rows), ("COLS", cols), ("WORDS", len(words)), ("ACT_WORDS", len(act_words))):
        text = substitute(text, rf"^const {name}: u32 = \d+;$", f"const {name}: u32 = {value};")
    text = substitute(text, r"^const Y_CHECK: u64 = \d+;$", f"const Y_CHECK: u64 = {y_check};")
    for bank, size in enumerate(bank_sizes(len(words))):
        text = substitute(text, rf"^const B{bank}_WORDS: u32 = \d+;$", f"const B{bank}_WORDS: u32 = {max(1, size)};")
        chunk = words[bank * BANK_WORDS: bank * BANK_WORDS + size] or [0]
        text = substitute_with(text, rf"^var mem_b{bank}: \[B{bank}_WORDS\]u64 = .*;$", initializer(bank, chunk))
    rows_text = [", ".join(str(w) for w in act_words[i:i + 8]) for i in range(0, len(act_words), 8)]
    act_decl = "var act_mem: [ACT_WORDS]u32 = [ACT_WORDS]u32{\n    " + ",\n    ".join(rows_text) + "\n};"
    return substitute_with(text, r"^var act_mem: \[ACT_WORDS\]u32 = .*;$", act_decl)


def matvec_engine(trits: list[int], acts: list[int], out: Path) -> dict:
    """Write the d5d2 matrix-vector store for the matrix `trits` (rows of len(acts)
    trits) and return its manifest entry."""
    fmt, cols = model.FMT_D5D2, len(acts)
    lanes = model.LANES[fmt]
    if len(trits) % (lanes * cols):
        raise SystemExit(f"--trits ({len(trits)}) must be a multiple of {lanes} rows of {cols} columns")
    if not 24 <= cols or cols * 128 >= 1 << 19:
        raise SystemExit(f"{cols} columns: the store needs 24 <= columns and columns * 128 < 2^19 (20-bit sums)")
    rows = len(trits) // cols
    result = model.matvec_results(fmt, trits, acts, rows, cols)
    stored = [word for _, word in model.words_of_trits(fmt, model.matvec_order(trits, rows, cols, lanes))]
    path = out / f"bram_trit_engine_{model.FORMAT_NAMES[fmt]}.t27"
    path.write_text(specialize_matvec(stored, model.act_words(acts), rows, cols, result["chk"]), encoding="utf-8")
    words = len(stored)
    result.update({
        "module": MODULES[fmt], "source": path.name, "used_bits_per_word": 36,
        "physical_bits_per_trit": 36 * words / len(trits), "payload_bits_per_trit": 36 / 22,
        "bank_words": bank_sizes(words),
        "ramb36_at_1k_x_36": sum(math.ceil(size / RAMB36_WORDS_36) for size in bank_sizes(words)),
        "trits_per_ramb36": RAMB36_WORDS_36 * len(trits) // words,
        "act_words": math.ceil(cols / 4),
    })
    return result


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
    parser.add_argument("--matvec-act", type=Path, default=None,
                        help="with --rom-trits: int8 activations, one signed byte each; the d5d2 store computes "
                             "y = W x (t27/rtl/bram_trit_matvec.t27), W the trits as rows of len(activations)")
    parser.add_argument("--seed", type=lambda v: int(v, 0), default=model.DEFAULT_SEED)
    parser.add_argument("--output-dir", default=str(ROOT / "build" / "fpga" / "bram"))
    args = parser.parse_args()
    trits = read_trits(args.rom_trits, args.trits) if args.rom_trits else None
    if args.trits is None:
        args.trits = len(trits) if trits is not None else 1013760
    if args.matvec_act is not None:
        if trits is None:
            parser.error("--matvec-act needs --rom-trits")
        write_matvec(args, trits)
        return
    if args.trits <= 0 or args.trits % 1980:
        parser.error("--trits must be a positive multiple of 1980")
    if not 0 < args.seed < (1 << 64):
        parser.error("--seed must be a nonzero 64-bit value")
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    engines = []
    formats = (model.FMT_B2, model.FMT_D5, model.FMT_D5D2) + ((model.FMT_D5P,) if trits is not None else ())
    for fmt in formats:
        lanes = model.PAIR_LANES if fmt == model.FMT_D5P else model.LANES[fmt]
        words = args.trits // lanes * (2 if fmt == model.FMT_D5P else 1)
        path = out / f"bram_trit_engine_{model.FORMAT_NAMES[fmt]}.t27"
        if trits is None:
            path.write_text(specialize(fmt, words, args.seed), encoding="utf-8")
            result = model.engine_results(fmt, words, args.seed, verify=False)
        else:
            result = model.rom_results(fmt, trits, pipe=args.pipe)
            stored = [word for _, word in (model.pair_words(trits) if fmt == model.FMT_D5P else model.words_of_trits(fmt, trits))]
            path.write_text(specialize_rom(fmt, stored, result["lanes_check"], args.pipe), encoding="utf-8")
        result.update({
            "module": MODULES[fmt], "source": path.name,
            "used_bits_per_word": 36 if fmt != model.FMT_D5 else 32,
            "physical_bits_per_trit": 36 * words / args.trits,
            "payload_bits_per_trit": {model.FMT_B2: 2.0, model.FMT_D5: 1.6, model.FMT_D5D2: 36 / 22, model.FMT_D5P: 1.6}[fmt],
            "bank_words": bank_sizes(words),
            "ramb36_at_1k_x_36": sum(math.ceil(size / RAMB36_WORDS_36) for size in bank_sizes(words)),
            "trits_per_ramb36": RAMB36_WORDS_36 * args.trits // words,
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
        print(f"{e['name']:>3}: {e['lanes']} trits/{'pair' if e['format'] == model.FMT_D5P else 'word'}, {e['words']} words, >= {e['ramb36_at_1k_x_36']} RAMB36, "
              f"{e['physical_bits_per_trit']:.3f} bits/trit; +1 {e['pos']} -1 {e['neg']} dot {e['dot']}")


def write_matvec(args, trits: list[int]) -> None:
    data = args.matvec_act.read_bytes()
    acts = [b - 256 if b > 127 else b for b in data]
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    engine = matvec_engine(trits, acts, out)
    sidecar = args.rom_trits.with_suffix(".json")
    act_sidecar = args.matvec_act.with_suffix(".json")
    manifest = {
        "schema": "trinity.fpga-bram-bench.v1", "mode": "matvec",
        "trits": len(trits), "seed": None, "word_bits": 36,
        "activation_rule": "int8 activations of the matvec file",
        "trit_stream": ("fixed matrix stored as array initializers, column by column in groups of 22 rows: "
                        "lane j of word k of group g is row 22g + j, column k"),
        "engines": [engine],
        "report_format": 1,
        "sources": {name: sha256(ROOT / name) for name in SOURCES + ["t27/rtl/bram_trit_matvec.t27"]},
        "entropy_bits_per_trit": math.log2(3),
        "rom": {"trits_file": args.rom_trits.name, "trits_sha256": sha256(args.rom_trits), "trits_used": len(trits),
                "source": json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else None},
        "matvec": {"rows": engine["rows"], "cols": engine["cols"], "groups": engine["rows"] // 22,
                   "act_file": args.matvec_act.name, "act_sha256": sha256(args.matvec_act),
                   "act_source": json.loads(act_sidecar.read_text(encoding="utf-8")) if act_sidecar.exists() else None,
                   "report": "pos/neg: outputs above/below zero; dot: sum of the outputs; chk: rotate-xor of the outputs "
                             "(64-bit two's complement, row order); bad_words: chk differs from the host's"},
    }
    (out / "bram_bench_manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    e = engine
    print(f"matvec d5d2: {e['rows']} x {e['cols']}, {e['words']} words, >= {e['ramb36_at_1k_x_36']} RAMB36 "
          f"+ {e['act_words']} activation words; y: {e['pos']} > 0, {e['neg']} < 0, sum {e['dot']}, "
          f"check {e['chk']:#018x}")


if __name__ == "__main__":
    main()
