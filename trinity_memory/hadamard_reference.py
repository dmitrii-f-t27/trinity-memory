"""The t27/hadamard.t27 rotation through the native library (#52).

The spec's functions are compiled into libtrinity_memory_t27 (native/core.c
includes the generated hadamard.h); this module is the thin ctypes binding
the contract's rotate call uses. It never re-implements the transform.
"""
from __future__ import annotations

import ctypes as C

from . import _native as n

ArrayI64 = C.c_int64 * 1024
ArrayI32 = C.c_int32 * 1024
_lib = None


def _load():
    global _lib
    if _lib is None:
        _lib = n.library()
        _lib.sw_rotate_numerator.restype = C.c_int64
        _lib.sw_rotate_numerator.argtypes = [C.c_uint32, C.c_uint32, ArrayI64, ArrayI32]
    return _lib


def rotate(block: int, values: list[float], signs: list[int]) -> list[float]:
    """The rotated block: numerator / normalisation, exact for integer inputs."""
    lib = _load()
    norm = {16: 4, 64: 8, 256: 16, 1024: 32}[block]
    x = ArrayI64(*[int(v) for v in values])
    sg = ArrayI32(*signs)
    return [lib.sw_rotate_numerator(k, block, x, sg) / norm for k in range(block)]
