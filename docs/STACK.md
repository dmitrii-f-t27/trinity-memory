# Five directions: reproducible software and RTL demonstrator

Version 0.3 connects all five directions through executable t27 and a native library.
Python exposes compatibility adapters; v0.2 remains an independent test oracle.
Build the native artifacts first using [the migration guide](T27-MIGRATION.md).
No physical FPGA, DDR, power, production driver or model-quality result is implied.

| Direction | Implementation | Acceptance path |
|---|---|---|
| Bridge | `t27/bridge.t27`, `t27/http.t27`, real loopback HTTP JSON-RPC memory service/client | upload/read exact bytes, limits/errors, SDK adapter |
| TensorPack | `t27/tensorpack*.t27`, TTPK v1 around TMEM v1 | named tensors, shape/axes/scales, strict bounds/CRC/schema |
| Stream Compute | `t27/rtl/dot_stream.t27`, native Icarus runner | dense5/baseline5 dot, int8 activations, ready/valid, invalid frames/reset |
| Conformance Lab | `examples/conformance.json`, `t27/experiments.t27`, tests | fixed independent bytes/sums and randomized network/RTL checks |
| Edge Demo | `t27/experiments.t27` | fixed signal classifier, two encodings, exact reference and optional RTL comparisons |

## Reproduce the complete path

Requirements: Python 3.10+, Icarus Verilog (`iverilog` and `vvp`) on PATH for RTL.
Python adapter dependencies: standard library plus the built native library. Run from a source checkout:

```sh
make check
make stack
make demo
```

`make demo` writes `build/edge-report.json` and `build/edge-report.html`. Open the
HTML locally; it contains its data and needs no external scripts. Historical v0.2 checked-in
release snapshots are `reports/stack.json`, `reports/stack.html` and
`reports/conformance.json`; see `reports/stack-validation.md` for provenance.

Without RTL tools, run `python3 -m trinity_memory edge-demo` or
`python3 -m trinity_memory conformance`. These commands explicitly omit RTL
evidence. Adding `--rtl` requires the tools and fails if they are missing.

The chain is:

```text
hand-authored template matrix + metadata
  -> TensorPack file
  -> BridgeClient -> actual HTTP -> BridgeServer (memory emulator)
  -> byte-for-byte readback -> decoded tensor
  -> exact integer matrix/vector result over RPC
  -> optional Icarus replay of the retrieved weights
  -> independent arithmetic comparison and HTML/JSON report
```

The HTTP backend executes the compiled t27 kernels. The RTL replay runs separately from that
backend, with retrieved weights and the same activations. This is not an HTTP
driver for a board or a hardware-executed RPC call.

## Use the modules separately

Create a JSON array of tensors as shown in `examples/tensorpack.json`:

```sh
python3 -m trinity_memory tensor-pack examples/tensorpack.json build/example.ttpk
python3 -m trinity_memory tensor-inspect build/example.ttpk
python3 -m trinity_memory tensor-unpack build/example.ttpk build/tensors.json
python3 -m trinity_memory serve --port 8787
```

In another terminal:

```sh
python3 -m trinity_memory upload build/example.ttpk --url http://127.0.0.1:8787
# Use the returned handle in these commands:
python3 -m trinity_memory download HANDLE build/readback.ttpk
python3 -m trinity_memory dot HANDLE weights examples/activations.json
```

The server stores data only in its process memory; restarting removes its objects.
It accepts only local requests and has no authentication or production deployment
mode. See [Bridge protocol](bridge.md) for limits and supported operations.

## Existing Trinity SDK integration

This repository provides `SDKMemoryBackend`, injected into the existing
`TrinityChip` constructor. The SDK's own `connect(backend="jsonrpc")` remains
unchanged and is still its upstream stub. The test pins the actual upstream source:

```sh
git clone https://github.com/gHashTag/trinity-sdk.git build/upstream-sdk
git -C build/upstream-sdk checkout fa8476397ac69438315268342958759e91da9e20
PYTHONPATH=build/upstream-sdk python3 scripts/test_sdk_bridge.py
```

The adapter checks a synthetic identity and software anchor constant, then uses
its memory client for upload/read/dot. It does not attest a physical chip, prove
inference or submit transactions. Those unsupported calls raise errors.
The upstream Rust node is not modified or substituted. Its `chip_info` naming
and the SDK's `trinity_chipInfo` naming are handled as documented emulator aliases;
no interoperability with real 32-byte PUF identities is claimed.

## Demo meaning

The classifier, its fixtures, the scoring rules and the report fields are stated in
[`specs/memory/edge_demo.t27`](../specs/memory/edge_demo.t27); the golden model
containers, the twelve predictions, the thirty-six RTL rows with their seeds and
the scoring cases are in
[`conformance/memory_edge_demo.json`](../conformance/memory_edge_demo.json),
replayed by `tests/test_spec_edge_demo.py` through the native demo, the Bridge
and the CLI. Byte counts and wall times in the report are labelled by their
evidence (`emulator` loopback, `rtl-simulation` rows); `fpga` never appears here.

Three hand-authored templates identify rising, falling and alternating signals.
The six fixed, synthetic fixtures illustrate the full data path. Constant input
is ambiguous and returns no label. This is not trained ML, a generalization
benchmark, or evidence of accuracy on sensor data. The example deliberately has
small data so every score is easy to reproduce by hand.

Raw payload bytes exclude TensorPack metadata. Whole-container sizes include it;
small tensors can be dominated by metadata. Loopback wall times include JSON,
HTTP and scheduling. RTL cycles include testbench stalls; neither metric proves
FPGA throughput or acceleration over another inference implementation.

## Specifications

The contracts behind these directions are being restated as sealed `.t27`
specifications under [`specs/memory/`](../specs/memory/), starting with
[`types.t27`](../specs/memory/types.t27) (lane codes, codec geometry, TMEM v1
framing, CRC32, status codes, evidence labels). Vectors in
[`conformance/`](../conformance/) are generated from the spec and replayed by the
native harness and the Python adapters; `tools/check-specs.sh` is the gate. The
Conformance Lab is specified in [`conformance.t27`](../specs/memory/conformance.t27):
the fixture schema, the experiment plan the native runtime executes (codec
order, seeded random cases, sparse transfers, six single-bit corruptions), the
native report, and the lab report that `tools/conformance-lab.py` assembles
from every consumer under evidence labels. Run it from a clean clone after
`make t27`:

```sh
python3 tools/conformance-lab.py --repeat 2 --output build/conformance-lab.json
```

Two runs must be identical except the `timing` key; the report lists the
corruption, interruption and reset cases it covers and where each is replayed.
The Bridge, TensorPack, Stream Compute, Conformance Lab and Edge Demo specs are
tracked in [epic #3](https://github.com/dmitrii-f-t27/trinity-memory/issues/3).

## Hardware continuation

The next physical milestone still requires a named board/part, clock constraints,
transport, synthesis/place-and-route, and captured device output. Reuse the board
infrastructure of [trinity-fpga](https://github.com/gHashTag/trinity-fpga) after
checking its current interfaces. [Hardware protocol](hardware.md) defines the
evidence required before making resource, speed or power claims.
