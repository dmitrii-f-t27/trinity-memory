# Ternary Check: first results on real checkpoints (2026-09-22, corrected 2026-09-23)

Stage 1 of the roadmap (#27): decode the same real tensors from every public
ternary weight-packing format and compare them trit for trit and scale for
scale. The trits and scales in the Results table come from executable t27
(`t27/formats.t27`); Python only fetches byte ranges and writes the report.
The 210-tensor scale and trailer checks and the tie counts in items 2 and 3
come from [`tools/bitnet_audit.py`](../tools/bitnet_audit.py), an independent
standard-library cross-check that reproduces the t27 numbers. Full data:
[`reports/ternary-check/2026-09-22.json`](../reports/ternary-check/2026-09-22.json),
[`reports/ternary-check/bitnet-scales-2026-09-22.json`](../reports/ternary-check/bitnet-scales-2026-09-22.json)
and [`reports/ternary-check/bitnet-audit-2026-09-23.json`](../reports/ternary-check/bitnet-audit-2026-09-23.json).

## Sources (pinned revisions)

| Artifact | Revision | File |
| --- | --- | --- |
| microsoft/bitnet-b1.58-2B-4T (packed, offline) | `04c3b9ad` | model.safetensors |
| microsoft/bitnet-b1.58-2B-4T-bf16 (master weights, `quantization_mode: online`) | `27668139` | model.safetensors |
| microsoft/bitnet-b1.58-2B-4T-gguf (bitnet.cpp) | `a1f2f1c7` | ggml-model-i2_s.gguf |
| prism-ml/Ternary-Bonsai-2-27B-gguf | `6ed5e12b` | PTQ1_0, PQ2_0 |
| prism-ml/Ternary-Bonsai-2-27B-gguf-dev | `2a263ef8` | Q2_0-prism-fork-required |
| prism-ml/Ternary-Bonsai-2-27B-mlx-2bit | `fcba37d2` | model.safetensors |

Only headers and the tensors below were read, by HTTP range requests; the
sha256 of every range read by the t27 run is in `fixtures/manifest.lock.json`,
and `tools/bitnet_audit.py` reads the remaining trailer ranges from the same
pinned revisions.

## Results

| Model, tensor | Weights | Compared | Trits | Scales |
| --- | ---: | --- | --- | --- |
| BitNet 2B4T, layer 0 `q_proj` | 6,553,600 | HF packed vs GGUF I2_S | identical | bf16 1.21875 vs f32 1.2188548 |
| BitNet 2B4T, layer 0 `down_proj` | 17,694,720 | HF packed vs GGUF I2_S | identical | bf16 2.15625 vs f32 2.1631613 |
| BitNet 2B4T, layer 0 `q_proj` | 6,553,600 | HF packed vs bf16 master under `WeightQuant` | 79,719 differ (1.22%) | — |
| BitNet 2B4T, layer 0 `down_proj` | 17,694,720 | HF packed vs bf16 master under `WeightQuant` | 101,673 differ (0.57%) | — |
| Ternary Bonsai 2 27B, layer 0 `ffn_down` | 89,128,960 | PTQ1_0, PQ2_0, Q2_0 (group 64), MLX 2-bit | identical in all four | identical in all four |

Details:

1. **BitNet stores one scale at two precisions.** The packed checkpoint
   stores `weight_scale` as bf16 and the GGUF stores an f32 scale after each
   I2_S tensor. In all 210 ternary tensors the bf16 value is exactly the
   f32 value rounded to bf16, so the two differ by 0.14% at the median and
   0.36% at most (layer 4 `attn_output`). The trits of the two tensors we
   compared are identical, so for the same int8 activations the integer
   products agree and only the per-tensor factor differs. We did not run
   inference.
2. **The packed trits cannot be recomputed from the published bf16 weights.**
   Applying `transformers` `WeightQuant` (`s = 1 / mean|w|` in float32,
   `round(w * s)`, clamp) to the bf16 master weights gives trits that differ
   from the packed checkpoint for 79,719 weights of layer 0 `q_proj` (1.22%)
   and 101,673 of layer 0 `down_proj` (0.57%). All of them have the single
   bf16 value nearest the rounding threshold, `|w| = 0.5 × weight_scale`
   (0.609375 and 1.078125), and the packed file does not treat that value
   one way: in `q_proj` 79,719 of the 163,223 weights with it are ±1 and
   83,504 are 0 (48.8% ±1), in `down_proj` 101,673 of 1,009,468 (10.1%).
   There `|w·s|` is 0.49996 and 0.49841, well beyond the rounding error of
   a float32 mean.
   Weights with the same bf16 value get different trits, so no rule applied
   to the published bf16 values reproduces the packed trits. That fits
   ternarization from higher-precision weights before the bf16 export, which
   we cannot check. Microsoft's model card labels the bf16 repository for
   training and fine-tuning; its config sets `quantization_mode: online`, so
   loading it for inference in `transformers` uses these slightly different
   trits.
3. **I2_S trailers carry leftover bytes.** Each I2_S tensor ends with a
   32-byte trailer: the f32 scale, then 28 bytes. In the published file those
   28 bytes are nonzero in all 210 I2_S tensors, and in all 210 they equal
   the bytes an earlier, at least as large tensor in the file holds at the
   same offset (`token_embd` or a tensor of the same layer). This is what a
   reused output buffer leaves behind: bitnet.cpp's C `quantize_i2_s`, which
   `llama-quantize` used when the GGUF was uploaded in April 2025, writes only
   the packed bytes and the scale. `quantize_to_i2_s` in the Python converter
   (`utils/convert-hf-to-gguf-bitnet.py` at `0b341e58`) writes zeros there, so
   the two conversion paths give different bytes. The dequantizer does not
   read them, so inference is unaffected.
4. **Ternary Bonsai 2 is consistent across its four distributions.** For
   layer 0 `ffn_down`, PTQ1_0 (1.75 bits per weight), PQ2_0 (2.125), the
   group-64 Q2_0 file (2.25) and MLX 2-bit (2.25) hold the same trits and the
   same fp16 scales; the Q2_0 file repeats each 128-weight scale twice, and in
   all 696,320 MLX groups the bias equals minus the scale. All three GGUF
   files declare `prism.*` metadata: the runtime must apply the Hadamard
   rotation. A stock llama.cpp build would load the valid Q2_0 bytes and run
   the model without the rotation; PrismML names that file
   `prism-fork-required` and documents gibberish output. We did not run it.

## Limits

- Storage and integer arithmetic only. Nothing here measures model quality
  or speed.
- Two BitNet tensors and one Bonsai tensor were compared trit for trit;
  the BitNet scale and trailer checks cover all 210 ternary tensors.
- No model was run; nothing here measures an effect on model output.
- The pipeline that produced the published files was not available to us;
  item 2 reports what the data rule out, not how the files were made.

## Layouts

Decoded by `t27/formats.t27`: llama.cpp TQ1_0, TQ2_0 and Q2_0; the PrismML
fork's PQ2_0 and PTQ1_0; bitnet.cpp I2_S; transformers BitNet packed uint8;
linear 2-bit rows (MLX affine 2-bit, ONNX Runtime `MatMulNBits` with
`bits=2`); and llama.cpp Q1_0, which is binary. Not yet decoded: STQ1_0
(open llama.cpp PR #22836) and the bitnet.cpp lookup-table layouts TL1 and
TL2.

## Contracts and negative cases

Each decoded layout above has a sealed contract in [`specs/formats/`](../specs/formats/)
that restates pinned upstream commits
([`upstream.lock.json`](../specs/formats/upstream.lock.json)), and a vector file
`conformance/formats_<family>.json` that the specs, `t27/formats.t27` (C and
WASM) and the Python bindings must all reproduce. The readers reject lengths
that are not whole blocks (truncated tensors), NaN or infinite scales, nonzero
padding digits in TQ1_0/PTQ1_0 `qh` bytes, GGUF type ids that are ambiguous
between the ggml-org, PrismML and bitnet.cpp namespaces, TL1 ids and TL2 ids
in files marked as bitnet.cpp (an unmarked TL2 file reads as Q2_0 and is caught,
if at all, by the extent check), misaligned offsets, GGUF and safetensors
tensors that overlap the tensor before or after them or run past the end of the
file (a GGUF tensor that begins inside a record of a type whose size the reader
does not know is not caught), safetensors tensors with a gap before or after
them or bytes after the last one, GGUF weight counts
and safetensors sizes that do not fit, safetensors offsets that wrap around
64 bits, and safetensors byte ranges that do not match
the shape and dtype. They decode, and flag, what upstream accepts silently and
real files may carry: 2-bit code 3 (+2), base-3 bytes that decode like
canonical ones, negative or zero scales, nonzero I2_S trailer bytes, nonzero
ONNX padding and MLX groups whose bias is not -scale. Group-128 bytes read as
group-64 Q2_0 (a synthetic case), I2_S bytes in the ARM or four-row layouts
of bitnet.cpp's `ggml-bitnet-mad.cpp` (which its pinned build does not compile)
or from the `quantize_i2_s` of its llama.cpp submodule (four consecutive weights
per byte), a Q1_0 byte (Q1_0 has no invalid bit pattern), and any
corrupted code byte or scale that stays valid decode without any signal. Every
negative vector records what each upstream reader does with the same bytes,
with the pinned `file:line`. For I2_S code 3 bitnet.cpp disagrees with itself:
`dequantize_row_i2_s` reads it as 0, `mul_mat` as +2. The full
table of status classes is in [`specs/formats/OWNERS.md`](../specs/formats/OWNERS.md).
STQ1_0 has no contract yet (the llama.cpp pull request is open), and TL1/TL2
byte order depends on build-time tile sizes that the file does not record.

## llama.cpp issue 15193 (TQ1_0/TQ2_0 on CPU)

At llama.cpp `e6ab7c1a` we found no TQ1_0/TQ2_0 storage or CPU kernel
defect that would explain the garbage output reported in llama.cpp issue
15193. On random blocks and on the BitNet layer-0 tensors above, the upstream
quantizers produce the same bytes as the t27 encoders, the upstream
dequantizers and the t27 decoders return the same trits and scales, and the
integer accumulators of the generic, NEON and AVX2 `vec_dot` kernels equal
the exact integer products. The kernels' float results are not bit-identical
(the AVX2 kernels sum in eight lanes); they agree within 1e-5 of the sum of
absolute block terms. The AVX2 kernels ran under Rosetta 2 on Apple silicon
and natively on the Ubuntu x86_64 CI runner, with the same results.

The upstream maintainer closed the issue on 2025-08-09, attributing the output
to quantizing a float-trained model (Qwen3-4B-Instruct-2507) to TQ1_0. The
synthetic float rows in the note agree with that explanation, but we did not
run a model, so it was not reproduced here. Note, commands and numbers:
[`docs/upstream/llama.cpp-15193.md`](upstream/llama.cpp-15193.md). Nothing
was reported upstream.

## Real layer: int8 matvec on decoded weights (#33)

For the three tensors above, every stored form is decoded by `t27/formats.t27`
and multiplied with one int8 activation vector by `t27/matvec.t27`, in
generated C and in `formats.wasm`. The activations are
`random.Random(27).randint(-128, 127)` of CPython, drawn in index order by the
t27 port of its generator (`t27/random.t27`); a tensor with n input columns
uses the first n of 17,408 draws (sha256 of the int8 bytes `1987f310…`, first
values 117, 13, 18, -28). Each row is widened to i64 once, and every group of
64 columns is one `tm_dot_i64` call of `t27/compute.t27`, so the report
carries partial sums per 64 columns as well as the row sums `y_int`. 64
divides every scale group here (64 for Q2_0, 128 for PTQ1_0, PQ2_0 and MLX).

| Model, tensor | Shape | Stored forms | Integer accumulators | max \|y_int\| | Float step |
| --- | --- | --- | --- | ---: | --- |
| BitNet 2B4T, layer 0 `q_proj` | 2560 × 2560 | HF packed, GGUF I2_S | identical: 2,560 rows, 102,400 partials | 10,924 | `y_int × weight_scale`: bf16 1.21875 vs f32 1.2188548, so all 2,560 rows differ |
| BitNet 2B4T, layer 0 `down_proj` | 2560 × 6912 | HF packed, GGUF I2_S | identical: 2,560 rows, 276,480 partials | 18,753 | bf16 2.15625 vs f32 2.1631613, all 2,560 rows differ |
| Ternary Bonsai 2 27B, layer 0 `ffn_down` | 5120 × 17408 | PTQ1_0, PQ2_0, Q2_0 (group 64), MLX 2-bit | identical: 5,120 rows, 1,392,640 partials | 36,778 | identical in all four, and equal to the exact value in all 5,120 rows |

So for every format that stores these tensors the integer results are the
same bits; the BitNet float results differ only because the two files store
the scale at two precisions (item 1), and every product there is exact.

The float step, with the arithmetic used:

- BitNet: `y[r] = y_int[r] × weight_scale`, one f64 product per row, with the
  bf16 `weight_scale` of the transformers checkpoint or the f32 scale of the
  I2_S tensor. The packed checkpoint's config selects `AutoBitLinear`
  (`linear_class: autobitlinear`, `quantization_mode: offline`, as
  [`specs/formats/hf_bitnet.t27`](../specs/formats/hf_bitnet.t27) records;
  `bitnet.py:317-324`), whose forward pass multiplies by `weight_scale`
  (transformers `2c4914fb`, `src/transformers/integrations/bitnet.py:292`);
  the class that divides, `BitLinear` (`:181`), is not used by this model.
  `|y_int| < 2^29` and a scale has at most 24 significant bits, so each
  product is exact.
- Bonsai: `y[r] = Σ_j f16(scale_j) × p[r][j]` over the 272 partials of a row,
  j ascending, summed in f64 from 0. Each product is exact; the sum could
  round, so an exact integer form `Σ_j (f16(scale_j) × 2^24) × p[r][j]` is
  computed in checked i64 as well, and the f64 result equals it in every row
  of every format. MLX computes `q × scale + bias`; that equals the ternary
  form because the bias is minus the scale in all 696,320 groups (item 4).
- Activation scale: `x` stands for activations already quantized with one
  scale per token. transformers' `ActQuant` (`bitnet.py:235-236`) uses
  `127 / max|x|`; the layer output is the float step divided by that scale,
  a common factor for every format of the same tensor, so it is left out.
  How each runtime quantizes its activations is not modeled.
- Hadamard rotation: the three Bonsai GGUF files carry `prism.hadamard.*`
  metadata (transform `normalized-sylvester-walsh-hadamard`), which the
  PrismML runtime applies at inference. It is a runtime transform, not
  storage: the products here use the stored trits and scales without it, so
  `y` is not the model's layer output, and the rotation cannot change the
  agreement between formats that store the same trits and scales.

The bf16 master weights are not a stored ternary form. Ternarized with
`WeightQuant` (item 2) they give accumulators that differ from the packed ones
in 2,529 of 2,560 rows of `q_proj` and 2,549 of 2,560 rows of `down_proj`.
The difference tensor D = packed − absmean is nonzero at exactly the 79,719
and 101,673 weights of item 2 (in 2,534 and 2,550 rows), and in every row
`y_packed − y_absmean = D · x` exactly; in 5 and 1 rows the differing trits
cancel. The accumulators differ because the trits differ, which is item 2:
the packed trits cannot be recomputed from the published bf16 weights.

Full data, deterministic (no timestamps): [`reports/ternary-check/matvec-2026-09-23.json`](../reports/ternary-check/matvec-2026-09-23.json),
with the sha256 of `x`, and per tensor and form the sha256 and first values of
the accumulators, the partials and the float step. `python3 -m trinity_memory.matvec --check`
recomputes it from the fixture cache, `tests/test_matvec.py` does the same in
the unit tests, `tests/native_matvec.c` checks the module against oracles
written in C (including the CPython stream for four seeds), and
`tests/matvec_wasm.mjs` recomputes all eight stored forms in `formats.wasm`
and checks them against the report when the fixture cache holds them (the CI
job `ternary-check` runs it with `TRINITY_REQUIRE_CACHED=1`, so a skipped form
fails there). The consumer `layer0_matvec` of
`fixtures/manifest.json` is now implemented; this supersedes the note under
Fixtures that it is planned. It reads the same 36 ranges as
`trinity_memory.ternary_check`.

## Matrix: every real tensor in every format (#32)

The three tensors above (BitNet 2B4T layer 0 `q_proj` and `down_proj`, Ternary
Bonsai 2 27B layer 0 `ffn_down`) against ten storage formats in twelve columns
(TQ1_0 and TQ2_0 each written by t27 and by llama.cpp) give 36 cells.
Every cell is decided by executable t27, `t27/matrix.t27` over the readers and
writers of `t27/formats.t27` (generated C, and `formats.wasm`, where
`tests/matrix_wasm.mjs` recomputes all 36 cells and the two derived ones below
when the caches hold their bytes; the CI job `ternary-check` runs it with
`TRINITY_REQUIRE_CACHED=1`, so a skipped cell fails there); Python only moves
bytes and renders. Each tensor has a reference, a published form chosen as the
baseline (HF packed for BitNet, PTQ1_0 for Bonsai; the choice says nothing
about which form was published first), and a cell is:

- `match`: trits identical, and every weight's scale has the same value (scales
  are compared weight by weight as exact values, so an f16 and a bf16 word of the
  same number are equal);
- `mismatch`: trits or scales differ, with the count, the first differing index,
  an explanation code checked by t27 and a minimal reproduction in
  `reports/ternary-check/repro/`;
- `not-representable`: the format cannot hold the tensor, with every reason that
  applies (`shape`, `binary_only`, `code_outside`, `group_scales_differ`,
  `scale_precision`), the number of affected units and the first weight index.

A cell's bytes come from one of three places, or from none (the `provenance` of
the cell). Published bytes of the pinned checkpoints (3 references and 5 other
cells) and bytes written by the pinned llama.cpp reference quantizers
`quantize_row_tq1_0_ref` and `quantize_row_tq2_0_ref` (4 cells, BitNet only) are
third-party evidence: for BitNet HF packed, I2_S, TQ1_0 and TQ2_0, for Bonsai
PTQ1_0, PQ2_0, Q2_0 (group 64) and MLX 2-bit. The other 24 cells are t27's alone:
15 t27 round trips (the t27 encoder writes the reference, the t27 decoder reads
it back), which show that a format can hold the tensor, and 9 not-representable
cells (provenance `not_written`), which t27 decides from the reference before
anything is written, so neither the t27 encoder nor a llama.cpp quantizer runs
for them (among them TQ1_0 and TQ2_0 of the Bonsai tensor in both columns).
None of these 24 is third-party evidence. The llama.cpp input is
`w = t × weight_scale` in float, one quantize call per row
(`tests/upstream/encode_llamacpp_tq.c`); the sources are the pinned ones of the
issue 15193 harness, fetched and sha256-checked by
`tests/upstream/run-llamacpp-matrix.sh`.

