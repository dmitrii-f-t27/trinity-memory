"""ctypes bindings for t27/formats.t27: external ternary weight-packing formats.

Container parsing, decoding, encoding, checks, ternarization and comparison
run in generated t27 code; this module only moves bytes and wraps results.
Value arrays stay ctypes arrays of int32 so that tensors of tens of millions of
weights are not copied into Python objects. Status classes and flag slots are
listed in specs/formats/OWNERS.md.
"""
from __future__ import annotations
import ctypes as C
from . import _native as n

TQ1_0, TQ2_0, Q2_0, Q1_0, PQ2_0, PTQ1_0, I2_S, HF_PACKED, LINEAR2, ONNX2 = range(1, 11)
NAMES = {TQ1_0: "TQ1_0", TQ2_0: "TQ2_0", Q2_0: "Q2_0", Q1_0: "Q1_0", PQ2_0: "PQ2_0",
         PTQ1_0: "PTQ1_0", I2_S: "I2_S", HF_PACKED: "HF packed", LINEAR2: "MLX 2-bit",
         ONNX2: "ONNX MatMulNBits 2-bit"}
ERRORS = {-50: "unknown format", -51: "length", -52: "capacity", -53: "code outside the format",
          -54: "nonzero padding", -55: "truncated", -56: "malformed container", -57: "not found",
          -58: "non-finite scale", -59: "ambiguous GGUF type id", -60: "layout without a storage contract",
          -61: "misaligned tensor offset",
          -62: "tensor bytes run into another tensor or past the end of the file, or do not match its shape"}
TOKENS = {-50: "format", -51: "length", -52: "capacity", -53: "code", -54: "padding", -55: "truncated",
          -56: "container", -57: "not_found", -58: "scale_nonfinite", -59: "type_ambiguous",
          -60: "layout_unsupported", -61: "misaligned", -62: "extent"}
FLAGS = ("outside_ternary", "noncanonical_base3", "scale_negative", "scale_zero", "trailer_nonzero",
         "padding_nonzero", "affine_not_ternary")
TRUNCATED, NOT_FOUND = -55, -57

U32 = C.POINTER(C.c_uint32)
U64 = C.c_uint64


class FormatError(ValueError):
    def __init__(self, status):
        super().__init__(ERRORS.get(status, f"status {status}"))
        self.status = status
        self.token = TOKENS.get(status)


class TensorInfo(C.Structure):
    _fields_ = [("tensor_type", C.c_uint32), ("dims", C.c_uint32), ("d0", U64), ("d1", U64),
                ("d2", U64), ("d3", U64), ("data_start", U64), ("offset", U64), ("alignment", U64),
                ("needed", U64), ("prism", C.c_bool), ("name_at", U64), ("name_size", U64),
                ("tensors", U64), ("bitnet", C.c_bool), ("next_offset", U64), ("has_next", C.c_bool),
                ("prev_end", U64), ("has_prev", C.c_bool)]

    @property
    def shape(self):
        """ggml order: ne[0] (contiguous) first."""
        return [self.d0, self.d1, self.d2, self.d3][: self.dims]


class SafeInfo(C.Structure):
    _fields_ = [("dtype", n.U8), ("dtype_size", n.SZ), ("dims", n.SZ), ("d0", U64), ("d1", U64),
                ("d2", U64), ("d3", U64), ("begin", U64), ("end", U64), ("needed", U64),
                ("dtype_bits", U64), ("next_begin", U64), ("has_next", C.c_bool),
                ("prev_end", U64), ("has_prev", C.c_bool)]

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


def format_of_gguf(tensor_type: int, prism: bool, bitnet: bool) -> int:
    """Format id under the file's namespace markers, 0, or a negative status."""
    return n.call("tf_format_of_gguf", C.c_int32, [C.c_uint32, C.c_bool, C.c_bool], tensor_type, prism, bitnet)


def gguf_check(info: TensorInfo, file_size: int) -> int:
    """Format id of a found tensor after the type-id, alignment, row and extent rules; raises on rejection."""
    return _check(n.call("tf_gguf_check", C.c_int32, [C.POINTER(TensorInfo), U64], C.byref(info), file_size))


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


def safetensors_check(info: SafeInfo, file_size: int) -> int:
    """0 when a found tensor's dtype, byte range and neighbours fit its shape and the file; raises otherwise."""
    return _check(n.call("tf_safetensors_check", C.c_int32, [C.POINTER(SafeInfo), U64], C.byref(info), file_size))


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


def _flags():
    return (C.c_int64 * len(FLAGS))()


def _flag_dict(flags):
    return {token: flags[slot] for slot, token in enumerate(FLAGS)}


def _int32s(values):
    return values if isinstance(values, C.Array) else n.integer_array(values, 32)[1]


def _words(words):
    return words if isinstance(words, C.Array) else (C.c_uint32 * max(1, len(words)))(*words)


def scale_class(word: int, kind: int) -> int:
    """0 positive, 1 zero, 2 negative, or -58 for NaN and infinity."""
    return n.call("tf_scale_class", C.c_int32, [C.c_uint32, C.c_int32], word, kind)


def scales_check(words, kind: int) -> dict:
    """Flags of scale words (scale_zero, scale_negative); raises on a non-finite word."""
    flags = _flags()
    _check(n.call("tf_scales_check", C.c_int64, [U32, n.SZ, C.c_int32, n.I64], _words(words), len(words), kind, flags))
    return _flag_dict(flags)


