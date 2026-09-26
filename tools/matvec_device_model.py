#!/usr/bin/env python3
"""Device model of the #64 DDR3 matvec (t27/rtl/fpga_ddr3_matvec.t27).

States the doorbell protocol, the memory layout, the decode of every device
word (the #62 reader's decoders), the eight-lane sub-word dot against the int8
activation store and every report line, so tests/test_ddr3_matvec.py can check
the C functions against this file and the whole module in Icarus against a
behavioural Wishbone memory. No RTL logic lives here: the expressions mirror
the module's, whose own comments are the contract.
"""
from __future__ import annotations

M32 = (1 << 32) - 1
M64 = (1 << 64) - 1
B40 = (1 << 40) - 1
FMT_B2, FMT_D5 = 0, 1
LANES = {FMT_B2: 64, FMT_D5: 80}
MATVEC_FORMAT = 1
MAGIC = 0x74337633  # "t3v3": the doorbell's top half
TAGS = {"q": 113, "k": 107, "v": 118, "d": 100, "y": 121, "c": 99, "o": 111,
        "w": 119, "n": 110, "u": 117, "t": 116, "z": 122}
TAG_NAMES = {value: name for name, value in TAGS.items()}


def lanes_of(fmt: int) -> int:
    return LANES[fmt]


def subs_of(fmt: int) -> int:
    return LANES[fmt] // 8


def words_per_row(cols: int, fmt: int) -> int:
    """0 unless cols is a multiple of the word lanes (the alignment guard)."""
    if fmt == FMT_D5:
        return cols // 80 if cols % 80 == 0 else 0
    return cols // 64


def act_words_of(cols: int) -> int:
    return (cols + 15) // 16


def doorbell(run: int, magic: int = MAGIC) -> tuple[int, int]:
    """The descriptor word halves: (lo, hi) of the 128-bit word at `daddr`."""
    return run & M32, magic & M32


def lane_word(trits: list[int], base: int) -> int:
    """Lanes base .. base+79 of the trit stream as a 160-bit vector (2 bits per lane)."""
    word = 0
    for j in range(80):
        trit = trits[base + j] if base + j < len(trits) else 0
        code = {1: 1, -1: 2, 0: 0}[trit]
        word |= code << (2 * j)
    return word


# The word codec is the #62 reader's, verbatim: tools/ddr3_read_model.py is the
# single source (encode_word(fmt, lanes) -> the 128-bit device word with byte n
# at bits [8n, 8n+7]; decode_word(fmt, word) -> (160/128-bit lanes, invalid)).
from ddr3_read_model import encode_word as _encode_word, decode_word as _decode_word

M128 = (1 << 128) - 1


def encode_word(fmt: int, lanes: int) -> tuple[int, int]:
    """A 160-bit lane vector -> the (lo, hi) 64-bit halves of the device word."""
    word = _encode_word(fmt, lanes)
    return word & M64, word >> 64


def decode_word(fmt: int, lo: int, hi: int) -> tuple[int, int]:
    """A device word -> (lane vector, invalid group count)."""
    return _decode_word(fmt, (hi << 64) | (lo & M64))


def act_words(activations: list[int]) -> list[tuple[int, int]]:
    """The int8 columns packed 16 per device word: [(lo, hi), ...]."""
    words = []
    for w in range(act_words_of(len(activations))):
        lo = hi = 0
        for j in range(16):
            column = w * 16 + j
            byte = activations[column] & 255 if column < len(activations) else 0
            if j < 8:
                lo |= byte << (8 * j)
            else:
                hi |= byte << (8 * (j - 8))
        words.append((lo, hi))
    return words


def sign_extend(byte: int) -> int:
    return byte - 256 if byte >= 128 else byte


def dot_chunk(lanes: int, base: int, word: int) -> int:
    """Eight lanes against eight activation bytes of one act store word."""
    total = 0
    for j in range(8):
        code = (lanes >> (2 * (base + j))) & 3
        trit = 1 if code == 1 else -1 if code == 2 else 0
        total += trit * sign_extend((word >> (8 * j)) & 255)
    return total


def run_expectation(trits: list[int], activations: list[int], cols: int, rows: int, fmt: int) -> dict:
    """Every accumulator of the chunk, and the per-run counters, as the module reports them."""
    wpr = words_per_row(cols, fmt)
    assert wpr, "cols must be a multiple of the word lanes"
    assert len(activations) == cols, "one activation per column"
    assert len(trits) == rows * cols
    # The act store: word i = acts[2i] lo (columns 16i..16i+7), acts[2i+1] hi.
    acts = []
    for lo, hi in act_words(activations):
        acts.extend((lo, hi))
    accumulators, bad_words, y_lines = [], 0, []
    for r in range(rows):
        acc = 0
        for w in range(wpr):
            lanes = lane_word(trits, r * cols + w * LANES[fmt])
            _, invalid = decode_word(fmt, *encode_word(fmt, lanes))
            if invalid:
                bad_words += 1
            for s in range(subs_of(fmt)):
                acc += dot_chunk(lanes, s * 8, acts[w * subs_of(fmt) + s])
        accumulators.append(acc)
        y_lines.append(("y", r, acc & B40))
    return {"accumulators": accumulators, "bad_words": bad_words, "y_lines": y_lines,
            "words": rows * wpr, "act_words": act_words_of(cols)}


def head_lines(cols: int, rows: int, fmt: int, cap: int, poll_div: int) -> list[tuple[int, int, int]]:
    wpr, aw = words_per_row(cols, fmt), act_words_of(cols)
    return [("q", cols, (MATVEC_FORMAT << 32) | (LANES[fmt] << 24) | cap),
            ("k", rows, (wpr << 32) | aw),
            ("v", MAGIC, poll_div)]


def run_lines(run: int, polls: int, words: int, cycles: int, cmd: int, wait: int, cons: int,
              bad: int, acts_run: int, stray: int, runs_done: int, total_bad: int) -> list[tuple[int, int, int]]:
    return [("d", run, polls & B40), ("c", words, cycles & B40),
            ("o", None, cmd & B40), ("w", None, wait & B40),
            ("n", bad, cons & B40), ("u", acts_run, stray),
            ("z", runs_done, total_bad & B40)]


def timeout_line(acks: int, acts_phase: bool, fmt: int, run: int) -> tuple[int, int, int]:
    return "t", acks, ((1 << 36) if acts_phase else 0) | (fmt << 32) | run
