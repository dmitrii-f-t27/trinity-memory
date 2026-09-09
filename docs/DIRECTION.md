# Trinity Memory - ternary storage and memory interfaces

Trinity Memory is a dedicated experimental direction for representing, storing,
and retrieving ternary data in memory devices. It has a separate repository,
format specification, reference code, RTL, tests, and evidence reports.

The development home is
[`dmitrii-f-t27/trinity-memory`](https://github.com/dmitrii-f-t27/trinity-memory).
Trinity is the shared project of Dmitrii Fedorov and Dmitrii Vasilev. This
repository's location does not imply that it is a solo replacement for the
Trinity stack or that its interfaces have been accepted upstream.

## Scope and architecture

The proposed device path is:

```text
host / workload
    -> agreed data format and loader
    -> encoded binary storage
    -> decoder and validity checks
    -> ternary output lanes / consumer
```

Version 0.2 adds five connected software/RTL directions: Bridge, TensorPack,
Stream Compute, Conformance Lab and Edge Demo. The real loopback HTTP service
stores containers in emulated memory and computes exact integer dots. The
downloaded weights can be replayed in an Icarus ready/valid dot pipeline.
See [the complete walkthrough](STACK.md) and [release evidence](../reports/stack-validation.md).

The original RTL storage loader remains a clocked write port. The new dot
pipeline has backpressure; the old storage sequencer does not. No physical
host bus, DDR controller or end-to-end memory device is claimed yet.

Weight tensors are the first test workload. QAT and model inference remain
consumers of the memory system, not the full product scope. Other ternary
workloads may be added after their representation and access needs are specified.

## Baseline: implemented and checked

- Dense 5/8, 17/27, and 22/35 bitstream codecs; a 2-bit reference baseline.
- Structured sparse codecs with at most one nonzero per four values or two per
  eight. They reject incompatible blocks rather than silently changing data.
- TMEM v1 lengths, canonical padding, and CRC32; CLI and raw RTL word export.
- Dense5 and sparse41 decoders, and dense5/baseline5 synchronous streams.
- Software tests, Icarus Verilog simulations, and synthetic byte/timing reports.

Evidence is in [validation](../reports/validation.md) and
[benchmark data](../reports/benchmark.json). Software bit density and correct
simulation do not establish physical BRAM savings, routed clock frequency,
DDR throughput, power, or model quality.

## Milestone 1: measured FPGA memory demonstrator

**Deliverable:** buildable packed and 2-bit baseline designs for an explicitly
identified board/device, using the same logical fixture and output interface.
AX7203 is the candidate in the initial research note; exact part, clock, loader,
memory policy, and toolchain must be verified before implementation.

**Acceptance evidence:**

- Every physical-device output agrees with the reference, including final lane
  masks, invalid codes, reset, and completion behavior.
- Saved synthesis and place-and-route reports record actual LUT/FF/BRAM use,
  constraints, achieved timing, tool versions, and commit hashes.
- Counters record delivered values, bytes, cycles, and stalls under a defined
  workload. Include partial groups and storage allocation overhead.

Use the [hardware protocol](hardware.md). A 35-bit software word stored in a
36-bit physical slot costs 36 allocated bits; count physical blocks separately
from logical bits. DDR and power claims require their own measured experiments.

## Milestone 2: host and stream interface

**Deliverable:** a documented interface for loading, reading, framing, flow
control, logical lengths, and error reporting. Choose the actual transport
after the demonstrator identifies its host and board constraints.

**Acceptance evidence:** reproducible host-to-device round trips, randomized
transfer lengths, backpressure, reset during transfer, and invalid/corrupt-data
tests. Report sustained payload and bus throughput plus latency distributions
under the stated workload. The new compute stream implements downstream
backpressure in simulation; connecting it to a physical storage controller
and transport remains work.

## Milestone 3: portability and workload integration

**Deliverable:** versioned adapters for agreed Trinity data and bus interfaces,
including scale/shape metadata where a workload needs them. Compare actual
storage, transfers, and decoding cost across supported configurations.

**Acceptance evidence:** the same conformance fixtures pass each adapter and
implementation. Publish measured overheads and unsupported cases. If pruning
or quantization changes weights, evaluate model quality separately from the
lossless storage round trip. CRC32 is an accidental-corruption check, not
authentication; any stronger integrity design needs a separate specification.

## Relationship to the existing stack

- [t27](https://github.com/gHashTag/t27): compiler and specifications. A future
  generated interface or conformance link must be explicitly agreed.
- [trinity-fpga](https://github.com/gHashTag/trinity-fpga): FPGA infrastructure;
  a likely integration point for board builds and measurements.
- [trinity](https://github.com/gHashTag/trinity): compute/runtime and potential
  workloads using the memory interface.

Keep this direction separately testable. Upstream integrations can be reviewed
as focused changes once their contracts and evidence are ready.

## Boundaries

This prototype is not physical multi-level ternary SRAM/NVM, a commodity
SSD/DRAM replacement, a measured production memory device, or authenticated
model storage. It makes no claim of a free decoder, unextractable weights,
novel base-3 packing, or measured inference acceleration. The
[research audit](research.md) records prior work and corrections to the initial note.
