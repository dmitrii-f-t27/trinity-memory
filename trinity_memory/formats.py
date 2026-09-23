"""ctypes bindings for t27/formats.t27: external ternary weight-packing formats.

Container parsing, decoding, ternarization and comparison run in generated t27
code; this module only moves bytes and wraps results. Value arrays stay ctypes
arrays of int32 so that tensors of tens of millions of weights are not copied
into Python objects.
"""
from __future__ import annotations
import ctypes as C
from . import _native as n

TQ1_0, TQ2_0, Q2_0, Q1_0, PQ2_0, PTQ1_0, I2_S, HF_PACKED, LINEAR2 = range(1, 10)
NAMES = {TQ1_0: "TQ1_0", TQ2_0: "TQ2_0", Q2_0: "Q2_0", Q1_0: "Q1_0", PQ2_0: "PQ2_0",
         PTQ1_0: "PTQ1_0", I2_S: "I2_S", HF_PACKED: "HF packed", LINEAR2: "linear 2-bit"}
ERRORS = {-50: "unknown format", -51: "length", -52: "capacity", -53: "code outside the format",
          -54: "nonzero padding", -55: "truncated", -56: "malformed container", -57: "not found"}
TRUNCATED, NOT_FOUND = -55, -57

U32 = C.POINTER(C.c_uint32)
U64 = C.c_uint64


class FormatError(ValueError):
    def __init__(self, status):
        super().__init__(ERRORS.get(status, f"status {status}"))
        self.status = status


class TensorInfo(C.Structure):
    _fields_ = [("tensor_type", C.c_uint32), ("dims", C.c_uint32), ("d0", U64), ("d1", U64),
                ("d2", U64), ("d3", U64), ("data_start", U64), ("offset", U64), ("alignment", U64),
                ("needed", U64), ("prism", C.c_bool), ("name_at", U64), ("name_size", U64),
                ("tensors", U64)]

    @property
    def shape(self):
        """ggml order: ne[0] (contiguous) first."""
        return [self.d0, self.d1, self.d2, self.d3][: self.dims]


class SafeInfo(C.Structure):
    _fields_ = [("dtype", n.U8), ("dtype_size", n.SZ), ("dims", n.SZ), ("d0", U64), ("d1", U64),
                ("d2", U64), ("d3", U64), ("begin", U64), ("end", U64), ("needed", U64)]

    @property
    def shape(self):
        return [self.d0, self.d1, self.d2, self.d3][: self.dims]


def _check(status):
    if status < 0:
        raise FormatError(status)
    return status


def _values(count):
    return (C.c_int32 * max(1, count))()


def gguf_find(prefix: bytes, name: str):
    """(status, TensorInfo). TRUNCATED: read info.needed bytes and retry."""
    info = TensorInfo()
    data, key = n.octets(prefix), n.octets(name.encode())
    status = n.call("tf_gguf_find", C.c_int32, [n.U8, n.SZ, n.U8, n.SZ, C.POINTER(TensorInfo)],
                    data, len(prefix), key, len(name.encode()), C.byref(info))
    return status, info


def gguf_nth(prefix: bytes, index: int):
    info = TensorInfo()
    status = n.call("tf_gguf_nth", C.c_int32, [n.U8, n.SZ, U64, C.POINTER(TensorInfo)],
                    n.octets(prefix), len(prefix), index, C.byref(info))
    name = prefix[info.name_at: info.name_at + info.name_size].decode() if status == 0 else None
    return status, info, name


def gguf_names(prefix: bytes):
    """Every tensor record of a complete GGUF header: (name, TensorInfo)."""
    data, out, index = n.octets(prefix), [], 0
    fn = n.function("tf_gguf_nth", C.c_int32, (n.U8, n.SZ, U64, C.POINTER(TensorInfo)))
    while True:
        info = TensorInfo()
        status = fn(data, len(prefix), index, C.byref(info))
        if status == NOT_FOUND:
            return out
        _check(status)
        out.append((prefix[info.name_at: info.name_at + info.name_size].decode(), info))
        index += 1


def format_of_ggml(tensor_type: int, prism: bool) -> int:
    return n.call("tf_format_of_ggml", C.c_int32, [C.c_uint32, C.c_bool], tensor_type, prism)


def gguf_tensor_bytes(info: TensorInfo) -> int:
    return n.call("tf_gguf_tensor_bytes", U64, [C.POINTER(TensorInfo)], C.byref(info))