| Format | BitNet `q_proj` | BitNet `down_proj` | Bonsai `ffn_down` |
| --- | --- | --- | --- |
| HF packed + bf16 `weight_scale` | reference | reference | not representable: one scale per tensor; 608,946 of 696,320 f16 scales have no bf16 word |
| I2_S (bitnet.cpp) | mismatch: scales, bf16 rounding of the f32 scale | mismatch: scales, bf16 rounding | not representable: one scale per tensor |
| PTQ1_0 | match (t27 round trip) | match (t27 round trip) | reference |
| PQ2_0 | match (t27 round trip) | match (t27 round trip) | match (published) |
| Q2_0, group 64 | match (t27 round trip) | match (t27 round trip) | match (published) |
| MLX 2-bit, group 128 | match (t27 round trip) | match (t27 round trip) | match (published) |
| TQ1_0, TQ2_0 (t27) | match (t27 round trip) | match (t27 round trip) | not representable: 347,140 of 348,160 blocks span two scales |
| TQ1_0, TQ2_0 (llama.cpp writer) | mismatch: scales of 50 all-zero blocks | match | not representable (decided before writing) |
| Q1_0 (binary) | not representable: 3,251,715 zeros | not representable: 6,761,794 zeros | not representable: 29,214,453 of 89,128,960 zeros |
| ONNX `MatMulNBits` bits=2, block 128 | match (t27 round trip) | match (t27 round trip) | match (t27 round trip) |

