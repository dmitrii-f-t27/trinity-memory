"""Explicit scalar boundaries for the pinned AutoBitLinear FFN (issue #107).

Optional experiment dependency: torch/numpy, not part of the library runtime.
The GF16 path stores exact GF16 values in FP32 carriers; accumulations are FP32.
"""
import hashlib
import numpy as np
import torch
import torch.nn.functional as F
from tools import gf16_reference as gf

POSITIVE = np.array(gf.POSITIVE, dtype=np.float64)
DECODE = np.array([gf.value(i) for i in range(65536)], dtype=np.float32)


def gf16_words(x):
    x = np.asarray(x, dtype=np.float32)
    mag = np.abs(x).astype(np.float64)
    hi = np.minimum(np.searchsorted(POSITIVE, mag), len(POSITIVE)-1)
    lo = np.maximum(hi-1, 0)
    left, right = mag-POSITIVE[lo], POSITIVE[hi]-mag
    index = np.where((left < right) | ((left == right) & (lo % 2 == 0)), lo, hi)
    index = np.where(mag >= gf.OVERFLOW_MIDPOINT, 0x7e00, index)
    words = index.astype(np.uint16) | np.where(np.signbit(x), 0x8000, 0).astype(np.uint16)
    return np.where(np.isnan(x), 0x7e01, words).astype(np.uint16)


def round_storage(x, profile):
    if profile == 'bf16':
        return x.to(torch.bfloat16).float()
    if profile == 'f32':
        return x.float()
    if profile != 'gf16':
        raise ValueError('unknown storage profile')
    return torch.from_numpy(DECODE[gf16_words(x.detach().float().cpu().numpy())].copy())


def actquant(x, profile):
    # Upstream ActQuant.forward first promotes to FP32, even for BF16 inputs.
    xf = x.float()
    scale = 127.0 / xf.abs().amax(dim=-1, keepdim=True).clamp(min=1e-5)
    codes = (xf * scale).round().clamp(-128,127)
    return round_storage(codes / scale, profile), codes.to(torch.int8), scale


class RangeFailure(ValueError):
    def __init__(self, boundary, stages, rounds, codes):
        super().__init__(f'nonfinite at {boundary}')
        self.boundary, self.stages, self.rounds, self.codes = boundary, stages, rounds, codes


def explicit_ffn(layer, x, profile, wide_product=False):
    rounds = {}
    stages = {'x': x.float()}
    codes = {}

    def rnd(name, value, wide=False):
        storage = 'f32' if wide else profile
        out = round_storage(value, storage)
        minimum = 2**-30 if storage == 'gf16' else 2**-126
        rounds[name] = {
            'storage': storage, 'input_peak': float(value.abs().max()),
            'overflow': int((torch.isfinite(value) & torch.isinf(out)).sum()),
            'underflow_to_zero': int(((value != 0) & (out == 0)).sum()),
            'subnormal_output': int(((out.abs() > 0) & (out.abs() < minimum)).sum()),
        }
        if not torch.isfinite(out).all():
            raise RangeFailure(name, stages, rounds, codes)
        return out

    def norm(name, value, weight):
        weight = rnd(name+'_weight', weight.float())
        variance = value.float().pow(2).mean(-1, keepdim=True)
        normalized = rnd(name+'_unit', value * torch.rsqrt(variance + layer.post_attention_layernorm.variance_epsilon))
        return rnd(name, weight * normalized)

    def linear(name, value, module):
        _, integer, scale = actquant(value, 'f32')
        # Count ActQuant output rounding with exactly the FP32 input to it.
        quant = rnd(name+'_actquant', integer.float() / scale)
        codes[name] = integer
        # BF16 control deliberately uses the actual BF16 GEMM kernel.
        dtype = torch.bfloat16 if profile == 'bf16' else torch.float32
        dot = F.linear(quant.to(dtype), module.weight.to(dtype)).float()
        dot = rnd(name+'_dot', dot)
        weight_scale = rnd(name+'_scale', module.weight_scale.float())
        return rnd(name, dot * weight_scale)

    x = rnd('x', x.float())
    stages['x'] = x
    stages['h'] = norm('h', x, layer.post_attention_layernorm.weight)
    stages['g'] = linear('g', stages['h'], layer.mlp.gate_proj)
    stages['u'] = linear('u', stages['h'], layer.mlp.up_proj)
    relu2 = rnd('relu2', stages['g'].clamp(min=0).pow(2), wide=wide_product)
    stages['a'] = rnd('a', relu2 * stages['u'], wide=wide_product)
    stages['s'] = norm('s', stages['a'], layer.mlp.ffn_sub_norm.weight)
    stages['y'] = linear('y', stages['s'], layer.mlp.down_proj)
    return stages, rounds, codes


def metrics(reference, candidate):
    if reference.shape != candidate.shape or reference.numel() == 0:
        raise ValueError('nonempty equal shapes required')
    a, b = reference.double(), candidate.double()
    if not torch.isfinite(a).all() or not torch.isfinite(b).all():
        raise ValueError('nonfinite metric input')
    energy, error = float(a.square().sum()), float((a-b).square().sum())
    return {'elements': a.numel(), 'mismatches': int((a != b).sum()),
            'nmse': error / energy if energy else (0 if error == 0 else None),
            'max_abs_error': float((a-b).abs().max())}


def digest(tensor):
    data = tensor.detach().float().cpu().numpy().astype('<f4').tobytes()
    return hashlib.sha256(data).hexdigest()
