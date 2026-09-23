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
