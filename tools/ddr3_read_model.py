"""Host model of the DDR3 read path (issue #62, t27/rtl/fpga_ddr3_reader.t27).

An independent Python statement of the rules the t27 reader implements: the trit
stream the board writes into DDR3 without a UART (the fill), the two TMEM payload
layouts it is written in, and what consumer (A) must report after reading it back.
tests/test_ddr3_reader.py checks the C that the pinned compiler generates from the
module's functions against it and the Icarus run of the whole top against it;
tools/fpga-ddr3-capture.py compares every board run with it.

Trit stream. A run pair p (runs 2p and 2p+1) writes the same `trits` logical trits,
first as baseline2 (run 2p), then as dense5 (run 2p+1). Trit i sits in block i >> 4
at position i & 15; the 16 trits of block b are the 16 two-bit lane codes of

    raw(K, b) = (K xor post(lin(mix(b)))) mod 2^32,   a pair 11 read as 00,
    mix(b)  = b xor ((b << 3) and (b << 7))            (mod 2^64)
    post(y) = y xor ((y >> 1) and (y >> 2))
    K       = lin(lin((seed << 32) | p) + KADD)        (lin, KADD: tools/ddr3_pattern_model.py)

so a trit is 0 with probability 1/2 and +1 or -1 with 1/4 each (lane codes 00 = 0,
01 = +1, 10 = -1, as the RTL decoders and tools/bram_trit_model.py use them). mix,
lin and post are bijections of 64 bits; mix and post are the nonlinear steps. With lin
alone (tried first), a region made of large aligned ranges of blocks maps to cosets of
linear subspaces, on which every lane pair is exactly uniform: for the 2560 x 6912 region
and the three keys tried the +1 and -1 counts came out equal (4,423,680 each) and the
dot product 0, so they would not have depended on the data written. With mix and post
they differ from key to key.

The trits of a region end at `trits`; every lane after them (the rest of the last
word) is a logical zero.

Layouts (docs/format.md). One Wishbone word of the x16 build is 128 bits, 16 bytes,
byte n at bits [8n, 8n+7]; the region's byte k is byte k mod 16 of the word at burst
address k / 16, so the payload bytes follow the stream across Wishbone words (where
UberDDR3 puts a word's bytes on the DDR3 beats and lanes is its own, not modelled here).
  baseline2 (format 0): four lane codes per byte, earliest lane lowest: 64 trits
    per word, word w = trits 64w .. 64w+63, lane j of the word at bits [2j, 2j+1].
  dense5 (format 1): byte n = sum over i < 5 of (t_i + 1) * 3^i for trits 5n .. 5n+4
    of the word: 80 trits per word, word w = trits 80w .. 80w+79. A padding trit is
    a logical zero (digit 1), so a byte of five padding trits is 121.
Payload bytes are ceil(trits / 4) and ceil(trits / 5); the bus moves whole words, and
the difference is padding. No scale or metadata is stored (0 bytes).

Consumer (A), per word read back, with the definitions of tools/bram_trit_model.py (restated
here; tests/test_ddr3_reader.py recomputes the results with that module's own functions):
lanes decoded (the codec of t27/rtl/bram_trit_codec.t27: baseline2 lanes with 11
cleared and counted invalid, dense5 bytes >= 243 counted invalid), compared with the
regenerated lanes (a word that differs anywhere is one bad word), +1 and -1 counts,
the dot product with the activation (i mod 8) + 1 of global trit index i, and the
rotate-xor checksum of the stored data in 64-bit halves in bus order (low half of
word 0, high half of word 0, low half of word 1, ...): chk = rotl1(chk) xor half.
"""
from __future__ import annotations

from ddr3_pattern_model import KADD, M32, M64, lin