Totals: 23 match, 4 mismatch, 9 not representable. What the matrix adds:

- **I2_S against HF packed.** The trits are identical and the scale differs for
  every weight; t27 checks that the bf16 `weight_scale` is the f32 I2_S scale
  rounded to nearest-even (explanation `scale_bf16_rounding`, item 1). Written
  again by the t27 encoder, the decoded I2_S tensor differs from the published
  bytes in exactly the 28 trailer bytes that `trailer_nonzero` counts (item 3).
- **llama.cpp's TQ1_0 and TQ2_0 writers on BitNet.** Both keep every trit of both
  tensors. In `q_proj` they store the scale 0 for 50 blocks of 256 weights
  (12,800 weights from index 3,934,464) whose trits are all 0, where the
  reference has the tensor scale: explanation `scale_zero_weights`, so every
  weight dequantizes to the same value. Given the scale words the upstream file
  holds, the t27 encoder writes the upstream bytes exactly; the t27 round trip,
  which writes the tensor scale into every block, differs from them in the 100
  bytes of those 50 scale words. In `down_proj` the upstream and t27 bytes are
  identical.
- **Triple check for BitNet.** The packed trits equal the I2_S trits; the bf16
  master weights under `WeightQuant` (a derived cell, not a storage format) differ
  from both in 79,719 (`q_proj`) and 101,673 (`down_proj`) weights, all of them at
  the bf16 value `0.5 × weight_scale` (0.609375 and 1.078125, where `|w·s|` is
  0.49996 and 0.49841). The packed file stores both trits there: at +0.609375
  39,708 ±1 and 41,560 zeros, at −0.609375 40,011 and 41,944; at ±1.078125 50,812
  and 453,819, and 50,861 and 453,976. t27 records this as `tie_split`: the same
  bf16 value carries different packed trits, so the packed trits cannot be
  recomputed from the published bf16 weights (item 2).
