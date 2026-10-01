"""Independent integer contract gf16-ffn-v1 (issue #111).

GF16 storage, bounded integer RMSNorm, rational ActQuant, exact ternary sums.
This deliberately specifies all arithmetic left on the host in iteration 4;
it is not bit-identical to platform-dependent FP32 GEMM/reciprocal kernels.
"""
from tools import gf16_wide_reference as wide

PROFILE = 'gf16-ffn-v1'
ONE = 0x3e00
EPS_Q40 = 10995116  # exact binary32(1e-5) in Q40


def fixed(word):
    """Exact signed integer word * 2**39; magnitude is below 2**71."""
    if not wide.finite(word):
        raise ValueError('finite GF16 word required')
    exponent, mantissa = (word >> 9) & 63, word & 511
    value = ((mantissa | 512) << (exponent-1)) if exponent else mantissa
    return -value if word & 0x8000 else value


def actquant(words):
    if not 1 <= len(words) <= 6912:
        raise ValueError('ActQuant length must be in 1..6912')
    values = [fixed(w)*2 for w in words]  # Q40 admits epsilon exactly
    maximum = max(EPS_Q40, max(map(abs, values)))
    codes = [(-1 if x < 0 else 1)*wide.rne(abs(x)*127, maximum) for x in values]
    if any(not -127 <= x <= 127 for x in codes):
        raise ValueError('ActQuant bound violated')
    quantized = [wide.encode_ratio(abs(k)*maximum, 127 << 40,
                                  0x8000 if k < 0 else 0) for k in codes]
    return quantized, codes, maximum


def linear(weights, rows, cols, vector, scale):
    if len(weights) != rows*cols or len(vector) != cols or rows <= 0:
        raise ValueError('projection shape mismatch')
    if not wide.finite(scale):
        raise ValueError('nonfinite scale')
    values = [fixed(w) for w in vector]
    totals = []
    for row in range(rows):
        total = 0
        for weight, value in zip(weights[row*cols:(row+1)*cols], values):
            if weight not in (-1, 0, 1):
                raise ValueError('invalid ternary weight')
            total += int(weight)*value
        if abs(total) >= 1 << 84:
            raise ValueError('ternary accumulator bound violated')
        totals.append(total)
    dots = [wide.encode_ratio(abs(v), 1 << 39, 0x8000 if v < 0 else 0)
            for v in totals]
    if not all(map(wide.finite, dots)):
        raise ValueError('nonfinite GF16 projection dot')
    output = [wide.multiply(v, scale) for v in dots]
    if not all(map(wide.finite, output)):
        raise ValueError('nonfinite GF16 scaled projection')
    return output, dots


def evaluate(model, x):
    """Model has flat ternary matrices and GF16-word weights/scales."""
    inner, hidden = model['shapes']['gate']
    output, down_cols = model['shapes']['down']
    if not (1 <= hidden <= 2560 and 1 <= inner <= 6912 and 1 <= output <= 2560):
        raise ValueError('unsupported FFN dimensions')
    if (model['shapes']['up'] != [inner, hidden] or down_cols != inner or
            len(x) != hidden or len(model['w_post']) != hidden or len(model['w_sub']) != inner):
        raise ValueError('inconsistent FFN dimensions')
    post = wide.row([ONE]*hidden, x, model['w_post'])
    if post['overflow']:
        raise ValueError('nonfinite post-norm')
    h = post['output']
    qh, ch, mh = actquant(h)
    g, gd = linear(model['gate'], inner, hidden, qh, model['scales']['gate'])
    u, ud = linear(model['up'], inner, hidden, qh, model['scales']['up'])
    sub = wide.row(g, u, model['w_sub'])
    if sub['overflow']:
        raise ValueError('nonfinite sub-norm')
    s = sub['output']
    qs, cs, ms = actquant(s)
    y, yd = linear(model['down'], output, inner, qs, model['scales']['down'])
    return {'profile': PROFILE, 'stages': {'h': h, 'g': g, 'u': u, 'a': sub['product'], 's': s, 'y': y},
            'actquant': {'h': qh, 's': qs}, 'codes': {'h': ch, 's': cs},
            'max_q40': {'h': mh, 's': ms}, 'dots': {'g': gd, 'u': ud, 'y': yd},
            'norms': {'h': post, 's': sub}}
