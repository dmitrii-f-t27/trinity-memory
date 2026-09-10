"""Bounded TensorPack v1 container for named C-order ternary tensors.

TensorPack adds shape/scale metadata around unchanged TMEM v1 files. CRC32
detects accidental corruption; it does not authenticate files or their origin.
See docs/tensorpack.md for the byte layout and implementation limits.
"""

from collections.abc import Sequence
from dataclasses import dataclass
import json
import math
import struct
import zlib

from .codecs import CodecError, get_codec, payload_size
from .container import HEADER_BYTES as TMEM_HEADER_BYTES
from .container import PREFIX as TMEM_PREFIX
from .container import decode_file, encode_file


class TensorPackError(CodecError):
    """Malformed, unsupported, or oversized TensorPack input."""


@dataclass(frozen=True)
class Tensor:
    name: str
    shape: tuple[int, ...]
    values: tuple[int, ...]
    codec: str = "dense5"
    scales: tuple[float, ...] = (1.0,)
    scale_axis: int | None = None
    axes: tuple[str, ...] = ()


MAGIC = b"TTPK"
VERSION = 1
PREFIX = struct.Struct("<4sBBHIIQ")
CHECKSUMS = struct.Struct("<II")
HEADER_BYTES = PREFIX.size + CHECKSUMS.size
MAX_TENSORS = 1024
MAX_METADATA_BYTES = 1 << 20
MAX_PAYLOAD_BYTES = 64 << 20
MAX_TOTAL_TRITS = 4 << 20
MAX_RANK = 16
MAX_DIMENSION = (1 << 31) - 1
MAX_NAME_BYTES = 256
MAX_AXIS_BYTES = 64
MAX_JSON_DEPTH = 8
_FIELDS = {"name", "shape", "codec", "scales", "scale_axis", "axes", "offset", "length"}


def _fail(message: str) -> None:
    raise TensorPackError(message)


def _text(value: str, limit: int, label: str) -> None:
    if type(value) is not str or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        _fail(f"{label} must be a nonempty string without ASCII control characters")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _fail(f"{label} must be valid Unicode")
    if size > limit:
        _fail(f"{label} exceeds {limit} UTF-8 bytes")


def _metadata(name, shape, codec, scales, scale_axis, axes) -> int:
    _text(name, MAX_NAME_BYTES, "tensor name")
    if type(shape) is not tuple or len(shape) > MAX_RANK:
        _fail(f"shape must be a tuple with rank at most {MAX_RANK}")
    count = 1
    for dim in shape:
        if type(dim) is not int or not 1 <= dim <= MAX_DIMENSION:
            _fail("dimensions must be positive integers; zero dimensions are unsupported")
        count *= dim
        if count > MAX_TOTAL_TRITS:
            _fail("tensor exceeds the decoded trit limit")
    if type(codec) is not str:
        _fail("codec must be a string")
    try:
        get_codec(codec)
    except CodecError as exc:
        raise TensorPackError(str(exc)) from None
    if type(axes) is not tuple or (axes and len(axes) != len(shape)):
        _fail("axes must be empty or contain one label per dimension")
    for axis in axes:
        _text(axis, MAX_AXIS_BYTES, "axis label")
    if len(set(axes)) != len(axes):
        _fail("axis labels must be unique within a tensor")
    if type(scales) is not tuple or not scales:
        _fail("scales must be a nonempty tuple of positive finite floats")
    if scale_axis is None:
        expected_scales = 1
    else:
        if type(scale_axis) is not int or not 0 <= scale_axis < len(shape):
            _fail("scale_axis must be null or a nonnegative axis index within rank")
        expected_scales = shape[scale_axis]
    if len(scales) != expected_scales:
        _fail("scale count does not match scalar or per-axis scale metadata")
    if any(type(scale) is not float or not math.isfinite(scale) or scale <= 0 for scale in scales):
        _fail("scales must contain positive finite floats, excluding integers and booleans")
    return count