- **Ternary Bonsai 2.** PQ2_0, Q2_0 and MLX match PTQ1_0 (item 4), and the t27
  encoder writes each published tensor back byte for byte. Formats with one scale
  per 256 weights or per tensor cannot hold it, because its 128-weight groups have
  their own scales, and Q1_0 has no code for 0. The Hadamard rotation that the
  Bonsai GGUF files declare is a runtime transform, not storage, so it is not a
  reason for any cell; the report records it per tensor.
- **ONNX Runtime `MatMulNBits` with `bits=2`** (block 128, fp16 scales, default
  zero point 2) holds all three tensors in t27 round trips.
- **Bits per weight.** Every cell with bytes carries `stored_bytes` and
  `bits_per_weight` (codes, scales, zero points or biases, and the format's own
  padding, as the table under "Bits per weight" in `specs/formats/OWNERS.md`
  counts them) and `metadata`, the tensor's metadata share as OWNERS.md defines
  it, both computed by t27 (`tmx_bits_per_weight`, `tmx_gguf_metadata_bytes`).
  For a published GGUF tensor the share is its info record and the alignment
  padding after its data: 61 bytes for Bonsai `ffn_down` in Q2_0, so 2.25 bits
  per weight become 2.2500055 (the case `specs/formats/llama_cpp.t27` tests).
  A safetensors tensor has no share of its own (the JSON header is file
  overhead), and neither have bytes in no container: the t27 round trips and
  the llama.cpp writers' output.

