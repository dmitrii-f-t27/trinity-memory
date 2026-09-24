# Trinity Memory Bridge v1

The bridge transfers and validates real TMEM/TensorPack bytes over localhost
HTTP. Its storage and compute backend is an **in-process emulator**. It does
not access FPGA registers, claim device performance, execute a model, generate
an attestation, or submit a transaction.

Implementation: [`trinity_memory/bridge.py`](../trinity_memory/bridge.py).
The protocol and the server are implemented here; they have not been merged
into the upstream Rust `trinity-node`.

## Specification

The contract on this page is restated as the sealed specification
[`specs/memory/bridge.t27`](../specs/memory/bridge.t27): envelope and field
masks, id bounds, error codes with their HTTP statuses, default limits, handle
layout, read-range and dot rules, base64 sizes, storage accounting, runtime
buffer capacities and the synthetic identity derivation. Its constant
invariants compile as `_Static_assert` and its test blocks execute in the
generated C runner. [`conformance/memory_bridge.json`](../conformance/memory_bridge.json)
holds request/response vectors for every method, every error code, raw HTTP
framing failures, an interrupted body, out-of-range reads and per-row-scale
dots; `tests/test_spec_bridge.py` replays them over real TCP against the native
server, `tests/native/test_spec_bridge_vectors.py` replays them in process
through the generated bridge module, and `tests/native_spec_bridge.c` ties the
spec constants and rules to `t27/bridge.t27` and `t27/client.t27`.
The transport remains loopback-only and unauthenticated; the spec labels the
backend `emulator` and `hardware: false` until a device backend exists.

## Python client and server

```python
from trinity_memory.bridge import BridgeClient, BridgeServer
from trinity_memory.tensorpack import Tensor, encode_tensors

data = encode_tensors([
    Tensor("weights", (2, 3), (1, -1, 0, -1, 1, 1),
           scales=(0.5, 2.0), scale_axis=0)
])

with BridgeServer(host="127.0.0.1", port=0) as server:
    client = BridgeClient(server.url)
    assert client.capabilities()["backend"] == "emulator"
    handle = client.upload(data)
    assert client.read(handle) == data
    assert client.read(handle, offset=3, length=5) == data[3:8]
    info = client.info(handle)
    result = client.dot(handle, "weights", [-128, 127, -128])
    assert result["accumulators"] == [-255, 127]
    assert result["scales"] == [0.5, 2.0]
    assert result["scale_applied"] is False
    client.delete(handle)
```

`port=0` asks the OS for an unused port. `start()`/`close()` are also available;
closing discards all stored objects. The server uses one worker, so operations
are serialized. No directory, local pathname, remote URL or device pathname is
accepted by any RPC method. A handle is a random 32-character lowercase hex
identifier referring to one validated container held by this server process.

## Wire protocol

Send `POST /` with `Content-Type: application/json` and one `Content-Length`.
Each call is one JSON-RPC 2.0 object containing `jsonrpc`, `id`, `method` and
optional object-valued `params`. Named parameters only; unknown fields are
rejected. Batch requests and notifications are not implemented. Request IDs
are strings up to 128 characters or integers in the inclusive range
`[-(2**53 - 1), 2**53 - 1]`; booleans are rejected.

```json
{"jsonrpc":"2.0","id":1,"method":"trinity.capabilities","params":{}}
```

| Method | Parameters | Result |
| --- | --- | --- |
| `trinity.capabilities` | None | Version, formats, codecs, supported methods, backend and resource limits |
| `memory.upload` | `data`: canonical padded base64 container | New `handle`, byte count and backend |
| `memory.read` | `handle`; optional integer `offset`, `length` in container bytes | Base64 bytes, offset, length, total container bytes and backend |
| `memory.info` | `handle` | Validated container metadata, tensor descriptors, SHA-256, handle and backend |
| `memory.delete` | `handle` | `deleted: true`; reclaimed storage |
| `compute.dot` | `handle`, `tensor_name`, `activations`: signed int8 list | Exact integer `accumulators`, output `scales`, shapes, backend and `scale_applied: false` |
| `trinity_chipInfo` | None | Synthetic 16-byte identity using SDK field names and integer anchor |
| `chip_info` | None | Same synthetic identity using node field names and hexadecimal text anchor |

