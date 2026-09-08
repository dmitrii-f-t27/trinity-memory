"""Explicitly lossy preparation helpers; these are not QAT or quality evidence."""

import math
from collections import Counter
from typing import Iterable

from .codecs import CodecError, validate_trits


def entropy_bpw(values: Iterable[int]) -> float:
    trits = validate_trits(values)
    if not trits:
        return 0.0
    return -sum((n / len(trits)) * math.log2(n / len(trits))
                for n in Counter(trits).values())


def symmetric_entropy(zero_fraction: float) -> float:
    """Marginal entropy, assuming equal probabilities for +1 and -1."""
    if not math.isfinite(zero_fraction) or not 0 <= zero_fraction <= 1:
        raise ValueError("zero_fraction must be finite and in [0, 1]")
    probabilities = (zero_fraction, (1 - zero_fraction) / 2, (1 - zero_fraction) / 2)
    return -sum(p * math.log2(p) for p in probabilities if p)


def project_topk(values: Iterable[float], block_size: int, max_nonzero: int) -> list[int]:
    """Keep top-|weight| entries per block and return their signs.

    Ties prefer lower indices. This discards magnitudes and other entries;
    callers must evaluate/retrain their model. A partial last block is allowed.
    """
    if (type(block_size) is not int or type(max_nonzero) is not int
            or block_size <= 0 or not 0 <= max_nonzero <= block_size):
        raise ValueError("require integer block_size > 0 and 0 <= max_nonzero <= block_size")
    weights = list(values)
    if any(isinstance(x, bool) or not isinstance(x, (float, int)) or not math.isfinite(x)
           for x in weights):
        raise CodecError("projection requires finite real weights")
    result = [0] * len(weights)
    for start in range(0, len(weights), block_size):
        indices = range(start, min(start + block_size, len(weights)))
        keep = sorted(indices, key=lambda i: (-abs(weights[i]), i))[:max_nonzero]
        for i in keep:
            result[i] = (weights[i] > 0) - (weights[i] < 0)
    return result