def safetensors_find(prefix: bytes, name: str):
    """(status, SafeInfo, dtype). The JSON workspace is sized from the header."""
    header = int.from_bytes(prefix[:8], "little") if len(prefix) >= 8 else 0
    token_capacity = max(1024, header // 3)
    tokens = (n.JsonToken * token_capacity)()
    arena = n.buffer(max(1024, header))
    info = SafeInfo()
    key = name.encode()
    status = n.call("tf_safetensors_find", C.c_int32,
                    [n.U8, n.SZ, n.U8, n.SZ, C.POINTER(n.JsonToken), n.SZ, n.U8, n.SZ, C.POINTER(SafeInfo)],
                    n.octets(prefix), len(prefix), n.octets(key), len(key), tokens, token_capacity,
                    arena, len(arena), C.byref(info))
    dtype = bytes(info.dtype[: info.dtype_size]).decode() if status == 0 else None
    return status, info, dtype


def block_geometry(fmt: int):
    per = n.call("tf_block_elements", n.SZ, [C.c_int32], fmt)
    size = n.call("tf_block_bytes", n.SZ, [C.c_int32], fmt)
    return per, size


def decode_blocks(fmt: int, data: bytes, count: int):
    """(values, raw fp16 scale words, count outside {-1,0,+1})."""
    per, _ = block_geometry(fmt)
    blocks = count // per if per else 0
    values, scales = _values(count), (C.c_uint32 * max(1, blocks))()
    outside = _check(n.call("tf_decode_blocks", C.c_int64,
                            [C.c_int32, n.U8, n.SZ, n.SZ, n.I32, n.SZ, U32, n.SZ],
                            fmt, n.octets(data), len(data), count, values, count, scales, blocks))
    return values, list(scales[:blocks]), outside


def decode_i2s(data: bytes, count: int):
    values, scale = _values(count), (C.c_uint32 * 1)()
    outside = _check(n.call("tf_decode_i2s", C.c_int64, [n.U8, n.SZ, n.SZ, n.I32, n.SZ, U32],
                            n.octets(data), len(data), count, values, count, scale))
    return values, scale[0], outside


def i2s_trailer_nonzero(data: bytes, count: int) -> int:
    return _check(n.call("tf_i2s_trailer_nonzero", C.c_int64, [n.U8, n.SZ, n.SZ], n.octets(data), len(data), count))


def decode_hf_packed(data: bytes, rows: int, cols: int):
    values = _values(rows * cols)
    outside = _check(n.call("tf_decode_hf_packed", C.c_int64, [n.U8, n.SZ, n.SZ, n.SZ, n.I32, n.SZ],
                            n.octets(data), len(data), rows, cols, values, rows * cols))
    return values, outside


def decode_linear2(data: bytes, rows: int, cols: int, row_bytes: int, zero_point: int):
    values = _values(rows * cols)
    outside = _check(n.call("tf_decode_linear2", C.c_int64,
                            [n.U8, n.SZ, n.SZ, n.SZ, n.SZ, C.c_int32, n.I32, n.SZ],
                            n.octets(data), len(data), rows, cols, row_bytes, zero_point, values, rows * cols))
    return values, outside


def absmean_bf16(data: bytes, count: int, margin: float = 1e-5):
    """(values, boundary count, mean |w|, s = 1/mean) of BitNet's WeightQuant."""
    values, result = _values(count), (C.c_double * 2)()
    boundary = _check(n.call("tf_absmean_bf16", C.c_int64, [n.U8, n.SZ, C.c_double, n.I32, n.SZ, n.F64],
                             n.octets(data), count, margin, values, count, result))
    return values, boundary, result[0], result[1]


def mismatches(a, b, count: int):
    first = (C.c_int64 * 1)()
    total = n.call("tf_mismatches", C.c_int64, [n.I32, n.I32, n.SZ, n.I64], a, b, count, first)
    return total, first[0]


def f16_value(bits: int) -> float:
    return n.call("tf_f16_value", C.c_double, [C.c_uint32], bits)


def bf16_value(bits: int) -> float:
    return n.call("tf_bf16_value", C.c_double, [C.c_uint32], bits)


def f32_value(bits: int) -> float:
    return n.call("tf_f32_value", C.c_double, [C.c_uint32], bits)


F16, BF16, F32 = 1, 2, 3
KIND_OF_DTYPE = {"F16": F16, "BF16": BF16, "F32": F32}


def words(data: bytes, width: int):
    count = len(data) // width
    out = (C.c_uint32 * max(1, count))()
    _check(n.call("tf_words", C.c_int64, [n.U8, n.SZ, n.SZ, U32, n.SZ],
                  n.octets(data), len(data), width, out, count))
    return out, count


def compare_scales(a, a_count, a_kind, a_group, b, b_count, b_kind, b_group, weights):
    """Weights whose scales differ between two layouts, and the first of them."""
    first = (C.c_int64 * 1)()
    if not isinstance(a, C.Array):
        a = (C.c_uint32 * max(1, len(a)))(*a)
    if not isinstance(b, C.Array):
        b = (C.c_uint32 * max(1, len(b)))(*b)
    total = _check(n.call("tf_compare_scales", C.c_int64,
                          [U32, n.SZ, C.c_int32, n.SZ, U32, n.SZ, C.c_int32, n.SZ, n.SZ, n.I64],
                          a, a_count, a_kind, a_group, b, b_count, b_kind, b_group, weights, first))
    return total, first[0]


def affine_not_ternary(scales, biases, count, kind):
    return n.call("tf_affine_not_ternary", C.c_int64, [U32, U32, n.SZ, C.c_int32], scales, biases, count, kind)