Uploads are atomic whole-container operations. Partial upload sessions are not
implemented. Reads may cover the whole container or an exact byte range;
out-of-bounds ranges fail rather than being silently shortened. Upload verifies
checksums, canonical codec encoding, lengths and TensorPack metadata before
allocating a handle. CRC32 and the returned SHA-256 are integrity tools, not
proofs of origin or execution.

TMEM has no tensor names or shape metadata, so the bridge presents it as one
vector called `weights` with scale `1.0`. TensorPack preserves stored names,
shapes and scales.

## Dot-product semantics

For a C-order matrix with shape `(rows, columns)`, one output is computed for
each row:

```text
accumulators[row] = sum(trit[row, column] * activation[column])
```

Trits are exactly `-1`, `0` or `1`. Activations must be JSON integers between
`-128` and `127`; floats and booleans are rejected. Python integer accumulation
does not wrap at a hardware word width. A vector produces one accumulator;
an empty TMEM vector with empty activations returns `[0]`. TensorPack scalar
and rank-greater-than-two tensors may be stored but cannot be passed to dot.

One positive scalar weight scale is repeated for each output, or a matrix may
have one scale per row (`scale_axis=0`). Scales on a contracted input axis are
rejected because they cannot be applied to one final raw accumulator. The
bridge returns scales separately; it does not multiply or round them. There
is no activation-scale parameter. Callers applying float scales must handle
their own numeric precision and overflow. This operation is a mathematical
reference for a ternary linear layer, not a model inference benchmark.

## Existing SDK integration

At the inspected SDK revision, `TrinityChip` accepts a backend instance, while
its bundled JSON-RPC backend raises `NotImplementedError`. The node registers
different method names. These source references pin the checked versions:

