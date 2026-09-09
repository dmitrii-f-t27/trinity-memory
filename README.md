# Trinity Memory

[![Reference codecs and RTL](https://github.com/dmitrii-f-t27/trinity-memory/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/dmitrii-f-t27/trinity-memory/actions/workflows/ci.yml)

**Ternary memory stack: Bridge, TensorPack, Stream Compute, Conformance Lab and Edge Demo.**

[Russian overview](README.ru.md) · [Direction and roadmap](docs/DIRECTION.md) ·
[Format specification](docs/format.md) · [Hardware protocol](docs/hardware.md) ·
[Research audit](docs/research.md) · [Five-direction walkthrough](docs/STACK.md)

Trinity Memory is a dedicated experimental memory direction for the Trinity
ecosystem. This repository contains the reference implementation and evidence
for storing and retrieving balanced ternary values (`-1`, `0`, `+1`) using
binary memory. It has its own code, tests, reports, and hardware roadmap.

Version 0.2 is a **working software and RTL-simulation demonstrator**. A real
loopback HTTP client/server connects tensor files to emulated memory and exact
integer computations; an optional Icarus replay verifies the retrieved weights
in RTL. Physical board transport and FPGA measurements remain next steps.

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
make check     # Python tests + original and new RTL tests; Icarus required
make stack     # network and RTL conformance
make demo      # build/edge-report.html and JSON
```

Open [the release report](reports/stack.html) locally after downloading it;
GitHub shows HTML source. For software only, run
`python3 -m trinity_memory edge-demo` without `--rtl`.
See [validation](reports/stack-validation.md) for evidence and limitations.

## Current implementation

- Six lossless reference codecs: `baseline2`, `dense5`, `dense17`, `dense22`,
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

Python 3.10+; the reference package has no third-party runtime dependencies.

```sh
python3 -m unittest discover -s tests -v
python3 -m trinity_memory benchmark --count 65536 --repeats 3
python3 scripts/render_report.py
```

Open [`reports/index.html`](reports/index.html) locally to use the report and
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

`make check` runs Python and RTL checks. `make report` regenerates the original
codec measurements and HTML report. Optional CLI installation:
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

Physical BRAM allocation, LUT use, routed timing, DDR throughput, power, and
model quality are **not yet measured**. Continuous 27/35-bit software packing
does not establish the physical cost of a memory device.

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
