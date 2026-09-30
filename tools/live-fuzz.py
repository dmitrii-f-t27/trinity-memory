#!/usr/bin/env python3
"""Issue #98: bounded, seeded differential fuzzing of the real GGUF readers.

This is test orchestration, not a second implementation of reader rules.
Expected verdicts come from generated t27; observations come from the pinned
gguf.cpp via the I/O-only worker built by tools/live-replay.sh. Assertions
are counted separately as upstream refusals. Scanner limits/truncation have
no t27 verdict and are never counted as agreements. This does not exercise
the complete model loader or inference.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import random
import select
import struct
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trinity_memory import live
from tests.test_live import _gguf, _string

ROOT = Path(__file__).resolve().parent.parent
NAMES = live.SPEC_NAMES
# Assertions remain refusals/signals; thousands of debugger launches are
# diagnostic overhead, not part of reader acceptance (ggml.c backtrace).
READER_ENV = {**os.environ, "GGML_NO_BACKTRACE": "1"}


def cases(seed, count):
    rng = random.Random(seed)
    types = [0, 1, 2, 3, 6, 8, 9, 10, 12, 15, 16, 30, 34, 35, 36, 38, 40, 42, 142, 143, 255]
    widths = [0, 1, 2, 16, 31, 32, 63, 64, 65, 127, 128, 129, 256, 1024, 1320]
    for index in range(count):
        category = index % 8
        records, offset = [], 0
        for i in range(rng.randrange(5)):
            dims = [rng.choice(widths)] + [rng.randrange(1, 9) for _ in range(rng.randrange(4))]
            name = rng.choice([f"blk.{i}.attn_q.weight", f"w{i}", "token_embd.weight", "output.weight"])
            records.append((name, dims, rng.choice(types), offset))
            offset += rng.choice([0, 32, 64, 1024, 4096])
        keys = []
        if category == 1:
            # Bound repeated assertion exits: retain wrong-type boundaries
            # in the first 256 cases, then vary actual u32 alignments.
            vtype = rng.choice([0, 2, 4, 5, 8]) if index < 256 else 4
            alignment = rng.choice([0, 1, 2, 3, 16, 32, 64, 128])
            payload = {0: struct.pack("<B", alignment), 2: struct.pack("<H", alignment),
                       4: struct.pack("<I", alignment), 5: struct.pack("<i", alignment),
                       8: _string(str(alignment))}[vtype]
            keys.append(("general.alignment", vtype, payload))
        elif category == 2:
            # Typed metadata, duplicate keys and embedded NUL C-string edges.
            for _ in range(rng.randrange(1, 5)):
                name = rng.choice(["k", "k", "", "abc\0tail", "general.architecture"])
                keys.append((name, 8, _string(rng.choice(["", "llama", "x\0y", "bitnet-b1.58"]))))
        elif category == 3:
            names = ["blk.0.attn_q.weight"] * rng.randrange(4)
            keys = [("general.tensor_extra.name", 9,
                     struct.pack("<IQ", 8, len(names)) + b"".join(_string(n) for n in names)),
                    ("general.tensor_extra.prec_a4", 9,
                     struct.pack("<IQ", 7, rng.randrange(4)))]
            keys[-1] = (*keys[-1][:2], keys[-1][2] + bytes(rng.randrange(2) for _ in
                         range(struct.unpack("<Q", keys[-1][2][4:])[0])))
        elif category == 4:
            keys = [("prism.hadamard.version", 4, struct.pack("<I", rng.randrange(4))),
                    ("prism.hadamard.tied_output", rng.choice([7, 4]), b"\x01")]
            if keys[-1][1] == 4:
                keys[-1] = (*keys[-1][:2], struct.pack("<I", rng.randrange(2)))
        elif category == 5:
            # Arrays cover every scalar element type, nested/unknown element
            # types, bounded counts, and deliberately truncated payloads.
            elem = rng.randrange(14)
            length = rng.randrange(5)
            sizes = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
            items = b"".join(_string("a") for _ in range(length)) if elem == 8 else bytes(sizes.get(elem, 1) * length)
            keys = [("array", 9, struct.pack("<IQ", elem, length) + items)]
        header = _gguf(records, keys, arch=rng.choice(["llama", "qwen35", "bitnet-b1.58", "clip", None]))
        size = len(header) + (1 << 20)
        if category == 6:
            buf = bytearray(header)
            field = rng.choice(["magic", "version", "tensors", "keys"])
            at = {"magic": 0, "version": 4, "tensors": 8, "keys": 16}[field]
            if field in ("tensors", "keys"):
                struct.pack_into("<Q", buf, at, rng.choice([0, 1, 7, (1 << 64) - 1]))
            else:
                struct.pack_into("<I", buf, at, rng.choice([0, 1, 2, 3, 4, 0x46554747, 0x47475546]))
            header = bytes(buf)
        elif category == 7:
            cut = rng.randrange(len(header) + 1)
            header = header[:cut]
            size = cut if rng.randrange(2) else size
        yield header, size, category


class Worker:
    def __init__(self, binary):
        self.binary, self.process = binary, None

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            self.process.wait(timeout=5)
            self.process.stdin.close()
            self.process.stdout.close()
            self.process = None

    def observe(self, header, size):
        if self.process is None:
            self.process = subprocess.Popen([str(self.binary)], stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=READER_ENV)
        process = self.process
        try:
            process.stdin.write(struct.pack("<QQ", len(header), size) + header)
            process.stdin.flush()
        except BrokenPipeError:
            pass
        readable, _, _ = select.select([process.stdout], [], [], 5)
        if not readable:
            process.kill()
            self.close()
            return None, "timeout"
        answer = process.stdout.read(1)
        if answer in (b"A", b"R"):
            return answer == b"A", None
        code = process.wait(timeout=5)
        self.close()
        if code < 0:
            return False, f"signal:{-code}"
        raise RuntimeError(f"reader adapter failed: {self.binary}, exit {code}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=27)
    parser.add_argument("--cases", type=int, default=160000)
    parser.add_argument("--readers", type=Path, default=ROOT / "build/replay")
    parser.add_argument("--out", type=Path, default=ROOT / "build/live/fuzz.json")
    parser.add_argument("--crosscheck-single", type=int, default=24)
    args = parser.parse_args()
    if args.cases <= 0:
        parser.error("--cases must be positive")
    pins, workers = {}, {}
    for runtime, name in NAMES.items():
        spec = json.loads((ROOT / "specs/runtimes" / f"{name}.json").read_text())
        source = args.readers / f"src-{name}"
        head = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        if head != spec["commit"]:
            parser.error(f"{name}: source is not at the current pin")
        binary = args.readers / f"gguf_fuzz_{name}"
        libraries = sorted((source / "build").rglob("libggml-base.*"))
        pins[live.RUNTIMES[runtime]] = {
            "commit": head, "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "libraries": {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in libraries if p.is_file()}}
        workers[runtime] = Worker(binary)
    counts = {r: Counter() for r in workers}
    statuses, categories, errors = Counter(), Counter(), Counter()
    digest, failures = hashlib.sha256(), []
    started = time.monotonic()
    completed, crosschecked = 0, 0
    try:
        with tempfile.TemporaryDirectory(prefix="live-fuzz-") as temporary:
            path = Path(temporary) / "header.gguf"
            for index, (header, size, category) in enumerate(cases(args.seed, args.cases)):
                categories[str(category)] += 1
                digest.update(struct.pack("<QQ", len(header), size) + header)
                for runtime, worker in workers.items():
                    observed, error = worker.observe(header, size)
                    count = counts[runtime]
                    count["attempted"] += 1
                    if error:
                        count["timeouts" if error == "timeout" else "crashes"] += 1
                        errors[f"{runtime}:{category}:{error}"] += 1
                    if index < args.crosscheck_single and observed is not None:
                        path.write_bytes(header)
                        single = subprocess.run([str(args.readers / f"gguf_replay_{NAMES[runtime]}"), str(path), str(size)],
                                                capture_output=True, timeout=5, env=READER_ENV)
                        accepted = single.returncode == 0 and single.stdout.startswith(b"ACCEPTED")
                        if accepted != observed:
                            raise RuntimeError(f"batch/single disagreement at {index}, runtime {runtime}")
                        crosschecked += 1
                    walked, _ = live.walk(header, size, runtime)
                    statuses[f"{runtime}:{live.status_token(walked.reader)}"] += 1
                    if walked.reader in (live.TRUNCATED, live.LIMIT):
                        count["no_verdict"] += 1
                    elif observed is None:
                        count["not_compared"] += 1
                    else:
                        count["accepted" if observed else "refused"] += 1
                        agrees = (walked.reader == 0) == observed
                        count["agree" if agrees else "disagree"] += 1
                        if not agrees:
                            failures.append({"case": index, "category": category, "runtime": live.RUNTIMES[runtime],
                                             "file_size": size, "t27_reader": walked.reader, "upstream": observed})
                completed = index + 1
                if failures:
                    args.out.parent.mkdir(parents=True, exist_ok=True)
                    args.out.with_suffix(".failure.gguf").write_bytes(header)
                    break
                if completed % 10000 == 0:
                    print(f"{completed}/{args.cases} cases, {time.monotonic() - started:.1f}s", flush=True)
    finally:
        for worker in workers.values():
            worker.close()
    report = {"schema": "trinity.live-reader-fuzz.v1", "seed": args.seed, "requested_cases": args.cases,
              "completed_cases": completed, "corpus_sha256": digest.hexdigest(), "runtime_pins": pins,
              "counts": {live.RUNTIMES[r]: dict(sorted(c.items())) for r, c in counts.items()},
              "categories": dict(categories), "t27_reader_statuses": dict(sorted(statuses.items())),
              "upstream_errors": dict(sorted(errors.items())),
              "single_reader_crosschecks": crosschecked, "disagreements": failures,
              "elapsed_seconds": round(time.monotonic() - started, 3),
              "scope": "GGUF reader acceptance only; no complete model loader or inference"}
    report["reader_environment"] = {"GGML_NO_BACKTRACE": "1"}
    report["source_sha256"] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
                               ("tools/live-fuzz.py", "tests/upstream/gguf_fuzz_reader.c",
                                "t27/live.t27", "t27/runtimes.t27", "native/compiler.lock")}
    report["pass"] = (completed == args.cases and not failures and all(
        c["agree"] > 0 and c["accepted"] > 0 and c["refused"] > 0 and c["timeouts"] == 0 for c in counts.values()))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"pass": report["pass"], "cases": completed, "counts": report["counts"]}, sort_keys=True))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
