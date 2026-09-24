#!/usr/bin/env python3
"""Decide whether a DDR3 bitstream may be loaded: the check behind `make ddr3-flash`.

  ddr3-flash-check.py BIT --expect <sha256>     the whole file must have this sha256
  ddr3-flash-check.py BIT --report build.json   the whole file must match the report's
                                                bitstream.sha256, or the file from its sync
                                                word on must match bitstream.sha256_from_sync

xc7frames2bit writes the date and time it ran into the .bit header, so a rebuild
of the same commit (a clean clone, say) never has the whole-file sha256 of the
report; what it reproduces is the file from the sync word (0xAA995566) on, which
is everything the FPGA receives. With --report both are accepted, and the line
printed says which one matched. Exit status 0 when the file may be loaded, 1
otherwise; nothing is loaded here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

SYNC_WORD = bytes.fromhex("AA995566")


def identity(data: bytes) -> dict:
    record = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    at = data.find(SYNC_WORD)
    if at >= 0:
        record["sync_offset"] = at
        record["sha256_from_sync"] = hashlib.sha256(data[at:]).hexdigest()
    return record


def check(bit: Path, expect: str | None = None, report: Path | None = None):
    """Return (ok, message)."""
    got = identity(bit.read_bytes())
    if expect:
        if got["sha256"] == expect:
            return True, f"{bit} sha256 {got['sha256']} as expected"
        return False, f"{bit} has sha256 {got['sha256']}, not {expect}"
    record = json.loads(report.read_text())["bitstream"]
    if got["sha256"] == record.get("sha256"):
        return True, f"{bit} sha256 {got['sha256']} as in {report} (whole file)"
    want_sync = record.get("sha256_from_sync")
    if want_sync and got.get("sha256_from_sync") == want_sync:
        return True, (f"{bit} sha256 from the sync word {got['sha256_from_sync']} as in {report} "
                      f"(whole file {got['sha256']}: another header time than the report's {record.get('sha256')})")
    return False, (f"{bit} has sha256 {got['sha256']} (from the sync word {got.get('sha256_from_sync')}), "
                   f"not {record.get('sha256')} (from the sync word {want_sync}) of {report}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("bit", type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--expect")
    group.add_argument("--report", type=Path)
    args = parser.parse_args()
    ok, message = check(args.bit, args.expect, args.report)
    print(f"ddr3-flash: {message}" + ("" if ok else "; nothing was flashed"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
