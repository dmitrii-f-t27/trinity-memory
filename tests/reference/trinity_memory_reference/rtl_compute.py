"""Run the framed ternary/int8 dot product in actual Icarus RTL simulation.

No simulator is replaced by a software estimate. RTL sources are resolved from
TRINITY_MEMORY_RTL_ROOT, a packaged ``rtl`` directory, or the source checkout.
See docs/stream-compute.md for protocol and simulation counter definitions.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .codecs import LANE_ENCODE, pack, validate_trits


class RTLSimulationError(RuntimeError):
    """Missing simulator/resources, failed compilation, or failed RTL assertion."""


def _codec_name(codec: str) -> str:
    if codec == "baseline5":
        return "baseline2"
    if codec not in ("dense5", "baseline2"):
        raise ValueError("RTL dot supports dense5 or baseline2 (five 2-bit lanes)")
    return codec


def _require_tools() -> tuple[str, str]:
    paths = tuple(shutil.which(tool) for tool in ("iverilog", "vvp"))
    missing = [tool for tool, path in zip(("iverilog", "vvp"), paths) if path is None]
    if missing:
        raise RTLSimulationError(
            f"Required simulator tool(s) not found: {', '.join(missing)}. "
            "Install Icarus Verilog; the explicit RTL path has no software fallback."
        )
    return paths  # type: ignore[return-value]


def _rtl_directory() -> Path:
    configured = os.environ.get("TRINITY_MEMORY_RTL_ROOT")
    if configured:
        candidate = Path(configured).expanduser().resolve()
        candidates = [candidate, candidate / "rtl"]
    else:
        package = Path(__file__).resolve().parent
        candidates = [package / "rtl", package.parent / "rtl"]
    for directory in candidates:
        if (directory / "trinity_dot_stream.v").is_file():
            return directory
    raise RTLSimulationError(
        "RTL sources unavailable. Run from the source checkout or set "
        "TRINITY_MEMORY_RTL_ROOT to its rtl directory."
    )


def _packet(code: int, activations: list[int], mask: int, last: bool) -> int:
    activation_word = sum((value & 255) << (8 * lane)
                          for lane, value in enumerate(activations))
    return code | (activation_word << 10) | (mask << 50) | (int(last) << 55)


def _packets(weights: list[int], activations: list[int], codec: str) -> list[int]:
    if not weights:
        return [_packet(121 if codec == "dense5" else 0, [0] * 5, 0, True)]
    dense_codes = pack(weights, codec="dense5") if codec == "dense5" else b""
    result = []
    for start in range(0, len(weights), 5):
        group = weights[start:start + 5]
        values = activations[start:start + 5]
        count = len(group)
        group += [0] * (5 - count)
        values += [0] * (5 - count)
        code = (dense_codes[start // 5] if codec == "dense5" else
                sum(LANE_ENCODE[value] << (2 * lane)
                    for lane, value in enumerate(group)))
        result.append(_packet(code, values, (1 << count) - 1, start + 5 >= len(weights)))
    return result


def _run_packets(
    packets: list[int], expected: list[tuple[int, bool]], codec: str,
    seed: int = 27, acc_width: int = 32,
) -> dict:
    """Internal file-driven simulation, also used by adversarial protocol tests."""
    codec = _codec_name(codec)
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("seed must be an unsigned 32-bit integer")
    if not packets or not expected or not 12 <= acc_width <= 32:
        raise ValueError("simulation requires packets, results, and 12..32 accumulator bits")
    compiler, runtime = _require_tools()
    rtl = _rtl_directory()
    sources = [rtl / "trinity_dot_stream.v", rtl / "tb_dot_stream.v",
               rtl / "generated/ternary_dense5_decoder.v",
               rtl / "ternary_baseline5_decoder.v"]
    for source in sources:
        if not source.is_file():
            raise RTLSimulationError(f"Required RTL source not found: {source}")
    with tempfile.TemporaryDirectory(prefix="trinity-dot-rtl-") as temporary:
        directory = Path(temporary)
        (directory / "packets.mem").write_text(
            "".join(f"{packet:016x}\n" for packet in packets), encoding="ascii")
        (directory / "expected.mem").write_text(
            "".join(f"{(int(error) << 32) | (result & 0xffffffff):016x}\n"
                    for result, error in expected), encoding="ascii")
        commands = [
            [compiler, "-g2012", "-Wall", "-s", "tb_dot_stream",
             f"-Ptb_dot_stream.DENSE5={int(codec == 'dense5')}",
             f"-Ptb_dot_stream.ACC_WIDTH={acc_width}",
             f"-Ptb_dot_stream.MAX_PACKETS={len(packets)}",
             f"-Ptb_dot_stream.MAX_RESULTS={len(expected)}",
             "-o", "dot.vvp", *map(str, sources)],
            [runtime, "dot.vvp", "+packets=packets.mem", "+expected=expected.mem",
             f"+packet_count={len(packets)}", f"+result_count={len(expected)}", f"+seed={seed}"],
        ]
        output = ""
        for command in commands:
            try:
                process = subprocess.run(command, cwd=directory, text=True,
                                         capture_output=True, timeout=180, check=False)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise RTLSimulationError(f"RTL simulator execution failed: {error}") from error
            if process.returncode:
                raise RTLSimulationError(
                    f"RTL {'compile' if command is commands[0] else 'simulation'} failed "
                    f"(exit {process.returncode}):\n{process.stdout}\n{process.stderr}"
                )
            output = process.stdout
    results = [dict(zip(("index", "result", "error", "cycles"), map(int, match)))
               for match in re.findall(
                   r"DOT_RESULT index=(\d+) result=(-?\d+) error=(\d+) cycles=(\d+)", output)]
    summaries = re.findall(
        r"DOT_SUMMARY cycles=(\d+) groups=(\d+) input_stalls=(\d+) "
        r"output_stalls=(\d+) source_bubbles=(\d+) resets=(\d+)", output)
    if len(results) != len(expected) or len(summaries) != 1:
        raise RTLSimulationError(f"Simulator did not report all scored outputs:\n{output}")
    metrics = dict(zip(("cycles", "groups", "input_stalls", "output_stalls",
                        "source_bubbles", "resets"), map(int, summaries[0])))
    metrics.update(codec=codec, evidence="rtl-simulation", simulator="Icarus Verilog",
                   accumulator_bits=acc_width, seed=seed, results=results,
                   stalls=metrics["input_stalls"] + metrics["output_stalls"])
    return metrics


def run_rtl_dot(
    weights: list[int], activations: list[int], codec: str = "dense5", seed: int = 27,
) -> dict:
    """Simulate an exact ternary/int8 dot product with checked signed32 sums.

    The Python dot is only a scoreboard oracle. ``result`` is parsed from the
    Verilog simulator, which must complete and match that independent oracle.
    A prefix group that exceeds signed32 raises instead of wrapping/saturating.
    """
    codec = _codec_name(codec)
    weights = validate_trits(weights)
    activations = list(activations)
    if len(weights) != len(activations):
        raise ValueError("weights and activations must have equal lengths")
    for index, activation in enumerate(activations):
        if type(activation) is not int or not -128 <= activation <= 127:
            raise ValueError(f"activation {index}: expected signed int8, got {activation!r}")
    expected = sum(weight * activation for weight, activation in zip(weights, activations))
    partial = 0
    for start in range(0, len(weights), 5):
        partial += sum(weight * activation for weight, activation in
                       zip(weights[start:start + 5], activations[start:start + 5]))
        if not -(2**31) <= partial < 2**31:
            raise OverflowError("dot product exceeds signed32 at a group boundary")
    report = _run_packets(_packets(weights, activations, codec), [(expected, False)], codec, seed)
    observed = report.pop("results")[0]
    report.update(result=observed["result"], error=bool(observed["error"]),
                  weight_count=len(weights), result_cycle=observed["cycles"],
                  encoded_weight_bits=report["groups"] * (8 if codec == "dense5" else 10))
    return report