- [SDK `TrinityChip` and constructor](https://github.com/gHashTag/trinity-sdk/blob/fa8476397ac69438315268342958759e91da9e20/trinity/chip.py)
- [SDK JSON-RPC stub](https://github.com/gHashTag/trinity-sdk/blob/fa8476397ac69438315268342958759e91da9e20/trinity/backends/jsonrpc.py)
- [Node RPC methods and response fields](https://github.com/gHashTag/trinity-node/blob/4da4d7f7fee7a76c78f91c3b80e97a23b54e344d/src/api.rs)
- [Node mock 32-byte PUF identifiers](https://github.com/gHashTag/trinity-node/blob/4da4d7f7fee7a76c78f91c3b80e97a23b54e344d/src/chip_init.rs)

With the separate Trinity SDK installed, inject the provided backend directly:

```python
from trinity import TrinityChip
from trinity_memory.bridge import BridgeClient, BridgeServer, SDKMemoryBackend

with BridgeServer() as server:
    memory = BridgeClient(server.url)
    chip = TrinityChip(SDKMemoryBackend(memory))
    assert memory.capabilities()["hardware"] is False
    info = chip.chip_info()
    assert len(info.phi_id) == 16
    assert chip.verify_anchor()  # Software constant check only.
```

The IDs are public deterministic emulator IDs, not PUF outputs. The upstream
`ChipInfo` dataclass has no backend-status field, so check capabilities when
presenting it. `verify_anchor()` only compares a software constant in this
configuration and proves no hardware identity. The legacy `chip_info` alias
adapts field names; it does not reproduce the upstream node's 32-byte ID
contract or truncate any real device ID. The memory RPC namespace is separate
from both upstream interfaces.

`prove_inference()` and `submit_to_bittensor()` on `SDKMemoryBackend` raise
explicit `NotImplementedError`. No synthetic successful proof or receipt is
returned. The memory API remains accessible through `backend.client`.

## Limits and failure behavior

Default `BridgeServer` limits are configurable constructor arguments:

| Argument | Default |
| --- | ---: |
| `max_request_bytes` | 2,097,152 |
| `max_object_bytes` | 524,288 |
| `max_storage_bytes` | 8,388,608 |
| `max_objects` | 16 |
| `max_trits` | 1,000,000 decoded trits per container |
| `timeout` | 5 seconds for socket inactivity and the complete request body |

TensorPack additionally enforces its own hard limits before decoding. Invalid
uploads never consume a handle or stored bytes. Deleting an object reclaims
both byte and count capacity. The Python client defaults to a 5-second socket
timeout and a 4,194,304-byte maximum response.

| JSON-RPC code | Meaning |
| --- | --- |
| `-32700` | Invalid JSON, duplicate keys, nonfinite numbers, truncated/timed-out body |
| `-32600` | Invalid envelope or unsupported HTTP framing/origin |
| `-32601` | Unknown or unsupported method |
| `-32602` | Invalid parameters, container, numeric range or layout |
| `-32603` | Sanitized unexpected implementation error |
| `-32004` | Missing object handle or tensor name |
| `-32010` | Request, object, storage, decoded-TMEM-trit or response limit exceeded |
| `-32000` | Client transport failure, redirect, or invalid response |

TensorPack's decoded-trit limit is a container-validation error (`-32602`).
Malformed bodies use HTTP 400, forbidden host/origin uses 403, an oversized
request uses 413, and an invalid content type uses 415; each carries a JSON-RPC
error. Validly framed RPC calls return HTTP 200, including method errors.

The server accepts only `127.0.0.1` or `localhost`, validates the HTTP Host and
rejects Origin headers. The client disables environment proxies and redirects.
There is no authentication: other local processes can call this emulator.
Remote binding requires a separate authenticated transport design. Do not
describe this process-local service as a production device management API.

## Verification

```bash
python3 -m unittest discover -s tests -p test_bridge.py -v
```

The tests use actual TCP sockets and cover round trips, chunk reads, exact
signed arithmetic, per-row scales, corrupted containers, unsupported layouts,
strict JSON, malformed RPCs, resource limits, timeout recovery, host/origin
checks, redirect refusal and explicit unsupported SDK operations. SDK source
compatibility and RTL/hardware execution are separate checks from these
bridge tests.

## Device backend fpga (issue #64)

The emulator stays the default: `BridgeServer()` and a zeroed native state behave byte for
byte as described above (every emulator vector of `conformance/memory_bridge.json` passes
unchanged, and `tests/native/test_bridge_parity.py` still compares them with the frozen Python
reference). A second backend, `fpga`, sends `compute.dot` to the AX7203 over its UART and checks
the device on `chip_info`:

```python
from trinity_memory.bridge import BridgeClient, BridgeServer, FpgaDevice, check_identity

device = FpgaDevice(port="/dev/cu.usbserial-110",            # the CP2102N; the name follows the USB socket
                    bitstream_sha256="<sha256 of the loaded .bit, from its build record>",
                    idcode="0x13636093", dna="0x00389c0c2d85e85c",  # what openFPGALoader reads over JTAG
                    build_id="<BUILD_ID of that bitstream>")
with BridgeServer(backend="fpga", device=device) as server:
    client = BridgeClient(server.url, timeout=120)             # an upload at 115200 baud takes tens of seconds
    check_identity(client.call("trinity_chipInfo"))            # what SDKMemoryBackend requires
    handle = client.upload(tensorpack_bytes)
    result = client.dot(handle, "q_proj_rows_0_319", activations)
```

What changes with `backend="fpga"`:

- **State.** `TMBridgeState` has `backend` (0 emulator, 1 fpga), `transport` (0 in-process, 1
  uart) and `link` (the `TMLink` of `t27/fpga_link.t27`, configured by `tm_runtime_fpga` in
  `native/runtime.c`). Every former `"backend":"emulator"` literal of `t27/bridge.t27` prints the
  state's backend.
- **Capabilities** name `"backend":"fpga"`, `"hardware":true`, `"transport":"uart"` and a
  `device` object: baud, minimum protocol, region, the device limits (1024 rows per run, 1024
  words per row, codecs baseline2 and dense5) and the configured part of the evidence. Nothing is
  read from the device for capabilities. The emulator's capabilities have no `transport` key, as
  before.
- **Identity** (`chip_info`, `trinity_chipInfo`): the Bridge first asks the device for its 23
  status lines and checks the protocol (at least 3, the loader's protocol 2 plus the extension
  below) and the build id against the configuration; a mismatch or no device is error `-32000`.
  The 16-byte IDs stay the public synthetic constants (the device has no PUF): `identity_kind` is
  still `synthetic-public-16-byte`, `status` is `memory device (fpga)`, and the device is
  identified by the evidence block.
- **Uploads, reads, info, delete** are unchanged: containers are validated and held in process
  memory (`"backend":"fpga"` names the Bridge's backend, not where the bytes are). The device
  receives a tensor's image at `compute.dot`.
- **compute.dot** runs on the device for baseline2 and dense5 tensors (other codecs: `-32602`;
  a tensor with no column: `-32602`; an image larger than the link's buffer, `max_trits / 2 +
  65536` bytes: `-32010`). The result has the emulator's fields (`accumulators` are the device's,
  `scales` returned unapplied) plus `hardware`, `reference`, `evidence` and `transfer`:
  - `reference`: the same exact sums computed in process, and how many rows differ from the
    device (`mismatches`, `first_mismatch`). A wrong device row is reported, not hidden and not
    replaced: a caller that needs verified numbers checks `mismatches == 0`.
  - `evidence`: `bitstream_sha256`, `idcode`, `dna`, `build_id` (configured; the build id and the
    protocol checked against the device in this call) and `capture_sha256` / `capture_bytes`: the
    sha256 and count of every byte received from the device during the call. A capture that does
    not fit its buffer fails the call (`-32010`) instead of producing a partial hash.
  - `transfer`: codec, image bytes and sha256, whether the image was uploaded in this call,
    read-back bytes, frames, retransmissions, naks, timeouts, lines with a bad check byte, late
    replies, garbage bytes, bytes sent and received, matvec runs and attempts, and the device's
    Z counters summed over the runs (words, cycles, idle clocks, latency, invalid codes, stray
    words, consumer stalls).
- **SDK rule** (`trinity_memory.bridge.check_identity`, used by `SDKMemoryBackend.get_chip_info`):
  the emulator with `hardware: false`, or `fpga` with `hardware: true` and all five evidence
  fields present and well formed (`bitstream_sha256` and `capture_sha256` 64 lowercase hex
  digits, `idcode` `0x` and 8, `dna` `0x` and 16, `build_id` 8). Anything else raises
  `BridgeError(-32000)`. Well formed is not proven: the Bridge cannot read the bitstream or JTAG
  itself; the operator configures what openFPGALoader and the build record say, and the device's
  own report checks only the build id and the protocol.
- **IDCODE.** openFPGALoader prints this board's IDCODE without the revision nibble (0x3636093;
  the full value is 0x13636093, `reports/fpga/README.md`). The Bridge prints the configured value
  with 8 digits and does not reinterpret it; record which one was configured.

### Host link (t27/fpga_link.t27)

Everything above the byte is t27: frames and CRC-32, the stream decoder (the loader's 20-byte
lines H A N C plus Y and Z, read-back frames, garbage), stop-and-wait with retransmission, the
chunk upload and read-back comparison, the activation and matvec commands, the Y and Z lines,
the accumulators and the counters. The OS hooks are C in `native/platform.c`:
`tm_os_serial_open` (8N1, raw, no flow control, non-blocking, a standard rate up to 921600,
pending input discarded; -1 for no path, not a terminal, another rate, or a driver refusing the
settings), `tm_os_serial_read` (waits up to a timeout for the first byte, returns what is there,
0 on timeout, -1 on error or hang-up; a signal does not end the wait), `tm_os_serial_write` (all
bytes, resuming partial writes, -1 when the time runs out), `tm_os_serial_drain`,
`tm_os_serial_close` and `tm_os_monotonic_ns`. The WebAssembly build of `platform.c` has stubs
that return -1 (and a clock of 0); no wasm module includes the link.

One `compute.dot`: (1) status exchange (protocol, build id, store size from the config word);
(2) the row-padded image of the tensor (below), uploaded in 4096-byte `L` frames each
acknowledged, then read back in 4096-byte `R` frames and compared byte for byte, unless the same
image (sha256, size, region) is known to be on the device; (3) the activations in `X` frames;
(4) one `M` run per 1024 rows, each collecting its Y and Z lines. Retransmission follows the
loader tool (`docs/uart-loader.md`): after a nak, or when no reply came `reply_timeout` (0.5 s by
default) after the later of the write's start plus its wire time and the write's return, the
host drains, stays quiet for `quiet` (0.15 s; replies arriving then are counted as late) and
sends the same frame (same seq, so a load whose ack was lost is answered `duplicate`); at most
`attempts` (8) times. A run is repeated with a new seq when it is nakked, when nothing of it
arrives in time, when one of its lines fails its check byte, when a row is missing, or when the Z
counters disagree with the request (rows, words per row, words, result checksum). A run's lines
extend its deadline: after each line of the run the host waits at least `reply_timeout` more.
The upload cache is forgotten when the port is reopened, when an `H` line (device reset)
arrives, and when the device's `bytes_committed` counter differs from the value it had right
after this host's last upload (someone else loaded, or the device was reset).

## Device matvec (#64)

`t27/rtl/fpga_ddr3_matvec.t27` is consumer (B) of #65: y = W x on the device, fed at bus-word
width by #62's reader. It is not wired into a board top yet; that and the device side of the
frames below are the next wave.

- **Input.** One 128-bit word per controller clock (`in_valid`, bytes 0-7 in `in_lo`, 8-15 in
  `in_hi`), taken in every clock: there is no ready signal, so the consumer never holds the bus.
  baseline2 words carry 64 lanes (four 2-bit codes per byte), dense5 words 80 (one base-3 code per
  byte, the dense5 tables of `fpga_ddr3_reader.t27`, no division).
- **Row layout: row-padded.** Every row starts at a word, `wpr = ceil(cols / lanes)` words per
  row; lanes at and after `cols` in a row's last word are masked to 0, so padding content never
  counts. A 2,560-column row is exactly 40 baseline2 or 32 dense5 words, so for q_proj the image
  is the TMEM payload itself (the test checks it). A 6,912-column row (down_proj) is 108
  baseline2 words (no padding) or 87 dense5 words (48 padding lanes in the last word). A
  continuous stream with row boundaries inside a word would have needed a lane rotator per word
  (row start offsets 0, 32, 64, 16, 48 lanes for dense5 at 6,912 columns); padding costs 9.6
  bytes per dense5 row at 6,912 columns (arithmetic) and none at 2,560.
- **Activation storage: 80 per clock.** Ten banks of 1024 x u64 (block RAM, each 8 activations);
  entry k of all ten banks is the activation block of word k of a row (80 activations for dense5,
  64 for baseline2 with banks 8 and 9 unused). Reading 80 per clock lets a dense5 word, the widest,
  take one clock like a baseline2 word; 64 per clock would take two reads per dense5 word and
  halve its rate, which would make the consumer, not DDR3, the limit of the dense5 measurement.
  1024 entries allow rows up to 65,536 (baseline2) or 81,920 (dense5) columns. In the 1K x 36
  configuration this is 20 RAMB36E1 (two per bank) plus one for the results.
- **Arithmetic.** Per lane an 8-bit biased term: +1 gives x xor 0x80 (x + 128), -1 gives x xor 0x7F
  (~x + 128 = -x - 1 + 128), 0 gives 0x80; the missing +1 of each -1 lane is added as a count, and
  128 x 80 is subtracted per word: sum over lanes of t x = sum of terms - 10,240 + count of -1
  lanes (checked in C against the exact product on random lanes). An adder tree of 80 terms in
  pipeline stages (S3 20 sums of 4, S4 5 sums of 4, S4b two partial sums, S5 the word, S6 the
  word's dot product), then S7 the row accumulator, 32-bit two's complement, starting at 0 on the
  first word of each row: |y| <= cols x 128 = 884,736 for down_proj (above 2^19, so 21 signed bits
  are needed; 32 are used; 1024 x 80 x 128 = 10,485,760 is the largest row the banks allow).
- **Output.** After the last word the pipeline drains, then one `Y` line per row and eleven `Z`
  lines go through `fpga_line_emitter.t27` (format below); results wait in a 1024 x u32 result
  memory because a line takes 1.74 ms at 115200 baud and a 2,560-column row 40 clocks.
- **Counters (Z lines).** status (0 ran, 1 refused: rows outside 1..1024, cols 0, format not 0 or
  1, more than 1024 words per row), rows done, words per row, words taken, cycles (first word to
  last word, inclusive), idle clocks (clocks of that span with no word offered: the bus starved the
  consumer, the memory-bound signal of #65), latency (clocks from the end of setup to the first
  word), invalid codes (dense5 codes 243-255, baseline2 lanes 11, in any byte or lane), stray words
  (offered while no word was expected), consumer stalls (0 by construction: an identity, not a
  result), and a result checksum (rotate-left-1 xor of the rows' results at their write into the
  result memory; the host recomputes it from the Y lines, so a result memory read that differs
  from its write shows). Words = rows x wpr holds by construction too: a run ends at its last row;
  the host checks it only as a consistency check of the lines it received.

**Simulation (Icarus, `tests/test_ddr3_matvec.py`, summary in
`reports/fpga/matvec-sim-2026-09-24-8309e515.json`, commit 8309e51).** The module with the real line emitter and a
transmitter model, fed through `tests/tb_ddr3_matvec.v` with random gaps between words:

| Case | Words | Result | Bench vs module |
| --- | ---: | --- | --- |
| q_proj rows 0-319 dense5, 30 % gaps, seed 11 | 10,240 (32 per row) | 320 of 320 accumulators equal t27/matvec.t27's | cycles 14,591, idle 4,351, latency 2: equal to the bench's own counts |
| q_proj rows 0-319 baseline2, 30 % gaps, seed 12 | 12,800 (40 per row) | 320 of 320 equal | cycles 18,297, idle 5,497, latency 1: equal |
| 6,912 columns, 7 row pairs x 2 formats (all -128 / all +127 activations with all +1 / all -1 rows, alternating, random, zero), 20 % gaps | 174 (dense5) / 216 (baseline2) per pair | exact, +-884,736 and +-877,824 reached; garbage in padding lanes and past the last activation not counted; an invalid code in a padding byte counted (1) | equal |
| one-word rows (37 and 80 columns), 81 and 65 columns (two words), 64 and 160 columns, 1 column; 0, 10, 30, 50 and 75 % gaps | 1 to 9 rows | exact | equal |
| stray words (3 during setup, 5 after the last word) | 160 | exact, stray 8 | equal |
| refused: rows 0 and 1025, cols 0, format 2, 81,921 dense5 columns | - | only the Z lines, status 1 | - |

The reference of the q_proj rows: the host builds the images with `t27/fpga_link.t27` (equal to
the TMEM dense5 and baseline2 payloads of the chunk), the activations are `tmv_activations` seed
27 (2,560), and the expected accumulators are the first 320 of `t27/matvec.t27`'s product of the
whole q_proj, whose sha256 over all 2,560 equals `reports/ternary-check/matvec-2026-09-23.json`
(`hf_packed`). The 320 accumulators' sha256 (i64 little endian) is `a2366b57…` in both formats;
the first eight are -2561, 2660, -52, -2385, 1269, 3446, 3451, -2365, the report's `first`. The
gaps are the bench's pseudo-random choice; the cycles, idle clocks and latency are the module's
counts of that simulated stream, not a DDR3 measurement.

**Out-of-context estimates (`tools/fpga-matvec-ooc.py`, record in
`reports/fpga/matvec-ooc-2026-09-24-8309e515/`, commit 8309e51).** Synthesis as the DDR3 designs (yosys 0.69,
`synth_xilinx -flatten -abc9 -arch xc7`, carry chains and DSPs allowed) with the 1K x 36 block RAM
library, then nextpnr-xilinx 0.9.7 (heap, router2, seeds 1-5, `--freq 83.33`). These are yosys
cell counts and nextpnr timing-model estimates in a harness (`fpga/ax7203/matvec/tms_matvec_ooc.v`:
a 388-bit input shift register and an XOR-reduced output around the module), not a bitstream and
not a board result. In the harness: 4,453 LUTs, 3,416 flip-flops (FDCE 3,074, FDPE 1, FDRE 341; the
harness's 388-bit shift register sits in the FDRE and in 2 SRLC32E), 539 CARRY4, 0 DSP48E1, 21
RAMB36E1 (yosys); nextpnr packs 8,144 SLICE_LUTX.
The module alone as the synthesis top, every output kept as a port (not placed): 4,632 LUTs, 3,251
flip-flops, 459 CARRY4, 0 DSP48E1, 21 RAMB36E1. Estimated Fmax after routing for the 83.33 MHz
clock: 112.45, 104.98, 111.02, 107.78 and 106.43 MHz for seeds 1-5 (all five meet it; 95.4-101.7
MHz after placement); the critical path of four seeds ends in the line check (`e_bits`), of the
fifth in the choice of the line's fields. Earlier versions of the module missed 83.33 MHz in the same flow
(scratch runs, not recorded here): the invalid-code sum sat in the decode stage, the line check
shared a clock with the choice of its fields, and the word sum took three adder levels in one
stage; each got its own stage.

## Wire protocol extension (matvec, protocol 3)

The loader's frames, lines, CRC-32 and ack/nak rules (`docs/uart-loader.md`) stay as they are.
The extension adds two host frames and two device lines; `tools/bridge_link_protocol.py` states
it in Python (`MatvecDevice` answers byte for byte), `t27/fpga_link.t27` is the host.

| Frame | cmd | addr | len, payload | Device answer |
| --- | --- | --- | --- | --- |
| activations `X` | 0x58 | first 80-byte activation block (entry of the ten banks) | a multiple of 8, 8 <= len <= 4080 (51 blocks), block + ceil(len / 80) <= 1024; the activation image bytes | `A` with command index 5 after the CRC matched and the bytes are in the banks; a repeat with the same seq, addr, len and CRC is answered `duplicate` and not written again |
| matvec `M` | 0x4D | region byte address, a multiple of 16, inside the store | 12: rows u32, cols u32, format u32 (0 baseline2, 1 dense5) | `A` with command index 6, then the run's `Y` lines and `Z` lines |

The activation image is `wpr` blocks of 80 bytes: block k byte i (i < lanes) is the activation of
column k x lanes + i, zero past the last column, bytes 64-79 zero for baseline2; block k goes to
entry k of the banks, bank b = bytes 8b .. 8b+7. The weight image is the row-padded one
(`tms_device_image_bytes = rows x wpr x 16`), loaded with ordinary `L` frames.

| Line | a | v |
| --- | --- | --- |
| `Y` (0x59) | `(seq << 24) \| row` | y[row], 32-bit two's complement |
| `Z` (0x5A) | `(seq << 24) \| (11 << 16) \| index` | the counter `index` (list above) |

Both carry the loader's check byte (low byte of the CRC-32 of tag, a, v). A refused run (status 1)
sends only its eleven Z lines. Header checks (nak `length`): `X` outside the bounds above, `M` with
len other than 12, an address not a multiple of 16 or past the store; the payload is checked by
the matvec (status 1). Protocol: the config word of `H` and status line 19 reports 3 for a build
with the extension; the host requires at least 3. `X` and `M` frames count in neither
`frames_committed` nor `bytes_committed`: the host's upload cache relies on `bytes_committed`
moving only with loads (`MatvecDevice` follows this rule). The device side still to be built
(next wave): the frame parser's two commands (`X` into the write port of the activation banks,
`M` starting #62's burst reader over the region and this module), the arbitration of the line
emitter between the loader's responses and the matvec's lines, and the protocol number.

## Upstream delta for #66 (not applied here)

gHashTag/t27 at 7e9de07d holds `specs/memory/tmem/bridge.t27`, identical to this repository's
`specs/memory/bridge.t27` before this change except line 2 (its path), and
`conformance/tmem_bridge.json`, byte-identical to this repository's `conformance/memory_bridge.json`
before this change (sha256 `9d3e0aad…`, 55 vectors). What #66 needs, nothing of which is sent from
here:

1. `specs/memory/tmem/bridge.t27`: the change of `specs/memory/bridge.t27` in this commit range,
   applied as is (the header lines on stage two, `enum BridgeTransport`, section 10's 34 constants,
   the 14 reference functions from `tms_bridge_backend_hardware` to `tms_device_accumulator_fits`,
   7 invariants and 4 test blocks), line 2 unchanged. The result's sha256 is `510d6263…`
   (computed here from that file; `spec_hash` of the new seal).
2. `conformance/tmem_bridge.json`: this repository's new `conformance/memory_bridge.json`
   (`tools/generate-spec-vectors.py`): the 55 vectors unchanged, 8 fpga vectors, `constants.fpga`,
   `sdk_adapter.fpga_chip_info_requires`, 6 more invariants, `replay.backend` and
   `replay.result_format`. The fpga vectors need a device double to replay; upstream has none,
   so either the double comes along (`tests/fake_fpga_device.py`, `tools/bridge_link_protocol.py`,
   `tools/uart_loader_protocol.py`) or upstream's replay skips vectors with a `backend` key and says
   so.
3. The seal `tmem_TrinityMemoryBridgeSpec`: `t27c seal --save specs/memory/tmem/bridge.t27` with
   upstream's own t27c. Here, the pinned bff21b85 gives `gen_hash_c` `f5f81c46…`; upstream's
   compiler may generate other code, so its generated-code hashes are its own.

## Not yet on the board

- No bitstream contains the device matvec or the `X`/`M` frame handler; nothing of this section
  ran on the AX7203. Every result above is simulation (Icarus), a C check, a synthesis count or a
  place-and-route estimate.
- The host link has run only against the fake device on a pseudo-terminal and the OS hooks only on
  ptys (macOS here); the CP2102N and its driver are untried with this code (the loader's Python
  tool reached them with pyserial).
- `compute.dot` with `backend: fpga` and `hardware: true` for a real layer chunk on the board, and
  the committed captures with hashes, are #64's board part.
- The upload at 115200 baud: the loader tool moved 11.37 kB/s of payload (`docs/uart-loader.md`),
  so the chunk's 163,840-byte dense5 image would take about 14.4 s to load and as long to read back,
  and the 204,800-byte baseline2 image about 18 s each way (arithmetic from that rate, not
  measured with this link). Faster rates are not used by the link yet (no `B` frame).
