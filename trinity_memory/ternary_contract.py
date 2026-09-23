"""The Ternary Check CLI contract v1 (ternary-check/CONTRACT.md).

ctypes bindings for t27/ternary_contract.t27, and the reference decoder that
implements the contract with the t27 readers and writers:

    trinity-memory ternary-check formats
    trinity-memory ternary-check decode FORMAT COUNT INPUT VALUES SCALES [key=value...]
    trinity-memory ternary-check encode FORMAT COUNT VALUES SCALES OUTPUT [key=value...]

Reading, rejecting, flagging, the class tokens, comparison and the verdict all
run in generated t27 code; this module moves bytes between files and buffers.
"""
from __future__ import annotations
import ctypes as C
import struct
import sys
from pathlib import Path

from . import _native as n
from . import formats as f

FORMATS = ("TQ1_0", "TQ2_0", "Q2_0", "Q1_0", "PQ2_0", "PTQ1_0", "I2_S", "HF_PACKED", "MLX2", "ONNX2")
SIDE_SCALES = ("HF_PACKED", "MLX2", "ONNX2")
FLAG_COUNT = 7
OUTCOME_COUNT = 9
FAIL_ON = {"mismatch": 0, "silent": 1, "never": 2}
FLAGS_EQUAL, FLAGS_UNREPORTED, FLAGS_DIFFER = 0, 1, 2
KINDS = {"F16": f.F16, "BF16": f.BF16, "F32": f.F32}
U32 = C.POINTER(C.c_uint32)
EXIT_REJECTED, EXIT_USAGE = 1, 2
ERR_FORMAT, ERR_LENGTH = -50, -51  # TF_ERR_FORMAT, TF_ERR_LENGTH of t27/formats.t27


def _token(name: str, value: int, lane=C.c_int32) -> str:
    out = n.buffer(64)
    size = n.call(name, C.c_int64, [lane, n.U8, n.SZ], value, out, 64)
    return bytes(out[:max(0, size)]).decode("ascii")


def format_id(name: str) -> int:
    """Format id of a contract name, 0 when it names none."""
    data = name.encode("utf-8", errors="replace")
    return n.call("tk_format_of_name", C.c_int32, [n.U8, n.SZ], n.octets(data), len(data))


def format_name(format: int) -> str:
    return _token("tk_format_token", format)


def error_token(status: int) -> str:
    """Class token of a TF_ERR_* status, "" for any other status."""
    return _token("tk_error_token", status)


def error_status(text: bytes) -> int:
    """TF_ERR_* status named by an error file, 0 when it names no class."""
    return n.call("tk_error_status", C.c_int32, [n.U8, n.SZ], n.octets(text), len(text))


def flag_token(slot: int) -> str:
    return _token("tk_flag_token", slot, n.SZ)


def flag_tokens() -> list[str]:
    return [flag_token(slot) for slot in range(FLAG_COUNT)]


def outcome_token(outcome: int) -> str:
    return _token("tk_outcome_token", outcome)


def parse_flags(text: bytes):
    """(status, slots): status 0, or -1 for a malformed flags file."""
    flags = (C.c_int64 * FLAG_COUNT)()
    status = n.call("tk_parse_flags", C.c_int32, [n.U8, n.SZ, n.I64], n.octets(text), len(text), flags)
    return status, list(flags)


def flags_text(flags) -> bytes:
    slots = (C.c_int64 * FLAG_COUNT)(*flags)
    out = n.buffer(1024)
    size = n.call("tk_flags_text", C.c_int64, [n.I64, n.U8, n.SZ], slots, out, 1024)
    if size < 0:
        raise ValueError(f"flags cannot be written: status {size}")
    return bytes(out[:size])


def compare_bytes(actual: bytes, expected: bytes):
    """(positions that differ, first of them or -1)."""
    first = (C.c_int64 * 1)()
    total = n.call("tk_compare_bytes", C.c_int64, [n.U8, n.SZ, n.U8, n.SZ, n.I64],
                   n.octets(actual), len(actual), n.octets(expected), len(expected), first)
    return total, first[0]


def compare_words(actual: bytes, width: int, expected) -> tuple[int, int]:
    words = (C.c_uint32 * max(1, len(expected)))(*expected)
    first = (C.c_int64 * 1)()
    total = n.call("tk_compare_words", C.c_int64, [n.U8, n.SZ, n.SZ, U32, n.SZ, n.I64],
                   n.octets(actual), len(actual), width, words, len(expected), first)
    if total < 0:
        raise ValueError(f"scale width {width} is not 2 or 4")
    return total, first[0]


