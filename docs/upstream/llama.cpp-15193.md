# llama.cpp issue 15193: TQ1_0/TQ2_0 output on CPU (2026-09-23)

Stage 1 sub-issue #35 of epic #27. Question: is the garbage output reported in
[llama.cpp issue 15193](https://github.com/ggml-org/llama.cpp/issues/15193) a
storage problem (quantization or packing of TQ1_0/TQ2_0) or a CPU kernel
problem?

## Verdict

No storage or CPU kernel defect was found at the pinned code; the cause of the
output was not reproduced.

What the harness shows, on random blocks and on real ternary tensors (below):

- Exact: the upstream quantizers write the same bytes as the t27 encoders;
  the upstream dequantizers and the t27 decoders return the same trits and
  scales; with unit scales the integer accumulators of the generic, NEON
  (DOTPROD and int16) and AVX2 `vec_dot` kernels equal the exact integer
  product in every row.
- Within tolerance, not bit-identical: the kernels' float results agree with
  each other and with a double reference within 1e-5 of the sum of absolute
  block terms. The AVX2 kernels keep eight lane sums, so their results round
  differently from the generic kernel (T3: 420 of 4000 identical).
- The x86 builds (AVX2 and generic) ran under Rosetta 2 on Apple silicon and
  natively on the Ubuntu x86_64 CI runner (run 35897659923 of 2026-09-23,
  commit 8ebf8f7), with the same results.

What it does not show: the cause. The maintainer who closed the issue on the
same day attributes the output to the input model: the reporter quantized a
float-trained model (Qwen3-4B-Instruct-2507) to TQ1_0 with `llama-quantize`;
TQ1_0 and TQ2_0 replace every block of 256 weights by trits times one fp16
absmax scale, and ternary quants are meant only for models trained for them.
The synthetic float rows of T5 agree with that explanation (relative weight
error 0.81-0.85, against 0.09-0.12 for Q4_0), but no model was run (see "What
an end-to-end confirmation would need"). Also not examined: the TQ2_0, IQ1_S
and IQ2_XXS files the reporter downloaded from Hugging Face (not named in the
issue), and the runtime around the kernels (graph, `llama-server`, the Windows
`GGML_CPU_ALL_VARIANTS` build).

Nothing was reported upstream, and nothing will be without the founders'
approval (#35).

## Issue facts

Read with `gh issue view 15193 --repo ggml-org/llama.cpp --comments` and the
issue events API on 2026-09-23.

| Field | Value |
| --- | --- |
| Title | "Eval bug: Fail to run models that is quantized as TQ1_0 by llama-quantize, llama-server outputs messy contents." |
| Opened | 2025-08-09T14:39:17Z by MaoJianwei |
| Build | b6122 = `34c9d765bf173c551398f1e7fa4595019bc53bab` (2025-08-09T12:00:24Z) |
| System | Windows, CPU backend, Intel Core i5-10400 (AVX2, FMA, F16C; no AVX-512) |
| Model | Qwen3-4B-Instruct-2507 (bf16, no `quantization_config`), quantized with `llama-quantize` to TQ1_0; the source GGUF is not named |
| Command | `./llama-server.exe --model ../Qwen3-4B-Instruct-2507-gguf-TQ1_0 --no-mmap` (path separators normalized) |
| Also reported | garbage from TQ2_0, IQ1_S and IQ2_XXS, including files downloaded from Hugging Face; Q4_K_M, Q3_K_S and Q8_0 worked. Only a screenshot was attached, no logs. |
| State | closed, reason "completed", 2025-08-09T16:58:40Z, by maintainer CISC, with no linked commit or PR |
| Closing explanation | ternary quants are meant only for models trained for them (the comment names BitCPM4-1B); anything else gives garbage. IQ1_S needs a good imatrix. |

The reporter answered by quoting the comment; the issue stayed closed. A
similar report, llama.cpp issue 14616 (Falcon-H1-7B in TQ1_0, perplexity near
1e13), was answered "looks normal" and closed as stale.

How the model reaches TQ1_0 at b6122 (`src/llama-quant.cpp`): hidden size 2560
= 10 x 256 and intermediate size 9728 = 38 x 256, so every block matrix
becomes TQ1_0; tied embeddings take the output rule (Q6_K); rows whose length
is not a multiple of 256 fall back to Q4_0 (lines 452-453). `quantize_tq1_0`
and `quantize_tq2_0` ignore the imatrix (`ggml/src/ggml-quants.c:2415, 2422` at
the pin below), and `llama-quantize` never asks for one for TQ types.

The reporter's Windows zip is built with `GGML_BACKEND_DL=ON` and
`GGML_CPU_ALL_VARIANTS=ON` (`.github/workflows/release.yml:284-285` at b6122),
so on an i5-10400 it very likely loaded an AVX2 variant, which runs the x86
AVX2 TQ kernels. This is inferred, not observed.

## Upstream code

Single llama.cpp pin for this repository: `ggml-org/llama.cpp`
`e6ab7c1a41054a888ada952eab4c886444c2f5ad` (2026-09-22), the revision
`tests/native_formats.c` follows. `tests/upstream/llama.cpp.lock.json` holds
the sha256 and git blob SHA of the five files the harness fetches
(`ggml-common.h`, `ggml-quants.c`, `ggml-cpu/quants.c` and the x86 and ARM
`quants.c`). The other files cited below (`ggml-cpu/ggml-cpu.c`,
`src/llama-quant.cpp`, `src/llama-model-loader.cpp`) were read at the pin
but are not in the lock.

| What | File:lines at `e6ab7c1a` |
| --- | --- |
| `block_tq1_0` (qs[48], qh[4], fp16 d) | `ggml/src/ggml-common.h:275-281` |
| `block_tq2_0` (qs[64], fp16 d) | `ggml/src/ggml-common.h:283-288` |
| `quantize_row_tq1_0_ref` (absmax d, `lroundf(x*id)`, ceiling division by 243) | `ggml/src/ggml-quants.c:2316-2380` |
| `quantize_row_tq2_0_ref` | `ggml/src/ggml-quants.c:2382-2412` |
| `quantize_tq1_0`, `quantize_tq2_0` ignore `quant_weights` | `ggml/src/ggml-quants.c:2415, 2422` |
| `dequantize_row_tq1_0`, `dequantize_row_tq2_0` | `ggml/src/ggml-quants.c:2428-2465, 2467-2484` |
| `quantize_row_q8_K_ref` (activations) | `ggml/src/ggml-quants.c:2768-2805` |
| TQ scale checks (NaN and Inf only): `llama-quantize` always checks its input and every chunk it writes; the inference loader checks only with `--check-tensors` (default off) | `ggml/src/ggml-quants.c:5323-5335, 5346-5352, 5570-5577`; `src/llama-quant.cpp:762-763, 799, 813` (output) and `:938` (input loaded with `check_tensors` on); `src/llama-model-loader.cpp:1486, 1650, 1678, 1743` |
| generic `vec_dot` tq1_0, tq2_0 | `ggml/src/ggml-cpu/quants.c:481-531, 533-563` |
| x86 tq1_0 (AVX2 from 1388), tq2_0 (AVX2 from 1520; comment "should not be 3" at 1534) | `ggml/src/ggml-cpu/arch/x86/quants.c:1376-1506, 1508-1572` |
| ARM tq1_0 (NEON from 1409, DOTPROD from 1417), tq2_0 | `ggml/src/ggml-cpu/arch/arm/quants.c:1397-1572, 1574-1683` |
| CPU type traits: `vec_dot_type` Q8_K, `nrows` 1 | `ggml/src/ggml-cpu/ggml-cpu.c:401-412` |

`llamafile/sgemm.cpp` and `repack.cpp` do not mention TQ types, so the CPU
matrix product for TQ always goes through `vec_dot`.

The same ranges are byte-identical at b6122 (at other line numbers:
`ggml-quants.c` 2103-2271, `ggml-cpu/quants.c` 335-417, x86 1079-1275, ARM
1130-1416), and on 2026-09-23 master (`4e416ee7`) all five files have the
blob SHAs of the pin. The code the reporter ran is the code tested here.

Upstream's own test, `tests/test-quantize-fns.cpp`, compares against the
original floats with loose ternary tolerances (0.01 total quantization error,
0.15 dot-product error); it has no bit-exact test of the kernels against the
dequantizer. The harness below adds one.

### Why `docs/research.md` cited `85c55223`

`docs/research.md` linked `ggml-common.h` at `85c55223` (2026-08-31). That file
has the same blob (`1dbbe326`) at `85c55223` and at `e6ab7c1a`, so the cited
facts do not change; the links now point to `e6ab7c1a`, and every llama.cpp
reference in this repository uses that one pin.

## Harness

Files: `tools/fetch-upstream.sh`, `tests/upstream/` (lock, extractor, fixture
glue, two C programs, AVX2 probe, driver), Makefile target `upstream-15193`, CI job
`upstream-15193` (Ubuntu x86_64 and macOS 15 arm64).

1. `tools/fetch-upstream.sh` downloads the five files from
   `raw.githubusercontent.com` at the pinned commit into
   `build/upstream/llama.cpp-e6ab7c1a/` and rejects any file whose sha256
   differs from the lock. llama.cpp is MIT-licensed; its sources are fetched at
   test time and never committed.
2. `tests/upstream/extract_llamacpp_tq.py` copies verbatim line ranges (block
   structs, `quantize_row_tq*_ref`, `dequantize_row_tq*`,
   `quantize_row_q8_K_ref`, Q4_0 for comparison, the generic kernels, the ARM
   and x86 kernels) into `build/upstream/15193/llamacpp_tq.c`. It refuses to
   write when a file's sha256 differs or when a range does not begin and end
   with the lines recorded in the lock.
3. `tests/upstream/llamacpp_fixtures.py` writes the real tensors from the
   fixture cache (`trinity_memory.fixtures`, sha256-checked against
   `fixtures/manifest.lock.json`): BitNet b1.58 2B4T layer 0 `q_proj` and
   `down_proj` (packed checkpoint `04c3b9ad`) and Ternary Bonsai 2 27B
   `blk.0.ffn_down` (PTQ1_0, `6ed5e12b`). These are byte ranges already
   recorded in the lock (about 25 MB of tensor data plus headers), read from
   `build/fixtures` or, on a clean checkout, fetched by range request; no
   whole checkpoint is downloaded.
4. Each C program compiles into one translation unit with the extracted
   upstream code and the generated `build/t27/formats.h`; the t27 side is the
   executable reference (`tf_decode_blocks`, `tf_encode_blocks`,
   `tf_decode_hf_packed`). Both exit nonzero on any mismatch.
5. `tests/upstream/run-llamacpp-15193.sh` builds and runs both programs for
   every CPU variant the host can execute: on Apple silicon NEON with DOTPROD
   (clang default), NEON without DOTPROD (`-march=armv8-a+nodotprod`, the
   int16 path), x86_64 AVX2 (`-arch x86_64 -mavx2 -mfma -mf16c`) and x86_64
   without AVX2 (the generic fallback), the x86_64 builds under Rosetta 2; on
   Linux x86_64 the AVX2 and generic builds natively (the AVX2 run fails if the
   CPU does not report AVX2). Each variant passes `HARNESS_EXPECT_DOTPROD` or
   `HARNESS_EXPECT_AVX2`, and `llamacpp_harness.h` stops the build with
   `#error` when the compiler selects another kernel path, so a variant cannot
   pass under the wrong name. (Before LLVM 19, clang turned on the default
   CPU's extensions under a bare `-march=armv8-a`, which on a Mac includes
   DOTPROD; hence `+nodotprod`.)
6. The driver refuses to run when the generated headers or the library in
   `build/t27` do not match the current `t27/*.t27` and `native/` sources
   (their lines of `build/t27/SHA256SUMS`).

Rosetta 2 translates AVX2, FMA and F16C only from macOS 15 on; on macOS 14
and earlier the AVX2 binaries stop at their first VEX instruction. The driver
first runs a small probe with the instructions the kernels use
(`tests/upstream/avx2_probe.c`) and skips the AVX2 variant when it fails, or
fails the run with `LLAMACPP_REQUIRE_X86=1` (set in CI). By default Rosetta 2
reports no AVX, AVX2, FMA or F16C through CPUID, so a llama.cpp build with
`GGML_CPU_ALL_VARIANTS` would not pick the AVX2 variant there. With
`ROSETTA_ADVERTISE_AVX=1` in the environment it reports them (checked on
macOS 26.7 with the CPUID score function of `ggml-cpu/arch/x86/cpu-feats.cpp`
at the pin: score 0 by default, 64 for the AVX2 "haswell" variant with the
variable set), and such a build would select that variant. The harness does
not depend on either: it compiles the AVX2 path statically. The Ubuntu CI job
runs the AVX2 path natively (see Native x86 below).

### Commands

```sh
export T27_ROOT=/path/to/gHashTag/t27   # at native/compiler.lock
sh tools/build-t27.sh
sh tests/upstream/run-llamacpp-15193.sh    # or: make upstream-15193
```

Logs: `build/upstream/15193/{test,real}-<variant>.log` and `summary.txt`.

### What each check asserts

Synthetic rows have K = 2560 (10 blocks, the Qwen3-4B hidden size).

| Check | Content | Pass condition |
| --- | --- | --- |
| T1 | every byte value and digit position of the TQ1_0 base-3 extraction: generic `(q*3)>>8`, the NEON `vhadd` form and the AVX2 `avg` form | identical, never above 2; exactly 13 qs byte values are never produced by the quantizer |
| T2 | 5000 rows of random bytes (including TQ1_0 bytes the quantizer never emits and TQ2_0 code 3), unit scales so every result is an exact integer | upstream dequantize = t27 `tf_decode_blocks` value for value; generic `vec_dot` = arch `vec_dot` = both integer products; t27 reports exactly the number of TQ2_0 code-3 digits (decoded as +2, like upstream) |
| T3 | 2000 Gaussian rows with real scales and q8_K activations | generic and arch results within 1e-5 of the sum of absolute block terms |
| T4 | 3000 ternary rows `t*s`, scales fp16-exact, bf16-exact or arbitrary f32 | t27 encode = upstream `quantize_row_tq*_ref` byte for byte; t27 decode of upstream bytes returns the trits and scales; values are unchanged for fp16- and bf16-exact scales |
| T6 | 2000 ternary rows, s = 1.21875 | arch `vec_dot` = exact integer result times scales, within 1e-5 of the sum of absolute block terms |
| T5 | informational: TQ1_0 on float rows (Gaussian, Laplace, Student-t) against Q4_0 | none |
| real | BitNet `q_proj` 2560 x 2560 and `down_proj` 2560 x 6912 through upstream TQ1_0 and TQ2_0 with c = `weight_scale` and c = 1/`weight_scale` | no trit changes; t27 re-encodes the upstream bytes exactly; generic and arch integer accumulators equal the exact integer product in every row; float within 1e-5; with c = `weight_scale` (fp16-exact) no value changes |
| real | Bonsai `ffn_down` (group-128 scales) through upstream TQ1_0 | no trit changes and exact t27 re-encoding; value changes are reported |

The checks are sensitive: changing one digit rule in the extracted
`dequantize_row_tq1_0` from `(q * 3) >> 8` to `(q * 3 + 1) >> 8` makes T2 report
3830 and 958 mismatching rows and T4 752 and 734 changed values, and the run
fails.

### Results on 2026-09-23 (Apple M4, macOS 26.7, Apple clang 21.0.0)

All eight runs passed: `test` and `real` for `arm64-dotprod`, `arm64-int16`,
`x86_64-avx2` (Rosetta 2) and `x86_64-generic` (Rosetta 2). Each log names
the kernel path it ran (`arm64 NEON+DOTPROD`, `arm64 NEON int16 path (no
DOTPROD)`, `x86_64 AVX2`, `x86_64 without AVX2 (generic fallback)`).

- T1: 0 mismatches; 13 of 256 qs byte values (1, 20, 40, 60, 79, 99, 119, 138,
  158, 178, 197, 217, 237) are never emitted by `quantize_row_tq1_0_ref`.
  They decode like their neighbours, so they are aliases, not errors.
- T2: 0 mismatches in 5000 rows on every variant; every TQ2_0 row held code-3
  digits, which upstream and t27 both decode as +2.
- T3: NEON (both paths) and the generic x86 build are bit-exact to the generic
  kernel in 4000 of 4000 results; AVX2 in 420 of 4000, with the largest
  difference 8.07e-7 of the sum of absolute block terms (eight lane sums).
- T4: 0 wrong trits or scales, 0 byte differences between the t27 encoder and
  the upstream quantizer; 0 changed values with fp16-exact and bf16 scales;
  with arbitrary f32 scales 3,025,380 values change, all from rounding the
  block scale to fp16.
- T6: at most 1.38e-7 (NEON, generic) and 2.95e-7 (AVX2) of the sum of
  absolute block terms.
- T5, synthetic float rows (not Qwen3 weights): TQ1_0 leaves 86.5% (Gaussian), 94.4% (Laplace)
  and 96.3% (Student-t, 4 degrees of freedom) of the trits zero; the relative
  RMSE of the weights is 0.81-0.85 and the relative error of the matrix-vector
  product through the kernel is 0.75-0.81. Q4_0 on the same rows gives
  0.09-0.12 and 0.08-0.11.
- BitNet 2B4T layer 0, `q_proj` (6,553,600 weights, `weight_scale` 1.21875)
  and `down_proj` (17,694,720 weights, 2.15625): 0 trits changed in TQ1_0 and
  TQ2_0, 0 byte differences on t27 re-encoding, integer accumulators exact in
  2560 of 2560 rows for the generic and arch kernels on every variant, 0 value
  changes with c = `weight_scale`. With c = 1/`weight_scale` the fp16 scale
  rounds, so 3,301,885 and 10,932,926 values change while the trits stay. The
  largest float error is 1.31e-7 (NEON), 1.67e-7 (generic x86) and 1.84e-6
  (AVX2) of the sum of absolute block terms.
- Ternary Bonsai 2 27B `blk.0.ffn_down` (17408 x 5120): in 347,140 of 348,160
  blocks of 256 the two group-128 scales differ (largest ratio 1.59), so one
  TQ1_0 scale per block cannot represent them. The trits survive, but
  29,869,932 of 59,914,507 nonzero weights change value (mean relative change
  3.9%). This is a not-representable cell for the compatibility matrix (#32),
  not a defect of TQ1_0.

Native x86: the Ubuntu x86_64 CI job (run 35897659923, 2026-09-23, commit
8ebf8f7) built and ran the AVX2 and generic variants natively, and all four
passed (`PASS x86_64-avx2 test`, `real`; `PASS x86_64-generic test`, `real`).
The numbers match the Rosetta 2 runs: T2 0 mismatches for both variants; T3
AVX2 bit-identical to the generic kernel in 420 of 4000 rows, largest
difference 8.07e-07 of the sum of absolute block terms (generic: 4000 of
4000, 0); T6 largest deviation from the exact integer times the scale
2.95e-07 (AVX2) and 1.22e-07 (generic); T4 0 wrong trits or scales, 0
re-encoded rows that differ from upstream's quantize.

## What an end-to-end confirmation would need

Not done: it needs model downloads and a llama.cpp build, and neither was
approved for this work.

1. Build llama.cpp at b6122 (`34c9d765`) with CMake on an AVX2 CPU.
2. Convert a float Qwen3 checkpoint (Qwen3-0.6B, or Qwen3-4B-Instruct-2507 as
   reported) with `convert_hf_to_gguf.py --outtype bf16`, quantize to TQ1_0 and
   to Q4_K_M, and compare `llama-perplexity` on a fixed text.
3. For a positive control, use a model trained for ternary weights that stock
   llama.cpp supports, for example a 1bitLLM `BitnetForCausalLM` checkpoint
   with `--outtype tq1_0` and `tq2_0`. Microsoft's `bitnet-b1.58-2B-4T` is not
   a valid control in stock llama.cpp: its graph applies SiLU where the model
   declares `relu2` (`src/models/bitnet.cpp:132` at the pin).

## Consequences for this repository

- Epic #27 described this issue as a report of garbage from TQ1_0/TQ2_0 on
  CPU. It is closed upstream. No TQ storage or kernel defect was found at the
  pin; the upstream maintainer attributes the output to the input model, and
  this repository did not reproduce that end to end.
- For the negative tests (#34), the upstream decoder accepts three classes
  without complaint: TQ2_0 code 3 (decoded as +2), non-canonical TQ1_0 bytes
  (aliases), and NaN or Inf scales. `llama-quantize` never writes NaN or Inf
  scales (it validates every chunk) and rejects them in its input; the
  inference loader rejects them only with `--check-tensors`, which is off by
  default. Negative scales are never rejected.
- For the release (#36): the extracted upstream dequantizer is a third-party
  decoder that the vectors can be checked against.
