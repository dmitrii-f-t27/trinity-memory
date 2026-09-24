#!/usr/bin/env python3
"""Independent reference for the three ternary packings decoded by t27/rtl/{dense5,baseline5,sparse41}_decoder.t27.

Written from the format definitions, not from the RTL. Output word layout (16 bits): lanes of 2 bits, lane i at
bits [2i+1:2i], lane encoding 00 = 0, 01 = +1, 10 = -1; a validity bit above the lanes (bit 10 for the five-lane
formats, bit 8 for sparse 4:1); an invalid code decodes to 0. Standard library only.

dense5    : one byte holds five trits in base 3, code = sum (t_i + 1) * 3^i, codes 243..255 are invalid.
baseline5 : ten bits hold five 2-bit lanes directly; a lane equal to 11 is reserved, so the code is invalid.
sparse4:1 : one byte holds four trits with at most one non-zero: 0 = all zero, 2k+1 = -1 in lane k,
            2k+2 = +1 in lane k (k = 0..3); codes 9..255 are invalid.
"""
from itertools import product
LANE = {0: 0b00, 1: 0b01, -1: 0b10}

def pack(trits, valid_bit):
    w = 1 << valid_bit
    for i, t in enumerate(trits):
        w |= LANE[t] << (2 * i)
    return w

DENSE5 = {}
for trits in product((-1, 0, 1), repeat=5):
    code = sum((t + 1) * 3 ** i for i, t in enumerate(trits))
    DENSE5[code] = pack(trits, 10)
def dense5(code):
    return DENSE5.get(code, 0)

def baseline5(code):
    lanes = [(code >> (2 * i)) & 3 for i in range(5)]
    if code >= 1024 or 3 in lanes:
        return 0
    inv = {v: k for k, v in LANE.items()}
    return pack([inv[l] for l in lanes], 10)

def sparse41(code):
    if code == 0:
        return pack([0, 0, 0, 0], 8)
    if 1 <= code <= 8:
        k, positive = (code - 1) // 2, code % 2 == 0
        trits = [0, 0, 0, 0]; trits[k] = 1 if positive else -1
        return pack(trits, 8)
    return 0

if __name__ == "__main__":
    assert len(DENSE5) == 243 and dense5(121) == 1024 and dense5(0) == 1706 and dense5(242) == 1365
    assert baseline5(0) == 1024 and baseline5(682) == 1706 and baseline5(3) == 0 and baseline5(768) == 0
    assert sparse41(1) == 258 and sparse41(2) == 257 and sparse41(8) == 320 and sparse41(9) == 0
    print("ternary_ref self-check ok")
