# GF16 on text-derived BitNet layer-0 states — iteration 3

Issue [#107](https://github.com/dmitrii-f-t27/trinity-memory/issues/107).
This follows the [synthetic boundary experiment](gf16-ffn-quantization.md).
The [runtime report](../reports/numeric/bitnet-layer0-runtime.json) changes the
arithmetic decision: **keeping every FFN stage in GF16 overflows on all four
texts. The product path needs a wider representation.**

## What actually ran

Four fixed, authored prompts (English explanation, arithmetic, Russian, code)
produce **45 tokens**, including each prompt's BOS. The checkpoint's tokenizer
pipeline runs unchanged. The [input lock](../fixtures/bitnet-layer0-capture.json)
contains the text, every token ID, tensor geometry and the hashes of all inputs.

The checkpoint is `microsoft/bitnet-b1.58-2B-4T` at
`04c3b9ad9361b824064a1f25ea60a8be9599b127`. We execute its actual embedding rows,
input norm, first-layer attention with rotary positions and causal masking,
attention sub-norm, output projection, residual addition and FFN. Layer 0 comes
from the installed upstream Transformers classes, using the checkpoint's offline
`AutoBitLinear` configuration. Every parameter is loaded strictly from pinned
ranges; no random parameter participates in the forward pass.

Only the needed layer and embedding rows are loaded: **58 ranges / 17,601,038
bytes**, plus the existing header prefix and tokenizer/config files. This is
not a full checkpoint download. Later layers, final norm, logits and text
generation are outside the experiment. The GF16 replacement receives the same
captured BF16 input to the FFN; attention is not rerun in GF16.

Runtime pins: torch 2.11.0, transformers 5.14.1, NumPy 2.4.3, tokenizers 0.22.2,
huggingface-hub 1.11.0, safetensors 0.8.0. The capture also verifies hashes of
the upstream BitNet integration, model implementation and activation functions.
The committed capture ran on arm64 CPU, one thread, deterministic algorithms,
eager attention, `torch.compile` disabled. Denormal flush is requested off; the
report records whether the API supports that setting on the host.

## Calibration before comparing GF16

An explicit BF16 implementation reproduces the captured `x,h,g,u,a,s,y` values:
**1,589,760 binary32 carrier words, zero numeric or bit-pattern mismatches**.
The six FFN output stages alone account for 1,474,560 values. The runner fails
before comparison if any calibrated bit differs. It shares torch's primitive
operations and BF16 GEMM kernel with the runtime, but does not call upstream
FFN forward methods when evaluating the explicit implementation.

This control includes rounding omitted by iteration 2:

1. RMSNorm computes its variance and reciprocal square root in FP32, rounds
   the normalized values to storage precision, then multiplies by the norm
   weights and rounds again. Epsilon is `1e-5` inside the square root.
2. ActQuant promotes to FP32 and uses per-token
   `scale = 127 / max(max(abs(x)), 1e-5)`. It rounds/clamps `x*scale` to
   `[-128,127]`, divides by that scale in FP32 and rounds to storage precision.
3. Ternary linear projections accumulate wide, round the dot result to storage
   precision, multiply by the stored weight scale and round again. BF16 control
   uses the BF16 GEMM kernel; GF16 values use FP32 carriers and FP32 GEMM.
4. `relu(g)^2` and its multiplication by `u` are separate rounded operations.

The three comparisons use the same checkpoint coefficients and captured inputs:

| Path | Storage / arithmetic boundary |
|---|---|
| BF16 control | Original runtime behavior, checked against all captured stages |
| Naive GF16 | GF16 for scalar storage, including `relu²` and `a` |
| GF16 + wide product | GF16 scalar storage; `relu²` and `a` remain FP32 into sub-norm |
| FP32+ActQuant reference | FP32 storage and arithmetic, retaining the same 8-bit ActQuant algorithm |

The GF16 variant is a software arithmetic candidate, not generated GF16 hardware.
Matvec and normalization reductions remain FP32. Keeping the product FP32 is a
tested control; this does not yet choose a fixed-point or floating-point FPGA
implementation of the wide path.

## Observed results

Output NMSE against **FP32+ActQuant**, evaluated on every token in each prompt:

| Prompt | Tokens | BF16 | GF16 + wide product | BF16 / GF16 NMSE | Naive GF16 |
|---|---:|---:|---:|---:|---|
| English explanation | 9 | 2.78158e-4 | 1.37214e-4 | 2.03× | 2 overflows in `a` |
| Arithmetic | 8 | 4.91491e-4 | 2.06047e-4 | 2.39× | 3 overflows in `a` |
| Russian | 17 | 3.65220e-4 | 1.57626e-4 | 2.32× | 2 overflows in `a` |
| Code | 11 | 3.34286e-4 | 1.25749e-4 | 2.66× | 3 overflows in `a` |

The first prompt's product reaches about **1.07e10**, exceeding GF16's maximum
finite value of about **4.29e9**. Each naive GF16 path stops at the first nonfinite
boundary; no output score is assigned to it. No bad lane/token is removed to
improve a score. The wide-product path is finite on every prompt.

The report also records each token's output NMSE, every stage's errors, rounding
losses (including the very small sub-norm coefficients identified in iteration 2),
and changes in ActQuant integer codes relative to BF16. ActQuant codes do change
when prior rounding changes the row maximum and element values. The earlier
14.7–20.6× synthetic storage-only ratios are **not** representative of this new
runtime experiment; its observed ratios are 2.03–2.66×.

These are local layer-0 errors on four short prompts. They do not establish
perplexity, full-model generation quality, throughput, energy or hardware cost.

## Reproduce

Use an isolated environment with the optional requirements; these dependencies
are not added to the trinity-memory library. On Linux, install torch's CPU wheel
first to avoid unnecessary CUDA packages.

```sh
python -m pip install -r tools/bitnet-capture-requirements.txt
TRINITY_REQUIRE_CAPTURE_RUNTIME=1 python -m unittest tests.test_bitnet_runtime -v
python tools/capture_bitnet_layer0.py --fetch
python tools/capture_bitnet_layer0.py --output build/bitnet-capture/replay.json
```

The second run is offline. Missing ranges fail; all cached bytes are rehashed.
`--record-lock` is reserved for reviewed creation of a new input lock and refuses
to overwrite an existing one. Token IDs and tensor/range geometry must match the
lock before executing. Raw arrays are saved locally to
`build/bitnet-capture/captures.npz`; model weight bytes remain in the ignored cache.
The committed JSON records raw tensor hashes and numeric results. Capture replay
on the same pinned host can compare JSON exactly; other CPUs are recorded as
separate reports and must pass their own exact BF16 calibration.

The dedicated GitHub workflow runs on relevant PR changes or manual dispatch.
It installs the optional environment, checks the vectorized GF16 converter
against all 314,883 scalar-oracle vectors and all GF16 round trips, checks
ActQuant against upstream, exercises overflow/metric/cache controls, then captures
the real layer. Linux reports and raw activations are uploaded as artifacts.

## Resulting hardware constraint

Retain the GF16 candidate around ActQuant and for selected storage boundaries.
**Do not narrow the `relu²(g) * u` path to GF16.** Next, bound and implement its
wide arithmetic and normalization, then compare that implementation against
these captured vectors. The existing validated Q16 FPGA FFN remains unchanged.
