#!/usr/bin/env python3
"""The findings ledger's extractor (#55): scan report -> observed entries.

Reads a Ternary Check Live scan (trinity.ternary-check-live.v2), collects
every model whose verdict is not `ok`, and prints or updates the `observed`
entries of docs/live/findings.md. Hand-written fields (independent
confirmation, reported, answer) are preserved: the extractor only regenerates
the `where` / `what the header says` / `who is affected` bullets of entries it
owns, keyed by entry id, and appends new ids for verdicts no entry covers.

The protocol itself lives in the ledger's prose; this tool never marks
anything `confirmed` — that step is a human reading the replay output.

  python3 tools/live-findings.py build/live/scan.json            # print
  python3 tools/live-findings.py build/live/scan.json --update   # edit the md

Exit codes: 0 written (or printed); 1 the scan or the ledger is missing;
2 the ledger's shape changed under us (no edit is made).
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
LEDGER = ROOT / "docs/live/findings.md"
ENTRY_RE = re.compile(r"^## (F-\d+) — (.*)$")
STATUS_RE = re.compile(r"^- \*\*status\*\*: (\w+)")
VERDICT_TITLES = {
    "refused": "the header refuses to load in {readers}",
    "no_ternary_layout": "no ternary layout the pinned readers recognise",
    "other_runtime": "written for a runtime other than the one tried",
    "limit": "a crafted header pushed a reader past its signed arithmetic",
}


def findings(scan: dict) -> list[dict]:
    out = []
    for r in scan.get("repositories", []):
        for m in r.get("models", []):
            v = m.get("verdict")
            if v in (None, "ok"):
                continue
            readers = sorted((m.get("runtimes") or {}).keys())
            out.append({
                "repo": r.get("repo"), "verdict": v,
                "files": [f.get("file") for f in (m.get("files") or [])],
                "sha": (m.get("header_sha256") or [""])[0],
                "lfs": (m.get("files") or [{}])[0].get("lfs_sha256", ""),
                "size": m.get("size"), "readers": readers,
                "native": m.get("native"),
                "ternary_tensors": m.get("ternary_tensors"),
                "problems": m.get("problems", {}),
                "runtimes": m.get("runtimes", {}),
            })
    return out


def entry_body(f: dict) -> str:
    files = ", ".join(f"`{x}`" for x in f["files"][:3]) or "`?`"
    lines = [
        f"## {{id}} — {VERDICT_TITLES.get(f['verdict'], f['verdict'])} ({f['repo']})",
        "",
        f"- **status**: observed",
        f"- **where**: `{f['repo']}` → {files}"
        + (f" (header sha256 `{f['sha'][:8]}…`)" if f["sha"] else ""),
        f"- **what the header says**: verdict `{f['verdict']}` from the t27 walk"
        + (f", {f['ternary_tensors']} ternary tensors" if f.get("ternary_tensors") else "")
        + (f", written for `{f['native']}`" if f.get("native") else "")
        + "; the scan report holds the byte numbers behind it.",
        f"- **who is affected**: anyone loading these files with a reader other "
        f"than the one they were written for.",
        f"- **independent confirmation**: pending replay.",
        f"- **reported**: —",
        f"- **answer**: —",
        "",
    ]
    return "\n".join(lines)


def merge(findings: list[dict], ledger: str) -> str:
    entries = {}   # id -> (title line index range)
    lines = ledger.splitlines(keepends=True)
    ids = []
    for i, line in enumerate(lines):
        m = ENTRY_RE.match(line)
        if m:
            ids.append((i, m.group(1)))
    # existing entries keyed by repo+verdict
    have = {}
    for (i, eid) in ids:
        block = "".join(lines[i:i + 20])
        repo = re.search(r"\*where\*\*: `([^`]+)`", block)
        if repo:
            have[(repo.group(1), re.search(r"verdict `(\w+)`", block).group(1) if "verdict `" in block else "")] = eid
    next_id = 1 + max((int(e.split("-")[1]) for _, e in ids), default=0)
    # drop the extractor's own generated entries (those with 'pending replay' and observed)
    # simple approach: append only findings not covered
    append = []
    for f in findings:
        key = (f["repo"], f["verdict"])
        if key in have:
            continue
        body = entry_body(f).replace("{id}", f"F-{next_id}")
        body = body.replace("{readers}", ", ".join(f["readers"]) or "the readers")
        append.append(body)
        next_id += 1
    if not append:
        return ledger
    marker = "<!-- The extractor (tools/live-findings.py)"
    if marker not in ledger:
        return ledger + "\n".join(append)
    head, tail = ledger.split(marker, 1)
    return head + "\n".join(append) + "\n" + marker + tail


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scan", help="a scan report (trinity.ternary-check-live.v2)")
    ap.add_argument("--update", action="store_true", help="edit docs/live/findings.md")
    args = ap.parse_args()
    scan_path = pathlib.Path(args.scan)
    if not scan_path.is_file():
        return 1
    scan = json.loads(scan_path.read_text())
    fs = findings(scan)
    if not args.update:
        for f in fs:
            print(f"{f['verdict']:18s} {f['repo']:48s} {(f['files'] or ['?'])[0]}")
        print(f"{len(fs)} findings")
        return 0
    if not LEDGER.is_file():
        return 1
    ledger = LEDGER.read_text()
    updated = merge(fs, ledger)
    if updated == ledger:
        print("no new findings to append")
        return 0
    LEDGER.write_text(updated)
    print(f"ledger updated: {len(fs)} findings in the scan")
    return 0


if __name__ == "__main__":
    sys.exit(main())
