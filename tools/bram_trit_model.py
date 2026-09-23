"""Host model of the block-RAM trit packing experiment (t27/rtl/bram_trit_*.t27).

An independent Python statement of the rules the device runs: the trit stream
(a 64-bit Fibonacci LFSR, two stream bits per trit), the three word formats,
the activation rule and the per-engine results. tools/generate-bram-bench.py
uses it for the manifest; tools/fpga-bram-capture.py for the comparison.
"""
from __future__ import annotations

FMT_B2, FMT_D5, FMT_D5D2, FMT_D5P = 0, 1, 2, 3
FORMAT_NAMES = {FMT_B2: "b2", FMT_D5: "d5", FMT_D5D2: "d5d2", FMT_D5P: "d5p"}
LANES = {FMT_B2: 18, FMT_D5: 20, FMT_D5D2: 22}
# d5p (read-only store only): a pair of 36-bit words holds 45 trits. Each word has
# four dense5 bytes (20 trits) in bits 31:0; the two parity nibbles together are one
# more dense5 byte, low nibble in the first word and high nibble in the second, whose
# five trits follow the second word's twenty. Its trits per pair are PAIR_LANES.
PAIR_LANES = 45
WORD_BITS = 36
WORD_MASK = (1 << WORD_BITS) - 1
MASK64 = (1 << 64) - 1
DEFAULT_SEED = 0x7E27_5EED_0B12_A777


def advance(state: int, bits: int) -> int:
    """The LFSR state after `bits` single steps (bits <= 44).

    One step: out = s[0]; s = (s >> 1) | ((s[0]^s[1]^s[3]^s[4]) << 63). For
    n <= 60 steps every feedback bit reads only original state bits, so the
    n output bits are the low n bits of the state and the new top bits are
    parities of the original state.
    """
    nxt = state >> bits
    for k in range(bits):
        fb = ((state >> k) ^ (state >> (k + 1)) ^ (state >> (k + 3)) ^ (state >> (k + 4))) & 1
        nxt |= fb << (64 - bits + k)
    return nxt & MASK64


def lanes_from(state: int, lanes: int) -> int:
    """Lane codes of one word: two stream bits per trit, 11 read as zero."""
    out = 0
    for j in range(lanes):
        v = (state >> (2 * j)) & 3
        out |= (0 if v == 3 else v) << (2 * j)
    return out


def trit_of_lane(code: int) -> int:
    return {0: 0, 1: 1, 2: -1}[code]


def digit_of_lane(code: int) -> int:
    return {0: 1, 1: 2, 2: 0}[code]


def encode_word(fmt: int, lanes: int) -> int:
    if fmt == FMT_B2:
        return lanes & ((1 << 36) - 1)
    word = 0
    for g in range(4):
        code = sum(digit_of_lane((lanes >> (2 * (5 * g + i))) & 3) * 3 ** i for i in range(5))
        word |= code << (8 * g)
    if fmt == FMT_D5D2:
        nib = digit_of_lane((lanes >> 40) & 3) + 3 * digit_of_lane((lanes >> 42) & 3)
        word |= nib << 32
    return word


def lane_of_digit(digit: int) -> int:
    return {0: 2, 1: 0, 2: 1}[digit]


