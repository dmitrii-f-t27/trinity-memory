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

`memory_types.json` is generated from `specs/memory/types.t27` by
`tools/generate-spec-vectors.py` using plain arithmetic and `zlib.crc32`; it does
not call the native implementation. Consumers that must reproduce every vector:

- native C harness `tests/native_spec_types.c` (run by `tools/check-specs.sh`);
- Python adapters, `tests/test_spec_types.py`;
- the same test also fails when the committed file is stale (`--check`).

Validate the files and refresh them:

```sh
"$T27_ROOT/target/release/t27c" validate-conformance --repo-root .
python3 tools/generate-spec-vectors.py --check      # or without --check to regenerate
python3 -m unittest tests.test_spec_types -v
```

These vectors state byte layouts and rejection rules. They do not measure FPGA
resources, timing or model quality.
