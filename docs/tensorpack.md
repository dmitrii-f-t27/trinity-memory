# TensorPack v1

TensorPack stores named ternary tensors, their dimensions, optional axis labels,
and scalar or per-axis scales. Each tensor's values are an unchanged [TMEM v1
file](format.md). The Python reference implementation is
[`trinity_memory/tensorpack.py`](../trinity_memory/tensorpack.py); independent
framing and rejection fixtures are in
[`tests/test_tensorpack.py`](../tests/test_tensorpack.py).

This format preserves integer trits and supplied metadata. It does not quantize,
train, prune, or infer tensor scales. A stored tensor is not evidence of model
quality, compatibility with a model runtime, or physical memory performance.

## Specification

The layout and rules on this page are restated as the sealed specification
[`specs/memory/tensorpack.t27`](../specs/memory/tensorpack.t27): header offsets
and CRC coverage, the v1 limits, the exact root and descriptor member sets and
their sorted writer order, dimension, name, label, axis and scale rules, the
contiguous offset chain, nested TMEM lengths, the float presentation thresholds
and the TensorPack status codes. Its constant invariants compile as
`_Static_assert` and its test blocks execute in the generated C runner.
[`conformance/memory_tensorpack.json`](../conformance/memory_tensorpack.json)
carries golden containers (one per codec, a scalar, per-axis scales, axis
labels, float presentation, an empty pack) that the native encoder must
reproduce byte for byte, and rejected containers that both readers must refuse;
`tests/test_spec_tensorpack.py` replays them through the Python adapters and the
native CLI, and `tests/native_spec_tensorpack.c` ties the spec constants and
rules to `t27/tensorpack.t27` and `t27/tensorpack_json.t27`.

## Python API

```python
from trinity_memory.tensorpack import (
    Tensor, encode_tensors, decode_tensors, inspect_tensorpack,
)

weights = Tensor(
    name="projection.weight",
    shape=(2, 3),
    values=(-1, 0, 1, 1, 0, -1),
    codec="dense5",
    scales=(0.5, 0.25),
    scale_axis=0,
    axes=("output", "input"),
)
data = encode_tensors([weights])
assert decode_tensors(data) == [weights]
info = inspect_tensorpack(data)
```

`Tensor` is a frozen dataclass with fields in this order:
`name`, `shape`, `values`, `codec="dense5"`, `scales=(1.0,)`,
`scale_axis=None`, `axes=()`. Construction creates a record; encoding validates
it. Shape, values, scales, and axes must be tuples. Values must be exact Python
integers in `{-1, 0, 1}`. Booleans and float trits are rejected.

`encode_tensors(Sequence[Tensor]) -> bytes` preserves tensor order and emits
deterministic metadata. `decode_tensors(data) -> list[Tensor]` validates the
complete pack. `inspect_tensorpack(data) -> dict` performs the same full
validation, including decoding nested TMEM payloads, before returning metadata.
Both readers accept bytes or bytearray and an optional keyword
`max_total_trits=N`. The limit must be an exact positive integer; it can lower
the implementation's decoded trit limit but cannot raise it. Format, input,
and limit errors raise `TensorPackError`, a subclass of `CodecError`/`ValueError`.

## Tensor semantics

Only **C order** is supported: the last dimension varies fastest. The product
of the dimensions equals the number of trits. A scalar has `shape=()` and one
trit. Zero dimensions are rejected; zero-length tensors are not represented
in v1. An empty pack with no tensors is valid.

Each tensor name is nonempty and unique within the pack. Optional axis labels
are either absent (`axes=()`) or one unique, nonempty label per dimension.
Names and labels are valid Unicode, contain no ASCII control characters
`U+0000..U+001F` or `U+007F`, and obey the byte limits below. Names are metadata,
not paths to open or identifiers to execute.

Scales must be positive, finite Python floats, represented as JSON real
numbers, for example `1.0`. Integer scales, booleans, zero, negative values,
NaN, and infinity are rejected. With `scale_axis=None`, exactly one scale
applies to every value. Otherwise `scale_axis` is a nonnegative integer less
than rank, and there is exactly one scale per coordinate along that dimension.
For coordinates `(i0, ..., in)`, the nominal scaled value is the trit multiplied
by `scales[0]` or `scales[i_scale_axis]`. Scale application belongs to the
consumer; TensorPack stores trits and scales separately. Finite positive
binary64 scale values round-trip through the Python JSON representation.

