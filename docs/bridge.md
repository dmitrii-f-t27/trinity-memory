# Trinity Memory Bridge v1

The bridge transfers and validates real TMEM/TensorPack bytes over localhost
HTTP. Its storage and compute backend is an **in-process emulator**. It does
not access FPGA registers, claim device performance, execute a model, generate
an attestation, or submit a transaction.

Implementation: [`trinity_memory/bridge.py`](../trinity_memory/bridge.py).
The protocol and the server are implemented here; they have not been merged
into the upstream Rust `trinity-node`.

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
