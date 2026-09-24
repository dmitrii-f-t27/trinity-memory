# Trinity Memory Bridge v1

The bridge transfers and validates real TMEM/TensorPack bytes over localhost
HTTP. Its default storage and compute backend is an **in-process emulator**. It does
not access FPGA registers, claim device performance, execute a model, generate
an attestation, or submit a transaction. (Stage two adds a second, opt-in backend,
`fpga`, whose `compute.dot` runs on the AX7203; storage stays in process: see
"Device backend fpga" below. Everything on this page before that section is the
emulator.)

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
default backend `emulator` with `hardware: false` (section 10 adds `fpga` with
`hardware: true`, below).

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
                    idcode="0x13636093",                         # 0x and 8 digits: see "IDCODE" below
                    dna="0x00389c0c2d85e85c",                    # openFPGALoader --read-dna
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
- **Configuration** (`FpgaDevice`, checked in Python and again in `tm_runtime_fpga`): the five
  evidence fields must match their patterns exactly (`fullmatch`: a trailing newline is refused);
  `min_protocol` is 4 (the default, the matvec extension below) or more, below 2^32 (it is a
  u32 on the device's side; a larger value used to be truncated silently); `baud` must be a
  standard rate that this host's termios can set: Linux has constants up to 921600, macOS's
  `<sys/termios.h>` stops at B230400, so on macOS 460800 and 921600 are refused when the Bridge is
  configured (`tm_os_serial_rate_ok`; a higher rate would need the IOSSIOSPEED ioctl, which the
  hooks do not use). The rate must also be the one the loaded bitstream's UART runs at: the link
  has no baud-switch frame, and the loader builds boot at their `BAUD_DIV` (217: 115200 at 25 MHz).
- **The serial port** is opened exclusively (TIOCEXCL, released with TIOCNXCL at close) at the
  first `chip_info` or `compute.dot` and stays open until the server closes (reopened only after
  an I/O error). While a `BridgeServer(backend="fpga")` runs, no other process should use the
  port: the board lock of the capture procedure must cover the server's whole lifetime. On Linux a
  second open fails with EBUSY (seen in this pass on a pseudo-terminal in a Linux container, uid
  1000, and a new open works after TIOCNXCL and close; `tests/test_bridge_link.py` asserts it on
  Linux, which here only CI runs);
  macOS pseudo-terminals ignore TIOCEXCL (a second open succeeded in this pass's probe); the review
  of this change probed another IOSerialFamily device on this Mac (`/dev/cu.Bluetooth-Incoming-Port`),
  where a third open after TIOCEXCL failed with "Resource busy". The CP2102N's driver is untried.
- **Capabilities** name `"backend":"fpga"`, `"hardware":true`, `"transport":"uart"` and a
  `device` object: baud, minimum protocol, region, the device limits (1024 rows per run, 1024
  words per row, codecs baseline2 and dense5) and the configured part of the evidence. Nothing is
  read from the device for capabilities. The emulator's capabilities have no `transport` key, as
  before.
- **Identity** (`chip_info`, `trinity_chipInfo`): the Bridge first asks the device for its 23
  status lines and checks the protocol of its config word (at least `min_protocol`) and its build
  id against the configuration. Every refusal is error `-32000`, told apart by its message: `device
  protocol is below the configured minimum`, `device build id does not match the configured
  evidence`, `cannot open the device serial port` (missing, busy, not a terminal, or settings
  refused), `device serial I/O failed`, `device did not acknowledge a frame`. The 16-byte IDs stay
  the public synthetic constants (the device has no PUF): `identity_kind` is still
  `synthetic-public-16-byte`, `status` is `memory device (fpga)`, and the device is identified by
  the evidence block.
- **Uploads, reads, info, delete** are unchanged: containers are validated and held in process
  memory (`"backend":"fpga"` names the Bridge's backend, not where the bytes are). The device
  receives a tensor's image at `compute.dot`.
- **compute.dot** runs on the device for baseline2 and dense5 tensors (other codecs and a tensor
  with no column: `-32602`). `-32010` ("tensor exceeds the device limits") when rows or columns
  exceed the link's buffers (`max_trits`), a row needs more than 1024 words (65,536 baseline2 or
  81,920 dense5 columns), the image does not fit the link's buffer (`max_trits / 2 + 65536`
  bytes), or the image does not fit the device's store after the configured region; `-32010`
  ("device capture exceeds its buffer") when the capture overflowed. The result has the
  emulator's fields (`accumulators` are the device's, `scales` returned unapplied) plus
  `hardware`, `reference`, `evidence` and `transfer`:
  - `reference`: `{"backend":"emulator","mismatches":n,"first_mismatch":row}`: the Bridge computes
    the exact sums in process and counts the rows where the device differs; the sums themselves
    are not returned. A wrong device row is reported, not hidden and not replaced: a caller that
    needs verified numbers checks `mismatches == 0`. A mismatch also forgets the upload cache, so
    the next dot uploads the image again and reads it back.
  - `evidence`: `bitstream_sha256`, `idcode`, `dna`, `build_id` (configured; only the build id,
    and the protocol, are checked against the device in this call) and `capture_sha256` /
    `capture_bytes`: the sha256 and count of every byte received from the device during the call.
    A capture that does not fit its buffer fails the call (`-32010`) instead of producing a
    partial hash.
  - `transfer`: codec, image bytes and sha256, whether the image was uploaded in this call,
    read-back bytes, frames, retransmissions, naks, timeouts, lines with a bad check byte, late
    replies, garbage bytes, bytes sent and received, matvec runs and attempts, `store_reloads`
    (below), and the device's Z counters summed over the accepted runs (words, cycles, idle
    clocks, latency, invalid codes, stray words, consumer stalls).
