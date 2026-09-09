#!/usr/bin/env python3
"""Run actual Icarus dot-stream simulations, including adversarial framing."""

from __future__ import annotations

import json
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trinity_memory.rtl_compute import (  # noqa: E402
    RTLSimulationError, _packet, _packets, _require_tools, _run_packets,
)


def protocol_suite(codec: str, seed: int = 27) -> dict:
    rng = random.Random(seed)
    packets: list[int] = []
    expected: list[tuple[int, bool]] = []

    def good(weights: list[int], activations: list[int]) -> None:
        packets.extend(_packets(weights, activations, codec))
        expected.append((sum(w * a for w, a in zip(weights, activations)), False))

    # Framing, every tail length, ternary signs, and the asymmetric int8 edge.
    for count in [0, 1, 2, 3, 4, 5, 6, 9, 10, 11, 17, 65, 211, 513]:
        good([rng.choice((-1, 0, 1)) for _ in range(count)],
             [rng.choice((-128, 127, 0, rng.randrange(-128, 128))) for _ in range(count)])
    good([-1] * 31, [-128] * 31)
    good([1] * 31, [-128] * 31)
    for _ in range(32):
        count = rng.randrange(1, 180)
        good([rng.choice((-1, 0, 1)) for _ in range(count)],
             [rng.randrange(-128, 128) for _ in range(count)])

    zero_code = 121 if codec == "dense5" else 0
    invalid_code = 243 if codec == "dense5" else 3
    # Reserved code; poison must persist across subsequent valid groups.
    packets.append(_packet(invalid_code, [1] * 5, 31, False))
    continuation = _packets([1] * 11, [7] * 11, codec)
    packets.extend(continuation)
    expected.append((0, True))
    # Reserved high bits (dense) / invalid final lane (baseline).
    packets.append(_packet((zero_code | 256) if codec == "dense5" else (3 << 8),
                           [0] * 5, 31, True))
    expected.append((0, True))
    # A mask hole cannot silently delete a term.
    packets.append(_packet(zero_code, [0] * 5, 0b01011, True))
    expected.append((0, True))
    # Non-final beats must have five valid lanes.
    packets.append(_packet(zero_code, [0] * 5, 0b00011, False))
    packets.extend(_packets([1], [11], codec))
    expected.append((0, True))
    # Inactive weights must be logical zero, even when their activations are zero.
    bad_padding = _packets([1, 0, 0, 0, 1], [0] * 5, codec)[0]
    packets.append((bad_padding & ~(31 << 50)) | (1 << 50))
    expected.append((0, True))
    # Inactive activation padding is independently checked.
    packets.append(_packet(zero_code, [0, 0, 0, 0, -128], 1, True))
    expected.append((0, True))
    # An empty final beat is only a whole empty frame, never a terminator.
    packets.append(_packet(zero_code, [0] * 5, 31, False))
    packets.append(_packet(zero_code, [0] * 5, 0, True))
    expected.append((0, True))
    good([1, -1, 0, 1, -1, 1], [127, -128, -128, -128, 127, 0])

    # Let preceding outputs drain before a reset scenario by waiting for a
    # deliberately discarded result. This also tests reset while out_valid stalls.
    packets.extend(_packets([1] * 10, [100] * 10, codec))
    packets.append((1 << 57) | len(expected))
    # Reset during an unfinished frame: all accepted terms must be forgotten.
    packets.append(_packet(zero_code, [127] * 5, 31, False))
    nonzero = _packets([-1] * 5, [-128] * 5, codec)[0] & ~(1 << 55)
    packets.extend([nonzero] * 3)
    packets.append(1 << 56)
    good([-1, 1, 1], [-128, 127, -128])
    report = _run_packets(packets, expected, codec, seed)
    if report["input_stalls"] == 0 or report["output_stalls"] == 0 or report["source_bubbles"] == 0:
        raise AssertionError("protocol suite failed to exercise stalls and bubbles")
    if report["resets"] != 2:
        raise AssertionError("protocol suite did not exercise both reset scenarios")
    return {key: value for key, value in report.items() if key != "results"} | {
        "checked_results": len(expected), "rejected_frames": sum(error for _, error in expected),
    }


def overflow_suite(codec: str) -> dict:
    # A narrow supported accumulator makes positive/negative overflow practical
    # to exhaust in simulation, using the same parameterized detection logic.
    packets = []
    expected = []
    cases = [
        ([-1] * 15 + [1], [-128] * 15 + [127], 2047, False),
        ([1] * 16, [-128] * 16, -2048, False),
        ([-1] * 17, [-128] * 17, 0, True),
        ([1] * 17, [-128] * 17, 0, True),
        # Overflow stays an error even if later terms cancel it.
        ([-1] * 20 + [1] * 20, [-128] * 40, 0, True),
        ([1, -1], [127, -128], 255, False),
    ]
    for weights, activations, result, error in cases:
        packets.extend(_packets(weights, activations, codec))
        expected.append((result, error))
    report = _run_packets(packets, expected, codec, 927, acc_width=12)
    return {key: value for key, value in report.items() if key != "results"} | {
        "checked_results": len(expected), "rejected_frames": sum(error for _, error in expected),
    }


def main() -> int:
    try:
        _require_tools()
        reports = [suite(codec) for codec in ("dense5", "baseline2")
                   for suite in (protocol_suite, overflow_suite)]
        print(json.dumps({"evidence": "rtl-simulation", "suites": reports}, indent=2))
    except (RTLSimulationError, AssertionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print("PASS actual RTL dot: framing, int8 edges, bubbles, stalls, reset, errors, overflow")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