def block_flags(fmt: int, data: bytes, count: int) -> dict:
    flags = _flags()
    _check(n.call("tf_block_flags", C.c_int64, [C.c_int32, n.U8, n.SZ, n.SZ, n.I64],
                  fmt, n.octets(data), len(data), count, flags))
    return _flag_dict(flags)


def encode_blocks(fmt: int, values, scale_words) -> bytes:
    per, size = block_geometry(fmt)
    count = len(values)
    capacity = (count // per) * size if per else 0
    out = n.buffer(capacity)
    written = _check(n.call("tf_encode_blocks", C.c_int64, [C.c_int32, n.I32, n.SZ, U32, n.SZ, n.U8, n.SZ],
                            fmt, _int32s(values), count, _words(scale_words), len(scale_words), out, capacity))
    return bytes(out[:written])


def i2s_flags(data: bytes, count: int) -> dict:
    flags = _flags()
    _check(n.call("tf_i2s_flags", C.c_int64, [n.U8, n.SZ, n.SZ, n.I64], n.octets(data), len(data), count, flags))
    return _flag_dict(flags)


def encode_i2s(values, scale_word: int) -> bytes:
    count = len(values)
    capacity = count // 4 + 32
    out = n.buffer(capacity)
    written = _check(n.call("tf_encode_i2s", C.c_int64, [n.I32, n.SZ, C.c_uint32, n.U8, n.SZ],
                            _int32s(values), count, scale_word, out, capacity))
    return bytes(out[:written])


def encode_hf_packed(values, rows: int, cols: int) -> bytes:
    capacity = rows // 4 * cols
    out = n.buffer(capacity)
    written = _check(n.call("tf_encode_hf_packed", C.c_int64, [n.I32, n.SZ, n.SZ, n.U8, n.SZ],
                            _int32s(values), rows, cols, out, capacity))
    return bytes(out[:written])


def encode_linear2(values, rows: int, cols: int, row_bytes: int, zero_point: int) -> bytes:
    capacity = rows * row_bytes
    out = n.buffer(capacity)
    written = _check(n.call("tf_encode_linear2", C.c_int64, [n.I32, n.SZ, n.SZ, n.SZ, C.c_int32, n.U8, n.SZ],
                            _int32s(values), rows, cols, row_bytes, zero_point, out, capacity))
    return bytes(out[:written])


def decode_mlx2(data: bytes, rows: int, cols: int, group: int):
    """(values, count of code 3) of MLX 2-bit words; the trit is q - 1."""
    values = _values(rows * cols)
    outside = _check(n.call("tf_decode_mlx2", C.c_int64, [n.U8, n.SZ, n.SZ, n.SZ, n.SZ, n.I32, n.SZ],
                            n.octets(data), len(data), rows, cols, group, values, rows * cols))
    return values, outside


def encode_mlx2(values, rows: int, cols: int, group: int) -> bytes:
    capacity = rows * cols // 4
    out = n.buffer(capacity)
    written = _check(n.call("tf_encode_mlx2", C.c_int64, [n.I32, n.SZ, n.SZ, n.SZ, n.U8, n.SZ],
                            _int32s(values), rows, cols, group, out, capacity))
    return bytes(out[:written])


def affine_check(scales, biases, kind: int) -> dict:
    """Flags of MLX groups (scale_zero, scale_negative, affine_not_ternary); raises on non-finite words."""
    flags = _flags()
    _check(n.call("tf_affine_check", C.c_int64, [U32, U32, n.SZ, C.c_int32, n.I64],
                  _words(scales), _words(biases), len(scales), kind, flags))
    return _flag_dict(flags)


def decode_onnx2(data: bytes, n_rows: int, k: int, block_size: int, zero_points: bytes = b""):
    """(values, count outside {-1, 0, +1}) of MatMulNBits bits=2; default zero point 2."""
    values = _values(n_rows * k)
    outside = _check(n.call("tf_decode_onnx2", C.c_int64,
                            [n.U8, n.SZ, n.SZ, n.SZ, n.SZ, n.U8, n.SZ, n.I32, n.SZ],
                            n.octets(data), len(data), n_rows, k, block_size, n.octets(zero_points),
                            len(zero_points), values, n_rows * k))
    return values, outside


def encode_onnx2(values, n_rows: int, k: int, block_size: int, zero_points: bytes = b"") -> bytes:
    capacity = n_rows * (-(-k // block_size) * block_size // 4) if block_size else 0
    out = n.buffer(capacity)
    written = _check(n.call("tf_encode_onnx2", C.c_int64, [n.I32, n.SZ, n.SZ, n.SZ, n.U8, n.SZ, n.U8, n.SZ],
                            _int32s(values), n_rows, k, block_size, n.octets(zero_points), len(zero_points),
                            out, capacity))
    return bytes(out[:written])


def onnx2_padding_nonzero(data: bytes, n_rows: int, k: int, block_size: int) -> int:
    return _check(n.call("tf_onnx2_padding_nonzero", C.c_int64, [n.U8, n.SZ, n.SZ, n.SZ, n.SZ],
                         n.octets(data), len(data), n_rows, k, block_size))
