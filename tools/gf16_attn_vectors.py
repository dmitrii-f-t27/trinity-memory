"""GF16 attention DDR3 framing and strict trace validation (no device access).

Builds the S=1 (position 0) input image for the gf16-attn-v1 controller from
a tiny or full Q16.16 model, and validates the emitted trace against the
tools.attn_reference.fpga_q16 datapath values bit for bit.
"""
import hashlib
import json
from pathlib import Path
import re

from tools import attn_reference as ar

MAGIC = 0x41545431


def regions(model, xs, run=1):
    dims = model["dims"]
    hidden, kv, head_dim = dims["hidden"], dims["kv_dim"], dims["head_dim"]
    if kv * 2 < hidden or not 1 <= hidden <= 2560 or len(xs) != 1 or len(xs[0]) != hidden:
        raise ValueError("GF16 attention slice-1 shape mismatch")
    positions = len(xs)
    result = {"doorbell": (64, [(run | (MAGIC << 32)) | (positions << 64)]),
              "scales": (80, [ar.to_q(model["scales"][s]) & 0xFFFFFFFF for s in ("q", "k", "v", "o")] +
                              [ar.to_q(head_dim ** -0.5) & 0xFFFFFFFF]),
              "x": (4096, [w & 0xFFFFFFFF for w in xs[0]]),
              "w_in": (8192, [ar.to_q(w) & 0xFFFFFFFF for w in model["w_in"]]),
              "w_sub": (10752, [ar.to_q(w) & 0xFFFFFFFF for w in model["w_sub"]])}
    for name, (_, words) in result.items():
        if name != "doorbell" and not all(-2**31 <= (w if w < 2**31 else w - 2**32) < 2**31 for w in words):
            raise ValueError("attention input out of Q16.16 range: " + name)
    for name, base, rows in (("q", 65536, hidden), ("k", 167936, kv),
                             ("v", 193536, kv), ("o", 219136, hidden)):
        weights = model[name]
        words = []
        for row in range(rows):
            for column in range(0, hidden, 64):
                word = 0
                for lane in range(min(64, hidden - column)):
                    value = weights[row * hidden + column + lane]
                    if value not in (-1, 0, 1):
                        raise ValueError("invalid ternary weight")
                    word |= {0: 0, 1: 1, -1: 2}[int(value)] << (lane * 2)
                words.append(word)
        result[name] = (base, words)
    return result


def write_inputs(work, model, xs, run=1):
    work = Path(work); work.mkdir(parents=True, exist_ok=True)
    manifest = {}
    with (work / "input.mem").open("w") as memory:
        for name, (base, words) in regions(model, xs, run).items():
            payload = b"".join(int(v).to_bytes(16, "little") for v in words)
            path = work / (name + ".bin"); path.write_bytes(payload)
            manifest[name] = {"word_address": base, "byte_address": base * 16, "file": path.name,
                              "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
            memory.write(f"@{base:x}\n")
            memory.writelines(f"{int(word):032x}\n" for word in words)
    (work / "inputs.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return work / "input.mem"


def expected_lines(stages, run=1, positions=1):
    dims_stage = {"n": "n", "q": "q", "k": "k", "v": "v", "qr": "qr", "kr": "kr",
                  "sc": "sc", "a": "a", "c": "c", "o": "o", "r": "r"}
    sequence = [("d", run, positions)]
    for tag, stage in ((103, "n"), (104, "q"), (105, "k"), (106, "v"), (107, "qr"),
                       (108, "kr"), (109, "sc"), (110, "a"), (111, "c"), (112, "o"),
                       (113, "r")):
        sequence.extend((chr(tag), i, v & 0xFFFFFFFF) for i, v in enumerate(stages[stage]))
    return sequence


def validate(raw, stages, run=1):
    if not raw or any(re.fullmatch(rb"[A-Za-z][0-9a-fA-F]{18}\n", line) is None
                      for line in raw.splitlines(keepends=True)):
        raise ValueError("malformed or truncated attention UART line")
    lines = [(t.decode(), int(a, 16), int(b, 16)) for t, a, b in
             re.findall(rb"([A-Za-z])([0-9a-fA-F]{8})([0-9a-fA-F]{10})\n", raw)]
    if any(t == "E" for t, _, _ in lines):
        raise ValueError("attention device reported error")
    data = [v for v in lines if v[0] not in ("G", "A")]
    wanted = expected_lines(stages, run)
    if data[:len(wanted)] != wanted:
        for i, (got, want) in enumerate(zip(data, wanted)):
            if got != want:
                raise ValueError(f"attention trace item {i}: {got} != {want}")
        raise ValueError("incomplete attention trace")
    summary = data[len(wanted):]
    keys = [("c", run), ("v", 0), ("v", 1), ("z", run)]
    if [v[:2] for v in summary] != keys or summary[-1][2] != 0:
        raise ValueError("attention completion/counters missing, duplicated or out of order")
    total, report, memory = [v[2] for v in summary[:3]]
    if not total or report + memory > total:
        raise ValueError("clock accounting exceeds total or is empty")
    return {"pass": True, "run": run, "profile": "gf16-attn-v1",
            "stage_values": sum(map(len, stages.values())),
            "capture_sha256": hashlib.sha256(raw).hexdigest(),
            "clock_split": {"total": total, "report_wait": report, "memory_wait": memory,
                            "controller_other": total - report - memory}}


def tiny_model(seed=7):
    """Two heads x 4 over one kv head, hidden 8 — the oracle's tiny fixture."""
    import random
    rng = random.Random(seed)
    d = {"hidden": 8, "heads": 2, "kv_heads": 1, "head_dim": 4,
         "kv_dim": 4, "group": 2, "pairs": 2}

    def trits(rows, cols):
        return [rng.choice((-1, 0, 1)) for _ in range(rows * cols)]

    return {
        "q": trits(d["hidden"], d["hidden"]),
        "k": trits(d["kv_dim"], d["hidden"]),
        "v": trits(d["kv_dim"], d["hidden"]),
        "o": trits(d["hidden"], d["hidden"]),
        "dims": dict(d),
        "w_in": [1.0, 0.5, -0.25, 1.25, 0.75, -1.0, 0.5, 0.5],
        "w_sub": [1.0, 0.75, -0.5, 0.25, 1.25, -1.0, 0.5, 0.5],
        "scales": {"q": 0.125, "k": 0.0625, "v": 0.25, "o": 0.5},
        "packed_sha256": {},
    }