def decode_word(fmt: int, word: int) -> tuple[int, int]:
    """(lanes, invalid groups) as the device decoder reports them."""
    word &= WORD_MASK
    lanes, bad = 0, 0
    if fmt == FMT_B2:
        for j in range(18):
            code = (word >> (2 * j)) & 3
            if code == 3:
                bad += 1
            else:
                lanes |= code << (2 * j)
        return lanes, bad
    for g in range(4):
        code = (word >> (8 * g)) & 255
        if code < 243:
            for i in range(5):
                lanes |= lane_of_digit((code // 3 ** i) % 3) << (2 * (5 * g + i))
        else:
            bad += 1
    if fmt == FMT_D5D2:
        nib = (word >> 32) & 15
        if nib < 9:
            lanes |= lane_of_digit(nib % 3) << 40
            lanes |= lane_of_digit((nib // 3) % 3) << 42
        else:
            bad += 1
    return lanes, bad


def activation(index: int) -> int:
    """Activation of global trit index i: (i mod 8) + 1."""
    return (index & 7) + 1


def rotl1(x: int) -> int:
    return ((x << 1) | (x >> 63)) & MASK64


def engine_results(fmt: int, words: int, seed: int = DEFAULT_SEED, verify: bool = True) -> dict:
    """What one engine must report after writing and reading `words` words."""
    k = LANES[fmt]
    state = seed
    pos = neg = dot = chk = 0
    index = 0
    for _ in range(words):
        lanes = lanes_from(state, k)
        word = encode_word(fmt, lanes)
        if verify:
            back, bad = decode_word(fmt, word)
            assert back == lanes and bad == 0, (fmt, hex(lanes), hex(word))
        for j in range(k):
            code = (lanes >> (2 * j)) & 3
            if code == 1:
                pos += 1
                dot += ((index + j) & 7) + 1
            elif code == 2:
                neg += 1
                dot -= ((index + j) & 7) + 1
        index += k
        chk = rotl1(chk) ^ word
        state = advance(state, 2 * k)
    return {"format": fmt, "name": FORMAT_NAMES[fmt], "lanes": k, "words": words, "trits": words * k,
            "write_ticks": words, "read_ticks": words + 1, "bad_words": 0, "invalid_groups": 0,
            "pos": pos, "neg": neg, "dot": dot, "chk": chk}


def lane_of_trit(trit: int) -> int:
    return {0: 0, 1: 1, -1: 2}[trit]


def words_of_trits(fmt: int, trits) -> list[tuple[int, int]]:
    """(lanes, stored word) of every word of a fixed trit sequence, lane j of word w
    holding trit w * LANES + j; the length must be a multiple of LANES."""
    k = LANES[fmt]
    if len(trits) % k:
        raise ValueError(f"{len(trits)} trits are not a whole number of {k}-trit words")
    out = []
    for w in range(0, len(trits), k):
        lanes = 0
        for j in range(k):
            lanes |= lane_of_trit(trits[w + j]) << (2 * j)
        out.append((lanes, encode_word(fmt, lanes)))
    return out


def dense5(digits) -> int:
    return sum(d * 3 ** i for i, d in enumerate(digits))


def pair_words(trits) -> list[tuple[int, int]]:
    """(lanes, stored word) of every word of the d5p layout: per 45 trits, word A holds
    lanes 0-19 (trits 0-19) and word B lanes 0-24 (trits 20-39, then 40-44 from the
    parity byte P = dense5(trits 40-44), P[3:0] in A's bits 35:32, P[7:4] in B's)."""
    if len(trits) % PAIR_LANES:
        raise ValueError(f"{len(trits)} trits are not a whole number of {PAIR_LANES}-trit pairs")
    out = []
    for g in range(0, len(trits), PAIR_LANES):
        group = trits[g:g + PAIR_LANES]
        lanes_a = sum(lane_of_trit(t) << (2 * j) for j, t in enumerate(group[:20]))
        lanes_b = sum(lane_of_trit(t) << (2 * j) for j, t in enumerate(group[20:45]))
        parity = dense5([t + 1 for t in group[40:45]])
        word_a = encode_word(FMT_D5, lanes_a) | ((parity & 15) << 32)
        word_b = encode_word(FMT_D5, lanes_b & ((1 << 40) - 1)) | ((parity >> 4) << 32)
        out += [(lanes_a, word_a), (lanes_b, word_b)]
    return out


def decode_pair(word_a: int, word_b: int) -> tuple[int, int, int]:
    """(lanes of A, lanes of B, invalid groups) as the d5p store decodes a pair."""
    lanes_a, bad_a = decode_word(FMT_D5, word_a & ((1 << 32) - 1))
    lanes_b, bad_b = decode_word(FMT_D5, word_b & ((1 << 32) - 1))
    parity = ((word_b >> 32) & 15) << 4 | ((word_a >> 32) & 15)
    if parity < 243:
        for i in range(5):
            lanes_b |= lane_of_digit((parity // 3 ** i) % 3) << (2 * (20 + i))
        return lanes_a, lanes_b, bad_a + bad_b
    return lanes_a, lanes_b, bad_a + bad_b + 1


def rom_results(fmt: int, trits, verify: bool = True, pipe: int = 0) -> dict:
    """What the read-only store (t27/rtl/bram_trit_rom.t27) must report for a fixed
    trit sequence: the engine's fields with no write phase, plus lanes_check, the
    rotate-xor checksum of the decoded lanes the store compares itself with. With
    pipe = 1 (three more register stages) a run takes three ticks more."""
    k = PAIR_LANES if fmt == FMT_D5P else LANES[fmt]
    pos = neg = dot = chk = lchk = 0
    words = pair_words(trits) if fmt == FMT_D5P else words_of_trits(fmt, trits)
    if verify and fmt == FMT_D5P:
        for w in range(0, len(words), 2):
            a, b, bad = decode_pair(words[w][1], words[w + 1][1])
            assert (a, b, bad) == (words[w][0], words[w + 1][0], 0), (w, hex(words[w][1]), hex(words[w + 1][1]))
    for w, (lanes, word) in enumerate(words):
        if verify and fmt != FMT_D5P:
            back, bad = decode_word(fmt, word)
            assert back == lanes and bad == 0, (fmt, w, hex(lanes), hex(word))
        chk = rotl1(chk) ^ word
        lchk = rotl1(lchk) ^ lanes
    for i, t in enumerate(trits):
        if t == 1:
            pos += 1
            dot += (i & 7) + 1
        elif t == -1:
            neg += 1
            dot -= (i & 7) + 1
    return {"format": fmt, "name": FORMAT_NAMES[fmt], "lanes": k, "words": len(words), "trits": len(trits),
            "write_ticks": 0, "read_ticks": len(words) + (4 if pipe else 1), "bad_words": 0, "invalid_groups": 0,
            "pos": pos, "neg": neg, "dot": dot, "chk": chk, "lanes_check": lchk, "pipe": pipe}


def stream_trits(count: int, seed: int = DEFAULT_SEED) -> list[int]:
    """The first `count` trits of the stream, one LFSR step per stream bit."""
    state, out = seed, []
    for _ in range(count):
        lo, hi = state & 1, (state >> 1) & 1
        v = lo | (hi << 1)
        out.append(trit_of_lane(0 if v == 3 else v))
        state = advance(state, 2)
    return out
