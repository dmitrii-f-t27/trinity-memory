# Ternary Check: first results on real checkpoints (2026-09-22, corrected 2026-09-23)

Stage 1 of the roadmap (#27): decode the same real tensors from every public
ternary weight-packing format and compare them trit for trit and scale for
scale. All decoding, container parsing, ternarization and comparison run in
executable t27 (`t27/formats.t27`); Python only fetches byte ranges and writes
the report. Full data: [`reports/ternary-check/2026-09-22.json`](../reports/ternary-check/2026-09-22.json)
and [`reports/ternary-check/bitnet-scales-2026-09-22.json`](../reports/ternary-check/bitnet-scales-2026-09-22.json).

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
sha256 of every range is in `fixtures/manifest.lock.json`.

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
   compared are identical, so the integer accumulators of a matrix-vector
   product agree and the outputs differ by the ratio of the two scales.
2. **The packed trits cannot be recomputed from the published bf16 weights.**
   Applying `transformers` `WeightQuant` (`s = 1 / mean|w|` in float32,
   `round(w * s)`, clamp) to the bf16 master weights gives trits that differ
   from the packed checkpoint for 79,719 weights of layer 0 `q_proj` (1.22%)
   and 101,673 of layer 0 `down_proj` (0.57%). All of them have the single
   bf16 value nearest the rounding threshold, `|w| = 0.5 × weight_scale`
   (0.609375 and 1.078125), and the packed file does not treat that value
   one way: in `q_proj` 79,719 of the 163,223 weights with it are ±1 and
   83,504 are 0 (48.8% ±1), in `down_proj` 101,673 of 1,009,468 (10.1%).
   Weights with the same bf16 value get different trits, so no rule applied
   to the published bf16 values reproduces the packed trits. That fits
   ternarization from higher-precision weights before the bf16 export, which
   we cannot check. Microsoft's model card labels the bf16 repository for
   training and fine-tuning; its config sets `quantization_mode: online`, so
   loading it for inference in `transformers` uses these slightly different
   trits.
3. **I2_S trailers are not zero.** Each I2_S tensor ends with a 32-byte
   trailer: the f32 scale, then 28 bytes. The current bitnet.cpp converter
   (`utils/convert-hf-to-gguf-bitnet.py`, `quantize_to_i2_s` at `0b341e58`)
   writes zeros there; in the published file all 28 bytes are nonzero in all
   11 tensors checked and repeat between tensors of the same shape. The
   dequantizer never reads them, so inference is unaffected, but the file
   cannot be reproduced byte for byte.
4. **Ternary Bonsai 2 is consistent across its four distributions.** For
   layer 0 `ffn_down`, PTQ1_0 (1.75 bits per weight), PQ2_0 (2.125), the
   group-64 Q2_0 file (2.25) and MLX 2-bit (2.25) hold the same trits and the
   same fp16 scales; the Q2_0 file repeats each 128-weight scale twice, and in
   all 696,320 MLX groups the bias equals minus the scale. All three GGUF
   files declare `prism.*` metadata: the runtime must apply the Hadamard
   rotation, which stock llama.cpp does not, so a stock build reads valid
   Q2_0 bytes into a model that produces wrong output.

## Limits

- Storage and integer arithmetic only. Nothing here measures model quality
  or speed.
- Two BitNet tensors and one Bonsai tensor were compared trit for trit;
  the BitNet scale comparison covers all 210 ternary tensors.
- The pipeline that produced the published files was not available to us;
  item 2 reports what the data rule out, not how the files were made.

## Layouts

Decoded by `t27/formats.t27`: llama.cpp TQ1_0, TQ2_0 and Q2_0; the PrismML
fork's PQ2_0 and PTQ1_0; bitnet.cpp I2_S; transformers BitNet packed uint8;
linear 2-bit rows (MLX affine 2-bit, ONNX Runtime `MatMulNBits` with
`bits=2`); and llama.cpp Q1_0, which is binary. Not yet decoded: STQ1_0
(open llama.cpp PR #22836) and the bitnet.cpp lookup-table layouts TL1 and
TL2.

## Reproduce

```sh
export T27_ROOT=/path/to/gHashTag/t27   # at native/compiler.lock
sh tools/build-t27.sh
python3 -m trinity_memory.ternary_check   # writes build/ternary-check.json
```