def _json_bytes(metadata: dict) -> bytes:
    return json.dumps(metadata, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"), sort_keys=True).encode("utf-8")


def encode_tensors(tensors: Sequence[Tensor]) -> bytes:
    """Encode validated tensors deterministically, preserving input tensor order."""
    if not isinstance(tensors, Sequence) or isinstance(tensors, (str, bytes, bytearray)):
        _fail("tensors must be a sequence of Tensor instances")
    if len(tensors) > MAX_TENSORS:
        _fail("too many tensors")
    entries, payloads, names = [], [], set()
    offset = total_count = 0
    for tensor in tensors:
        if not isinstance(tensor, Tensor):
            _fail("every item must be a Tensor instance")
        count = _metadata(tensor.name, tensor.shape, tensor.codec, tensor.scales,
                          tensor.scale_axis, tensor.axes)
        if tensor.name in names:
            _fail("duplicate tensor name")
        names.add(tensor.name)
        total_count += count
        if total_count > MAX_TOTAL_TRITS:
            _fail("TensorPack exceeds the total decoded trit limit")
        if type(tensor.values) is not tuple or len(tensor.values) != count:
            _fail("values must be a tuple with length equal to the shape product")
        try:
            payload = encode_file(tensor.values, tensor.codec)
        except CodecError as exc:
            raise TensorPackError(str(exc)) from None
        entries.append({"name": tensor.name, "shape": tensor.shape, "codec": tensor.codec,
                        "scales": tensor.scales, "scale_axis": tensor.scale_axis,
                        "axes": tensor.axes, "offset": offset, "length": len(payload)})
        payloads.append(payload)
        offset += len(payload)
        if offset > MAX_PAYLOAD_BYTES:
            _fail("TensorPack payload exceeds the byte limit")
    metadata = _json_bytes({"order": "C", "tensors": entries})
    if len(metadata) > MAX_METADATA_BYTES:
        _fail("TensorPack metadata exceeds the byte limit")
    payload = b"".join(payloads)
    prefix = PREFIX.pack(MAGIC, VERSION, 0, 0, len(metadata), len(entries), len(payload))
    checksums = CHECKSUMS.pack(zlib.crc32(metadata, zlib.crc32(prefix)), zlib.crc32(payload))
    return prefix + checksums + metadata + payload


def _object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate JSON object key")
        result[key] = value
    return result


def _integer(value: str) -> int:
    if len(value) > 20:
        _fail("JSON integer exceeds the supported length")
    return int(value)


def _constant(value: str):
    _fail("nonfinite JSON numbers are unsupported")


def _parse_metadata(data: bytes) -> dict:
    # Bound parser nesting before json.loads, including malformed JSON that
    # would otherwise exhaust the interpreter recursion limit.
    depth = 0
    in_string = escaped = False
    for byte in data:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                in_string = False
        elif byte == 34:
            in_string = True
        elif byte in (91, 123):
            depth += 1
            if depth > MAX_JSON_DEPTH:
                _fail("JSON nesting exceeds the supported depth")
        elif byte in (93, 125):
            depth -= 1
    try:
        metadata = json.loads(data.decode("utf-8"), object_pairs_hook=_object,
                              parse_int=_integer, parse_constant=_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, TensorPackError):
            raise
        raise TensorPackError("invalid UTF-8 JSON metadata") from None
    if type(metadata) is not dict or set(metadata) != {"order", "tensors"}:
        _fail("metadata must have exactly the order and tensors fields")
    if metadata["order"] != "C" or type(metadata["tensors"]) is not list:
        _fail("only C-order tensor lists are supported")
    return metadata


def _read(data: bytes, max_total_trits: int) -> tuple[list[Tensor], dict]:
    if type(max_total_trits) is not int or max_total_trits <= 0:
        _fail("max_total_trits must be a positive integer")
    max_total_trits = min(max_total_trits, MAX_TOTAL_TRITS)
    if not isinstance(data, (bytes, bytearray)) or len(data) < HEADER_BYTES:
        _fail("truncated TensorPack header (requires 32 bytes)")
    magic, version, flags, reserved, meta_size, tensor_count, payload_bytes = PREFIX.unpack_from(data)
    if magic != MAGIC or version != VERSION:
        _fail("invalid TensorPack magic or unsupported version")
    if flags or reserved:
        _fail("unsupported TensorPack flags or nonzero reserved field")
    if meta_size > MAX_METADATA_BYTES or payload_bytes > MAX_PAYLOAD_BYTES or tensor_count > MAX_TENSORS:
        _fail("TensorPack header exceeds implementation limits")
    if len(data) != HEADER_BYTES + meta_size + payload_bytes:
        _fail("TensorPack length mismatch, truncation, or trailing bytes")
    meta_crc, payload_crc = CHECKSUMS.unpack_from(data, PREFIX.size)
    payload_start = HEADER_BYTES + meta_size
    metadata_bytes = data[HEADER_BYTES:payload_start]
    payload = data[payload_start:]
    if zlib.crc32(metadata_bytes, zlib.crc32(data[:PREFIX.size])) != meta_crc:
        _fail("TensorPack metadata/header CRC32 mismatch")
    if zlib.crc32(payload) != payload_crc:
        _fail("TensorPack payload CRC32 mismatch")
    metadata = _parse_metadata(metadata_bytes)
    entries = metadata["tensors"]
    if len(entries) != tensor_count:
        _fail("tensor count does not match the metadata")
    names, validated = set(), []
    offset = total_count = 0
    # Validate every descriptor and total allocation bound before decoding any
    # TMEM trits, including cross-checks against each nested TMEM header.
    for entry in entries:
        if type(entry) is not dict or set(entry) != _FIELDS:
            _fail("tensor descriptor has missing or unknown fields")
        if any(type(entry[key]) is not list for key in ("shape", "scales", "axes")):
            _fail("shape, scales, and axes must be JSON arrays")
        count = _metadata(entry["name"], tuple(entry["shape"]), entry["codec"],
                          tuple(entry["scales"]), entry["scale_axis"], tuple(entry["axes"]))
        if entry["name"] in names:
            _fail("duplicate tensor name")
        names.add(entry["name"])
        total_count += count
        if total_count > max_total_trits:
            _fail("TensorPack exceeds the total decoded trit limit")
        length = TMEM_HEADER_BYTES + payload_size(count, entry["codec"])
        if (type(entry["offset"]) is not int or type(entry["length"]) is not int
                or entry["offset"] != offset or entry["length"] != length
                or offset + length > payload_bytes):
            _fail("tensor offsets or lengths are invalid, overlapping, or noncontiguous")
        inner = TMEM_PREFIX.unpack_from(payload, offset)
        if inner[2] != get_codec(entry["codec"]).id or inner[5] != count:
            _fail("TMEM codec/count does not match tensor metadata")
        validated.append((entry, count))
        offset += length
    if offset != payload_bytes:
        _fail("unreferenced payload bytes")
    tensors, descriptions = [], []
    for entry, count in validated:
        start, length = entry["offset"], entry["length"]
        try:
            values = tuple(decode_file(payload[start:start + length]))
        except CodecError as exc:
            raise TensorPackError(str(exc)) from None
        tensors.append(Tensor(entry["name"], tuple(entry["shape"]), values, entry["codec"],
                              tuple(entry["scales"]), entry["scale_axis"], tuple(entry["axes"])))
        descriptions.append({**entry, "count": count, "payload_bytes": length - TMEM_HEADER_BYTES})
    info = {"format": "TensorPack", "version": VERSION, "order": "C", "tensor_count": tensor_count,
            "total_count": total_count, "header_bytes": HEADER_BYTES, "metadata_bytes": meta_size,
            "payload_bytes": payload_bytes, "container_bytes": len(data), "validated": True,
            "tensors": descriptions}
    return tensors, info


def decode_tensors(data: bytes, *, max_total_trits: int = MAX_TOTAL_TRITS) -> list[Tensor]:
    """Validate framing, metadata, bounds, checksums, and every nested TMEM file."""
    return _read(data, max_total_trits)[0]


def inspect_tensorpack(data: bytes, *, max_total_trits: int = MAX_TOTAL_TRITS) -> dict:
    """Fully validate the pack, then report metadata and separate storage sizes."""
    return _read(data, max_total_trits)[1]
