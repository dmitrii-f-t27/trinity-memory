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

## llama.cpp issue 15193 (TQ1_0/TQ2_0 on CPU)

The garbage output reported in llama.cpp issue 15193 comes from quantizing a
float-trained model (Qwen3-4B-Instruct-2507) to TQ1_0, not from TQ storage or
the CPU kernels; the issue was closed upstream on 2025-08-09 for that reason.
At llama.cpp `e6ab7c1a` the upstream TQ1_0/TQ2_0 quantizers, dequantizers and
the generic, NEON and AVX2 `vec_dot` kernels agree with the t27 decoders and
encoders bit for bit on random blocks and on the BitNet layer-0 tensors above.
Note, commands and numbers: [`docs/upstream/llama.cpp-15193.md`](upstream/llama.cpp-15193.md).
Nothing was reported upstream.

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
