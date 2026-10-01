"""Independent integer oracle for gf16-wide-norm-v1 (issue #109).

Only the product's exactly representable binary64 intermediate uses host
floating point. Normalization and GF16 rounding use unbounded integers.
"""
import bisect
import math
from tools import gf16_reference as gf

MAX_LENGTH = 6912
EPS_MANTISSA = 10995116  # binary32(1e-5) = this integer * 2**-40
GF_Q39 = [int(v * 2**39) for v in gf.POSITIVE]


def finite(word):
    return 0 <= word <= 0xffff and word & 0x7e00 != 0x7e00


def rne(n, d):
    q, r = divmod(n, d)
    return q + (2*r > d or (2*r == d and q & 1))


def scale_integer(n, shift):
    return n << shift if shift >= 0 else rne(n, 1 << -shift)


def product(gate, up):
    if not finite(gate) or not finite(up):
        raise ValueError('finite GF16 words required')
    g, u = gf.value(gate), gf.value(up)
    # Three ten-bit significands need at most 30 bits: this binary64
    # expression is exact; packing performs the only binary32 rounding.
    return gf.f32_bits(max(g, 0.0)**2 * u)


def encode_ratio(numerator, denominator, sign=0):
    """Nearest GF16 by distances to its value lattice, without double rounding."""
    scaled = numerator << 39
    high = bisect.bisect_left(GF_Q39, scaled // denominator)
    while high < len(GF_Q39) and GF_Q39[high]*denominator < scaled:
        high += 1
    if high == len(GF_Q39):
        midpoint = (GF_Q39[-1] + (1 << 71)) * denominator
        code = 0x7e00 if scaled*2 >= midpoint else 0x7dff
    elif high == 0:
        code = 0
    else:
        low = high-1
        left, right = scaled-GF_Q39[low]*denominator, GF_Q39[high]*denominator-scaled
        code = low if left < right or (left == right and low % 2 == 0) else high
    return sign | code


def multiply(a, b):
    if not finite(a) or not finite(b):
        raise ValueError('finite GF16 words required')
    return gf.encode(gf.f32_bits(gf.value(a)*gf.value(b)))


def normalize(products, weights):
    n = len(products)
    if not 1 <= n <= MAX_LENGTH or len(weights) != n:
        raise ValueError('equal row lengths in 1..6912 required')
    if any(not finite(w) for w in weights):
        raise ValueError('finite GF16 weights required')
    if any(not 0 <= p <= 0xffffffff or (p & 0x7fffffff and
           not 10 <= (p >> 23) & 255 <= 222) for p in products):
        raise ValueError('product outside finite GF16 product domain')
    peak = max((p >> 23) & 255 for p in products)
    block = max(peak-167, -54)
    magnitudes = [scale_integer((p & 0x7fffff) | (0x800000 if p & 0x7fffffff else 0),
                               ((p >> 23) & 255)-150-block)
                  for p in products]
    total = sum(v*v for v in magnitudes)
    mean = rne(total, n)
    epsilon = scale_integer(EPS_MANTISSA, -40-2*block)
    radicand = (mean+epsilon) << 64
    root = math.isqrt(radicand)
    assert max(magnitudes) < 1 << 41 and total < 1 << 95
    assert mean+epsilon < 1 << 93 and 0 < root < 1 << 79
    unit = [encode_ratio(v << 32, root, (p >> 16) & 0x8000)
            for v, p in zip(magnitudes, products)]
    output = [multiply(v, w) for v, w in zip(unit, weights)]
    return {'block_exponent': block, 'magnitudes': magnitudes, 'sum_squares': total,
            'mean': mean, 'epsilon': epsilon, 'root': root, 'unit': unit,
            'output': output, 'overflow': sum(not finite(v) for v in output)}


def row(gate, up, weights):
    if len(gate) != len(up):
        raise ValueError('gate/up lengths differ')
    products = [product(g, u) for g, u in zip(gate, up)]
    return {'product': products, **normalize(products, weights)}
