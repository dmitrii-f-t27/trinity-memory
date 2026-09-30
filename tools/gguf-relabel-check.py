#!/usr/bin/env python3
"""Relabel check for legacy group-128 Q2_0 files (findings ledger, #55).

Copies a cached GGUF header, rewrites every tensor record of type 42 (Q2_0)
to 142 (PrismML's PQ2_0, the fork's name for the same group-128 bytes; the
fork's own gguf.cpp says the legacy layout "is byte-identical to PQ2_0"),
and feeds the copy to each pinned reader built by tools/live-replay.sh.
Only the 4-byte type field of each record changes; offsets and weights do
not. This checks the GGUF reader only: it says nothing about running the model.

  python3 tools/gguf-relabel-check.py build/live/scan.json build/replay out.json
"""
from __future__ import annotations
import json, pathlib, struct, subprocess, sys

SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}

def relabel(header: bytes, frm: int = 42, to: int = 142) -> tuple[bytes, int]:
    b = bytearray(header); p = 4
    def u(fmt):
        nonlocal p; v = struct.unpack_from(fmt, b, p)[0]; p += struct.calcsize(fmt); return v
    def skip_str():
        nonlocal p; n = u("<Q"); p += n
    def skip_val(t):
        nonlocal p
        if t == 8: skip_str()
        elif t == 9:
            at = u("<I"); n = u("<Q")
            for _ in range(n): skip_val(at)
        else: p += SIZES[t]
    if b[:4] != b"GGUF": raise ValueError("not a GGUF header")
    u("<I"); n_tensors = u("<Q"); n_kv = u("<Q")
    for _ in range(n_kv): skip_str(); skip_val(u("<I"))
    changed = 0
    for _ in range(n_tensors):
        skip_str(); dims = u("<I"); p += 8 * dims; at = p
        if u("<I") == frm: struct.pack_into("<I", b, at, to); changed += 1
        u("<Q")
    return bytes(b), changed

def main() -> int:
    scan, replay, out = map(pathlib.Path, sys.argv[1:4])
    report = json.loads(scan.read_text()); cache = scan.parent / "headers" / "sha256"
    results = []
    for repo in report["repositories"]:
        for model in repo.get("models", []):
            for f in model.get("files", []):
                if not any(t == 42 for t, _ in model.get("ggml_types", [])): continue
                src = cache / f["lfs_sha256"][:2] / f"{f['lfs_sha256']}.header"
                data, changed = relabel(src.read_bytes())
                dst = out.parent / f"{f['lfs_sha256'][:12]}.142.header"; dst.write_bytes(data)
                answers = {}
                for binary in sorted(replay.glob("gguf_replay_*")):
                    before = subprocess.run([binary, src, str(f["size"])], capture_output=True, text=True)
                    after = subprocess.run([binary, dst, str(f["size"])], capture_output=True, text=True)
                    answers[binary.name.removeprefix("gguf_replay_")] = {
                        "before": (before.stdout + before.stderr).strip(),
                        "after": (after.stdout + after.stderr).strip()}
                results.append({"repo": repo["repo"], "file": f["file"], "lfs_sha256": f["lfs_sha256"],
                                "records_relabelled": changed, "readers": answers})
    out.write_text(json.dumps(results, indent=1) + "\n")
    return 0

if __name__ == "__main__":
    sys.exit(main())
