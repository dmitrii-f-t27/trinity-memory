"""ctypes bindings for t27/matrix.t27: the cells of the Ternary Check matrix (issue #32).

Representability, scale conversion, the round trips through the t27 writers
and readers, the trit and scale comparisons and their explanations run in
generated t27 code; this module only allocates buffers and names the results.
The status, explanation and reason tokens mirror the TMX_* constants of the
generated header (tests/test_ternary_check.py checks them against it).
"""
from __future__ import annotations
import ctypes as C

from . import _native as n
from . import formats as f

MATCH, MISMATCH, NOT_REPRESENTABLE = 0, 1, 2
STATUS = {MATCH: "match", MISMATCH: "mismatch", NOT_REPRESENTABLE: "not-representable"}
EXPLAIN = {0: None, 1: "scale_bf16_rounding", 2: "tie_split", 3: "scale_zero_weights", 4: "unexplained"}
REASONS = ("shape", "binary_only", "code_outside", "group_scales_differ", "scale_precision")
REPRESENTABLE = -1
RESULT = ("status", "trits_differ", "trits_first", "trits_explain", "scales_differ", "scales_first",
          "scales_explain", "reason", "outside", "code_bytes", "stored_bytes")
TIES = ("tie_word", "pos_nonzero", "pos_zero", "neg_nonzero", "neg_zero", "differ_at_ties", "differ_elsewhere",
        "first_elsewhere", "derived_nonzero")
LOCATE = ("byte", "position", "width", "scale_byte")
U32 = f.U32


def _check(status):
    if status < 0:
        raise f.FormatError(status)
    return status


def words(values):
    """A ctypes u32 array of scale words (kept as is when it already is one)."""
    return values if isinstance(values, C.Array) else (C.c_uint32 * max(1, len(values)))(*values)


def _result():
    return (C.c_int64 * len(RESULT))()


def _reasons():
    return (C.c_int64 * (2 * len(REASONS)))()


def result_dict(result) -> dict:
    return {name: result[slot] for slot, name in enumerate(RESULT)}


def reasons_list(reasons) -> list:
    """The reasons that apply, in the order t27 checks them: (token, count, first)."""
    return [(token, reasons[2 * slot], reasons[2 * slot + 1]) for slot, token in enumerate(REASONS)
            if reasons[2 * slot] > 0]


def float_bits(value: float, kind: int) -> int:
    return n.call("tmx_float_bits", C.c_int64, [C.c_double, C.c_int32], value, kind)


def bf16_round(word: int) -> int:
    return n.call("tmx_bf16_round", C.c_uint32, [C.c_uint32], word)


def bf16_half(word: int) -> int:
    return n.call("tmx_bf16_half", C.c_int64, [C.c_uint32], word)


def scale_count(rows: int, cols: int, group: int) -> int:
    return n.call("tmx_scale_count", n.SZ, [n.SZ, n.SZ, n.SZ], rows, cols, group)


def scale_index(i: int, cols: int, group: int) -> int:
    return n.call("tmx_scale_index", n.SZ, [n.SZ, n.SZ, n.SZ], i, cols, group)


def scale_kind(fmt: int) -> int:
    return n.call("tmx_scale_kind", C.c_int32, [C.c_int32], fmt)


def scale_group(fmt: int, group: int) -> int:
    return n.call("tmx_scale_group", n.SZ, [C.c_int32, n.SZ], fmt, group)


def representable(fmt, rows, cols, group, values, scales, kind, scale_grp):
    """(first reason token or None, [(token, count, first), ...])."""
    scales = words(scales)
    reasons = _reasons()
    primary = n.call("tmx_representable", C.c_int64,
                     [C.c_int32, n.SZ, n.SZ, n.SZ, n.I32, U32, n.SZ, C.c_int32, n.SZ, n.I64],
                     fmt, rows, cols, group, values, scales, len(scales), kind, scale_grp, reasons)
    if primary < REPRESENTABLE:
        raise f.FormatError(primary)
    return (None if primary == REPRESENTABLE else REASONS[primary]), reasons_list(reasons)


