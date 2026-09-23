# Conformance vectors (`conformance/*.json`)

Language-independent expectations for the contracts stated under `specs/memory/`
and `specs/formats/`. Each file names its `module` and `spec_path`, lists the
spec's invariants, the constants a consumer may rely on, and a `vectors` array.
Vector kinds of `memory_types.json`:

| kind | fields | meaning |
|---|---|---|
| `group` | `codec`, `trits`, `payload_hex` | encoding one or more groups must produce exactly these bytes and decode back |
| `container` | `codec`, `trits`, `tmem_hex` | the complete TMEM v1 container, header and CRC32 included |
| `invalid_word` | `codec`, `count`, `payload_hex` | a decoder must reject this payload (reserved code, invalid lane or nonzero padding) |
| `invalid_sparsity` | `codec`, `trits` | an encoder must reject the block instead of pruning weights |

`memory_bridge.json` (from `specs/memory/bridge.t27`) uses request/response kinds:

| kind | fields | meaning |
|---|---|---|
| `rpc` | `request` or `request_text`, optional `limits`, `expect` | one JSON-RPC body against a server configured with the given limits |
| `transport` | `headers` + `body`, or `raw` with `{port}`, optional `limits`, `expect` | HTTP framing rules; replayed over TCP only |
| `sequence` | `steps[]` with `request`, `expect`, optional `capture`; `$name` substitutes a captured result field | ordered calls on one server (upload, read, info, dot, delete) |

`expect` carries `http_status` (default 200), `error_code` or a `result` subset,
optional `id`, `result_keys` and `result_format` (`uuid4-hex32`).

`memory_tensorpack.json` (from `specs/memory/tensorpack.t27`) uses container kinds:

| kind | fields | meaning |
|---|---|---|
| `pack` | `tensors` (input records), `ttpk_hex`, `metadata_json`, `inspect` | the native encoder must produce exactly these bytes; both readers restore the tensors and report the inspect subset |
| `invalid_pack` | `ttpk_hex`, `reason`, `error_class` | both readers must refuse the container |

`memory_stream_compute.json` (from `specs/memory/stream_compute.t27`) uses simulation kinds:

| kind | fields | meaning |
|---|---|---|
| `dot_trace` | `dense`, `acc_width`, `cycles[]` with per-cycle `stimulus` and `expect` | cycle-exact replay of `trinity_dot_stream_t27` in Icarus (`tests/tb_spec_dot_trace.v`) |
| `storage_trace` | `dense`, `trit_count`, `cycles[]` | cycle-exact replay of the native storage stream with its view (`tests/tb_spec_storage_trace.v`) |
| `frame` | `codec`, `acc_width`, `weights`, `activations`, `expect` | a whole dot product through the native runner; result and error must match the checked-accumulation reference |

`memory_conformance.json` (from `specs/memory/conformance.t27`) is the lab manifest rather than a vector list:
the fixture the native conformance experiment consumes (`fixture_document`, `fixture_text`), the containers it
uploads and the six corruption blobs, the fixtures it must reject (`invalid_fixtures`, two of them as compact
recipes), the map from lab sections to the sibling files and their consumers (`sections`), and the catalogue of
corruption, interruption and reset cases with the vector ids that exercise each (`catalogue`). Its `vectors`
array lists the fixture vectors, corruption blobs and invalid fixtures for the validator.

`memory_edge_demo.json` (from `specs/memory/edge_demo.t27`) uses demo kinds:

| kind | fields | meaning |
|---|---|---|
| `model` | `codec`, `ttpk_hex`, `container_bytes`, `container_sha256`, `raw_weight_payload_bytes` | the classifier container the native demo must produce |
| `fixture` | `name`, `samples`, `expected`, `accumulators`, `scores`, `label`, `ambiguous` | one synthetic signal with its exact scores |
| `prediction` / `rtl_row` | per codec and fixture: label and accumulators / row seed, result, groups, encoded bits | what the native report and its RTL witnesses must contain |
| `scoring` | scales, samples, expected label or error | tie, per-row scale and finiteness rules through the Bridge |