- **SDK rule** (`trinity_memory.bridge.check_identity`, used by `SDKMemoryBackend.get_chip_info`):
  the emulator with `hardware: false`, or `fpga` with `hardware: true` and all five evidence
  fields present and well formed (`bitstream_sha256` and `capture_sha256` exactly 64 lowercase hex
  digits, `idcode` `0x` and 8, `dna` `0x` and 16, `build_id` 8; nothing before or after). Anything
  else raises `BridgeError(-32000)`. Well formed is not proven: the Bridge cannot read the
  bitstream or JTAG itself; the operator configures what openFPGALoader and the build record say,
  and the device's own report checks only the build id and the protocol. `prove_inference` and
  `submit_to_bittensor` raise `NotImplementedError` in both backends.
- **IDCODE.** openFPGALoader prints this board's IDCODE without its revision nibble: `0x3636093`
  (7 digits; the committed loader captures record exactly that, e.g.
  `reports/fpga/uart-loader-2026-09-24-1d474000/`, and `reports/fpga/README.md` lists it). The
  configuration takes exactly `0x` and 8 digits, so the printed value is refused as it stands; pad
  it to `0x03636093`, or give the full 32-bit IDCODE with its revision nibble, `0x13636093`, which
  the tests and vectors use. That full value is not in a committed capture of this repository: it
  comes from an earlier JTAG read recorded in the maintainers' board notes. The Bridge prints the
  configured value and does not reinterpret it; record which one was configured.

### Host link (t27/fpga_link.t27)