All six existing codecs are supported. Sparse codecs retain their existing
block constraints over flattened C-order values, including blocks that cross
a row boundary. Encoding incompatible sparsity fails; no values are removed.

## Byte layout

All header integers are unsigned and little endian. The header is exactly
32 bytes; its first 24 bytes form the checksum prefix.

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 4 | Magic bytes `TTPK` |
| 4 | 1 | Version, `1` |
| 5 | 1 | Flags, must be zero |
| 6 | 2 | Reserved, must be zero |
| 8 | 4 | UTF-8 JSON metadata length in bytes |
| 12 | 4 | Tensor count |
| 16 | 8 | Total payload region length in bytes |
| 24 | 4 | CRC32 of `header[0:24] + metadata` |
| 28 | 4 | CRC32 of the complete payload region |
| 32 | metadata length | Metadata bytes, without BOM |
| following | payload length | Contiguous complete TMEM files |

CRC32 is the IEEE CRC used by `zlib.crc32`, with its default initial state. The
metadata checksum is equivalently
`zlib.crc32(metadata, zlib.crc32(header[:24]))`. The payload checksum is
`zlib.crc32(payload)`. Nested TMEM checksums are also verified. CRC32 detects
accidental corruption; it is **not authentication** and does not establish the
publisher's identity or protect against deliberate rewriting with new checksums.

Metadata has exactly two top-level fields: `order`, whose value is `"C"`, and
`tensors`, an array in payload order. Every descriptor has exactly these fields:

```json
{
  "order": "C",
  "tensors": [{
    "name": "projection.weight",
    "shape": [2, 3],
    "axes": ["output", "input"],
    "codec": "dense5",
    "scales": [0.5, 0.25],
    "scale_axis": 0,
    "offset": 0,
    "length": 26
  }]
}
```

`offset` is relative to the start of the payload region. `length` includes the
complete TMEM header and encoded payload. In this example, six dense5 trits
use two encoded bytes, so the TMEM length is `24 + 2 = 26` bytes. The first
offset is zero; every subsequent offset is the previous offset plus length.
Gaps, overlaps, reordered offsets, unused payload tails, and trailing file
bytes are rejected. Each TMEM codec and count must match its descriptor before
any trits are decoded. Inner reserved codes, nonzero padded trits, and nonzero
unused high bits remain invalid even with correctly recomputed checksums.

The writer uses UTF-8 JSON with sorted keys, no insignificant whitespace,
unescaped Unicode, and no nonfinite numbers. Readers accept insignificant JSON
whitespace and arbitrary key order but reject duplicate keys at every object
level, missing or unknown fields, unsupported order, invalid types, excessive
nesting, and invalid UTF-8. A format change requires a new version; v1 does not
silently ignore extension fields.

## Reference implementation limits and inspection

These are resource limits of this implementation, not a claim of hardware or
full-model capacity. All descriptors, offset bounds, nested counts, and the
aggregate decoded trit count are checked before decoding any values.

| Limit | Maximum |
| --- | --- |
| Tensors per pack | 1,024 |
| Metadata | 1,048,576 bytes (`1 << 20`) |
| Payload region | 67,108,864 bytes (`64 << 20`) |
| Total decoded trits | 4,194,304 (`4 << 20`) |
| Rank | 16 |
| Individual dimension | 2,147,483,647, also constrained by shape product |
| Tensor name / axis label | 256 / 64 UTF-8 bytes |
| JSON nesting / integer token length | 8 levels / 20 characters |

Inspection reports `format`, `version`, `order`, `tensor_count`, `total_count`,
`header_bytes`, `metadata_bytes`, `payload_bytes`, `container_bytes`,
`validated`, and `tensors`. Each tensor descriptor adds `count` and its raw
codec `payload_bytes`. At the pack level, `payload_bytes` includes all nested
TMEM headers. Thus:

```text
container_bytes = 32 + metadata_bytes + sum(24 + tensor.payload_bytes)
```

Report raw codec bytes and whole-file bytes separately. Neither measures FPGA
BRAM allocation, bus transfer padding, flash pages, or physical memory cells.

Run the standalone verification with:

```sh
python3 -m unittest discover -s tests -p 'test_tensorpack.py' -v
```

Tests cover independent binary framing, all codecs, scalar and mixed tensors,
per-axis metadata, exact round trips, corruption, malformed-but-resealed input,
schema and offset rejection, nested TMEM padding, and predecode resource limits.