def flag_state(reported: bool, parse_status: int, actual, expected) -> int:
    a, e = (C.c_int64 * FLAG_COUNT)(*actual), (C.c_int64 * FLAG_COUNT)(*expected)
    return n.call("tk_flag_state", C.c_int32, [C.c_bool, C.c_int32, n.I64, n.I64], reported, parse_status, a, e)


def verdict(expected: int, ended: bool, exit_code: int, error: int, value_diff: int, scale_diff: int,
            flags: int) -> int:
    return n.call("tk_verdict", C.c_int32,
                  [C.c_int32, C.c_int32, C.c_int32, C.c_int32, C.c_int64, C.c_int64, C.c_int32],
                  expected, 1 if ended else 0, exit_code, error, value_diff, scale_diff, flags)


def fails(outcome: int, policy: int) -> bool:
    return n.call("tk_fails", C.c_bool, [C.c_int32, C.c_int32], outcome, policy)


def scale_count(format: int, count: int, rows: int, cols: int, group: int) -> int:
    return n.call("tk_scale_count", n.SZ, [C.c_int32, n.SZ, n.SZ, n.SZ, n.SZ], format, count, rows, cols, group)


def scale_width(format: int, kind: int) -> int:
    return n.call("tk_scale_width", n.SZ, [C.c_int32, C.c_int32], format, kind)


def encoded_bytes(format: int, count: int, rows: int, cols: int, group: int) -> int:
    return n.call("tk_encoded_bytes", n.SZ, [C.c_int32, n.SZ, n.SZ, n.SZ, n.SZ], format, count, rows, cols, group)


def _word_array(words):
    return (C.c_uint32 * max(1, len(words)))(*words)


def decode(format: int, data: bytes, count: int, rows=0, cols=0, group=0, kind=0, side=(), biases=(),
           zero_points=b""):
    """(status, values, scale words, flags): status is the scale word count or a TF_ERR_* status."""
    work, values = (C.c_int32 * max(1, count))(), n.buffer(count)
    capacity = max(1, len(side), scale_count(format, count, rows, cols, group))
    scales, flags = (C.c_uint32 * capacity)(), (C.c_int64 * FLAG_COUNT)()
    status = n.call("tk_decode", C.c_int64,
                    [C.c_int32, n.U8, n.SZ, n.SZ, n.SZ, n.SZ, n.SZ, C.c_int32, U32, n.SZ, U32, n.SZ, n.U8, n.SZ,
                     n.I32, n.U8, n.SZ, U32, n.SZ, n.I64],
                    format, n.octets(data), len(data), count, rows, cols, group, kind, _word_array(side), len(side),
                    _word_array(biases), len(biases), n.octets(zero_points), len(zero_points), work, values, count,
                    scales, capacity, flags)
    if status < 0:
        return status, b"", [], [0] * FLAG_COUNT
    return status, bytes(values[:count]), list(scales[:status]), list(flags)


def encode(format: int, values: bytes, count: int, rows=0, cols=0, group=0, scales=(), zero_points=b""):
    """(status, stored bytes): status is the byte count or a TF_ERR_* status."""
    if len(values) != count:
        return ERR_LENGTH, b""
    capacity = max(1, encoded_bytes(format, count, rows, cols, group))
    work, out = (C.c_int32 * max(1, count))(), n.buffer(capacity)
    status = n.call("tk_encode", C.c_int64,
                    [C.c_int32, n.U8, n.SZ, n.SZ, n.SZ, n.SZ, U32, n.SZ, n.U8, n.SZ, n.I32, n.SZ, n.U8, n.SZ],
                    format, n.octets(values), count, rows, cols, group, _word_array(scales), len(scales),
                    n.octets(zero_points), len(zero_points), work, count, out, capacity)
    return status, (bytes(out[:status]) if status >= 0 else b"")


# ---- the reference decoder behind the contract ------------------------------
class UsageError(Exception):
    pass


GEOMETRY = {"rows": "rows", "cols": "cols", "group": "group", "n": "rows", "k": "cols", "block_size": "group"}


def _options(arguments):
    options = {}
    for argument in arguments:
        key, sep, value = argument.partition("=")
        if not sep or not key:
            raise UsageError(f"expected key=value, got {argument!r}")
        options[key] = value  # keys this decoder does not use are ignored
    return options


