"""Independent value-based oracle for gf16-rne-gradual-v1 (issue #103).

All decoded GF16 values and binary32 inputs are exact in Python binary64.
Encoding searches representable values instead of duplicating RTL bit shifts.
"""
import bisect
import math
import struct


def f32_value(bits):
    return struct.unpack('>f', struct.pack('>I', bits))[0]


def f32_bits(value):
    return struct.unpack('>I', struct.pack('>f', value))[0]


def value(bits):
    sign = -1 if bits & 0x8000 else 1
    exponent, mantissa = (bits >> 9) & 63, bits & 511
    if exponent == 63:
        return math.nan if mantissa else sign * math.inf
    if exponent == 0:
        return sign * math.ldexp(float(mantissa), -39)
    return sign * math.ldexp(1 + mantissa / 512, exponent - 31)


POSITIVE = tuple(value(bits) for bits in range(0x7e00))
OVERFLOW_MIDPOINT = 2**32 - 2**21


def encode(bits):
    x = f32_value(bits)
    if math.isnan(x):
        return 0x7e01
    sign = 0x8000 if math.copysign(1, x) < 0 else 0
    x = abs(x)
    if x >= OVERFLOW_MIDPOINT:
        return sign | 0x7e00
    hi = bisect.bisect_left(POSITIVE, x)
    if hi == 0:
        return sign
    if hi == len(POSITIVE):
        return sign | (hi - 1)
    lo = hi - 1
    left, right = x - POSITIVE[lo], POSITIVE[hi] - x
    return sign | (lo if left < right or (left == right and lo % 2 == 0) else hi)


def decode(bits):
    x = value(bits)
    return 0x7fc00000 if math.isnan(x) else f32_bits(x)
