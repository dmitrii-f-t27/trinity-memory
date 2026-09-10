"""ctypes representation and OS boundary for generated t27 implementations.

The shared library is mandatory. Source checkouts may use a freshly built
``build/t27`` library; installed wheels carry platform binaries. There is no
Python implementation fallback.
"""
from __future__ import annotations
import ctypes as C
from functools import lru_cache
import json
import os
from pathlib import Path
import sys

U8 = C.POINTER(C.c_uint8)
I32 = C.POINTER(C.c_int32)
I64 = C.POINTER(C.c_int64)
F64 = C.POINTER(C.c_double)
SZ = C.c_size_t


class NativeLibraryError(RuntimeError):
    pass


def _candidates(name: str, environment: str):
    configured = os.environ.get(environment)
    if configured:
        yield Path(configured).expanduser().resolve()
        return
    package = Path(__file__).resolve().parent
    yield package / "_native_runtime" / name
    yield package.parent / "build" / "t27" / name


@lru_cache(maxsize=1)
def library():
    suffix = "dylib" if sys.platform == "darwin" else "so"
    for path in _candidates(f"libtrinity_memory_t27.{suffix}", "TRINITY_MEMORY_NATIVE_LIBRARY"):
        if path.is_file():
            try:
                return C.CDLL(str(path))
            except OSError as error:
                raise NativeLibraryError(f"Cannot load native t27 library {path}: {error}") from error
    raise NativeLibraryError("Native t27 library is missing. Install a platform wheel or run tools/build-t27.sh with T27_ROOT set. No Python fallback is available.")


def executable() -> Path:
    for path in _candidates("trinity-memory-t27", "TRINITY_MEMORY_NATIVE_CLI"):
        if path.is_file():
            return path
    raise NativeLibraryError("Native t27 CLI is missing. Install a platform wheel or rebuild native artifacts.")


@lru_cache(maxsize=None)
def function(name, result, arguments):
    try:
        value = getattr(library(), name)
    except AttributeError as error:
        raise NativeLibraryError(f"Native t27 library lacks {name}; rebuild the matching source revision") from error
    value.restype, value.argtypes = result, arguments
    return value


def call(name, result, arguments, *values):
    return function(name, result, tuple(arguments))(*values)


def octets(value: bytes | bytearray):
    return (C.c_uint8 * max(1, len(value))).from_buffer_copy(bytes(value) if value else b"\0")


def buffer(capacity: int):
    return (C.c_uint8 * max(1, capacity))()


def integer_array(values, bits=32, error=ValueError):
    items = list(values)
    low, high = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    if any(type(value) is not int or not low <= value <= high for value in items):
        raise error(f"Expected integers representable by signed {bits}-bit FFI lanes")
    scalar = C.c_int32 if bits == 32 else C.c_int64
    return items, (scalar * max(1, len(items)))(*items)


def real_array(values, error=ValueError):
    items = list(values)
    if any(isinstance(value, bool) or not isinstance(value, (float, int)) for value in items):
        raise error("Expected real-number FFI lanes")
    try:
        converted = [float(value) for value in items]
    except (OverflowError, ValueError) as exc:
        raise error("Real value cannot be represented by native f64") from exc
    return items, (C.c_double * max(1, len(items)))(*converted)


def checked_size(value, name="size", minimum=0):
    if type(value) is not int or not minimum <= value <= sys.maxsize:
        raise ValueError(f"{name} must be an integer in [{minimum}, {sys.maxsize}]")
    return value


class JsonToken(C.Structure):
    _fields_ = [("kind", C.c_int32), ("first", C.c_int64), ("next", C.c_int64),
                ("text", U8), ("text_size", SZ), ("integer", C.c_int64), ("real", C.c_double),
                ("uinteger", C.c_uint64), ("negative", C.c_bool), ("integer_fits_i64", C.c_bool)]


class JsonWriter(C.Structure):
    _fields_ = [("data", U8), ("capacity", SZ), ("used", SZ), ("error", C.c_int32)]


class RPCWorkspace(C.Structure):
    _fields_ = [("tokens", C.POINTER(JsonToken)), ("token_capacity", SZ), ("arena", U8),
                ("arena_capacity", SZ), ("root", C.c_int64), ("token_count", SZ)]


class RPCReply(C.Structure):
    _fields_ = [("result_index", C.c_int64), ("error_index", C.c_int64), ("remote_error", C.c_bool)]


def json_bytes(value) -> bytes:
    # Python-object representation only; native parser validates JSON syntax,
    # types, duplicate fields, limits and the applicable protocol schema.
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()


def tree_value(work, index):
    capacity = max(1024, work.contents.arena_capacity * 6 + work.contents.token_count * 32)
    output = buffer(capacity)
    writer = JsonWriter(output, capacity, 0, 0)
    status = call("tm_json_write_tree", C.c_int32,
                  [C.POINTER(JsonWriter), C.POINTER(JsonToken), SZ, C.c_int64],
                  C.byref(writer), work.contents.tokens, work.contents.token_count, index)
    if status:
        raise ValueError("Native JSON representation export failed")
    return json.loads(bytes(output[:writer.used]))


def rpc_workspace(capacity):
    result = call("tm_runtime_rpc_new", C.POINTER(RPCWorkspace), [SZ], capacity)
    if not result:
        raise MemoryError("Cannot allocate native JSON workspace")
    return result


def rpc_free(work):
    call("tm_runtime_rpc_free", None, [C.POINTER(RPCWorkspace)], work)


def strict_json(data: bytes):
    encoded = octets(data)
    work = rpc_workspace(len(data) + 1)
    try:
        status = call("tm_rpc_parse", C.c_int32, [U8, SZ, C.POINTER(RPCWorkspace)], encoded, len(data), work)
        if status:
            raise ValueError("Invalid JSON or native parser bounds exceeded")
        return tree_value(work, work.contents.root)
    finally:
        rpc_free(work)


def tensor_workspace(capacity, max_trits=4194304):
    result = call("tm_runtime_tensor_new", C.c_void_p, [SZ, SZ], capacity, max_trits)
    if not result:
        raise MemoryError("Cannot allocate native TensorPack workspace")
    return result


def tensor_free(work):
    call("tm_runtime_tensor_free", None, [C.c_void_p], work)


def base64_encode(data):
    encoded = octets(data)
    capacity = 4 * ((len(data) + 2) // 3)
    output = buffer(capacity)
    writer = JsonWriter(output, capacity, 0, 0)
    call("tm_bridge_b64_encode", None, [C.POINTER(JsonWriter), U8, SZ], C.byref(writer), encoded, len(data))
    if writer.error:
        raise ValueError("Native base64 encoding failed")
    return bytes(output[:writer.used]).decode("ascii")


def base64_decode(text):
    data = text.encode("utf-8")
    encoded, output = octets(data), buffer(len(data))
    count = call("tm_bridge_b64_decode", C.c_int64, [U8, SZ, U8, SZ], encoded, len(data), output, len(data))
    if count < 0:
        raise ValueError("Invalid or noncanonical base64 data")
    return bytes(output[:count])
