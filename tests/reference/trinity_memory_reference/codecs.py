"""Canonical, lossless codecs for integer weights in {-1, 0, +1}.

Groups and fields are stored least significant first. Sparse codecs reject
incompatible inputs; encoding never prunes weights. See docs/format.md.
"""

from dataclasses import dataclass
from itertools import combinations
from typing import Iterable


class CodecError(ValueError):
    """Invalid values, incompatible sparsity, or noncanonical payload."""


@dataclass(frozen=True)
class Codec:
    id: int
    name: str
    group_size: int
    group_bits: int
    max_nonzero: int | None = None


CODECS = {c.name: c for c in (
    Codec(0, "baseline2", 4, 8),
    Codec(1, "dense5", 5, 8),
    Codec(2, "dense17", 17, 27),
    Codec(3, "dense22", 22, 35),
    Codec(4, "sparse41", 4, 4, 1),
    Codec(5, "sparse82", 8, 8, 2),
)}
CODECS_BY_ID = {c.id: c for c in CODECS.values()}
LANE_ENCODE = {0: 0, 1: 1, -1: 2}
LANE_DECODE = {0: 0, 1: 1, 2: -1}


def get_codec(name: str) -> Codec:
    try:
        return CODECS[name]
    except (KeyError, TypeError):
        raise CodecError(f"unknown codec: {name!r}") from None


def validate_trits(values: Iterable[int]) -> list[int]:
    result = list(values)
    for i, value in enumerate(result):
        if type(value) is not int or value not in (-1, 0, 1):
            raise CodecError(f"weight {i}: expected an integer -1, 0, or 1; got {value!r}")
    return result


def _sparse_states(n: int, k: int) -> tuple[tuple[int, ...], ...]:
    states = []
    for nonzeros in range(k + 1):
        for positions in combinations(range(n), nonzeros):
            for signs in range(1 << nonzeros):
                values = [0] * n
                for bit, position in enumerate(positions):
                    values[position] = 1 if signs & (1 << bit) else -1
                states.append(tuple(values))
    return tuple(states)


SPARSE_STATES = {c.name: _sparse_states(c.group_size, c.max_nonzero)
                 for c in CODECS.values() if c.max_nonzero is not None}
SPARSE_CODES = {name: {state: i for i, state in enumerate(states)}
                for name, states in SPARSE_STATES.items()}


def payload_size(count: int, codec: str = "dense5") -> int:
    if type(count) is not int or count < 0:
        raise CodecError("count must be a nonnegative integer")
    c = get_codec(codec)
    groups = (count + c.group_size - 1) // c.group_size
    return (groups * c.group_bits + 7) // 8


def pack(values: Iterable[int], codec: str = "dense5") -> bytes:
    c = get_codec(codec)
    trits = validate_trits(values)
    output = bytearray()
    buffer = bits = 0
    for start in range(0, len(trits), c.group_size):
        group = trits[start:start + c.group_size]
        group += [0] * (c.group_size - len(group))
        if c.max_nonzero is not None:
            try:
                word = SPARSE_CODES[codec][tuple(group)]
            except KeyError:
                raise CodecError(f"group at weight {start}: {codec} allows at most "
                                 f"{c.max_nonzero} nonzeros per {c.group_size} weights") from None
        elif codec == "baseline2":
            word = sum(LANE_ENCODE[t] << (2 * i) for i, t in enumerate(group))
        else:
            word = 0
            for t in reversed(group):
                word = word * 3 + t + 1
        buffer |= word << bits
        bits += c.group_bits
        while bits >= 8:
            output.append(buffer & 255)
            buffer >>= 8
            bits -= 8
    if bits:
        output.append(buffer)
    return bytes(output)


def unpack(payload: bytes, count: int, codec: str = "dense5") -> list[int]:
    c = get_codec(codec)
    expected = payload_size(count, codec)
    if not isinstance(payload, (bytes, bytearray)):
        raise CodecError("payload must be bytes or bytearray")
    if len(payload) != expected:
        raise CodecError(f"payload length {len(payload)} != expected {expected}")
    output = []
    buffer = bits = offset = 0
    groups = (count + c.group_size - 1) // c.group_size
    for group_index in range(groups):
        while bits < c.group_bits:
            buffer |= payload[offset] << bits
            bits += 8
            offset += 1
        word = buffer & ((1 << c.group_bits) - 1)
        buffer >>= c.group_bits
        bits -= c.group_bits
        if c.max_nonzero is not None:
            if word >= len(SPARSE_STATES[codec]):
                raise CodecError(f"invalid {codec} code {word} at group {group_index}")
            group = list(SPARSE_STATES[codec][word])
        elif codec == "baseline2":
            try:
                group = [LANE_DECODE[(word >> (2 * i)) & 3] for i in range(c.group_size)]
            except KeyError:
                raise CodecError(f"reserved 2-bit lane 11 at group {group_index}") from None
        else:
            if word >= 3 ** c.group_size:
                raise CodecError(f"invalid {codec} code {word} at group {group_index}")
            group = []
            for _ in range(c.group_size):
                word, digit = divmod(word, 3)
                group.append(digit - 1)
        remaining = count - len(output)
        if remaining < c.group_size and any(group[remaining:]):
            raise CodecError("nonzero padded trits in final group")
        output.extend(group[:remaining])
    if buffer:
        raise CodecError("nonzero unused high bits in final byte")
    return output
