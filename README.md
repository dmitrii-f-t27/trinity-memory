# Trinity Memory

[![Executable t27 stack](https://github.com/dmitrii-f-t27/trinity-memory/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/dmitrii-f-t27/trinity-memory/actions/workflows/ci.yml)
[![Ternary Check weekly](https://github.com/dmitrii-f-t27/trinity-memory/actions/workflows/ternary-check-weekly.yml/badge.svg?branch=master)](https://github.com/dmitrii-f-t27/trinity-memory/actions/workflows/ternary-check-weekly.yml)

**Ternary memory stack: Bridge, TensorPack, Stream Compute, Conformance Lab and Edge Demo.**

**Version 0.4 adds [t27 Ternary Check](#t27-ternary-check-v04)**, an exact
compatibility check of the public ternary weight formats ([changelog](CHANGELOG.md)).
**Since version 0.3 the stack is implemented in executable t27:** codecs, containers,
JSON/HTTP Bridge, compute, experiments, reports and CLI live in [`t27/`](t27/).
The compiler generates native C, Verilog and the browser's WebAssembly codec.
Python is a compatibility adapter; the v0.2 implementation is a frozen test oracle.
See [build instructions and compatibility limits](docs/T27-MIGRATION.md) and
[issue #1](https://github.com/dmitrii-f-t27/trinity-memory/issues/1).

[Native Edge report](reports/t27/edge.html) · [Native benchmark](reports/t27/index.html) · [Migration validation](reports/t27/validation.md)

[Russian overview](README.ru.md) · [Direction and roadmap](docs/DIRECTION.md) ·
[Format specification](docs/format.md) · [Hardware protocol](docs/hardware.md) ·
[Research audit](docs/research.md) · [Five-direction walkthrough](docs/STACK.md)

Trinity Memory is a dedicated experimental memory direction for the Trinity
ecosystem. This repository contains the native implementation and evidence
for storing and retrieving balanced ternary values (`-1`, `0`, `+1`) using
binary memory. It has its own code, tests, reports, and hardware roadmap.

Version 0.4 is a **working software and RTL-simulation demonstrator**. A real
loopback HTTP client/server connects tensor files to emulated memory and exact
integer computations; an optional Icarus replay verifies the retrieved weights
in RTL. On 2026-09-11 an ALINX AX7203 (XC7A200T) ran the player written in t27: all 34
stream-compute trace vectors including the joined read -> decode -> dot path matched
the reference, a streaming workload delivered 0.94 beats per tick (23.5 M beats/s at
25 M ticks/s) with every result recomputed by the host, and the Edge Demo classifier
labelled all six fixtures on the device in 7 ticks each
([reports/fpga](reports/fpga/README.md)). On 2026-09-22 the block-RAM packing bench,
also t27, stored one 1 013 760-trit tensor on the device in 55, 50 and 45 RAMB36E1
(two bits per trit, dense5 bytes, dense5 plus a dense2 nibble in the parity bits:
2.000, 1.800 and 1.636 bits per trit) and read every word back without error, 18, 20
and 22 trits per read ([docs/hardware.md](docs/hardware.md)). DDR and power remain
unmeasured.

## t27 Ternary Check (v0.4)

Ternary Check answers one question for the public ternary weight-packing
formats: **do two files, or a file and a decoder, hold the same trits and the
same scale values, exactly?** Between files, scales are compared as values (an
f16 and a bf16 word of the same number are equal); against a decoder, the
Action compares the scale words as stored, bit for bit. Every verdict of the
matrix and of the Action is made by executable t27
([`t27/formats.t27`](t27/formats.t27), [`t27/matrix.t27`](t27/matrix.t27),
[`t27/ternary_contract.t27`](t27/ternary_contract.t27)); Python only moves bytes.
The scale and trailer checks over all 210 BitNet ternary tensors (findings 1
and 3) come from [`tools/bitnet_audit.py`](tools/bitnet_audit.py), a
standard-library Python cross-check whose committed report the CI job
`fixtures` replays.
It covers llama.cpp TQ1_0, TQ2_0, Q2_0 and Q1_0, the PrismML fork's PQ2_0 and
PTQ1_0, bitnet.cpp I2_S, transformers BitNet packed `uint8`, MLX 2-bit and
ONNX Runtime `MatMulNBits` with `bits=2`, against byte-level contracts in
[`specs/formats/`](specs/formats/) pinned to upstream commits.

**Real checkpoints.** Layer 0 of BitNet b1.58 2B4T (`q_proj`, `down_proj`) and of
Ternary Bonsai 2 27B (`ffn_down`), read by HTTP range requests from pinned
Hugging Face revisions, in every format
([`reports/ternary-check.json`](reports/ternary-check.json),
[`reports/ternary-check.html`](reports/ternary-check.html): download the HTML to
view it). Each tensor is compared with a reference, one of its published forms:

<!-- ternary-check-table: generated from reports/ternary-check.json, checked by tests/test_release.py -->
| Format | BitNet `q_proj` | BitNet `down_proj` | Bonsai `ffn_down` |
|---|---|---|---|
| HF packed uint8 + bf16 weight_scale | reference | reference | not representable |
| I2_S (bitnet.cpp) | mismatch (published) | mismatch (published) | not representable |
| PTQ1_0 (PrismML) | match (t27 round trip) | match (t27 round trip) | reference |
| PQ2_0 (PrismML) | match (t27 round trip) | match (t27 round trip) | match (published) |
| Q2_0, group 64 (llama.cpp) | match (t27 round trip) | match (t27 round trip) | match (published) |
| MLX 2-bit affine, group 128 | match (t27 round trip) | match (t27 round trip) | match (published) |
| TQ1_0 (llama.cpp) | match (t27 round trip) | match (t27 round trip) | not representable |
| TQ2_0 (llama.cpp) | match (t27 round trip) | match (t27 round trip) | not representable |
| Q1_0, binary (llama.cpp) | not representable | not representable | not representable |
| ONNX MatMulNBits bits=2, block 128 | match (t27 round trip) | match (t27 round trip) | match (t27 round trip) |
| TQ1_0 by llama.cpp quantize_row_tq1_0_ref | mismatch (llama.cpp writer) | match (llama.cpp writer) | not representable |
| TQ2_0 by llama.cpp quantize_row_tq2_0_ref | mismatch (llama.cpp writer) | match (llama.cpp writer) | not representable |
<!-- /ternary-check-table -->

36 cells: 23 match, 4 mismatch, 9 not representable. The 12 cells marked
reference, published or llama.cpp writer are third-party evidence (bytes of the
pinned checkpoints, or bytes written by llama.cpp's pinned reference
quantizers); the 15 t27 round trips show only that a format can hold the
tensor. "Not representable" names a reason, such as one scale per tensor or per
256 weights for Bonsai's 128-weight groups, or no code for 0 in binary Q1_0.
The I2_S mismatch is the scale precision of finding 1; the llama.cpp writers
store the scale 0 for 50 all-zero blocks of `q_proj`, where every weight still
dequantizes to the same value. Every mismatch has a reproduction in
[`reports/ternary-check/repro/`](reports/ternary-check/repro/).

**Four findings** ([docs/ternary-check.md](docs/ternary-check.md)):

1. BitNet stores one scale at two precisions: bf16 in the packed checkpoint,
   f32 in the GGUF I2_S file, the bf16 value being the f32 value rounded, in
   all 210 ternary tensors; the trits of the compared tensors are identical
   ([details](docs/ternary-check.md#results)).
2. The packed BitNet trits cannot be recomputed from the published bf16
   weights: `transformers` `WeightQuant` on them differs from the packed trits
   for 79,719 weights of layer-0 `q_proj` (1.22%) and 101,673 of `down_proj`
   (0.57%), all at the bf16 value `0.5 × weight_scale`, where the packed file
   stores both ±1 and 0 ([details](docs/ternary-check.md#results)).
3. I2_S trailers carry leftover bytes: after the f32 scale, 28 bytes that are
   not all zero in all 210 I2_S tensors, equal to the bytes an earlier tensor
   holds at the same offset (a reused buffer); the dequantizer does not read them, so inference
   is unaffected ([details](docs/ternary-check.md#results)).
4. Ternary Bonsai 2 is consistent across its four distributions: PTQ1_0, PQ2_0,
   Q2_0 (group 64) and MLX 2-bit hold the same trits and fp16 scales for layer
   0 `ffn_down`; the GGUF files declare `prism.*` metadata, a Hadamard rotation
   the runtime must apply
   ([details](docs/ternary-check.md#results)).

Storage and integer arithmetic only: three tensors compared trit for trit (the
BitNet scale and trailer checks cover all 210 ternary tensors), no model was
run.

**One command** from a clean clone reproduces the reports byte for byte (it
fetches the pinned fixture ranges, 205.8 MiB, and builds the pinned t27
compiler; `OFFLINE=1` uses the caches only):

```sh
make ternary-check          # writes reports/ternary-check.json, .html and repro/
make ternary-check-verify   # recompute and compare with the committed reports
```

**Check your own decoder** in GitHub Actions. It must implement the small
[CLI contract](ternary-check/CONTRACT.md) (see also the
[Action documentation](ternary-check/README.md)). The Action runs the 126
payload vectors of `conformance/formats_*.json`, 178 decoder calls, and compares
values, scale words, flags and refusals with their error classes; by default
any mismatch, wrong refusal or silent acceptance fails the step:

```yaml
- uses: dmitrii-f-t27/trinity-memory/ternary-check@v0.4.0
  with:
    decoder: ./build/my-decoder   # the contract arguments are appended
    formats: TQ1_0,TQ2_0          # optional: only the formats you implement
```

By default (`runtime: release`) the Action runs from the release wheel, so the
runner must be Linux x86_64, or macOS arm64 with macOS 14 or later; elsewhere,
build from source and set `runtime` to the checkout
([runtime](ternary-check/README.md#runtime)).

The weekly workflow (badge above) checks that the pinned upstream files and
model files have not changed and reruns every vector.

## Five directions, one reproducible chain

| Direction | Available now | Documentation |
|---|---|---|
| Bridge | HTTP JSON-RPC memory emulator, client, SDK adapter, bounded upload/read/dot | [Protocol](docs/bridge.md) |
| TensorPack | Versioned names, shapes, axes and scales around the existing TMEM codecs | [Format](docs/tensorpack.md) |
| Stream Compute | Dense5/2-bit ternary-int8 dot, ready/valid, frame errors and checked accumulation | [RTL](docs/stream-compute.md) |
| Conformance Lab | Golden byte/sum vectors, HTTP round trips, corruption and RTL replay | [Walkthrough](docs/STACK.md) |
| Edge Demo | Hand-authored signal classifier with exact reference comparisons and HTML/JSON report | [Report data](reports/stack.json) |

```sh
git clone https://github.com/dmitrii-f-t27/trinity-memory.git
cd trinity-memory
git clone https://github.com/gHashTag/t27.git build/compiler
git -C build/compiler checkout "$(cat native/compiler.lock)"
export T27_ROOT="$PWD/build/compiler"
make t27       # see prerequisite toolchains in docs/T27-MIGRATION.md
make t27-test  # native sanitizers, frozen-oracle parity, HTTP, RTL, WASM
make check     # public adapters + hardware regression checks
make stack     # network and RTL conformance
make demo      # build/edge-report.html and JSON
```

Open [the historical v0.2 release report](reports/stack.html) locally after downloading it;
GitHub shows HTML source. For software only, run
`python3 -m trinity_memory edge-demo` without `--rtl`.
See [validation](reports/stack-validation.md) for evidence and limitations.

## Specifications (contract layer)

[`specs/memory/`](specs/memory/) holds sealed `.t27` specifications that state the
contracts the executable modules implement. [`types.t27`](specs/memory/types.t27)
defines trit lane codes, codec identifiers and group geometry, valid-code limits,
TMEM v1 framing, CRC32 parameters, native status codes and evidence labels
(`emulator`, `software`, `rtl-simulation`, `fpga`);
[`bridge.t27`](specs/memory/bridge.t27) states the Bridge protocol: envelope,
error codes and HTTP statuses, limits, handles, read and dot rules, identity
(see [docs/bridge.md](docs/bridge.md)); [`tensorpack.t27`](specs/memory/tensorpack.t27)
states the TTPK v1 container: header, limits, metadata schema, descriptor and
chain rules, float presentation (see [docs/tensorpack.md](docs/tensorpack.md));
[`stream_compute.t27`](specs/memory/stream_compute.t27) states the framed dot
pipeline, the ready-capable storage sequencer and the view, and the joined
read -> decode -> dot path (`rtl/t27/stream_dot.v`), with cycle-exact traces
replayed in Icarus (see [docs/stream-compute.md](docs/stream-compute.md));
[`conformance.t27`](specs/memory/conformance.t27) states the conformance fixture
schema, the native experiment plan and the lab report that
[`tools/conformance-lab.py`](tools/conformance-lab.py) reproduces over every
consumer (see [docs/STACK.md](docs/STACK.md)); [`edge_demo.t27`](specs/memory/edge_demo.t27)
states the template classifier, its fixtures, scoring rules and report fields.
Invariants are constant
expressions compiled as `_Static_assert`; `test` blocks execute in the generated C
test runner. Each spec has a seal in [`.trinity/seals/`](.trinity/seals/) and
language-independent vectors in [`conformance/`](conformance/), generated from
the spec by [`tools/generate-spec-vectors.py`](tools/generate-spec-vectors.py)
without calling the native code.

[`specs/formats/`](specs/formats/) holds the byte-level contracts of the external
ternary weight-packing formats that t27 Ternary Check reads
([epic #27](https://github.com/dmitrii-f-t27/trinity-memory/issues/27)):
[`llama_cpp.t27`](specs/formats/llama_cpp.t27) (TQ1_0, TQ2_0, Q2_0, Q1_0 and the GGUF
type-id, alignment and extent rules), [`prismml.t27`](specs/formats/prismml.t27)
(PQ2_0, PTQ1_0), [`bitnet_cpp.t27`](specs/formats/bitnet_cpp.t27) (I2_S),
[`hf_bitnet.t27`](specs/formats/hf_bitnet.t27) (transformers packed weights and
`weight_scale`), [`mlx.t27`](specs/formats/mlx.t27) (2-bit affine) and
[`onnx.t27`](specs/formats/onnx.t27) (MatMulNBits `bits=2`). Each restates pinned
upstream commits ([`upstream.lock.json`](specs/formats/upstream.lock.json)) with block
geometry, code tables, bits per weight, rejection rules and flags;
[`specs/formats/OWNERS.md`](specs/formats/OWNERS.md) lists the status classes. Their
vectors `conformance/formats_*.json` include rejected, flagged and silent cases and
bytes cut from BitNet b1.58 2B4T and Ternary Bonsai 2;
[`tests/native_spec_formats.c`](tests/native_spec_formats.c) replays them through the
specs and `t27/formats.t27`, and
[`tests/spec_formats_wasm_replay.mjs`](tests/spec_formats_wasm_replay.mjs) through
`build/t27/formats.wasm`.

[`tools/check-specs.sh`](tools/check-specs.sh) is the gate (also run by
`make t27-test` and the `spec` CI job): lexer and parser completeness,
typecheck, C and Verilog generation, executed tests, seal verification,
conformance validation, and the differential harness
[`tests/native_spec_types.c`](tests/native_spec_types.c) that ties the spec
constants to `t27/codecs.t27` and `t27/container.t27`. For `specs/formats/` the gate
also fails when a spec has no vectors file or no harness that replays it.
[`tests/test_spec_types.py`](tests/test_spec_types.py) replays the vectors through
the Python adapters. Conventions and the pinned-compiler pitfalls are recorded in
[`specs/memory/OWNERS.md`](specs/memory/OWNERS.md); the roadmap for the remaining
directions is [epic #3](https://github.com/dmitrii-f-t27/trinity-memory/issues/3).

```sh
export T27_ROOT="$PWD/build/compiler"   # pinned checkout, built by make t27
make check-specs
```

## Current implementation

- Six lossless native codecs: `baseline2`, `dense5`, `dense17`, `dense22`,
  `sparse41`, and `sparse82`.
- TMEM v1 files with explicit lengths, canonical padding, and CRC32 integrity.
- A CLI for memory/tensor packing, inspection, transfer, computation and reports.
- Matching Verilog dense5 and sparse41 decoders, plus synchronous packed/baseline
  memory streams with five decoded lanes per active cycle.
- Exhaustive small-codebook tests, stream-protocol simulation, synthetic
  measurements, and a self-contained interactive benchmark report.

The sparse codecs require a per-block constraint and reject incompatible data.
They do not silently prune weights. This repository does not contain a trained
model, a DDR controller, or a physical multi-level memory-cell implementation.

## Reproduce

Build the native library first (instructions above). Python 3.10+ adapters have
no third-party Python runtime dependencies. Platform libraries are described in
[the migration guide](docs/T27-MIGRATION.md).

```sh
python3 -m unittest discover -s tests -v
python3 -m trinity_memory benchmark --count 65536 --repeats 3
python3 scripts/render_report.py
```

Open the historical [`reports/index.html`](reports/index.html) locally to use the report and
five-trit encoder. It has no server or CDN dependency. The checked-in report
has Russian labels; the raw results are in [`benchmark.json`](reports/benchmark.json).
GitHub displays HTML source, so download/open the file to use its controls.

RTL tests require [Icarus Verilog](https://github.com/steveicarus/iverilog):

```sh
# macOS
brew install icarus-verilog
# Ubuntu/Debian: sudo apt-get install iverilog
python3 scripts/test_rtl.py
```

`make check` runs Python and RTL checks. `make report` records new native
codec measurements and renders the HTML report. Optional CLI installation:
`python3 -m pip install .`. Built wheels include the RTL resources; Icarus must
still be installed separately to use `--rtl`.

```sh
mkdir -p build
python3 -m trinity_memory pack examples/trits.json build/example.tmem --codec dense5
python3 -m trinity_memory inspect build/example.tmem
python3 -m trinity_memory unpack build/example.tmem build/restored.json
python3 -m trinity_memory export-rtl examples/trits.json build/dense.mem --codec dense5
```

## Measured storage

Each synthetic dataset has **65,536 weights**, seed 27. These are actual payload
bytes, including final padding. TMEM adds **24 header bytes**. TensorPack v1
adds names, shapes, axes, scales and its own framing; the table below describes
the original TMEM payloads and excludes TensorPack overhead.

| Codec | Payload bytes | Payload bits/weight | Required structure |
|---|---:|---:|---|
| baseline2 | 16,384 | 2.000000 | Any trits |
| dense5 (5/8) | 13,108 | 1.600098 | Any trits |
| dense17 (17/27) | 13,014 | 1.588623 | Any trits |
| dense22 (22/35) | 13,034 | 1.591064 | Any trits |
| sparse41 (4, <=1) | 8,192 | 1.000000 | At most one nonzero in each block of four |
| sparse82 (8, <=2) | 8,192 | 1.000000 | At most two nonzeros in each block of eight |

All **15 dataset/codec combinations** recover their input exactly. The recorded
v0.1 local validation also passed **22 Python tests** and **1,296 binary decoder
patterns**, plus unknown-input and stream tests. Sources:
[`benchmark.json`](reports/benchmark.json), [`validation.md`](reports/validation.md).

Asymptotically, `1 - 1.6/2 = 20%` less payload implies at most `2/1.6 = 1.25x`
transfer throughput if bandwidth alone limits performance. This is a model,
not measured FPGA or inference speed. Python timings are median wall times
from three repetitions after warm-up, not optimized CPU-kernel performance.

For the three 36-bit word layouts, block-RAM allocation, LUT and flip-flop use
and timing estimates are reported by the open flow (yosys, nextpnr-xilinx) for the
AX7203's part, and all three layouts ran on the board without error
([docs/hardware.md](docs/hardware.md), "Block-RAM trit packing"). DDR throughput,
power, and model quality are **not yet measured**. Continuous 27/35-bit software
packing does not establish the physical cost of a memory device.

## Place in Trinity

- [`gHashTag/t27`](https://github.com/gHashTag/t27): compiler and specification stack.
- [`gHashTag/trinity-fpga`](https://github.com/gHashTag/trinity-fpga): FPGA infrastructure.
- [`gHashTag/trinity`](https://github.com/gHashTag/trinity): broader compute/runtime stack.
- **This repository:** memory representation, decoding, storage interfaces,
  and reproducible memory experiments.

This is a standalone implementation, not a fork of those repositories or an
already-merged subsystem. The adapter is tested with the existing Trinity SDK
at an explicit source revision; upstream SDK/node remain unchanged. Our
TensorPack and memory protocol are experimental local contracts, not an
upstream-approved ABI. Base-3 packing and structured
ternary coding have prior art; the [research audit](docs/research.md) identifies
the sources and corrects unsupported claims from the initial research note.

Trinity is the shared project of Dmitrii Fedorov and Dmitrii Vasilev. Hosting
this memory implementation under `dmitrii-f-t27` gives it a direct development
home without representing the wider Trinity stack as a solo project.

## License

Apache License 2.0: see [LICENSE](LICENSE) and [NOTICE](NOTICE).
