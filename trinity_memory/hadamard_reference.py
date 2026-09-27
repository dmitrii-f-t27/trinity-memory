"""The t27/hadamard.t27 rotation as the contract's reference (issue #52).

Loads the generated C of the spec once (gen-c + cc into a private directory)
and exposes rotate(block, values, signs) -> list[float]; the values are the
spec's own -- this module never re-implements the transform.
"""
from __future__ import annotations

import ctypes as C
import pathlib
import re
import subprocess
import sys
import tempfile

from . import _native as n

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = ROOT / "t27/hadamard.t27"
_lib = None


def _load():
    global _lib
    if _lib is not None:
        return _lib
    compiler = ROOT / "build/compiler/target/release/t27c"
    work = pathlib.Path(tempfile.mkdtemp(prefix="hadamard-ref-"))
    c = work / "hadamard.c"
    gen = subprocess.run([str(compiler), "gen-c", str(SPEC)], capture_output=True,
                         text=True, check=True).stdout
    gen = re.sub(r"(\w+\[\d+\]) = 0;", r"\1 = {0};", gen)
    c.write_text(gen)
    lib_path = work / ("libhadamard.dylib" if sys.platform == "darwin" else "libhadamard.so")
    subprocess.run(["cc", "-shared", "-fPIC", "-O1", "-Wno-parentheses-equality",
                    "-Wno-shift-count-overflow", str(c), "-o", str(lib_path)],
                   check=True, capture_output=True)
    _lib = C.CDLL(str(lib_path))
    _lib.sw_rotate_numerator.restype = C.c_int64
    ArrayI64 = C.c_int64 * 1024
    ArrayI32 = C.c_int32 * 1024
    _lib.sw_rotate_numerator.argtypes = [C.c_uint32, C.c_uint32, ArrayI64, ArrayI32]
    _lib._a64, _lib._a32 = ArrayI64, ArrayI32
    return _lib


def rotate(block: int, values: list[float], signs: list[int]) -> list[float]:
    lib = _load()
    norm = {16: 4, 64: 8, 256: 16, 1024: 32}[block]
    x = lib._a64(*[int(v) for v in values])
    sg = lib._a32(*signs)
    return [lib.sw_rotate_numerator(k, block, x, sg) / norm for k in range(block)]