def _integer(text, name):
    if not text.isdigit():
        raise UsageError(f"{name} must be a decimal integer, got {text!r}")
    return int(text)


def _geometry(options):
    geometry = {"rows": 0, "cols": 0, "group": 0}
    for key, target in GEOMETRY.items():
        if key in options:
            geometry[target] = _integer(options[key], key)
    return geometry


def _words_of(data: bytes, width: int):
    words, count = f.words(data, width)
    return list(words[:count])


def _pack_words(words, width: int) -> bytes:
    return struct.pack(f"<{len(words)}{'H' if width == 2 else 'I'}", *words)


def _reject(status: int, message: str) -> int:
    token = error_token(status)
    Path("error").write_text(token + "\n", encoding="ascii")
    print(f"ternary-check: refused ({token or status}): {message}", file=sys.stderr)
    return EXIT_REJECTED


def _side_words(options, key, kind):
    """Scale or bias words of a separate tensor; raises FormatError when not whole words."""
    if key not in options:
        return ()
    data = Path(options[key]).read_bytes()
    return _words_of(data, 4 if kind == f.F32 else 2) if data else ()


def _command_formats() -> int:
    for operation in ("decode", "encode"):
        for name in FORMATS:
            print(f"{operation} {name}")
    return 0


def _command_decode(arguments) -> int:
    if len(arguments) < 5:
        raise UsageError("decode FORMAT COUNT INPUT VALUES SCALES [key=value...]")
    name, count_text, source, values_path, scales_path = arguments[:5]
    options, count = _options(arguments[5:]), _integer(count_text, "COUNT")
    format = format_id(name)
    if format == 0:
        return _reject(ERR_FORMAT, f"unknown format {name!r}")
    geometry = _geometry(options)
    kind = 0
    side = biases = ()
    zero_points = b""
    if name in SIDE_SCALES:
        kind = KINDS.get(options.get("scale_kind", ""), 0)
        try:
            side = _side_words(options, "scales", kind)
            biases = _side_words(options, "biases", kind)
        except f.FormatError as error:
            return _reject(error.status, "a scale file is not whole words")
        if "zero_points" in options:
            zero_points = Path(options["zero_points"]).read_bytes()
    data = Path(source).read_bytes()
    status, values, scales, flags = decode(format, data, count, geometry["rows"], geometry["cols"],
                                           geometry["group"], kind, side, biases, zero_points)
    if status < 0:
        return _reject(status, f"{name}, {count} weights, {len(data)} bytes")
    Path(values_path).write_bytes(values)
    Path(scales_path).write_bytes(_pack_words(scales, scale_width(format, kind)))
    Path("flags").write_bytes(flags_text(flags))
    return 0


def _command_encode(arguments) -> int:
    if len(arguments) < 5:
        raise UsageError("encode FORMAT COUNT VALUES SCALES OUTPUT [key=value...]")
    name, count_text, values_path, scales_path, output = arguments[:5]
    options, count = _options(arguments[5:]), _integer(count_text, "COUNT")
    format = format_id(name)
    if format == 0:
        return _reject(ERR_FORMAT, f"unknown format {name!r}")
    geometry = _geometry(options)
    values = Path(values_path).read_bytes()
    scales = ()
    if name not in SIDE_SCALES:
        data = Path(scales_path).read_bytes()
        try:
            scales = _words_of(data, scale_width(format, 0)) if data else ()
        except f.FormatError as error:
            return _reject(error.status, "the scale file is not whole words")
    zero_points = Path(options["zero_points"]).read_bytes() if "zero_points" in options else b""
    status, stored = encode(format, values, count, geometry["rows"], geometry["cols"], geometry["group"], scales,
                            zero_points)
    if status < 0:
        return _reject(status, f"{name}, {count} weights")
    Path(output).write_bytes(stored)
    return 0


def command(arguments) -> int:
    """`formats`, `decode` or `encode` of the contract; exit status 0, 1 (refused) or 2 (usage)."""
    try:
        if arguments[:1] == ["formats"]:
            return _command_formats()
        if arguments[:1] == ["decode"]:
            return _command_decode(arguments[1:])
        if arguments[:1] == ["encode"]:
            return _command_encode(arguments[1:])
        raise UsageError("expected formats, decode or encode")
    except (UsageError, OSError) as error:
        print(f"ternary-check: {error}", file=sys.stderr)
        return EXIT_USAGE
