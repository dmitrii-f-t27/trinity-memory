# Changelog

All notable changes to this project are recorded here. The format is loosely
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) (dated
versions, compare links; the sections are grouped by topic), and versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) (pre-1.0: a
minor version may change interfaces). Most entries name the pull requests
that made them; 0.2.0 was made by direct commits, and the README and Release
entries of 0.4.0 come from the release pull request itself.

## [0.4.0] - 2026-09-23

Stage 1 of the roadmap ([epic #27](https://github.com/dmitrii-f-t27/trinity-memory/issues/27)):
**t27 Ternary Check**, a compatibility check of the public ternary
weight-packing formats written in executable t27, with a JSON report, an HTML
table, a GitHub Action for third-party decoders and a weekly upstream
re-check. Released under [issue #36](https://github.com/dmitrii-f-t27/trinity-memory/issues/36).
This version also collects the `specs/memory` contract layer and the AX7203
device work merged after v0.3.0.

### Ternary Check

- `t27/formats.t27`: decoders and encoders for llama.cpp TQ1_0, TQ2_0, Q2_0 and
  Q1_0, the PrismML fork's PQ2_0 and PTQ1_0, bitnet.cpp I2_S, transformers
  BitNet packed weights and linear 2-bit rows (MLX 2-bit, ONNX Runtime
  `MatMulNBits` with `bits=2`); GGUF and safetensors tensor lookup; trit and
  scale comparison. First results on BitNet b1.58 2B4T and Ternary Bonsai 2 27B
  in [docs/ternary-check.md](docs/ternary-check.md)
  ([#37](https://github.com/dmitrii-f-t27/trinity-memory/pull/37), closes #30).
- Compatibility matrix decided by `t27/matrix.t27`: 3 real tensors in 12
  columns, 36 cells: 23 match, 4 mismatch, 9 not representable, each mismatch
  with a reproduction in `reports/ternary-check/repro/`. Real layer: int8
  matvec with 64-bit accumulators (`t27/matvec.t27`) on BitNet and Bonsai
  layer 0. Reports [`reports/ternary-check.json`](reports/ternary-check.json)
  (schema `trinity.ternary-check.v1`) and
  [`reports/ternary-check.html`](reports/ternary-check.html); one command,
  `make ternary-check` (the CI job `ternary-check` runs it on a clean checkout
  and fails unless the committed reports come out unchanged), and
  `make ternary-check-verify`, which recomputes the reports and compares them
  with the committed ones
  ([#45](https://github.com/dmitrii-f-t27/trinity-memory/pull/45), closes #32 and #33).
- CLI contract v1 ([`ternary-check/CONTRACT.md`](ternary-check/CONTRACT.md)),
  verdicts in `t27/ternary_contract.t27`, the runner
  `trinity-memory ternary-check run`, the reference decoder
  `trinity-memory ternary-check decode|encode|formats`, and the composite
  Action [`ternary-check/action.yml`](ternary-check/action.yml)
  (`uses: dmitrii-f-t27/trinity-memory/ternary-check@v0.4.0`), tested in CI
  with the reference decoder (must pass) and a deliberately wrong decoder
  (must fail). Weekly workflow `ternary-check-weekly.yml`:
  `tools/upstream-drift.py` compares the pinned upstream files and model files
  with their upstream heads, and the workflow rebuilds the t27 decoders and
  reruns every vector through the Action with the reference decoder
  ([#46](https://github.com/dmitrii-f-t27/trinity-memory/pull/46)).

### Format specifications and vectors

- [`specs/formats/`](specs/formats/): six sealed byte-level contracts
  (`llama_cpp`, `prismml`, `bitnet_cpp`, `hf_bitnet`, `mlx`, `onnx`), each
  restating pinned upstream commits
  ([`upstream.lock.json`](specs/formats/upstream.lock.json)); reject and flag
  classes in [`specs/formats/OWNERS.md`](specs/formats/OWNERS.md).
- `conformance/formats_*.json`: 171 vectors in six files, including negative
  vectors with their error classes and what each upstream reader does with the
  same bytes. They are replayed through the specs, `t27/formats.t27` in C and
  `build/t27/formats.wasm`, which the wheel now ships. The spec gate requires a
  vectors file and a harness for every format spec
  ([#41](https://github.com/dmitrii-f-t27/trinity-memory/pull/41), closes #29 and #34).

### Fixtures

- [`fixtures/manifest.json`](fixtures/manifest.json): the six pinned Hugging
  Face revisions and every byte range read, 544 ranges (205.8 MiB) with
  sha256; `tools/fetch-fixtures.py` fetches them anonymously into a strict
  cache and verifies it offline; CI restores the cache and replays the BitNet
  audit ([#42](https://github.com/dmitrii-f-t27/trinity-memory/pull/42), closes #31).

### Upstream note

- llama.cpp issue 15193: a harness compares the pinned upstream TQ1_0/TQ2_0
  quantizers, dequantizers and generic, NEON and AVX2 `vec_dot` kernels with the
  t27 decoders on random blocks and real BitNet tensors (`make upstream-15193`,
  CI job `upstream-15193`); no storage or CPU-kernel defect was found. Note:
  [docs/upstream/llama.cpp-15193.md](docs/upstream/llama.cpp-15193.md). Nothing
  was reported upstream
  ([#43](https://github.com/dmitrii-f-t27/trinity-memory/pull/43), closes #35).

### FPGA (ALINX AX7203, XC7A200T)

- Trace player on the device: the stream-compute trace vectors replayed on the
  board, with captures in `reports/fpga/`
  ([#20](https://github.com/dmitrii-f-t27/trinity-memory/pull/20),
  [#22](https://github.com/dmitrii-f-t27/trinity-memory/pull/22)).
- The player written in t27: joined read -> decode -> dot traces, a throughput
  workload and the Edge Demo on the device
  ([#25](https://github.com/dmitrii-f-t27/trinity-memory/pull/25), closes #24;
  captures in [#26](https://github.com/dmitrii-f-t27/trinity-memory/pull/26)).
- Trits in block RAM, written in t27: one 1,013,760-trit tensor in 55, 50 and 45
  RAMB36E1 (2.000, 1.800 and 1.636 bits per trit), every word read back without
  error; a start of the trace player is now accepted only while idle
  ([#38](https://github.com/dmitrii-f-t27/trinity-memory/pull/38)).
  Details in [docs/hardware.md](docs/hardware.md).

### Specifications (`specs/memory`)

- Sealed `.t27` contracts with conformance vectors, harnesses and the spec gate
  `tools/check-specs.sh`: types
  ([#12](https://github.com/dmitrii-f-t27/trinity-memory/pull/12), closes #4),
  Bridge ([#13](https://github.com/dmitrii-f-t27/trinity-memory/pull/13), closes #5),
  TensorPack ([#14](https://github.com/dmitrii-f-t27/trinity-memory/pull/14), closes #6),
  Stream Compute with cycle traces in Icarus
  ([#16](https://github.com/dmitrii-f-t27/trinity-memory/pull/16), closes #7),
  Conformance Lab ([#17](https://github.com/dmitrii-f-t27/trinity-memory/pull/17), closes #8)
  and Edge Demo ([#19](https://github.com/dmitrii-f-t27/trinity-memory/pull/19), closes #9).
- The storage sequencer joined to the dot pipeline: a ready-capable sequencer
  and the joined read -> decode -> dot path
  ([#21](https://github.com/dmitrii-f-t27/trinity-memory/pull/21), closes #15).

### Fixed

- CLI: `edge-demo` and `conformance` accept `--seed`
  ([#23](https://github.com/dmitrii-f-t27/trinity-memory/pull/23), closes #18).

### License

- Apache License 2.0: [LICENSE](LICENSE) and [NOTICE](NOTICE), stated in both
  READMEs and in the package metadata
  ([#39](https://github.com/dmitrii-f-t27/trinity-memory/pull/39), closes #28).

### Docs

- [docs/ternary-check.md](docs/ternary-check.md): finding 2 corrected (the
  packed BitNet trits cannot be recomputed from the published bf16 weights)
  and finding 1 made precise (the bf16 scale is the f32 GGUF scale rounded to
  bf16 in all 210 ternary tensors)
  ([#40](https://github.com/dmitrii-f-t27/trinity-memory/pull/40)).
- README (EN/RU): a Ternary Check section with the result table, the command,
  the Action and a badge for the weekly workflow; this changelog.

### Release

- Version 0.4.0. The wheel manifest takes its version from `pyproject.toml`.
  The source distribution now also carries this changelog, the license files,
  the JSON schemas, the Ternary Check reports, the Action, and the files the
  test suite reads (the workflows under `.github/workflows/` and
  `reports/t27/edge.json`).
- `tools/build-release-assets.py` builds and checks the release assets
  (macOS arm64 wheel, source distribution, reports, `validation.json`,
  `SHA256SUMS`) from a clean tree at a given commit and the Linux wheel built
  by CI. The macOS wheel is built for macOS 14.0 (tag `macosx_14_0_arm64`):
  the native code needs macOS 13.3, which a wheel tag cannot state, and the
  tool refuses a tag below any binary's minimum. The CI evidence must be a
  push run of `ci.yml` on the commit with every job successful; with the
  run's artifact list and artifact zip, the Linux wheel is tied to that run
  by the artifact's digest. The unittest gate runs last, with
  `TRINITY_REQUIRE_CACHED=1`, and the gate logs are kept.
- The Action's `runtime: release` takes its version only from its own ref (a
  `VERSION` variable in the calling workflow no longer redirects it) and on
  macOS stops with a clear error when the runner is older than the wheel's
  tag.
- Workflow `ternary-check-release-smoke.yml`: after a release is published,
  the Action with `runtime: release` on ubuntu-latest, macos-14 and macos-15.

## [0.3.0] - 2026-09-10

Executable t27 stack
([PR #2](https://github.com/dmitrii-f-t27/trinity-memory/pull/2), issue #1).

### Changed

- Trinity Memory's five directions (Bridge, TensorPack, Stream Compute,
  Conformance Lab and Edge Demo) run from executable `.t27`. The same sources
  generate native C, RTL and the interactive report's WebAssembly codec. All 14
  CLI commands are native; Python exposes FFI adapters without a legacy
  algorithm fallback.
- TMEM/TTPK formats remain compatible. Mixed integer/float top-k preserves exact
  large-integer ordering. Storage capacity is specialized in `.t27` before
  generation. See the [build and compatibility guide](docs/T27-MIGRATION.md).

### Verification

- 68 public tests, 39 native differential groups, 28 frozen-reference tests,
  ASan/UBSan, actual HTTP, Icarus, embedded WASM and isolated wheel installation;
  all five Linux CI jobs passed, including Python 3.10/3.12/3.14 and the pinned
  TrinityChip SDK ([validation report](reports/t27/validation.md)).
- Assets: source distribution, macOS 26 arm64 wheel, Linux x86_64 wheel (no
  manylinux claim), native reports ZIP, `validation.json` and `SHA256SUMS`.

### Limits

- Icarus is required for explicit `--rtl` runs. Physical FPGA, DDR/HBM, P&R,
  utilization, power and model-quality measurements were not performed in this
  release.

## [0.2.0] - 2026-09-09

Five connected Trinity memory directions in one reproducible software/RTL
demonstrator.

### Added

- Bridge: bounded upload/read/info/delete/dot JSON-RPC and an adapter tested
  against the existing Trinity SDK at a pinned revision.
- TensorPack: names, C-order dimensions, axes and scalar/per-axis scales around
  unchanged TMEM v1 codecs.
- Stream Compute: dense5 and 2-bit modes, signed int8 activations, ready/valid,
  masks, reset, malformed-frame and overflow handling.
- Conformance Lab: golden bytes/sums, actual HTTP round trips, corruption
  rejection and RTL comparison.
- Edge Demo: source, raw JSON and a self-contained HTML report
  ([walkthrough](docs/STACK.md), [evidence](reports/stack-validation.md)).

### Verification

- 60 local Python tests; 126 checked stream outputs including 20 expected errors;
  46 network/format checks and 22 corresponding RTL dots; 12 demo predictions and
  36 RTL row results; the installed wheel was tested outside the checkout. GitHub
  CI passed for the released commit: Python 3.10/3.12/3.14, RTL, and upstream
  SDK integration.

### Limits

- No physical FPGA/DDR/power/BRAM measurements, trained-model quality result, ZK
  proof or production device driver. HTTP computes in Python; RTL is a separate
  replay. The memory interfaces are experimental, not an upstream-approved ABI.

[0.4.0]: https://github.com/dmitrii-f-t27/trinity-memory/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/dmitrii-f-t27/trinity-memory/releases/tag/v0.3.0
[0.2.0]: https://github.com/dmitrii-f-t27/trinity-memory/releases/tag/v0.2.0