Reports, all deterministic (the time and host of a run go to
`build/ternary-check/run.json`, outside them):
[`reports/ternary-check.json`](../reports/ternary-check.json) (schema
`trinity.ternary-check.v1`, JSON Schema
[`schemas/ternary-check.v1.schema.json`](../schemas/ternary-check.v1.schema.json);
its `matvec` section is the #33 report above),
[`reports/ternary-check.html`](../reports/ternary-check.html) (the table as one
self-contained page), and one reproduction per mismatch in
[`reports/ternary-check/repro/`](../reports/ternary-check/repro/) (schema
[`schemas/ternary-check.repro.v1.schema.json`](../schemas/ternary-check.repro.v1.schema.json)):
file offsets, bytes, bit positions, decoded trits and scale words of the first
differing weights, and for the derived cells weights with the same bf16 master
word and different packed trits. The error and flag tokens of the report
(`taxonomy.errors`, `taxonomy.flags`) are those of `specs/formats/OWNERS.md`
(`constants.errors`, `constants.flags` of the conformance files), and each
published cell carries its reader flags. The cell statuses, reasons and
explanations are the `TMX_*` tokens of `t27/matrix.t27`: verdicts on valid
input, not rejects (OWNERS.md relates them to the reject classes in its
paragraph on `t27/matrix.t27`).

One command, from a clean clone:

```sh
make ternary-check            # fetch fixtures (strict), build t27, run llama.cpp's writers, write the reports
make ternary-check OFFLINE=1  # the same from the caches only (build/fixtures, build/upstream, T27_ROOT)
make ternary-check-verify     # recompute and compare with the committed reports
```

Without `T27_ROOT` the script fetches gHashTag/t27 at `native/compiler.lock` into
`build/compiler`. For the matrix this replaces the two commands under Reproduce
below: `python3 -m trinity_memory.ternary_check` alone now also needs the
llama.cpp-encoded tensors in `build/upstream/matrix/` (written by
`sh tests/upstream/run-llamacpp-matrix.sh`, which `make ternary-check` runs), it
writes `build/ternary-check.json`, `build/ternary-check.html` and
`build/ternary-check/repro/`, and when a cache file is missing it stops with
exit status 2 and a message naming the step, not a traceback.

The CI job `ternary-check` runs `make ternary-check` on a clean checkout with the
fixture cache and fails unless the committed reports come out exactly; then, with
`TRINITY_REQUIRE_CACHED=1`, `tests/matrix_wasm.mjs` and `tests/matvec_wasm.mjs`
recompute every cell and stored form in `formats.wasm` and the unit tests of both
reports recompute them in the generated C, and a skip fails.
`tests/test_ternary_check.py` validates the report and every reproduction against
the schemas, checks that every number this section quotes is the report's and is
written here, and recomputes all committed files from the caches (skipped only
when a cache file is missing); `tests/native_matrix.c` checks the t27 module
against oracles written in C, including a consistent tie rule that must stay
unexplained and a zero-scale block with a nonzero trit on one side.

`reports/ternary-check/2026-09-22.json`, the "Full data" of the Results table, is
the first run's snapshot, kept as history. It carries the schema string
`trinity.ternary-check.v1` but predates the JSON Schema: its shape is
`{schema, generated, checks}`, with a timestamp and run times, it does not
validate against `schemas/ternary-check.v1.schema.json` (whose `$comment` says
so), and nothing regenerates it. `reports/ternary-check.json` holds each of its
numbers, and `tests/test_ternary_check.py` checks that they are equal.

Limits: three tensors; the t27 round trips are not third-party evidence;
llama.cpp is the only upstream writer run here; no model was run.

## Reproduce

```sh
export T27_ROOT=/path/to/gHashTag/t27   # at native/compiler.lock
sh tools/build-t27.sh
python3 -m trinity_memory.ternary_check   # about 216 MB of byte ranges, writes build/ternary-check.json
```

The BitNet observations can also be checked without building t27:
[`tools/bitnet_audit.py`](../tools/bitnet_audit.py) uses only the Python
standard library, reads about 63 MB of byte ranges and prints the 210-tensor
scale and trailer table and the layer-0 counts above as JSON
(`python3 tools/bitnet_audit.py > audit.json`). The t27 build needs a Rust
toolchain (`cargo +1.94.0`) for the pinned compiler.

## Fixtures

[`fixtures/manifest.json`](../fixtures/manifest.json) (schema
`trinity.fixtures-manifest.v1`) is the single source of truth for the real
weights used here. For each of the six Hugging Face repositories above it
pins the full commit sha, the license, and the name and size of every file
read; for every byte range it gives the file, the kind (`prefix`, `tensor`,
`scale`, `trailer`), the tensor name, dtype or GGUF type id, shape, begin,
end (exclusive), sha256 and `used_by`, the consumers that read it. It
lists 544 ranges, 205.8 MiB in total, exactly what
`trinity_memory.ternary_check` (36 ranges) and `tools/bitnet_audit.py`
(521 ranges, 66,692,572 bytes measured, the "about 63 MB" above in MiB)
read; 13 of them are read by both. They include all 210 I2_S trailers,
whose first 4 bytes are the f32 scale words tabulated in
`reports/ternary-check/bitnet-scales-2026-09-22.json`. The consumer
`layer0_matvec` (#33) is planned, not implemented: it tags the same 36
ranges `ternary_check` reads (BitNet layer-0 `q_proj` and `down_proj` in all
three forms with their scales; Bonsai `ffn_down` in PTQ1_0, PQ2_0, Q2_0 and
MLX with scales and biases) and adds no range of its own. No whole
checkpoint is ever downloaded. This supersedes the note under Sources that
the lock holds only the ranges of the t27 run: every range
`tools/bitnet_audit.py` reads, trailers included, is now pinned by sha256 in
the manifest and in `fixtures/manifest.lock.json`.

One command fetches them into `build/fixtures/`:

```sh
python3 tools/fetch-fixtures.py            # fetch missing ranges, verify all
python3 tools/fetch-fixtures.py --offline  # verify the cache; no network, nothing written
```

Each range is an anonymous HTTP range request (no token is sent) that must
answer 206 with exactly the requested length and match its sha256 before it
is written. A first run sends 544 range requests. Hugging Face allows
anonymous clients 3,000 resolver requests per IP address per 5-minute window
(its rate-limit page, September 2025); the tool retries a 429 after the time
the `RateLimit` header gives, retries a 5xx, a network error or a body cut
short after 2, 4 and 8 seconds (the header's time only when it reports no
requests left), and uses 4 concurrent requests by default (`--jobs`).
`trinity_memory.fixtures` re-hashes a cached range on every read; a cached
prefix chunk is hashed on its first read in a process and again whenever
`prefix.bin` changes (size, inode, modification or change time). It refuses
a range the manifest does not list; `tools/fetch-fixtures.py --record REPO
FILE BEGIN END --kind KIND --tensor NAME --dtype DTYPE --shape N ...
--used-by CONSUMER` adds one explicitly, and a new prefix chunk must start
where the pinned prefix ends. With `TRINITY_FIXTURES_OFFLINE=1` (or
`tools/bitnet_audit.py --offline`, or `tools/fetch-fixtures.py --offline`)
nothing is fetched. The tool never
deletes or truncates cache files, since several checkouts may share one
cache; a file the manifest does not list, or a `prefix.bin` longer than the
pinned prefix, fails the check unless `--no-strict` is given.
`fixtures/manifest.lock.json` is generated from the manifest (`--write-lock`
writes `manifest.lock.json` next to `--manifest`) and keeps the flat
`range: sha256` view referenced above.
