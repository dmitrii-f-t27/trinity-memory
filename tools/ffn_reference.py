#!/usr/bin/env python3
"""Layer-0 BitNet FFN oracle and the explicit Q16.16 FPGA arithmetic.

The FFN of microsoft/BitNet-b1.58-2B-4T layer 0 (config.json: hidden 2560,
intermediate 6912, hidden_act relu2, rms_norm_eps 1e-5):

    h = rmsnorm(x, post_attention_layernorm)      x is the 2560-wide input
    g = scale_g * (gate_proj @ h)                 gate_proj: [6912, 2560] trits
    u = scale_u * (up_proj @ h)                   up_proj:   [6912, 2560] trits
    a = relu2(g) * u                              relu2(g) = max(g, 0) ** 2
    s = rmsnorm(a, mlp.ffn_sub_norm)              the 6912-wide BitNet sub-norm
    y = scale_d * (down_proj @ s)                 down_proj: [2560, 6912] trits

The oracle evaluates the unquantized-activation FFN math in f64 (math.fsum)
from the cached fixtures the board tools read. It is not a bit-exact replay
of transformers AutoBitLinear: its ActQuant and bf16 rounding are not modeled.
The candidate FPGA arithmetic evaluates the same math again under an explicit
between-stage contract; no hardware implementation is claimed here:

* h, g, u, s, y are signed Q16.16 (s32); a is held wide as Q32.32 (s64),
  because relu2(g) * u of the real layer-0 weights overflows Q16.16 (the
  naive contract saturates 2792 of 6912 lanes) and the sub-norm that follows
  re-normalises it anyway.
* Every scalar operation rounds to nearest-even, exactly once: norms divide
  the exact s64/s128 numerator by the Q32.32/Q64.64 rms in one step (a
  pre-rounded Q16.16 factor would cost ~4e-4 relative on this layer), the
  ternary matvecs keep exact s64 accumulators (a trit only adds or subtracts
  an activation, so no product rounding happens inside a matvec), and the
  bf16 weight scale costs one rounding.
* Both norms use sqrt(mean(v*v) + epsilon), as BitNetRMSNorm does.
  The rms of h runs in Q32.32 and the rms of a in Q64.64. Python uses
  unbounded intermediates: summing squares of MAX_VECTOR signed 64-bit
  values requires 139 unsigned bits, not 128. RTL must budget these widths.
* Saturations are counted and clamped, never wrapped, including the signed
  64-bit relu2 product before the wide norm.

The report compares the two evaluation stage by stage, so a later board
capture is judged against a written contract instead of re-deriving one.

  python3 tools/ffn_reference.py               print the comparison summary
  python3 tools/ffn_reference.py --json OUT    also write the JSON report
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trinity_memory import fixtures as fx, formats as f, matvec as mv, ternary_check as tc  # noqa: E402

SCHEMA = "trinity.ffn-reference.v1"
HIDDEN, INTERMEDIATE = 2560, 6912
MAX_VECTOR = INTERMEDIATE
Q, QQ = 16, 32                       # fraction bits of the stage values / norm path
ONE_Q16 = 1 << Q
EPS = 1e-5                           # config.json rms_norm_eps
EPS_Q32 = int(EPS * (1 << QQ) + 0.5)
S32_MIN, S32_MAX = -(1 << 31), (1 << 31) - 1
S64_MIN, S64_MAX = -(1 << 63), (1 << 63) - 1
WIDE_SUMSQ_BITS = (MAX_VECTOR * (1 << 126)).bit_length()
SEED = 27                            # the house activation seed
ORACLE_SCOPE = "unquantized-activation FFN math; no ActQuant or bf16 execution rounding"
RMSNORM_CONTRACT = "sqrt(mean(x*x) + epsilon)"
UPSTREAM_SOURCES = {
    "rmsnorm": "https://github.com/huggingface/transformers/blob/7cd73d9df0c14b151c684b708a9f27d8d0349dfe/src/transformers/models/bitnet/modeling_bitnet.py",
    "autobitlinear": "https://github.com/huggingface/transformers/blob/d6abd1333078195535f461589b64f112b53688b2/src/transformers/integrations/bitnet.py",
    "config": "https://huggingface.co/microsoft/bitnet-b1.58-2B-4T/blob/04c3b9ad9361b824064a1f25ea60a8be9599b127/config.json",
}

LAYER = "model.layers.0"
TENSORS = {
    "gate": f"{LAYER}.mlp.gate_proj.weight",
    "up": f"{LAYER}.mlp.up_proj.weight",
    "down": f"{LAYER}.mlp.down_proj.weight",
    "w_post": f"{LAYER}.post_attention_layernorm.weight",
    "w_sub": f"{LAYER}.mlp.ffn_sub_norm.weight",
    "scale_gate": f"{LAYER}.mlp.gate_proj.weight_scale",
    "scale_up": f"{LAYER}.mlp.up_proj.weight_scale",
    "scale_down": f"{LAYER}.mlp.down_proj.weight_scale",
}


def rne(num: int, shift: int) -> int:
    """Round num / 2**shift to the nearest integer, halves to even."""
    if shift <= 0:
        return num
    unit = 1 << (shift - 1)
    q, r = divmod(num, 1 << shift)
    if r > unit or (r == unit and q & 1):
        q += 1
    return q


def div_rne(num: int, den: int) -> int:
    """Round num / den to the nearest integer, halves to even (den > 0)."""
    q, r = divmod(num, den)
    twice = 2 * r
    if twice > den or (twice == den and q & 1):
        q += 1
    return q


def to_q(value: float, bits: int = Q) -> int:
    """A float to Q`bits`.16-style fixed point, nearest-even."""
    scaled = abs(value) * (1 << bits)
    floor = math.floor(scaled)
    frac = scaled - floor
    n = floor + (1 if frac > 0.5 or (frac == 0.5 and floor & 1) else 0)
    return -n if value < 0 else n


EPS_Q64 = int(EPS * (1 << (QQ + QQ)) + 0.5)


def clamp_s32(value: int, saturations: list) -> int:
    if value < S32_MIN:
        saturations.append(-1)
        return S32_MIN
    if value > S32_MAX:
        saturations.append(1)
        return S32_MAX
    return value


def clamp_s64(value: int, saturations: list) -> int:
    if value < S64_MIN:
        saturations.append(-1)
        return S64_MIN
    if value > S64_MAX:
        saturations.append(1)
        return S64_MAX
    return value


def sha256_le32(values) -> str:
    return hashlib.sha256(struct.pack(f"<{len(values)}i", *values)).hexdigest()


def load_ffn() -> dict:
    """Trits, scales and norm weights of layer 0 from the fixture cache."""
    packed = tc.BITNET["packed"]

    def tensor(name, rows):
        info, dtype, blob = fx.safetensors_tensor(packed, name)
        if dtype != "U8":
            raise fx.FixtureError(f"{name}: expected packed U8, got {dtype}")
        if info.d0 * 4 != rows:
            raise fx.FixtureError(f"{name}: packed shape {info.d0} does not cover {rows} rows")
        values, _ = f.decode_hf_packed(blob, rows, info.d1)
        return values, blob

    def bf16(name, count):
        info, dtype, blob = fx.safetensors_tensor(packed, name)
        if dtype != "BF16" or (info.end - info.begin) != 2 * count:
            raise fx.FixtureError(f"{name}: expected BF16[{count}]")
        return [f.bf16_value(w) for w in struct.unpack(f"<{count}H", blob)]

    def scale(name):
        info, dtype, blob = fx.safetensors_tensor(packed, name)
        if dtype != "BF16" or (info.end - info.begin) != 2:
            raise fx.FixtureError(f"{name}: expected one BF16 scale")
        return f.bf16_value(struct.unpack("<H", blob)[0])

    gate, gate_blob = tensor(TENSORS["gate"], INTERMEDIATE)
    up, up_blob = tensor(TENSORS["up"], INTERMEDIATE)
    down, down_blob = tensor(TENSORS["down"], HIDDEN)
    return {
        "gate": gate, "up": up, "down": down,
        "shapes": {"gate": [INTERMEDIATE, HIDDEN], "up": [INTERMEDIATE, HIDDEN],
                   "down": [HIDDEN, INTERMEDIATE]},
        "w_post": bf16(TENSORS["w_post"], HIDDEN),
        "w_sub": bf16(TENSORS["w_sub"], INTERMEDIATE),
        "scales": {"gate": scale(TENSORS["scale_gate"]), "up": scale(TENSORS["scale_up"]),
                   "down": scale(TENSORS["scale_down"])},
        "packed_sha256": {"gate": hashlib.sha256(gate_blob).hexdigest(),
                          "up": hashlib.sha256(up_blob).hexdigest(),
                          "down": hashlib.sha256(down_blob).hexdigest()},
    }


def reference_f64(model: dict, x: list[int | float]) -> dict:
    """The f64 oracle: fsum everywhere, eps inside the sqrt (HF RMSNorm)."""

    def rmsnorm(v, w):
        mean = math.fsum(t * t for t in v) / len(v)
        inv = 1.0 / math.sqrt(mean + EPS)
        return [t * inv * wi for t, wi in zip(v, w)]

    gr, gc = model["shapes"]["gate"]
    dr, dc = model["shapes"]["down"]
    h = rmsnorm([float(t) for t in x], model["w_post"])
    g = [model["scales"]["gate"] * s for s in _rows_dot(model["gate"], gr, gc, h)]
    u = [model["scales"]["up"] * s for s in _rows_dot(model["up"], gr, gc, h)]
    # Explicit binary64 multiplies: libm pow(t, 2) can differ by an ulp
    # between platforms and change the committed Q32.32 stage statistics.
    relu = [max(t, 0.0) for t in g]
    a = [r * r * ui for r, ui in zip(relu, u)]
    s = rmsnorm(a, model["w_sub"])
    y = [model["scales"]["down"] * v for v in _rows_dot(model["down"], dr, dc, s)]
    return {"h": h, "g": g, "u": u, "a": a, "s": s, "y": y}


def _rows_dot(trits, rows: int, cols: int, vec):
    return [math.fsum(trits[r * cols + c] * vec[c] for c in range(cols)) for r in range(rows)]


def fpga_q16(model: dict, x_q16: list[int]) -> tuple[dict, dict]:
    """The explicit Q16.16 datapath. Returns (stages, saturation counts)."""
    sat = {name: 0 for name in ("h", "g", "u", "a", "s", "y")}
    events = {name: [] for name in sat}

    def clamp(stage, value):
        return clamp_s32(value, events[stage])

    def rmsnorm_q16(stage, v, w):
        """Q16.16 in, Q16.16 out; one combined rounding per element."""
        sumsq = sum(t * t for t in v)                     # exact, Q32.32
        mean_q32 = div_rne(sumsq, len(v))
        rms_q32 = math.isqrt((mean_q32 + EPS_Q32) << QQ)
        out = []
        for t, wi in zip(v, w):
            out.append(clamp(stage, div_rne((t * to_q(wi)) << Q, rms_q32)))
        return out

    def rmsnorm_wide(stage, v, w):
        """Q32.32 in (s64), Q16.16 out; the rms itself runs in Q64.64."""
        sumsq = sum(t * t for t in v)                     # exact, Q64.64
        mean_q64 = div_rne(sumsq, len(v))
        rms_q64 = math.isqrt((mean_q64 + EPS_Q64) << (2 * QQ))
        out = []
        for t, wi in zip(v, w):
            # numerator Q48.48 << 32 over the Q64.64 rms leaves Q16.16 units
            out.append(clamp(stage, div_rne((t * to_q(wi)) << QQ, rms_q64)))
        return out

    def matvec(stage, trits, rows, cols, vec, scale):
        scale_q16 = to_q(scale)
        out = []
        for r in range(rows):
            acc = 0
            base = r * cols
            for c in range(cols):
                trit = trits[base + c]
                if trit:
                    acc += vec[c] if trit > 0 else -vec[c]
            out.append(clamp(stage, rne(acc * scale_q16, Q)))
        return out

    gr, gc = model["shapes"]["gate"]
    dr, dc = model["shapes"]["down"]
    h = rmsnorm_q16("h", x_q16, model["w_post"])
    g = matvec("g", model["gate"], gr, gc, h, model["scales"]["gate"])
    u = matvec("u", model["up"], gr, gc, h, model["scales"]["up"])
    a = []
    for gi, ui in zip(g, u):
        r = gi if gi > 0 else 0
        a.append(clamp_s64(rne(r * r * ui, Q), events["a"]))
    s = rmsnorm_wide("s", a, model["w_sub"])
    y = matvec("y", model["down"], dr, dc, s, model["scales"]["down"])
    for name, ev in events.items():
        sat[name] = len(ev)
    return {"h": h, "g": g, "u": u, "a": a, "s": s, "y": y}, sat


STAGE_BITS = {"h": Q, "g": Q, "u": Q, "a": QQ, "s": Q, "y": Q}
# The guard band of the verdict. Quantising h before the two 2560-wide dots
# costs a small bias that relu2 then amplifies near its kink, so a tight ulp
# envelope would fail on noise; 2**-6 of each stage's peak instead catches
# contract bugs (wrong rounding, scale, norm or saturation) with an order of
# magnitude to spare. Observed errors are recorded in the generated report.
GUARD_REL, GUARD_ABS = 2 ** -6, 2


def compare(ref: dict, q16: dict) -> dict:
    """Per-stage max/mean error of the Q datapath against the oracle."""
    stages = {}
    for name, values in ref.items():
        bits = STAGE_BITS[name]
        rq = [to_q(v, bits) for v in values]
        diffs = [abs(q - r) for q, r in zip(q16[name], rq)]
        peak = max(abs(r) for r in rq)
        stages[name] = {
            "max_abs": max(diffs),
            "mean_abs": sum(diffs) / len(diffs),
            "peak": peak,
            "max_rel": max(d / max(abs(r), 1) for d, r in zip(diffs, rq)),
            "outside_guard": max(diffs) > GUARD_ABS + peak * GUARD_REL,
        }
    return stages


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", type=Path, help="also write the JSON report here")
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()

    model = load_ffn()
    x8 = list(mv.activations(HIDDEN, args.seed))
    ref = reference_f64(model, x8)
    q16, sat = fpga_q16(model, [t << Q for t in x8])
    stages = compare(ref, q16)

    ok = all(s == 0 for s in sat.values()) and \
        all(not st["outside_guard"] for st in stages.values())
    report = {
        "schema": SCHEMA,
        "oracle_scope": ORACLE_SCOPE,
        "rmsnorm_contract": RMSNORM_CONTRACT,
        "upstream_sources": UPSTREAM_SOURCES,
        "seed": args.seed,
        "activations_int8": x8,
        "widths": {"hidden": HIDDEN, "intermediate": INTERMEDIATE, "max_vector": MAX_VECTOR,
                   "wide_sumsq_unsigned_bits": WIDE_SUMSQ_BITS},
        "scales": {k: {"bf16": v, "q16": to_q(v)} for k, v in model["scales"].items()},
        "packed_sha256": model["packed_sha256"],
        "stages": stages,
        "saturations": sat,
        "y_f64_q16_sha256_le": sha256_le32([to_q(v) for v in ref["y"]]),
        "y_q16_sha256_le": sha256_le32(q16["y"]),
        "verdict": "pass" if ok else "fail",
    }
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=1) + "\n")
    print(f"FFN oracle ({SCHEMA}) seed {args.seed}: {report['verdict']}")
    for name, st in stages.items():
        print(f"  {name:>2}: max {st['max_abs']:12d} of peak {st['peak']:13d} q{STAGE_BITS[name]}"
              f"  mean {st['mean_abs']:11.2f}  max_rel {st['max_rel']:.2e}"
              f"  {'OUTSIDE' if st['outside_guard'] else 'ok'}")
    print(f"  saturations: {sat}")
    print(f"  y sha256 (oracle->q16 / datapath): {report['y_f64_q16_sha256_le'][:16]}... / "
          f"{report['y_q16_sha256_le'][:16]}...")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