`formats_<family>.json` (from `specs/formats/<family>.t27`: `llama_cpp`, `prismml`,
`bitnet_cpp`, `hf_bitnet`, `mlx`, `onnx`) state the external ternary weight-packing
formats. `constants.errors` maps each reject class to its status, `constants.flags`
each flag class to its slot, `constants.silent` lists the cases no reader can detect
(see `specs/formats/OWNERS.md`), and `upstream` names the pinned commits. Byte strings
are hex; `values_hex` holds one signed byte per weight. Every decode vector has
`expect.status` (the reader's return: the count of weights outside {-1, 0, +1}, or a
negative status). A vector with `error_class` has `"kind": "reject"` and names the
reader it exercises in `reader` (one of the kinds below, with that kind's fields); the
strict reader must return exactly the class's status from `constants.errors` (in
`expect.status`, `expect.check`, `expect.scale_status` or `expect.affine_status`) and
write nothing. A vector with `flag_class` decodes and reports that flag; one with
`silent_class` decodes to the wrong weights without any signal and carries the
`intended` input: for a layout confusion the intended values that produce the same
bytes, for a corruption the original `data_hex` and its values and scales (with the
`source` of real bytes). Every
reject, flag and silent vector carries `silent_output`, a list of upstream readers'
views of the same bytes: `upstream` (a key of `specs/formats/upstream.lock.json`),
`behaviour` (`decodes`, `rejects`, `not_applicable` or `unknown`), `cite` (pinned
`path:line` ranges, or `UNKNOWN` where the code was not found at the pin), `note`, and,
where an upstream decodes to values that can be stated exactly, `values_hex` with the
raw `scale_words` or `scale_word`; for an I2_S flag vector, `code3_value` says what that
reader makes of code 3 (the rest of `values_hex` equals the vector's values).

| kind | fields | meaning |
|---|---|---|
| `block` | `format`, `count`, `data_hex`, `encode`, `expect` {`status`, `values_hex`, `scale_words`, `flags`}, optional `intended`, `source` | block formats (TQ1_0, TQ2_0, Q2_0, Q1_0, PQ2_0, PTQ1_0); with `encode` the encoder must reproduce `data_hex` from the values and scale words |
| `block_encode` | `format`, `count`, `values_hex`, `scale_words`, `error_class`, `expect` | an encoder must refuse the codes and write nothing |
| `gguf` | `gguf_hex`, `tensor`, `file_size`, `expect` {`find`, `ggml_type`, `prism`, `bitnet`, `offset`, `alignment`, `data_start`, `has_next`, `next_offset`, `has_prev`, `prev_end`, `check`, optional `tensor_bytes`} | a GGUF header in a file of `file_size` bytes: lookup, namespace markers and the type-id, alignment, weight-count, row-length and extent rules. `next_offset` is the least offset at or above the tensor's among other records that hold weights; `prev_end` the greatest end of a record of known size (ternary layouts, F32, F16, BF16) that begins below it. The tensor must end by `next_offset` and begin at or after `prev_end` (records whose size the reader does not know bound nothing from below) |
| `safetensors` | `safetensors_hex`, `tensor`, `file_size`, `data_offsets` (every entry's pair), `expect` {`find`, `dtype`, `shape`, `begin`, `end`, `dtype_bits`, `has_next`, `next_begin`, `has_prev`, `prev_end`, `check`} | a safetensors header in a file of `file_size` bytes: lookup, dtype width and the extent rules (numel × width, the tensor must end by the next nonzero-size tensor's begin and begin at or after the end of every one that begins below it, end of file). When an entry's absolute offset does not fit in 64 bits, `find` is the `extent` status, `check` repeats it and the range fields are 0 |
| `i2s` / `i2s_encode` | `count`, `data_hex` (codes, f32 scale, 28-byte trailer) or `values_hex`, `scale_word` | bitnet.cpp I2_S in the x86 ACT_PARALLEL layout |
| `reject` | `reader` plus that reader's fields, `error_class`, `silent_output` | the strict reader refuses the input with the class's status |
| `hf_packed` / `hf_encode` | `rows`, `cols`, `data_hex` or `values_hex`, `scale_word`, `scale_kind` | transformers packed uint8 weights and `weight_scale`; `expect.scale_status` is the scale check |
| `mlx` / `mlx_encode` | `rows`, `cols`, `group`, `data_hex` or `values_hex`, `scale_words`, `bias_words`, `scale_kind` | MLX 2-bit words; `expect.affine_status` is the scale and bias check |
| `onnx` / `onnx_encode` | `n`, `k`, `block_size`, `data_hex` or `values_hex`, `zero_points_hex`, `scale_words`, `scale_kind` | ONNX Runtime MatMulNBits `bits=2`; `expect.scale_status` is the scale check |

Vectors with `source` hold bytes cut from the pinned real checkpoints (repository,
revision, file, byte range and SHA-256 of the cut); the rest are synthetic.

`memory_types.json` is generated from `specs/memory/types.t27` by
`tools/generate-spec-vectors.py` using plain arithmetic and `zlib.crc32`; it does
not call the native implementation. Consumers that must reproduce every vector:

- native C harnesses `tests/native_spec_*.c` (run by `tools/check-specs.sh`); `tests/native_spec_formats.c`
  replays every `formats_*.json` vector through both the spec and the implementation, `tests/spec_formats_wasm_replay.mjs`
  through `build/t27/formats.wasm`, and `tests/test_spec_formats.py` through `trinity_memory.formats`;
- Python adapters, `tests/test_spec_types.py`, `tests/test_spec_tensorpack.py` (also through the native CLI when built), `tests/test_spec_stream_compute.py` (native runner and Icarus traces), `tests/test_spec_conformance.py` (native experiment and the lab report), `tests/test_spec_edge_demo.py` (native demo, Bridge and CLI); TCP replay `tests/test_spec_bridge.py`; in-process replay `tests/native/test_spec_bridge_vectors.py`; Icarus trace replay `tests/spec_stream_replay.py`; WASM replay `tests/spec_wasm_replay.mjs`; the lab report `tools/conformance-lab.py`;
- the same test also fails when the committed file is stale (`--check`).

Validate the files and refresh them:

```sh
"$T27_ROOT/target/release/t27c" validate-conformance --repo-root .
python3 tools/generate-spec-vectors.py --check      # or without --check to regenerate
python3 -m unittest tests.test_spec_types -v
```

These vectors state byte layouts and rejection rules. They do not measure FPGA
resources, timing or model quality.
