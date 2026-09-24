#!/usr/bin/env python3
"""Independent bit-exact reference model for OCP FP8 E4M3FN (torch / ml_dtypes float8_e4m3fn).

Written separately from the RTL in t27/rtl/fp8_e4m3_{decode,encode}.t27: the RTL shifts
and rounds bit fields, this model enumerates the exact rational value of every code and
picks the nearest one (ties to the even code). Standard library only.

decode(code)            -> binary32 bit pattern of the code's exact value (NaN -> 0x7FC00000 | sign)
encode(bits, saturate)  -> E4M3 code for a binary32 input, round to nearest, ties to even;
                           overflow and infinities give +-448 when saturate else NaN.
"""
from fractions import Fraction
import struct, bisect

BIAS = 7
def code_value(code: int):
    """Exact value of a code as a Fraction, or None for NaN."""
    s = -1 if code & 0x80 else 1
    e = (code >> 3) & 0xF
    m = code & 0x7
    if e == 0xF and m == 0x7:
        return None
    if e == 0:
        return s * Fraction(m, 8) * Fraction(1, 2 ** (BIAS - 1))
    return s * (1 + Fraction(m, 8)) * (Fraction(2) ** (e - BIAS))

# Non-negative finite magnitudes, codes 0..126, strictly increasing.
POS = [code_value(c) for c in range(127)]
assert all(POS[i] < POS[i + 1] for i in range(126))
# The value code 127 would have had (480); it is NaN in E4M3FN, so reaching it means overflow.
VIRTUAL_NEXT = (1 + Fraction(7, 8)) * 2 ** 8

def f32_bits(x: float) -> int:
    return struct.unpack(">I", struct.pack(">f", x))[0]

def bits_value(bits: int):
    """Exact value of binary32 bits: Fraction, or 'nan' / 'inf' / '-inf'."""
    s = -1 if bits >> 31 else 1
    e = (bits >> 23) & 0xFF
    f = bits & 0x7FFFFF
    if e == 0xFF:
        return "nan" if f else ("inf" if s > 0 else "-inf")
    if e == 0:
        return s * Fraction(f, 2 ** 23) * Fraction(1, 2 ** 126)
    return s * (1 + Fraction(f, 2 ** 23)) * (Fraction(2) ** (e - 127))

def decode(code: int) -> int:
    v = code_value(code)
    sign = (code >> 7) << 31
    if v is None:
        return sign | 0x7FC00000
    if v == 0:
        return sign
    b = f32_bits(float(v))          # every E4M3 value is exact in binary32
    assert bits_value(b) == v
    return b

def encode(bits: int, saturate: bool) -> int:
    sign = (bits >> 31) << 7
    v = bits_value(bits)
    overflow = sign | (0x7E if saturate else 0x7F)
    if v == "nan":
        return sign | 0x7F
    if v in ("inf", "-inf"):
        return overflow
    a = abs(v)
    if a >= VIRTUAL_NEXT:
        return overflow
    table = POS + [VIRTUAL_NEXT]
    i = bisect.bisect_left(table, a)          # table[i-1] < a <= table[i]
    if table[i] == a:
        lo = hi = i
    else:
        lo, hi = i - 1, i
    if lo == hi:
        c = lo
    else:
        dl, dh = a - table[lo], table[hi] - a
        c = lo if dl < dh else hi if dh < dl else (lo if lo % 2 == 0 else hi)
    if c == 127:                               # rounded onto the NaN slot: overflow
        return overflow
    return sign | c

if __name__ == "__main__":
    assert encode(f32_bits(1000.0), True) == 0x7E and encode(f32_bits(1000.0), False) == 0x7F
    assert encode(f32_bits(464.0), False) == 0x7E and encode(f32_bits(465.0), False) == 0x7F
    assert decode(0x7E) == f32_bits(448.0) and decode(0x01) == f32_bits(2.0 ** -9)
    print("e4m3_ref self-check ok")
