"""Reproducible synthetic storage experiment; CPU timings are illustrative."""

from datetime import datetime, timezone
import platform
import random
import statistics
import sys
from time import perf_counter_ns

from .codecs import CODECS, pack, unpack
from .container import HEADER_BYTES, decode_file, encode_file
from .sparsity import entropy_bpw


def _sparse_dataset(rng: random.Random, count: int, n: int, k: int) -> list[int]:
    values = []
    for start in range(0, count, n):
        group = [0] * min(n, count - start)
        for position in rng.sample(range(len(group)), min(k, len(group))):
            group[position] = rng.choice((-1, 1))
        values.extend(group)
    return values


def _timed(call, repeats: int):
    call()  # Untimed warm-up.
    samples = []
    result = None
    for _ in range(repeats):
        begin = perf_counter_ns()
        result = call()
        samples.append((perf_counter_ns() - begin) / 1_000_000)
    return result, statistics.median(samples)


def run_benchmark(count: int = 65536, repeats: int = 3, seed: int = 27) -> dict:
    if type(count) is not int or count < 1:
        raise ValueError("count must be a positive integer")
    if type(repeats) is not int or repeats < 1:
        raise ValueError("repeats must be a positive integer")
    rng = random.Random(seed)
    dense = ["baseline2", "dense5", "dense17", "dense22"]
    datasets = [
        ("uniform", [rng.choice((-1, 0, 1)) for _ in range(count)], dense),
        ("sparse_4_1", _sparse_dataset(rng, count, 4, 1), dense + ["sparse41", "sparse82"]),
        ("sparse_8_2", _sparse_dataset(rng, count, 8, 2), dense + ["sparse82"]),
    ]
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {"python": sys.version.split()[0],
                        "platform": f"{platform.system()} {platform.release()} {platform.machine()}"},
        "seed": seed, "repeats": repeats, "weights_per_dataset": count,
        "measurement_scope": "Synthetic integer trits. Exact bytes and verified round trips. "
                             "Median Python payload encode/decode wall time after one warm-up; "
                             "container creation/CRC/file I/O excluded from timing. No FPGA, "
                             "DDR, model quality, power, or tokens/s measurements.",
        "datasets": [],
    }
    for name, values, codecs in datasets:
        baseline_bytes = len(pack(values, "baseline2"))
        entry = {"name": name, "count": count, "zero_fraction": values.count(0) / count,
                 "entropy_bpw": entropy_bpw(values), "results": []}
        for name_codec in codecs:
            c = CODECS[name_codec]
            payload, encode_ms = _timed(lambda: pack(values, name_codec), repeats)
            recovered, decode_ms = _timed(lambda: unpack(payload, count, name_codec), repeats)
            container = encode_file(values, name_codec)
            if recovered != values or decode_file(container) != values:
                raise AssertionError(f"round trip failed: {name}/{name_codec}")
            entry["results"].append({
                "codec": name_codec, "group_size": c.group_size, "group_bits": c.group_bits,
                "payload_bytes": len(payload), "container_bytes": len(container),
                "payload_bpw": len(payload) * 8 / count,
                "container_bpw": (len(payload) + HEADER_BYTES) * 8 / count,
                "encode_ms": round(encode_ms, 6), "decode_ms": round(decode_ms, 6),
                "roundtrip": True,
                "ideal_payload_ratio_vs_baseline": baseline_bytes / len(payload),
            })
        report["datasets"].append(entry)
    return report
