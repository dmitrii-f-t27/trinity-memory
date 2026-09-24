"""Host statement of the DDR3 pattern test's data (t27/rtl/fpga_ddr3_pattern.t27, issue #61).

An independent Python statement of the rules the t27 module implements, used by
tests/test_ddr3_pattern.py to check the C that the pinned compiler generates from
the module's functions, and by the Icarus test to predict what a fault costs.

Data of a burst: the Wishbone data of one BL8 burst is 64 * BYTE_LANES bits, taken
as BYTE_LANES 64-bit words (word w = bits [64w, 64w+63]). Word w of the burst at
address a in one pass is

    data(key, a, w, complement) = kdata ^ lin((w << 32) | a),   kdata = key or ~key

`lin` is nine xorshift steps of 64 bits, three triples (each step x ^= x << s or
x ^= x >> s is invertible), so it is a bijection and linear over GF(2): two different (address,
word) pairs never get the same word, whatever the key. A stuck or aliased address
line makes two addresses of the region share storage, and the one read back then
carries the other's data, which differs in at least one bit of every word. The
complement pass writes ~data, so every stored bit is written and read as 0 and as 1
in one round: a stuck DQ bit or a dropped write shows in one of the two passes.

The round key is derived from the seed, the controller clock count at which
calibration completed (it varies from load to load) and the round number:

    key = lin(lin((seed << 32 | clocks mod 2^32) + (round << 32 | round)) + KADD)
"""
from __future__ import annotations

M64 = (1 << 64) - 1
M32 = (1 << 32) - 1
KADD = 6364136223846793005          # below 2^63, so gen-c's untyped #define stays a signed 64-bit literal
TRIPLES = ((13, 7, 17), (21, 35, 4), (9, 29, 25))


def lin(x: int) -> int:
    x &= M64
    for left, right, left2 in TRIPLES:
        x ^= (x << left) & M64
        x ^= x >> right
        x ^= (x << left2) & M64
    return x


def base_key(seed: int, calib_clocks: int) -> int:
    return ((seed & M32) << 32) | (calib_clocks & M32)


def round_key(base: int, rnd: int) -> int:
    k = (base + (((rnd & M32) << 32) | (rnd & M32))) & M64
    k = lin(k)
    k = (k + KADD) & M64
    return lin(k)


def word(key: int, addr: int, w: int, complement: bool) -> int:
    kdata = key ^ M64 if complement else key
    return kdata ^ lin(((w & 0xFFFFFFFF) << 32) | (addr & M32))


def burst(key: int, addr: int, lanes: int, complement: bool) -> int:
    """The whole Wishbone data word of one burst (64 * lanes bits), word 0 in the low bits."""
    value = 0
    for w in range(lanes):
        value |= word(key, addr, w, complement) << (64 * w)
    return value


def popcount(x: int) -> int:
    return bin(x).count("1")


def fold(diff: int, lanes: int) -> int:
    """DQ bits that differ in a 64-bit word: byte b of the word is byte lane b % lanes."""
    mask = 0
    for b in range(8):
        byte = (diff >> (8 * b)) & 0xFF
        mask |= byte << (8 * (b % lanes))
    return mask
