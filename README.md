# Trinity Memory

**Ternary storage formats, memory RTL, and experimental memory interfaces.**

[Russian overview](README.ru.md) · [Direction and roadmap](docs/DIRECTION.md) ·
[Format specification](docs/format.md) · [Hardware protocol](docs/hardware.md) ·
[Research audit](docs/research.md)

Trinity Memory is a dedicated experimental memory direction for the Trinity
ecosystem. This repository contains the reference implementation and evidence
for storing and retrieving balanced ternary values (`-1`, `0`, `+1`) using
binary memory. It has its own code, tests, reports, and hardware roadmap.

The current deliverable is a **software and RTL prototype**. Physical FPGA
measurements and a complete memory-device interface remain next steps.

## Current implementation

- Six lossless reference codecs: `baseline2`, `dense5`, `dense17`, `dense22`,
  `sparse41`, and `sparse82`.
- TMEM v1 files with explicit lengths, canonical padding, and CRC32 integrity.
- A CLI for packing, unpacking, inspection, benchmarking, and RTL memory export.
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

`make check` runs Python and RTL checks. `make report` regenerates measurements
and the HTML report. Optional CLI installation: `python3 -m pip install -e .`.

```sh
mkdir -p build
python3 -m trinity_memory pack examples/trits.json build/example.tmem --codec dense5
python3 -m trinity_memory inspect build/example.tmem
python3 -m trinity_memory unpack build/example.tmem build/restored.json
python3 -m trinity_memory export-rtl examples/trits.json build/dense.mem --codec dense5
```

## Measured storage

Each synthetic dataset has **65,536 weights**, seed 27. These are actual payload
bytes, including final padding. TMEM adds **24 header bytes**; tensor scales,
shape, and other model metadata are outside this prototype's format.

| Codec | Payload bytes | Payload bits/weight | Required structure |
|---|---:|---:|---|
| baseline2 | 16,384 | 2.000000 | Any trits |
| dense5 (5/8) | 13,108 | 1.600098 | Any trits |
| dense17 (17/27) | 13,014 | 1.588623 | Any trits |
| dense22 (22/35) | 13,034 | 1.591064 | Any trits |
| sparse41 (4, <=1) | 8,192 | 1.000000 | At most one nonzero in each block of four |
| sparse82 (8, <=2) | 8,192 | 1.000000 | At most two nonzeros in each block of eight |

All **15 dataset/codec combinations** recover their input exactly. The recorded
local validation also passed **22 Python tests** and **1,296 binary decoder
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
already-merged subsystem. Shared tensor formats, scales, buses, and controller
interfaces require explicit integration work. Base-3 packing and structured
ternary coding have prior art; the [research audit](docs/research.md) identifies
the sources and corrects unsupported claims from the initial research note.

Trinity is the shared project of Dmitrii Fedorov and Dmitrii Vasilev. Hosting
this memory implementation under `dmitrii-f-t27` gives it a direct development
home without representing the wider Trinity stack as a solo project.
