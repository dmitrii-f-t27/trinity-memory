# GF16 / BF16 FFN boundary precision — iteration 2

Follow-up: [text-derived runtime captures](gf16-bitnet-runtime.md) include
ActQuant and reveal product overflows absent from these ordinary synthetic
probes. Use that evidence before narrowing the FFN product to GF16.

Issue [#105](https://github.com/dmitrii-f-t27/trinity-memory/issues/105).
The [replayable report](../reports/numeric/ffn-quantization-v1.json) measures
**real layer-0 BitNet weights with synthetic hidden-state inputs**. These are
not captured activations from text inference. This experiment selects candidate
storage boundaries; it does not implement GF16 arithmetic or assess model quality.

## Fixed conditions

- `microsoft/bitnet-b1.58-2B-4T`, revision
  `04c3b9ad9361b824064a1f25ea60a8be9599b127`; hidden 2560, intermediate 6912.
  The loader verifies every fixture range against the committed SHA-256.
  Report provenance includes all nine used ranges and the numeric source hashes.
- The existing [`ffn_reference.py`](../tools/ffn_reference.py) binary64 oracle:
  post-attention RMSNorm, gate/up ternary projections, `relu(g)^2 * u`, sub-norm,
  down projection. Epsilon is `1e-5` inside each square root. No residual,
  attention, ActQuant, or transformers BF16 execution is modeled.
- GF16 uses [`gf16-rne-gradual-v1`](gf16-contract.md); BF16 uses nearest/ties-even
  and gradual underflow. Both canonicalize NaNs. Neither saturates to max-finite.
- Every stored boundary takes a binary64 intermediate through binary32 RNE,
  then the selected 16-bit converter. The binary32 conversion error is reported
  separately. This is **not direct binary64-to-GF16 rounding**.
- Ternary matvec sums use `math.fsum`, with no per-addition 16-bit rounding.
  Norm squares/sums, reciprocal square root, coefficient multiplication and the
  two multiplications of `relu(g)^2*u` execute in binary64. Epsilon stays wide.
  Thus the experiment isolates storage precision from arithmetic implementation.

Two policies are evaluated for each format:

| Policy | Rounded to the selected 16-bit format |
|---|---|
| `stage16` | Input `x`, both norm-weight vectors, all three scales, `h,g,u,a,s,y` |
| `wide_a` | Same boundaries except `a`, which stays **binary64** into sub-norm |

`wide_a` is a numerical control, not a proposed 64-bit FPGA unit. Coefficients
start as BF16 checkpoint values; the ternary weight matrices remain unchanged.
Independent (non-propagated) quantization is also measured on every oracle
stage, so its local error is not confused with errors from earlier stages.

## Inputs and result

`seed27_int8` uses 2560 draws of `random.Random(27).randint(-128,127)`, matching
the board-reference input recipe. `seed28_scaled` uses seed 28 divided by 64.
`seed27_tiny` is the first vector scaled by `2^-30`, testing small values and
epsilon-sensitive normalization. `zero` checks the zero-energy convention.
`range_stress` alternates ±`2^40` to deliberately exceed GF16's range. Each input
is fingerprinted as little-endian binary32; no randomly sampled input is omitted.

Output NMSE against the same binary64 oracle (lower is better):

| Input | GF16 stage16 | BF16 stage16 | GF16 wide_a | BF16 wide_a |
|---|---:|---:|---:|---:|
| seed27_int8 | 1.414004253e-6 | 2.911458494e-5 | 1.151955908e-6 | 2.497509215e-5 |
| seed28_scaled | 1.862839494e-6 | 2.742458231e-5 | 1.531274102e-6 | 2.284983594e-5 |
| seed27_tiny | 1.253062710e-6 | 2.117313774e-5 | 1.169359345e-6 | 1.797487923e-5 |
| zero | 0 | 0 | 0 | 0 |
| range_stress | overflow at x | 1.596825411e-5 | overflow at x | 1.376576470e-5 |

For the two ordinary probes, GF16 stage16 reduces NMSE by **20.59× and 14.72×**
relative to BF16 stage16. Those are ratios of squared-error metrics, not speedups
or improvements in language-model quality. Keeping `a` wide improves the result
for both formats, but the current samples do not establish an optimal hardware
width or justify its area/latency cost.

Range findings are part of the result:

- All 2560 post-norm coefficients and the three weight scales convert exactly
  to GF16. In sub-norm, **330 of 6912 nonzero coefficients become zero**; their
  maximum magnitude is `7.997156313e-26`. The coefficient NMSE is
  `4.689367740e-55`, small on this checkpoint but not bit-exact compatibility.
- On `seed27_tiny`, GF16 stage16 stores 1323 subnormal `a` values and rounds
  58 nonzero `a` values to zero. The separate wide-a run still rounds three
  nonzero `s` values to zero. BF16 reports no range loss on these probes.
- Both GF16 policies overflow all 2560 inputs of `range_stress`. The run stops
  at `x`; it has **no output-error score**, rather than a score on a filtered
  subset. BF16 remains finite. This input is an intentional range test.

## Metric and replay rules

`NMSE = sum((candidate-reference)^2) / sum(reference^2)`; `relative_l2` is its
square root. Zero reference energy gives NMSE 0 only for an exact zero output;
otherwise NMSE is undefined (`null`). Any nonfinite reference/candidate makes
the whole metric undefined. No lane is discarded. The report distinguishes
binary32 overflow/underflow, 16-bit overflow, tiny inputs, subnormal outputs,
and nonzero-to-zero rounding. Statistics use full binary64; JSON presentation
rounds to ten significant digits for cross-platform replay. Bit-pattern hashes
are exact. There is no retrospectively chosen quality pass threshold.

With the pinned native stack built and the existing fixtures cached:

```sh
python3 -m unittest tests.test_ffn_quantization -v
python3 tools/ffn_quantization.py --check
```

The report runner always uses offline fixture reads. Missing/corrupt inputs fail
instead of skipping. To deliberately regenerate a reviewed report, omit
`--check`. CI runs unit tests and full real-weight replay in the fixture-backed
`ternary-check` job. Unit tests cover every BF16 round trip and finite midpoint,
nonfinite metric handling, range counters, the zero case, a wide-product control,
and an identity-quantizer control against the existing binary64 FFN oracle.

## Next arithmetic decision

Keep GF16 as the candidate for activation/scale storage, with explicit underflow
and overflow accounting. Preserve the original checkpoint coefficients as the
import reference. Keep matvec accumulators, normalization reductions and the
unrounded product wide until a separate bounded arithmetic contract and tests
justify narrowing them. The validated Q16 board FFN is unchanged.

Before accepting GF16 for inference, capture actual hidden states from a pinned
runtime/text corpus, model ActQuant and the remaining scalar rounding, and repeat
the comparison. These five probes alone cannot establish behavior across tokens,
layers or models. No FPGA timing/resource measurement is claimed here.