FMT_B2, FMT_D5 = 0, 1
FORMAT_NAMES = {FMT_B2: "baseline2", FMT_D5: "dense5"}
LANES = {FMT_B2: 64, FMT_D5: 80}           # logical trits per 128-bit word
TRITS_PER_BYTE = {FMT_B2: 4, FMT_D5: 5}
WORD_BYTES = 16
BLOCK = 16                                   # trits per generator block
BLOCKS_PER_WORD = {FMT_B2: 4, FMT_D5: 5}
PAIRS_32 = 0x55555555
M128 = (1 << 128) - 1


def pair_key(seed: int, pair: int) -> int:
    k = lin(((seed & M32) << 32) | (pair & M32))
    k = (k + KADD) & M64
    return lin(k)


def clean32(x: int) -> int:
    """Every lane pair 11 read as 00."""
    both = x & (x >> 1) & PAIRS_32
    return x ^ (both | (both << 1))


def mix(b: int) -> int:
    b &= M64
    return b ^ ((b << 3) & (b << 7) & M64)


def post(y: int) -> int:
    return y ^ ((y >> 1) & (y >> 2))


def block_raw(key: int, b: int) -> int:
    return clean32((key ^ post(lin(mix(b)))) & M32)


def words_of(fmt: int, trits: int) -> int:
    return -(-trits // LANES[fmt])


def payload_bytes(fmt: int, trits: int) -> int:
    return -(-trits // TRITS_PER_BYTE[fmt])


def word_lanes(fmt: int, key: int, trits: int, w: int) -> int:
    """The lane codes of word w (2 bits per lane, lane j at bits [2j, 2j+1]); lanes at or
    after `trits` are zero."""
    blocks = BLOCKS_PER_WORD[fmt]
    lanes = 0
    for k in range(blocks):
        lanes |= block_raw(key, blocks * w + k) << (32 * k)
    first = LANES[fmt] * w
    keep = max(0, min(LANES[fmt], trits - first))
    return lanes & ((1 << (2 * keep)) - 1)


def digit_of_lane(code: int) -> int:
    return {0: 1, 1: 2, 2: 0}[code]


def lane_of_digit(digit: int) -> int:
    return {0: 2, 1: 0, 2: 1}[digit]


def encode_word(fmt: int, lanes: int) -> int:
    """The 128-bit Wishbone word (byte n at bits [8n, 8n+7]) of a word's lanes."""
    if fmt == FMT_B2:
        return lanes & M128
    word = 0
    for n in range(16):
        code = sum(digit_of_lane((lanes >> (2 * (5 * n + i))) & 3) * 3 ** i for i in range(5))
        word |= code << (8 * n)
    return word


def decode_word(fmt: int, word: int) -> tuple[int, int]:
    """(lanes, invalid groups) of a 128-bit word, as the reader's four codec instances decode it."""
    lanes = bad = 0
    if fmt == FMT_B2:
        for j in range(64):
            code = (word >> (2 * j)) & 3
            if code == 3:
                bad += 1
            else:
                lanes |= code << (2 * j)
        return lanes, bad
    for n in range(16):
        code = (word >> (8 * n)) & 255
        if code < 243:
            for i in range(5):
                lanes |= lane_of_digit((code // 3 ** i) % 3) << (2 * (5 * n + i))
        else:
            bad += 1
    return lanes, bad


def rotl(x: int, n: int) -> int:
    n %= 64
    return ((x << n) | (x >> (64 - n))) & M64 if n else x


def run_results(fmt: int, key: int, trits: int) -> dict:
    """What one read run must report (pure Python, one word at a time)."""
    lanes_per_word = LANES[fmt]
    words = words_of(fmt, trits)
    pos = neg = dot = chk = 0
    for w in range(words):
        lanes = word_lanes(fmt, key, trits, w)
        data = encode_word(fmt, lanes)
        back, bad = decode_word(fmt, data)
        assert back == lanes and bad == 0, (fmt, w)
        for j in range(lanes_per_word):
            code = (lanes >> (2 * j)) & 3
            act = (j & 7) + 1                  # (global index mod 8) + 1; a word starts at a multiple of 8
            if code == 1:
                pos += 1
                dot += act
            elif code == 2:
                neg += 1
                dot -= act
        chk = rotl(chk, 1) ^ (data & M64)
        chk = rotl(chk, 1) ^ (data >> 64)
    return summary(fmt, trits, words, pos, neg, dot, chk)


def summary(fmt: int, trits: int, words: int, pos: int, neg: int, dot: int, chk: int) -> dict:
    payload = payload_bytes(fmt, trits)
    return {"format": fmt, "name": FORMAT_NAMES[fmt], "lanes_per_word": LANES[fmt], "trits": trits,
            "words": words, "bus_bytes": words * WORD_BYTES, "payload_bytes": payload,
            "padding_bytes": words * WORD_BYTES - payload, "scale_metadata_bytes": 0,
            "bad_words": 0, "invalid_groups": 0, "pos": pos, "neg": neg, "dot": dot, "chk": chk}


def run_results_fast(fmt: int, key: int, trits: int) -> dict:
    """run_results with numpy, for board-sized regions (same rules, vectorized)."""
    import numpy as np

    lanes_per_word = LANES[fmt]
    words = words_of(fmt, trits)
    nblocks = words * BLOCKS_PER_WORD[fmt]
    x = np.arange(nblocks, dtype=np.uint64)
    x ^= (x << np.uint64(3)) & (x << np.uint64(7))
    for left, right, left2 in ((13, 7, 17), (21, 35, 4), (9, 29, 25)):
        x ^= x << np.uint64(left)
        x ^= x >> np.uint64(right)
        x ^= x << np.uint64(left2)
    x ^= (x >> np.uint64(1)) & (x >> np.uint64(2))
    raw = (x ^ np.uint64(key)) & np.uint64(M32)
    both = raw & (raw >> np.uint64(1)) & np.uint64(PAIRS_32)
    raw ^= both | (both << np.uint64(1))
    shifts = np.arange(0, 32, 2, dtype=np.uint64)
    codes = ((raw[:, None] >> shifts[None, :]) & np.uint64(3)).astype(np.uint8).reshape(-1)
    codes[trits:] = 0
    total = words * lanes_per_word
    codes = codes[:total]
    act = (np.arange(total, dtype=np.int64) & 7) + 1
    is_pos, is_neg = codes == 1, codes == 2
    pos, neg = int(is_pos.sum()), int(is_neg.sum())
    dot = int(act[is_pos].sum()) - int(act[is_neg].sum())
    if fmt == FMT_B2:
        c = codes.reshape(-1, 4).astype(np.uint8)
        data = c[:, 0] | (c[:, 1] << 2) | (c[:, 2] << 4) | (c[:, 3] << 6)
    else:
        digits = np.array([1, 2, 0, 0], dtype=np.uint16)[codes].reshape(-1, 5)
        data = (digits * np.array([1, 3, 9, 27, 81], dtype=np.uint16)).sum(axis=1).astype(np.uint8)
    halves = np.ascontiguousarray(data.astype(np.uint8)).view("<u8").astype(np.uint64)
    n = len(halves)
    rot = ((n - 1 - np.arange(n)) % 64).astype(np.uint64)
    rotated = np.where(rot == 0, halves, (halves << rot) | (halves >> ((np.uint64(64) - rot) % np.uint64(64))))
    chk = int(np.bitwise_xor.reduce(rotated)) if n else 0
    return summary(fmt, trits, words, pos, neg, dot, chk)


def results(fmt: int, key: int, trits: int) -> dict:
    """run_results_fast when numpy is there, run_results otherwise."""
    try:
        import numpy  # noqa: F401
    except ImportError:
        return run_results(fmt, key, trits)
    return run_results_fast(fmt, key, trits)


def expected_run(seed: int, run: int, trits: int) -> dict:
    """The model's report of run `run` (format run & 1, key of pair run >> 1)."""
    fmt, pair = run & 1, run >> 1
    out = results(fmt, pair_key(seed, pair), trits)
    out["run"], out["pair"] = run, pair
    return out