Everything above the byte is t27: frames and CRC-32, the stream decoder (the loader's 20-byte
lines H A N C plus Y and Z, read-back frames, garbage), stop-and-wait with retransmission, the
chunk upload and read-back comparison, the activation and matvec commands, the Y and Z lines,
the accumulators and the counters. The OS hooks are C in `native/platform.c`:
`tm_os_serial_rate_ok` (1 when this host's termios has a constant for the rate),
`tm_os_serial_open` (8N1, raw, no flow control, non-blocking, exclusive, pending input
discarded; -1 for no path, busy, not a terminal, a rate the host cannot set, or a driver refusing
the settings), `tm_os_serial_read` (waits up to a timeout for the first byte, returns what is
there, 0 on timeout, -1 on error or hang-up; a signal does not end the wait), `tm_os_serial_write`
(all bytes, resuming partial writes, -1 when the time runs out), `tm_os_serial_drain`,
`tm_os_serial_close` and `tm_os_monotonic_ns`. The WebAssembly build of `platform.c` has stubs
that fail (-1, `tm_os_serial_rate_ok` 0, a clock of 0); no wasm module includes the link.

One `compute.dot`: (1) status exchange (protocol, build id, store size from the config word);
(2) the row-padded image of the tensor (below), uploaded in 4096-byte `L` frames each
acknowledged, then read back in 4096-byte `R` frames and compared byte for byte, unless the same
image (sha256, size, region) is known to be on the device; (3) the activations in `X` frames;
(4) one `M` run per 1024 rows, each collecting its Y and Z lines. Retransmission follows the
loader tool (`docs/uart-loader.md`): after a nak, or when no reply came `reply_timeout` (0.5 s by
default) after the later of the write's start plus its wire time and the write's return, the
host drains, stays quiet for `quiet` (0.15 s; replies arriving then are counted as late) and
sends the same frame (same seq, so a load whose ack was lost is answered `duplicate`); at most
`attempts` (8) times. A run's lines extend its deadline: after each line of the run the host
waits at least `reply_timeout` more. What a run's end means:

| The run | The host |
| --- | --- |
| nakked, nothing of it in time, a line with a bad check byte, a row missing, Z counters that disagree with the request (rows, words per row, words, result checksum) | repeats it with a new seq (up to `attempts` runs; then `-32000` "device matvec result incomplete or inconsistent") |
| Z status 2 (ended early: the bus words stopped, or abort) or stray words (Z8 > 0) | repeats it with a new seq, as above |
| Z status 1 (refused) | fails the call at once, `-32000` "device refused the matvec run": the same request would be refused again |
| accepted, but invalid codes (Z7 > 0) | an image the host built has none (dense5 bytes are at most 242, baseline2 lanes never 11), so the store no longer holds what was uploaded: the upload cache is forgotten, the image uploaded and read back again and the whole dot repeated, once (`store_reloads` 1); a second such run fails with `-32000` "device store does not hold the uploaded image" |

A store changed to other valid codes cannot show in the device's counters; it shows as rows under
`reference.mismatches`, and the cache is then forgotten for the next dot. The upload cache is also
forgotten when the port is reopened, when an `H` line (device reset) arrives, and when the
device's `bytes_committed` counter differs from the value it had right after this host's last
upload (someone else loaded, or the device was reset).

## Device matvec (#64)

`t27/rtl/fpga_ddr3_matvec.t27` is consumer (B) of #65: y = W x on the device, fed at bus-word
width by #62's reader. It is not wired into a board top yet; that and the device side of the
frames below are the next wave.

- **Input and handshake.** One 128-bit word per controller clock at most (`in_valid`, bytes 0-7
  in `in_lo`, 8-15 in `in_hi`). A word is taken in a clock where `in_valid` and `in_ready` are
  both high; `in_ready` is high exactly while the module is in RUN and still expects words. After
  `start`, SETUP takes one clock per word of a row (up to 1024) and MASK one more before RUN, so
  the producer must hold its words until `in_ready` rises (or start after it rose): a word offered
  while `in_ready` is low is not taken. `start` is acted on only while the module is idle; a
  start while it is busy is ignored. The review showed what the earlier version did (in_ready
  constant high, words before RUN thrown away, no timeout): a reader that started early, or a
  stream one word short, left the module in RUN for good with no Y or Z line. Now a run that takes
  no word for 65,536 clocks (786 us at 83.33 MHz, arithmetic), or whose `abort` input was high in
  any clock since `start`, ends with status 2: it takes nothing more, drains its pipeline, sends
  only its eleven Z lines and is idle again, so the next `start` runs.
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
  memory because a line takes 1.74 ms at 115200 baud (arithmetic: 20 bytes x 10 bits) and a
  2,560-column row takes at least 40 clocks in baseline2 or 32 in dense5 (one word per clock).
- **Counters (Z lines).**
  - 0 status: 0 ran; 1 refused (rows outside 1..1024, cols 0, format not 0 or 1, more than 1024
    words per row; the device's M handler also refuses a region past the store, below); 2 ended
    early (no word for 65,536 clocks in RUN, or abort). Refused and ended-early runs send only
    their Z lines.
  - 1 rows done, 2 words per row, 3 words taken. Words = rows x wpr holds by construction for a
    run that ran (it ends at its last row): the host checks it only as consistency of the lines
    it received.
  - 4 cycles (first word taken to last, inclusive) and 5 idle clocks (clocks of that span with no
    word offered: the bus starved the consumer, the memory-bound signal of #65). Every clock of
    that span either takes a word or is idle, so cycles = words + idle clocks by construction: an
    identity, not a second measurement; idle clocks carry the information.
  - 6 latency: clocks from the start of RUN to the first word taken ("setup" in the RTL comment
    is SETUP and MASK together).
  - 7 invalid codes (dense5 codes 243-255, baseline2 lanes 11, in any byte or lane of the words
    taken).
  - 8 stray words: clocks with `in_valid` while no run expects words (idle without `start`, after
    the last word, DRAIN, EMIT).
  - 9 consumer stalls: clocks with `in_valid` while a started run is not ready yet (the clock of
    `start`, SETUP, MASK): the producer was held off by `in_ready`. A measurement now (it was 0 by
    construction while `in_ready` was a constant).
  - 10 result checksum: rotate-left-1 xor of the rows' results at their write into the result
    memory; the host recomputes it from the Y lines, so a result memory read that differs from its
    write shows (it cannot show a wrong computation upstream of the write).

**Simulation (Icarus, `tests/test_ddr3_matvec.py`, every run below in
`reports/fpga/matvec-sim-2026-09-24-39f9a2d6.json`, RTL of commit 39f9a2d).** The module with the
real line emitter and a transmitter model, fed through `tests/tb_ddr3_matvec.v` with
pseudo-random gaps between words. "Bench" is the bench's own count of what it offered, independent
of the module; the identities above (cycles = words + idle, words = rows x wpr) are checked too
but are not listed as evidence.

| Case | Words | Result | Module's counts vs the bench's |
| --- | ---: | --- | --- |
| q_proj rows 0-319 dense5, 30 % gaps, seed 11 | 10,240 (32 per row) | 320 of 320 accumulators equal t27/matvec.t27's | idle 4,351, latency 2: equal |
| q_proj rows 0-319 baseline2, 30 % gaps, seed 12 | 12,800 (40 per row) | 320 of 320 equal | idle 5,497, latency 1: equal |
| 6,912 columns, 7 row pairs x 2 formats (all -128 / all +127 activations with all +1 / all -1 rows, alternating, random, zero), 20 % gaps | 174 (dense5) / 216 (baseline2) per pair | exact; -884,736, 884,736, 877,824 and -877,824 reached; garbage in the whole dense5 padding bytes and past the last activation not counted; an invalid code in a padding byte counted (1) | equal |
| +1 in every padding lane, activation 77 behind every padding lane: baseline2 3 x 65, 2 x 97, 4 x 1; dense5 2 x 6,912, 3 x 81, 2 x 83 (lanes in partly used bytes) | 4 to 174 | exact, 0 invalid codes | equal |
| limits: 1 x 81,920 dense5 (1,024 words per row); 1,024 x 16 dense5 (1,024 rows); 5 % gaps | 1,024 each | exact | equal (idle 48 in both) |
| one-word rows (37 and 80 columns), 81 and 65 columns (two words), 64 and 160 columns, 1 column; 0, 10, 30, 50 and 75 % gaps | 1 to 9 rows | exact | equal |
| a word in the clock of start, 3 while SETUP runs, 5 after the last word (4 x 2,560 baseline2) | 160 | exact | consumer stalls 4, stray words 5: equal |
| a reader that offers from the clock after start and holds each word until in_ready: 1 x 2,560 baseline2, 3 x 2,560 dense5 | 40 / 96 | exact, latency 0 | consumer stalls 41 / 33 (SETUP + MASK): equal |
| a stream of 5 of 6 words (3 x 160 dense5), then a second run | 5, then 6 | first run: status 2 after the idle limit, only its Z lines, 2 rows done, idle 65,537; second run exact | equal (second run) |
| abort after 50 of 160 words (4 x 2,560 baseline2), then a second run | 50, then 160 | first run: status 2, only its Z lines, 1 row done; second run exact | equal (second run) |
| refused: rows 0 and 1025, cols 0, format 2, 81,921 dense5 columns | - | only the Z lines, status 1 | - |

The reference of the q_proj rows: the host builds the images with `t27/fpga_link.t27` (equal to
the TMEM dense5 and baseline2 payloads of the chunk), the activations are `tmv_activations` seed
27 (2,560), and the expected accumulators are the first 320 of `t27/matvec.t27`'s product of the
whole q_proj, whose sha256 over all 2,560 equals `reports/ternary-check/matvec-2026-09-23.json`
(`hf_packed`). The 320 accumulators' sha256 (i64 little endian) is `a2366b57…` in both formats; the first eight are -2561, 2660, -52, -2385, 1269, 3446, 3451, -2365, the report's `first`. The gaps are the bench's pseudo-random choice; the idle clocks, latency,
stalls and stray words are the module's counts of that simulated stream, not a DDR3 measurement.
The record holds every figure of the table (42 runs, 119.5 s of Icarus); 884,736 = 128 x 6,912
and 877,824 = 127 x 6,912 are also the arithmetic the test asserts. The idle-limit run shows
65,537 idle clocks: the module ends the run in the clock after its counter of wordless clocks
reached 65,536, and that clock is idle too.

**Out-of-context estimates (`tools/fpga-matvec-ooc.py`, record in
`reports/fpga/matvec-ooc-2026-09-24-39f9a2d6/`, RTL of commit 39f9a2d).** Synthesis as the DDR3
designs (yosys 0.69, `synth_xilinx -flatten -abc9 -arch xc7`, carry chains and DSPs allowed) with
the 1K x 36 block RAM library, then nextpnr-xilinx 0.9.7 (heap, router2, seeds 1-5, `--freq
83.33`). These are yosys cell counts and nextpnr timing-model estimates in a harness
(`fpga/ax7203/matvec/tms_matvec_ooc.v`: a 389-bit input shift register and an XOR-reduced output
around the module), not a bitstream and not a board result. In the harness: 4,463 LUTs, 3,483
flip-flops (FDCE 3,140, FDPE 1, FDRE 342; the harness's shift register sits in the FDRE and in 2
SRLC32E), 555 CARRY4, 0 DSP48E1, 21 RAMB36E1 (yosys); nextpnr packs 8,282 SLICE_LUTX. The module
alone as the synthesis top, every output kept as a port (not placed): 4,709 LUTs, 3,317
flip-flops, 475 CARRY4, 0 DSP48E1, 21 RAMB36E1. Estimated Fmax after routing for the 83.33 MHz
clock: 106.94, 104.40, 100.06, 105.86 and 96.34 MHz for seeds 1-5 (all five meet it; 96.66-102.80
MHz after placement); the critical path of four seeds ends in the line check (`e_bits`), of seed 3
in the choice of the line's fields (`e_v`). The version before the review fixes (its record,
`reports/fpga/matvec-ooc-2026-09-24-8309e515/`, is in git history at 6adb4a0 and was replaced by
this one) estimated 104.98-112.45 MHz with 4,453 LUTs and 3,416 flip-flops in the same harness
minus the abort bit. Earlier versions of the module missed 83.33 MHz in the same flow (scratch
runs, not recorded here): the invalid-code sum sat in the decode stage, the line check shared a
clock with the choice of its fields, and the word sum took three adder levels in one stage; each
got its own stage.

## Wire protocol extension (matvec, protocol 4)

The loader's frames, lines, CRC-32 and ack/nak rules (`docs/uart-loader.md`) stay as they are.
The extension adds two host frames and two device lines; `tools/bridge_link_protocol.py` states
it in Python (`MatvecDevice` answers byte for byte), `t27/fpga_link.t27` is the host.

| Frame | cmd | addr | len, payload | Device answer |
| --- | --- | --- | --- | --- |
| activations `X` | 0x58 | first 80-byte activation block (entry of the ten banks) | a multiple of 8, 8 <= len <= 4080 (51 blocks), block + ceil(len / 80) <= 1024; the activation image bytes | `A` with command index 5 after the CRC matched and the bytes are in the banks; a repeat with the same seq, addr, len and CRC is answered `duplicate` and not written again |
| matvec `M` | 0x4D | region byte address, a multiple of 16, below the store's size | 12: rows u32, cols u32, format u32 (0 baseline2, 1 dense5), little endian | `A` with command index 6, then the run's `Y` lines and `Z` lines |

The activation image is `wpr` blocks of 80 bytes: block k byte i (i < lanes) is the activation of
column k x lanes + i, zero past the last column, bytes 64-79 zero for baseline2; block k goes to
entry k of the banks, bank b = bytes 8b .. 8b+7 of the block as one little-endian u64 (byte 8b + i
in bits 8i .. 8i+7, the order the module's `terms8` reads and the tests pack). The weight image is
the row-padded one (`tms_device_image_bytes = rows x wpr x 16`), loaded with ordinary `L` frames.

| Line | a | v |
| --- | --- | --- |
| `Y` (0x59) | `(seq << 24) \| row` | y[row], 32-bit two's complement |
| `Z` (0x5A) | `(seq << 24) \| (11 << 16) \| index` | the counter `index` (list above) |

Both carry the loader's check byte (low byte of the CRC-32 of tag, a, v). A refused run (status 1)
and a run that ended early (status 2) send only their eleven Z lines.

Rules for the device side (next wave), which the module alone cannot enforce:

- **Header checks** (nak `length`): `X` outside the bounds above; `M` with len other than 12, an
  address not a multiple of 16 or not below the store's size.
- **Region** (status 1): `M` whose region `addr .. addr + rows x wpr x 16` runs past the store is
  refused after its ack, with only the Z lines. The M handler checks it (it knows the store size;
  the module's own configuration check sees rows, cols and format only); the spec states it as
  `tms_device_region_fits`, and `MatvecDevice` applies it. The host never sends such a run
  (`tl_dot` checks the same bound first and answers `-32010`).
- **While a run is busy** (from `M` to the module's `done`): an `X` must not write the banks (it
  would change the activations under the run) and an `M` must not start the module (a start while
  busy is ignored); both are nakked `not_ready` (reason 9 of the DDR3 loader, protocol 3) and the
  host retransmits them after its quiet time.
- **The reader** starts over the region after the module's `in_ready` rose, or holds its words
  until it does; it must deliver exactly rows x wpr words. A reader that gives up (its watchdog)
  should also raise the module's `abort`, so the run ends at once rather than after the idle limit.
- **Protocol**: the config word of `H` and status line 19 reports 4 for a build with the
  extension; the host requires at least 4 (`min_protocol`). 4 is one above the DDR3 loader's
  protocol 3 (#63 part 2), which a matvec build carries underneath; a loader without the extension
  (protocol 2 or 3) is refused.
- `X` and `M` frames count in neither `frames_committed` nor `bytes_committed`: the host's upload
  cache relies on `bytes_committed` moving only with loads (`MatvecDevice` follows this rule).

## Upstream delta for #66 (not applied here)

gHashTag/t27 at 7e9de07d holds `specs/memory/tmem/bridge.t27`, identical to this repository's
`specs/memory/bridge.t27` before this change except line 2 (its path), and
`conformance/tmem_bridge.json`, byte-identical to this repository's `conformance/memory_bridge.json`
before this change (sha256 `9d3e0aad…`, 55 vectors). What #66 needs, nothing of which is sent from
here:

1. `specs/memory/tmem/bridge.t27`: the change of `specs/memory/bridge.t27` in this commit range,
   applied as is (the header lines on stage two, `enum BridgeTransport`, section 10's 33 constants,
   the 16 reference functions from `tms_bridge_backend_hardware` to `tms_device_accumulator_fits`,
   7 invariants and 4 test blocks), line 2 unchanged. The result's sha256 is `27ea45d6…`
   (computed here from that file; `spec_hash` of the new seal).
2. `conformance/tmem_bridge.json`: this repository's new `conformance/memory_bridge.json`
   (`tools/generate-spec-vectors.py`, sha256 `e80057f3…`): the 55 vectors unchanged, 8 fpga
   vectors (63 in all), `constants.fpga`, `sdk_adapter.fpga_chip_info_requires`, 6 more
   invariants (15 in all), and the replay keys `replay.backend`, `replay.result_format`,
   `replay.result_at_least` (lower bounds for counters that timing can raise) and
   `replay.error_message_contains` (which refusal, when several share `-32000`). The fpga vectors
   need a device double to replay; upstream has none, so either the double comes along
   (`tests/fake_fpga_device.py`, `tools/bridge_link_protocol.py`, `tools/uart_loader_protocol.py`)
   or upstream's replay skips vectors with a `backend` key and says so. Upstream's replay must
   also implement the two new expectation keys, or skip the vectors that use them.
3. The seal `tmem_TrinityMemoryBridgeSpec`: `t27c seal --save specs/memory/tmem/bridge.t27` with
   upstream's own t27c. Here, the pinned bff21b85 gives `gen_hash_c` `8f57d8f2…` for that file;
   upstream's compiler may generate other code, so its generated-code hashes are its own.

## Not yet on the board

- No bitstream contains the device matvec or the `X`/`M` frame handler; nothing of this section
  ran on the AX7203. Every result above is simulation (Icarus), a C check, a synthesis count or a
  place-and-route estimate.
- The host link has run only against the fake device on a pseudo-terminal and the OS hooks only on
  ptys (macOS here; Linux in CI). The paced fake (`FakeDevice(pace_baud=...)`, bytes at 10 / baud
  seconds each way, with the default deadlines at 115200 and 9600 baud) exercises the wire-time
  terms of the deadlines, but it is Python scheduling, not a UART. The CP2102N and its driver are
  untried with this code (the loader's Python tool reached them with pyserial): whether macOS
  `poll(2)`, whose manual says it "does not support devices", wakes on its data, and whether its
  driver honours TIOCEXCL, are open.
- The DDR3 loader (#63 part 2) reports 37 status lines at protocol 3 (the 23 of protocol 2 plus 14);
  the host's status exchange accepts only a set of 23 lines. A matvec build on top of it (protocol
  4) needs the host to accept that count: next wave, with the device side.
- `compute.dot` with `backend: fpga` and `hardware: true` for a real layer chunk on the board, and
  the committed captures with hashes, are #64's board part.
- The upload at 115200 baud: the loader tool moved 11.37 kB/s of payload (`docs/uart-loader.md`),
  so the chunk's 163,840-byte dense5 image would take about 14.4 s to load and as long to read back,
  and the 204,800-byte baseline2 image about 18 s each way (arithmetic from that rate, not
  measured with this link). Faster rates are not used by the link yet (no `B` frame).
- `reports/fpga/README.md` lists the two records of this section; if the parallel DDR3 loader
  branch renumbers its protocol, `TL_PROTO_MATVEC`, `TMS_DEVICE_PROTOCOL_MATVEC`,
  `bridge_link_protocol.PROTO` and `FpgaDevice`'s default move with it (one above the loader's).
