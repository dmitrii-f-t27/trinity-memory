"""GF16 attention DDR3 framing and strict trace validation (no device access).

Builds the S-position input image for the gf16-attn-v1 controller from a
Q16.16 model (x already in Q16.16 units), and validates the emitted trace
against the tools.attn_reference.fpga_q16 datapath values bit for bit.
"""
import hashlib
import json
from pathlib import Path
import re

from tools import attn_reference as ar

MAGIC = 0x41545431


def regions(model, xs, tables_q16, run=1, result=False):
    dims = model["dims"]
    hidden, kv, head_dim = dims["hidden"], dims["kv_dim"], dims["head_dim"]
    positions = len(xs)
    if (not 1 <= hidden <= 2560 or not 1 <= positions <= 8 or
            positions * kv + hidden > 8192 or len(xs[0]) != hidden or
            len(tables_q16) < positions):
        raise ValueError("GF16 attention shape/run mismatch")
    result = {"doorbell": (64, [(run | (MAGIC << 32)) | (positions << 64) | ((1 if result else 0) << 96)]),
              "scales": (80, [ar.to_q(model["scales"][s]) & 0xFFFFFFFF for s in ("q", "k", "v", "o")] +
                               [ar.to_q(head_dim ** -0.5) & 0xFFFFFFFF]),
              "w_in": (4096, [ar.to_q(w) & 0xFFFFFFFF for w in model["w_in"]]),
              "w_sub": (8192, [ar.to_q(w) & 0xFFFFFFFF for w in model["w_sub"]])}
    flat_x = []
    for x in xs:
        flat_x.extend(w & 0xFFFFFFFF for w in x)
    result["x"] = (32768, flat_x)
    rope = []
    for t in range(positions):
        for cos_q, sin_q in tables_q16[t]:
            rope.append(cos_q & 0xFFFFFFFF)
            rope.append(sin_q & 0xFFFFFFFF)
    result["rope"] = (321536, rope)
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


def write_inputs(work, model, xs, tables_q16, run=1, result=False):
    work = Path(work); work.mkdir(parents=True, exist_ok=True)
    manifest = {}
    with (work / "input.mem").open("w") as memory:
        for name, (base, words) in regions(model, xs, tables_q16, run, result).items():
            payload = b"".join(int(v).to_bytes(16, "little") for v in words)
            path = work / (name + ".bin"); path.write_bytes(payload)
            manifest[name] = {"word_address": base, "byte_address": base * 16, "file": path.name,
                              "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
            memory.write(f"@{base:x}\n")
            memory.writelines(f"{int(word):032x}\n" for word in words)
    (work / "inputs.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return work / "input.mem"


def expected_lines(stages, positions, result_only=False):
    """Per-position interleave matching the controller's emission order."""
    hidden = len(stages["r"]) // positions
    kv = len(stages["k"]) // positions
    heads = len(stages["sc"]) // sum(p + 1 for p in range(positions))
    sequence = [("d", 1, positions)]
    sc_at = 0
    for t in range(positions):
        base, kvbase = t * hidden, t * kv
        if not result_only:
            for tag, stage, lo, hi in ((103, "n", base, base + hidden), (104, "q", base, base + hidden),
                                       (105, "k", kvbase, kvbase + kv), (106, "v", kvbase, kvbase + kv),
                                       (107, "qr", base, base + hidden), (108, "kr", kvbase, kvbase + kv)):
                sequence.extend((chr(tag), i, v & 0xFFFFFFFF)
                                for i, v in enumerate(stages[stage][lo:hi]))
            # the controller interleaves per head: its scores, then its context
            for h in range(heads):
                take = t + 1
                sequence.extend((chr(109), h * take + i, v & 0xFFFFFFFF)
                                for i, v in enumerate(stages["sc"][sc_at:sc_at + take]))
                sc_at += take
                hd = hidden // heads
                sequence.extend((chr(110), h * hd + i, v & 0xFFFFFFFF)
                                for i, v in enumerate(stages["a"][base + h * hd:base + (h + 1) * hd]))
            for tag, stage in ((111, "c"), (112, "o")):
                sequence.extend((chr(tag), i, v & 0xFFFFFFFF)
                                for i, v in enumerate(stages[stage][base:base + hidden]))
        sequence.extend((chr(113), i, v & 0xFFFFFFFF)
                        for i, v in enumerate(stages["r"][base:base + hidden]))
    return sequence


def validate(raw, stages, run=1, positions=1, result_only=False):
    if not raw or any(re.fullmatch(rb"[A-Za-z][0-9a-fA-F]{18}\n", line) is None
                      for line in raw.splitlines(keepends=True)):
        raise ValueError("malformed or truncated attention UART line")
    lines = [(t.decode(), int(a, 16), int(b, 16)) for t, a, b in
             re.findall(rb"([A-Za-z])([0-9a-fA-F]{8})([0-9a-fA-F]{10})\n", raw)]
    if any(t == "E" for t, _, _ in lines):
        raise ValueError("attention device reported error")
    data = [v for v in lines if v[0] not in ("G", "A")]
    wanted = expected_lines(stages, positions, result_only)
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
    hidden = len(stages["r"]) // positions
    return {"pass": True, "run": run, "profile": "gf16-attn-v1",
            "positions": positions,
            "stage_values": positions * hidden if result_only else sum(len(stages[s]) for s in
                              ("n", "q", "k", "v", "qr", "kr", "sc", "a", "c", "o", "r")),
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
