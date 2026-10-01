#!/usr/bin/env python3
"""Issue #105: GF16/BF16 storage precision on real FFN weights, synthetic inputs.

This is a boundary-rounding experiment, not a 16-bit arithmetic implementation
or a replay of AutoBitLinear/ActQuant. See docs/gf16-ffn-quantization.md.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import random
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import ffn_reference as fr, gf16_reference as gf

SCHEMA = 'trinity.ffn-quantization.v1'
PROFILES = {'gf16': 'gf16-rne-gradual-v1', 'bf16': 'bf16-rne-gradual-v1'}
REPORT = ROOT / 'reports/numeric/ffn-quantization-v1.json'


def bf16_encode(bits):
    if bits & 0x7f800000 == 0x7f800000 and bits & 0x7fffff:
        return 0x7fc0
    return ((bits + 0x7fff + ((bits >> 16) & 1)) >> 16) & 0xffff


def binary32(value):
    try:
        return gf.f32_bits(float(value))
    except OverflowError:
        return 0xff800000 if value < 0 else 0x7f800000


def metrics(reference, candidate):
    if not reference or len(reference) != len(candidate):
        raise ValueError('metrics require nonempty, equal-length vectors')
    bad_ref = sum(not math.isfinite(v) for v in reference)
    bad_out = sum(not math.isfinite(v) for v in candidate)
    result = {'count': len(reference), 'nonfinite_reference': bad_ref,
              'nonfinite_candidate': bad_out, 'nmse': None, 'max_abs_error': None,
              'relative_l2': None}
    # Never hide range failures by dropping nonfinite lanes from the metric.
    if bad_ref or bad_out:
        return result
    energy = math.fsum(v*v for v in reference)
    squared_error = math.fsum((a-b)*(a-b) for a,b in zip(reference, candidate))
    nmse = squared_error / energy if energy else (0.0 if squared_error == 0 else None)
    result.update(nmse=nmse, max_abs_error=max(abs(a-b) for a,b in zip(reference,candidate)),
                  relative_l2=math.sqrt(nmse) if nmse is not None else None)
    return result


def quantize(values, fmt):
    if fmt not in PROFILES:
        raise ValueError(f'unknown format: {fmt}')
    minimum = 2**-30 if fmt == 'gf16' else 2**-126
    out, source, words = [], [], []
    counts = dict.fromkeys(('input_nonfinite', 'f32_overflow', 'f32_underflow_to_zero',
                            'format_overflow', 'tiny_input', 'subnormal_output',
                            'underflow_to_zero'), 0)
    for v in values:
        bits = binary32(v)
        x = gf.f32_value(bits)
        word = gf.encode(bits) if fmt == 'gf16' else bf16_encode(bits)
        y = gf.value(word) if fmt == 'gf16' else gf.f32_value(word << 16)
        source.append(x); out.append(y); words.append(word)
        counts['input_nonfinite'] += not math.isfinite(v)
        counts['f32_overflow'] += math.isfinite(v) and math.isinf(x)
        counts['f32_underflow_to_zero'] += v != 0 and x == 0
        counts['format_overflow'] += math.isfinite(x) and math.isinf(y)
        counts['tiny_input'] += 0 < abs(x) < minimum
        counts['subnormal_output'] += 0 < abs(y) < minimum
        counts['underflow_to_zero'] += x != 0 and y == 0
    return out, {'events': counts, 'vs_binary32': metrics(source, out),
                 'binary32_vs_input': metrics(values, source),
                 'bits_sha256_le16': hashlib.sha256(struct.pack(f'<{len(words)}H', *words)).hexdigest()}


def validate(model, x):
    rows, cols = model['shapes']['gate']
    outputs, inner = model['shapes']['down']
    if min(rows, cols, outputs) <= 0 or model['shapes']['up'] != [rows, cols] or inner != rows:
        raise ValueError('inconsistent FFN shapes')
    for name, length in [('gate', rows*cols), ('up', rows*cols), ('down', outputs*rows),
                         ('w_post', cols), ('w_sub', rows)]:
        if len(model[name]) != length:
            raise ValueError(f'{name}: wrong length')
    if len(x) != cols or any(not math.isfinite(v) for v in x):
        raise ValueError('input must be a finite hidden-width vector')


class RangeFailure(Exception):
    pass


def propagate(model, x, fmt, reference, wide_a=False):
    """Quantize coefficients and boundaries; fsum matvec/norm stay binary64."""
    validate(model, x)
    records = {}

    def store(name, values, target=None):
        out, record = quantize(values, fmt)
        if target is not None:
            record['vs_f64_oracle'] = metrics(target, out)
        records[name] = record
        if any(not math.isfinite(v) for v in out):
            raise RangeFailure(name)
        return out

    def norm(v, weights):
        inv = 1.0 / math.sqrt(math.fsum(t*t for t in v) / len(v) + fr.EPS)
        return [t * inv * w for t, w in zip(v, weights)]

    def stage(name, values):
        if name == 'a' and wide_a:
            out = values
            records[name] = {'storage': 'binary64', 'vs_f64_oracle': metrics(reference[name], out)}
        else:
            out = store(name, values, reference[name])
        return out

    try:
        w_post = store('w_post', model['w_post'])
        w_sub = store('w_sub', model['w_sub'])
        scales = store('scales', [model['scales'][k] for k in ('gate', 'up', 'down')])
        xq = store('x', x, x)
        rows, cols = model['shapes']['gate']
        dr, dc = model['shapes']['down']
        h = stage('h', norm(xq, w_post))
        g = stage('g', [scales[0]*v for v in fr._rows_dot(model['gate'], rows, cols, h)])
        u = stage('u', [scales[1]*v for v in fr._rows_dot(model['up'], rows, cols, h)])
        a = stage('a', [max(t,0.0)*max(t,0.0)*v for t,v in zip(g,u)])
        s = stage('s', norm(a, w_sub))
        stage('y', [scales[2]*v for v in fr._rows_dot(model['down'], dr, dc, s)])
        status, blocked_at = 'finite', None
    except RangeFailure as error:
        status, blocked_at = 'nonfinite', str(error)
    return {'status': status, 'blocked_at': blocked_at, 'boundaries': records}


def probes(width):
    def seeded(seed):
        rng = random.Random(seed)
        return [float(rng.randint(-128,127)) for _ in range(width)]
    x27, x28 = seeded(27), seeded(28)
    return {
        'seed27_int8': x27,
        'seed28_scaled': [v / 64 for v in x28],
        'seed27_tiny': [v * 2**-30 for v in x27],
        'zero': [0.0] * width,
        'range_stress': [2.0**40 if i % 2 == 0 else -2.0**40 for i in range(width)],
    }


def case_report(model, name, x):
    validate(model, x)
    reference = fr.reference_f64(model, x)
    if any(not math.isfinite(v) for values in reference.values() for v in values):
        raise ValueError('nonfinite reference')
    isolated, paths = {}, {}
    values = {'x': x, **reference, 'w_post': model['w_post'], 'w_sub': model['w_sub'],
              'scales': [model['scales'][k] for k in ('gate', 'up', 'down')]}
    for fmt in PROFILES:
        isolated[fmt] = {}
        for key, vec in values.items():
            out, record = quantize(vec, fmt)
            record['vs_f64_oracle'] = metrics(vec, out)
            isolated[fmt][key] = record
        for wide in (False, True):
            key = fmt + ('_wide_a' if wide else '_stage16')
            paths[key] = propagate(model, x, fmt, reference, wide_a=wide)
    return {'name': name, 'input_kind': 'synthetic_hidden_state_probe',
            'input_sha256_le_f32': hashlib.sha256(struct.pack(f'<{len(x)}f', *x)).hexdigest(),
            'input_peak': max(map(abs,x)),
            'reference_peaks': {k: max(map(abs,v)) for k,v in reference.items()},
            'isolated': isolated, 'propagated': paths}


def provenance():
    data = json.loads((ROOT / 'fixtures/manifest.json').read_text())
    item = next(m for m in data['models'] if m['repo'] == fr.tc.BITNET['packed'].repo)
    return {k: item[k] for k in ('repo','revision')} | {
        'ranges': [r for r in item['ranges'] if 'ffn_reference' in r['used_by']],
        'compiler_revision': (ROOT / 'native/compiler.lock').read_text().strip(),
        'source_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                          for name in ('tools/ffn_reference.py', 'tools/gf16_reference.py',
                                       'tools/ffn_quantization.py')}}


def report():
    model = fr.load_ffn()
    result = {'schema': SCHEMA, 'profiles': PROFILES, 'scope':
              'real layer-0 weights, synthetic inputs; boundary storage experiment; '
              'binary64 inner arithmetic; no ActQuant, captured text activations or inference-quality claim',
              'rms_norm_eps': fr.EPS,
              'rounding': 'binary64 intermediate -> binary32 RNE -> selected 16-bit profile; '
                          'wide_a keeps binary64 a, including before the second norm',
              'model': provenance(), 'shapes': model['shapes'],
              'packed_sha256': model['packed_sha256'], 'cases': []}
    for name, x in probes(model['shapes']['gate'][1]).items():
        print(f'quantization: {name}', file=sys.stderr, flush=True)
        result['cases'].append(case_report(model, name, x))
    return result


def stable(value):
    """Cross-platform report precision; comparisons still use full binary64."""
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('nonfinite report scalar')
        return float(format(value, '.10g'))
    if isinstance(value, list):
        return [stable(v) for v in value]
    if isinstance(value, dict):
        return {k: stable(v) for k,v in value.items()}
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--output', type=Path, default=REPORT)
    parser.add_argument('--check', action='store_true', help='recompute and compare committed report')
    args = parser.parse_args()
    os.environ['TRINITY_FIXTURES_OFFLINE'] = '1'
    text = json.dumps(stable(report()), indent=2, allow_nan=False) + '\n'
    if args.check:
        if args.output.read_text() != text:
            print(f'report mismatch: {args.output}', file=sys.stderr)
            return 1
        print('PASS FFN quantization report replay')
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
        print(args.output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
