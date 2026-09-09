# Five directions: reproducible software and RTL demonstrator

Version 0.2 connects the five directions in one repository and one Python package.
No physical FPGA, DDR, power, production driver or model-quality result is implied.

| Direction | Implementation | Acceptance path |
|---|---|---|
| Bridge | `trinity_memory/bridge.py`, real loopback HTTP JSON-RPC memory service/client | upload/read exact bytes, limits/errors, SDK adapter |
| TensorPack | `trinity_memory/tensorpack.py`, TTPK v1 around TMEM v1 | named tensors, shape/axes/scales, strict bounds/CRC/schema |
| Stream Compute | `rtl/trinity_dot_stream.v`, Python Icarus runner | dense5/baseline5 dot, int8 activations, ready/valid, invalid frames/reset |
| Conformance Lab | `examples/conformance.json`, `trinity_memory/conformance.py`, tests | fixed independent bytes/sums and randomized network/RTL checks |
| Edge Demo | `trinity_memory/edge.py` | fixed signal classifier, two encodings, exact reference and optional RTL comparisons |

## Reproduce the complete path

Requirements: Python 3.10+, Icarus Verilog (`iverilog` and `vvp`) on PATH for RTL.
Python runtime dependencies: standard library only. Run from a source checkout:

```sh
make check
make stack
make demo
```

`make demo` writes `build/edge-report.json` and `build/edge-report.html`. Open the
HTML locally; it contains its data and needs no external scripts. Checked-in
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

The HTTP backend computes in Python. The RTL replay runs separately from that
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

Three hand-authored templates identify rising, falling and alternating signals.
The six fixed, synthetic fixtures illustrate the full data path. Constant input
is ambiguous and returns no label. This is not trained ML, a generalization
benchmark, or evidence of accuracy on sensor data. The example deliberately has
small data so every score is easy to reproduce by hand.

Raw payload bytes exclude TensorPack metadata. Whole-container sizes include it;
small tensors can be dominated by metadata. Loopback wall times include JSON,
HTTP and scheduling. RTL cycles include testbench stalls; neither metric proves
FPGA throughput or acceleration over another inference implementation.

## Hardware continuation

The next physical milestone still requires a named board/part, clock constraints,
transport, synthesis/place-and-route, and captured device output. Reuse the board
infrastructure of [trinity-fpga](https://github.com/gHashTag/trinity-fpga) after
checking its current interfaces. [Hardware protocol](hardware.md) defines the
evidence required before making resource, speed or power claims.