def round_trip(fmt, rows, cols, group, values, scales, kind, scale_grp):
    """One t27 round-trip cell: (result dict, reasons, written code bytes, target scale words)."""
    scales = words(scales)
    count = rows * cols
    target = scale_count(rows, cols, scale_group(fmt, group))
    capacity = count // 2 + rows * max(group, 64) + 64
    out, target_words = n.buffer(capacity), (C.c_uint32 * max(1, target))()
    decoded, decoded_words = (C.c_int32 * max(1, count))(), (C.c_uint32 * max(1, target))()
    reasons, result = _reasons(), _result()
    _check(n.call("tmx_round_trip", C.c_int32,
                  [C.c_int32, n.SZ, n.SZ, n.SZ, n.I32, U32, n.SZ, C.c_int32, n.SZ, U32, n.SZ, n.U8, n.SZ,
                   n.I32, n.SZ, U32, n.I64, n.I64],
                  fmt, rows, cols, group, values, scales, len(scales), kind, scale_grp, target_words, target,
                  out, capacity, decoded, count, decoded_words, reasons, result))
    cell = result_dict(result)
    written = bytes(out[: cell["code_bytes"]]) if cell["status"] != NOT_REPRESENTABLE else b""
    stored_words = list(target_words[:target]) if cell["status"] != NOT_REPRESENTABLE else []
    return cell, reasons_list(reasons), written, stored_words


def compare(a, b, rows, cols, a_scales=None, a_kind=0, a_group=0, b_scales=None, b_kind=0, b_group=0):
    """Trits of a and b and, when both scale lists are given, their scales weight by weight."""
    with_scales = a_scales is not None and b_scales is not None
    a_scales, b_scales = words(a_scales or [0]), words(b_scales or [0])
    result = _result()
    _check(n.call("tmx_compare", C.c_int32,
                  [n.I32, n.I32, n.SZ, n.SZ, U32, n.SZ, C.c_int32, n.SZ, U32, n.SZ, C.c_int32, n.SZ, C.c_bool, n.I64],
                  a, b, rows, cols, a_scales, len(a_scales), a_kind, a_group, b_scales, len(b_scales), b_kind,
                  b_group, with_scales, result))
    return result


def explain_ties(master: bytes, count: int, weight_scale: int, reference, derived, result):
    """Tie counts of tmx_explain_ties; updates `result` (from compare) in place."""
    detail = (C.c_int64 * len(TIES))()
    _check(n.call("tmx_explain_ties", C.c_int32, [n.U8, n.SZ, n.SZ, C.c_uint32, n.I32, n.I32, n.I64, n.I64],
                  n.octets(master), len(master), count, weight_scale, reference, derived, detail, result))
    return {name: detail[slot] for slot, name in enumerate(TIES)}


def reencode(fmt, rows, cols, group, values, scale_words, published: bytes):
    """(differing bytes, first) between the t27 writer's bytes and published code bytes."""
    scale_words = words(scale_words)
    scratch, first = n.buffer(len(published)), (C.c_int64 * 1)()
    total = _check(n.call("tmx_reencode", C.c_int64,
                          [C.c_int32, n.SZ, n.SZ, n.SZ, n.I32, U32, n.SZ, n.U8, n.SZ, n.U8, n.SZ, n.I64],
                          fmt, rows, cols, group, values, scale_words, len(scale_words), n.octets(published),
                          len(published), scratch, len(published), first))
    return total, first[0]


def encode(fmt, rows, cols, group, values, scale_words, capacity: int) -> bytes:
    scale_words = words(scale_words)
    out = n.buffer(capacity)
    written = _check(n.call("tmx_encode", C.c_int64, [C.c_int32, n.SZ, n.SZ, n.SZ, n.I32, U32, n.SZ, n.U8, n.SZ],
                            fmt, rows, cols, group, values, scale_words, len(scale_words), out, capacity))
    return bytes(out[:written])


def bytes_differ(a: bytes, b: bytes):
    if len(a) != len(b):
        raise f.FormatError(-51)
    first = (C.c_int64 * 1)()
    total = n.call("tmx_bytes_differ", C.c_int64, [n.U8, n.U8, n.SZ, n.I64], n.octets(a), n.octets(b), len(a), first)
    return total, first[0]


def locate(fmt, rows, cols, group, index):
    out = (C.c_int64 * len(LOCATE))()
    _check(n.call("tmx_locate", C.c_int32, [C.c_int32, n.SZ, n.SZ, n.SZ, n.SZ, n.I64],
                  fmt, rows, cols, group, index, out))
    return {name: out[slot] for slot, name in enumerate(LOCATE)}


def mismatch_indices(a, b, count: int, limit: int):
    out = (C.c_int64 * limit)()
    written = n.call("tmx_mismatch_indices", C.c_int64, [n.I32, n.I32, n.SZ, n.I64, n.SZ], a, b, count, out, limit)
    return list(out[:written])


def word_indices(master: bytes, count: int, word: int, reference, trit: int, limit: int):
    out = (C.c_int64 * limit)()
    written = _check(n.call("tmx_word_indices", C.c_int64, [n.U8, n.SZ, n.SZ, C.c_uint32, n.I32, C.c_int32, n.I64, n.SZ],
                            n.octets(master), len(master), count, word, reference, trit, out, limit))
    return list(out[:written])
