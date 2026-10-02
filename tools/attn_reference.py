#!/usr/bin/env python3
"""Layer-0 BitNet attention/residual oracle and the explicit Q16.16 FPGA arithmetic.

The attention block of microsoft/BitNet-b1.58-2B-4T layer 0 (config.json:
hidden 2560, 20 heads x 128, 5 kv heads -> GQA 4:1, rope_theta 500000,
rms_norm_eps 1e-5, causal; modeling_bitnet.py applies attn_sub_norm to the
attention context right before o_proj and adds the residual afterwards):

    n_t = rmsnorm(x_t, input_layernorm)          x_t is the 2560-wide input
    q_t = scale_q * (q_proj @ n_t)               q_proj: [2560, 2560] trits
    k_t = scale_k * (k_proj @ n_t)               k_proj: [640, 2560] trits
    v_t = scale_v * (v_proj @ n_t)               v_proj: [640, 2560] trits
    q_t, k_t = rope(q_t, k_t, t, 500000)         rotate_half, 64 pairs/head
    s_{t,h,j} = (q_{t,h} . k_{j,g}) * 128**-0.5  g = h // 4, causal j <= t
    p_{t,h,.} = softmax(s_{t,h,.})
    a_t = concat_h(sum_j p_{t,h,j} * v_{j,g})    the 2560-wide context
    c_t = rmsnorm(a_t, self_attn.attn_sub_norm)
    o_t = scale_o * (o_proj @ c_t)               o_proj: [2560, 2560] trits
    r_t = x_t + o_t                              the residual the FFN eats

The oracle evaluates the unquantized-activation math in f64 (math.fsum) from
the cached fixtures the board tools read; like the FFN oracle it is not a
bit-exact replay of transformers AutoBitLinear (no ActQuant, no bf16
execution rounding). The candidate FPGA arithmetic evaluates the same math
under an explicit between-stage contract; no hardware implementation is
claimed here:

* Every stage value is signed Q16.16 (s32) — including the context a: it is
  a convex combination of v lanes (softmax weights are non-negative and sum
  to one), so unlike the FFN relu2 product it cannot overflow Q16.16 and no
  Q32.32 stage exists in this block.
* Every scalar operation rounds to nearest-even exactly once: the norms
  divide the exact s64 numerator by the Q32.32 rms in one step, the ternary
  matvecs keep exact accumulators (a trit only adds or subtracts an
  activation), and every bf16/constant factor (four weight scales, the
  attention scaling 128**-0.5) costs one rounding.
* RoPE: cos/sin are rounded once to a Q16.16 host table, exactly like the
  bf16 scale constants; the rotation numerator a*cos - b*sin is an exact
  s64 product pair, one combined rounding at the shift. The angles come
  from plain libm cos/sin: a 1-2 ulp platform difference cannot flip a
  Q16.16 rounding here (boundary spacing 2**-16 versus a double ulp of
  ~2**-52 on [0, 1]), unlike the relu2 pow incident the FFN oracle recorded.
* Softmax is the one transcendental of the Q datapath: e_i is defined as
  the correctly rounded Q16.16 exp of the exact Q16.16 delta from the row
  maximum, the denominator is the exact integer sum, and every context
  element is one division. RTL must later implement an exp unit proven
  inside the report guard band; the oracle does not model a polynomial.
* Position 0 is the untouched board case: RoPE is the identity there and
  the single-entry softmax returns v exactly, so the BOS vector exercises
  projections, norms and the residual only.
* Saturations are counted and clamped, never wrapped.

The report compares the two evaluations stage by stage over a causal
S-position prefill (house activation seed at position 0), so a later board
capture is judged against a written contract instead of re-deriving one.

  python3 tools/attn_reference.py               print the comparison summary
  python3 tools/attn_reference.py --json OUT    also write the JSON report
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
from tools.ffn_reference import rne, div_rne, to_q, clamp_s32, sha256_le32, EPS_Q32  # noqa: E402

SCHEMA = "trinity.attn-reference.v1"
HIDDEN, HEADS, KV_HEADS, HEAD_DIM = 2560, 20, 5, 128
GROUP, PAIRS = HEADS // KV_HEADS, HEAD_DIM // 2          # 4:1 GQA, 64 rope pairs
KV_DIM = KV_HEADS * HEAD_DIM                              # 640
Q, QQ = 16, 32                      # fraction bits of stage values / norm path
ONE_Q16 = 1 << Q
EPS = 1e-5                                                # config.json rms_norm_eps
ROPE_THETA = 500000.0                                     # config.json rope_theta
ATTENTION_SCALING = HEAD_DIM ** -0.5                      # modeling_bitnet.py
SEED, POSITIONS = 27, 8                                   # house seed, causal prefill length
ORACLE_SCOPE = "unquantized-activation attention/residual math; no ActQuant or bf16 execution rounding"
RMSNORM_CONTRACT = "sqrt(mean(x*x) + epsilon)"
EXP_CONTRACT = ("e_i = round_q16(exp(s_i - row_max)); exact integer denominator, "
               "one division per context element; the RTL exp unit must stay inside the guard band")
UPSTREAM_SOURCES = {
    "attention": "https://github.com/huggingface/transformers/blob/7cd73d9df0c14b151c684b708a9f27d8d0349dfe/src/transformers/models/bitnet/modeling_bitnet.py",
    "autobitlinear": "https://github.com/huggingface/transformers/blob/d6abd1333078195535f461589b64f112b53688b2/src/transformers/integrations/bitnet.py",
    "config": "https://huggingface.co/microsoft/bitnet-b1.58-2B-4T/blob/04c3b9ad9361b824064a1f25ea60a8be9599b127/config.json",
}

LAYER = "model.layers.0"
TENSORS = {
    "q": f"{LAYER}.self_attn.q_proj.weight",
    "k": f"{LAYER}.self_attn.k_proj.weight",
    "v": f"{LAYER}.self_attn.v_proj.weight",
    "o": f"{LAYER}.self_attn.o_proj.weight",
    "w_in": f"{LAYER}.input_layernorm.weight",
    "w_sub": f"{LAYER}.self_attn.attn_sub_norm.weight",
    "scale_q": f"{LAYER}.self_attn.q_proj.weight_scale",
    "scale_k": f"{LAYER}.self_attn.k_proj.weight_scale",
    "scale_v": f"{LAYER}.self_attn.v_proj.weight_scale",
    "scale_o": f"{LAYER}.self_attn.o_proj.weight_scale",
}

# RTL width budgets: worst-case unsigned bit lengths of the exact integer
# intermediates. Python runs unbounded; hardware must budget these. Every
# stage value is Q16.16 s32, so a product of two of them stays below 2**62.
WIDTHS = {
    "norm_sumsq_bits": (HIDDEN * (1 << 62)).bit_length(),         # 2560 squares per norm
    "proj_acc_bits": (HIDDEN * (1 << 62)).bit_length(),           # 2560-long ternary dot
    "score_acc_bits": (HEAD_DIM * (1 << 62)).bit_length(),        # 128-long q.k dot
    "score_scale_bits": (HEAD_DIM * (1 << 62)).bit_length() + Q,  # dot * Q16.16 scaling
    "softmax_num_bits": (POSITIONS * (1 << (Q + 31))).bit_length(),  # summed e*v products
}


def rope_tables(positions: int, theta: float = ROPE_THETA, head_dim: int = HEAD_DIM):
    """Per-position (cos, sin) Q16.16 tables and their f64 sources.

    inv_freq[i] = theta ** (-2i/head_dim) pairs index i with i + head_dim/2
    inside each head, the transformers rotate_half convention.
    """
    pairs = head_dim // 2
    inv_freq = [theta ** (-(2 * i) / head_dim) for i in range(pairs)]
    tables_f64, tables_q16 = [], []
    for t in range(positions):
        row_f64, row_q16 = [], []
        for freq in inv_freq:
            angle = t * freq
            cos_f, sin_f = math.cos(angle), math.sin(angle)
            row_f64.append((cos_f, sin_f))
            row_q16.append((to_q(cos_f), to_q(sin_f)))
        tables_f64.append(row_f64)
        tables_q16.append(row_q16)
    return tables_f64, tables_q16


def load_attn() -> dict:
    """Trits, scales and norm weights of layer 0 from the fixture cache."""
    packed = tc.BITNET["packed"]

    def tensor(name, rows, cols):
        info, dtype, blob = fx.safetensors_tensor(packed, name)
        if dtype != "U8":
            raise fx.FixtureError(f"{name}: expected packed U8, got {dtype}")
        if info.d0 * 4 != rows or info.d1 != cols:
            raise fx.FixtureError(f"{name}: packed shape {info.d0}x{info.d1} does not cover {rows}x{cols}")
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

    q, q_blob = tensor(TENSORS["q"], HIDDEN, HIDDEN)
    k, k_blob = tensor(TENSORS["k"], KV_DIM, HIDDEN)
    v, v_blob = tensor(TENSORS["v"], KV_DIM, HIDDEN)
    o, o_blob = tensor(TENSORS["o"], HIDDEN, HIDDEN)
    return {
        "q": q, "k": k, "v": v, "o": o,
        "dims": {"hidden": HIDDEN, "heads": HEADS, "kv_heads": KV_HEADS,
                 "head_dim": HEAD_DIM, "kv_dim": KV_DIM, "group": GROUP, "pairs": PAIRS},
        "shapes": {"q": [HIDDEN, HIDDEN], "k": [KV_DIM, HIDDEN],
                   "v": [KV_DIM, HIDDEN], "o": [HIDDEN, HIDDEN]},
        "w_in": bf16(TENSORS["w_in"], HIDDEN),
        "w_sub": bf16(TENSORS["w_sub"], HIDDEN),
        "scales": {name: scale(TENSORS[f"scale_{name}"]) for name in ("q", "k", "v", "o")},
        "packed_sha256": {name: hashlib.sha256(blob).hexdigest() for name, blob in
                          (("q", q_blob), ("k", k_blob), ("v", v_blob), ("o", o_blob))},
    }


def _rows_dot(trits, rows: int, cols: int, vec):
    return [math.fsum(trits[r * cols + c] * vec[c] for c in range(cols)) for r in range(rows)]


def _rope_vector(vec, cos_sin, offset: int, pairs: int):
    """Rotate one head-slice: pair i lives at offset+i and offset+pairs+i."""
    out = list(vec)
    for i in range(pairs):
        a, b = vec[offset + i], vec[offset + pairs + i]
        cos_f, sin_f = cos_sin[i]
        out[offset + i] = a * cos_f - b * sin_f
        out[offset + pairs + i] = b * cos_f + a * sin_f
    return out


def reference_f64(model: dict, xs: list[list[float]], tables_f64) -> dict:
    """The f64 oracle: fsum everywhere, eps inside the sqrt (HF RMSNorm)."""
    dims = model["dims"]
    heads, kv_heads, head_dim = dims["heads"], dims["kv_heads"], dims["head_dim"]
    hidden, kv_dim, group = dims["hidden"], dims["kv_dim"], dims["group"]
    scaling = head_dim ** -0.5

    def rmsnorm(v, w):
        mean = math.fsum(t * t for t in v) / len(v)
        inv = 1.0 / math.sqrt(mean + EPS)
        return [t * inv * wi for t, wi in zip(v, w)]

    stages = {name: [] for name in ("n", "q", "k", "v", "qr", "kr", "sc", "p", "a", "c", "o", "r")}
    ks, vs = [], []
    for t, x in enumerate(xs):
        n = rmsnorm([float(v) for v in x], model["w_in"])
        q = [model["scales"]["q"] * s for s in _rows_dot(model["q"], hidden, hidden, n)]
        k = [model["scales"]["k"] * s for s in _rows_dot(model["k"], kv_dim, hidden, n)]
        v = [model["scales"]["v"] * s for s in _rows_dot(model["v"], kv_dim, hidden, n)]
        q_rot, k_rot = list(q), list(k)
        for h in range(heads):
            q_rot = _rope_vector(q_rot, tables_f64[t], h * head_dim, head_dim // 2)
        for h in range(kv_heads):
            k_rot = _rope_vector(k_rot, tables_f64[t], h * head_dim, head_dim // 2)
        context = []
        ks.append(k_rot)                    # causal: position t attends to j <= t
        vs.append(v)
        for h in range(heads):
            base_q, base_kv = h * head_dim, (h // group) * head_dim
            scores = [scaling * math.fsum(
                q_rot[base_q + d] * ks[j][base_kv + d] for d in range(head_dim)) for j in range(t + 1)]
            peak = max(scores)
            exps = [math.exp(s - peak) for s in scores]
            denom = math.fsum(exps)
            probs = [e / denom for e in exps]
            stages["sc"].extend(scores)
            stages["p"].extend(probs)
            for d in range(head_dim):
                context.append(math.fsum(p * vs[j][base_kv + d] for j, p in enumerate(probs)))
        c = rmsnorm(context, model["w_sub"])
        o = [model["scales"]["o"] * s for s in _rows_dot(model["o"], hidden, hidden, c)]
        stages["n"].extend(n)
        stages["q"].extend(q)
        stages["k"].extend(k)
        stages["v"].extend(v)
        stages["qr"].extend(q_rot)
        stages["kr"].extend(k_rot)
        stages["a"].extend(context)
        stages["c"].extend(c)
        stages["o"].extend(o)
        stages["r"].extend(xi + oi for xi, oi in zip(x, o))
    return stages


def fpga_q16(model: dict, xs_q16: list[list[int]], tables_q16) -> tuple[dict, dict]:
    """The explicit Q16.16 datapath. Returns (stages, saturation counts)."""
    dims = model["dims"]
    heads, kv_heads, head_dim = dims["heads"], dims["kv_heads"], dims["head_dim"]
    hidden, kv_dim, group, pairs = dims["hidden"], dims["kv_dim"], dims["group"], dims["pairs"]
    sat = {name: 0 for name in ("n", "q", "k", "v", "qr", "kr", "sc", "a", "c", "o", "r")}
    events = {name: [] for name in sat}
    scaling_q16 = to_q(head_dim ** -0.5)

    def clamp(stage, value):
        return clamp_s32(value, events[stage])

    def rmsnorm_q16(stage, v, w):
        """Q16.16 in, Q16.16 out; one combined rounding per element.

        The sum of Q16.16 squares is exact Q32.32 and so is the rms that
        isqrt produces from it; the per-element numerator t*w runs Q48.48
        before the single division back to Q16.16."""
        sumsq = sum(t * t for t in v)                     # exact, Q32.32
        mean_q32 = div_rne(sumsq, len(v))
        rms_q32 = math.isqrt((mean_q32 + EPS_Q32) << QQ)
        return [clamp(stage, div_rne((t * to_q(wi)) << Q, rms_q32)) for t, wi in zip(v, w)]

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

    def rope_q16(stage, vec, table):
        out = list(vec)
        for i in range(pairs):
            a, b = vec[i], vec[pairs + i]
            cos_q, sin_q = table[i]
            out[i] = clamp(stage, rne(a * cos_q - b * sin_q, Q))
            out[pairs + i] = clamp(stage, rne(b * cos_q + a * sin_q, Q))
        return out

    stages = {name: [] for name in
              ("n", "q", "k", "v", "qr", "kr", "sc", "p", "a", "c", "o", "r")}
    ks, vs = [], []
    for t, x in enumerate(xs_q16):
        n = rmsnorm_q16("n", x, model["w_in"])
        q = matvec("q", model["q"], hidden, hidden, n, model["scales"]["q"])
        k = matvec("k", model["k"], kv_dim, hidden, n, model["scales"]["k"])
        v = matvec("v", model["v"], kv_dim, hidden, n, model["scales"]["v"])
        q_rot = [rope_q16("qr", q[h * head_dim:(h + 1) * head_dim], tables_q16[t])
                 for h in range(heads)]
        k_rot = [rope_q16("kr", k[h * head_dim:(h + 1) * head_dim], tables_q16[t])
                 for h in range(kv_heads)]
        q_flat = [value for head in q_rot for value in head]
        k_flat = [value for head in k_rot for value in head]
        ks.append(k_flat)                   # causal: position t attends to j <= t
        vs.append(v)
        context = []
        for h in range(heads):
            head_q = q_rot[h]
            base_kv = (h // group) * head_dim
            scores = []
            for j in range(t + 1):
                acc = 0
                kj = ks[j]
                for d in range(head_dim):
                    acc += head_q[d] * kj[base_kv + d]
                # acc is a Q32.32 dot; the Q16.16 scaling leaves Q48.48,
                # so the single rounding shifts by QQ, not Q.
                scores.append(clamp("sc", rne(acc * scaling_q16, QQ)))
            peak = max(scores)
            exps = [to_q(math.exp((s - peak) / ONE_Q16)) for s in scores]
            denom = sum(exps)
            stages["sc"].extend(scores)
            stages["p"].extend(div_rne(e << Q, denom) for e in exps)
            for d in range(head_dim):
                acc = sum(e * vs[j][base_kv + d] for j, e in enumerate(exps))
                context.append(clamp("a", div_rne(acc, denom)))
        c = rmsnorm_q16("c", context, model["w_sub"])
        o = matvec("o", model["o"], hidden, hidden, c, model["scales"]["o"])
        r = [clamp("r", xi + oi) for xi, oi in zip(x, o)]
        stages["n"].extend(n)
        stages["q"].extend(q)
        stages["k"].extend(k)
        stages["v"].extend(v)
        stages["qr"].extend(q_flat)
        stages["kr"].extend(k_flat)
        stages["a"].extend(context)
        stages["c"].extend(c)
        stages["o"].extend(o)
        stages["r"].extend(r)
    for name, ev in events.items():
        sat[name] = len(ev)
    return stages, sat


STAGE_BITS = {name: Q for name in
              ("n", "q", "k", "v", "qr", "kr", "sc", "p", "a", "c", "o", "r")}
# The guard band of the verdict, the FFN oracle's value: quantisation error
# of the early stages is amplified through scores/softmax, so a tight ulp
# envelope would fail on noise; 2**-6 of each stage's peak catches contract
# bugs (wrong rounding, scale, norm, RoPE pairing or saturation) with an
# order of magnitude to spare. Observed errors are recorded per stage.
GUARD_REL, GUARD_ABS = 2 ** -6, 2


def compare(ref: dict, q16: dict) -> dict:
    """Per-stage max/mean error of the Q datapath against the oracle."""
    stages = {}
    for name, values in ref.items():
        rq = [to_q(v, STAGE_BITS[name]) for v in values]
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
    ap.add_argument("--positions", type=int, default=POSITIONS)
    args = ap.parse_args()

    model = load_attn()
    xs = [list(mv.activations(HIDDEN, args.seed + t)) for t in range(args.positions)]
    tables_f64, tables_q16 = rope_tables(args.positions)
    ref = reference_f64(model, xs, tables_f64)
    q16, sat = fpga_q16(model, [[t << Q for t in x] for x in xs], tables_q16)
    stages = compare(ref, q16)

    ok = all(s == 0 for s in sat.values()) and \
        all(not st["outside_guard"] for st in stages.values())
    report = {
        "schema": SCHEMA,
        "oracle_scope": ORACLE_SCOPE,
        "rmsnorm_contract": RMSNORM_CONTRACT,
        "exp_contract": EXP_CONTRACT,
        "upstream_sources": UPSTREAM_SOURCES,
        "seed": args.seed,
        "positions": args.positions,
        "activations_int8": xs,
        "widths": dict(WIDTHS, hidden=HIDDEN, heads=HEADS, kv_heads=KV_HEADS,
                       head_dim=HEAD_DIM, rope_theta=ROPE_THETA),
        "scales": {**{f"{k}": {"bf16": v, "q16": to_q(v)} for k, v in model["scales"].items()},
                   "attention_scaling": {"f64": ATTENTION_SCALING, "q16": to_q(ATTENTION_SCALING)}},
        "packed_sha256": model["packed_sha256"],
        "stages": stages,
        "saturations": sat,
        "r_last_f64_q16_sha256_le": sha256_le32([to_q(v) for v in ref["r"][-HIDDEN:]]),
        "r_last_q16_sha256_le": sha256_le32(q16["r"][-HIDDEN:]),
        "verdict": "pass" if ok else "fail",
    }
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=1) + "\n")
    print(f"Attention oracle ({SCHEMA}) seed {args.seed} x {args.positions} positions: {report['verdict']}")
    for name, st in stages.items():
        print(f"  {name:>2}: max {st['max_abs']:12d} of peak {st['peak']:13d} q{STAGE_BITS[name]}"
              f"  mean {st['mean_abs']:11.2f}  max_rel {st['max_rel']:.2e}"
              f"  {'OUTSIDE' if st['outside_guard'] else 'ok'}")
    print(f"  saturations: {sat}")
    print(f"  r_last sha256 (oracle->q16 / datapath): {report['r_last_f64_q16_sha256_le'][:16]}... / "
          f"{report['r_last_q16_sha256_le'][:16]}...")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
