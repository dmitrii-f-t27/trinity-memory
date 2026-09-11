# Conformance vectors (`conformance/*.json`)

Language-independent expectations for the contracts stated under `specs/memory/`.
Each file names its `module` and `spec_path`, lists the spec's invariants, the
constants a consumer may rely on, and a `vectors` array. Vector kinds:

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

`memory_types.json` is generated from `specs/memory/types.t27` by
`tools/generate-spec-vectors.py` using plain arithmetic and `zlib.crc32`; it does
not call the native implementation. Consumers that must reproduce every vector:

- native C harnesses `tests/native_spec_*.c` (run by `tools/check-specs.sh`);
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
