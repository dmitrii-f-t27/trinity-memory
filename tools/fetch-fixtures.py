#!/usr/bin/env python3
"""Fetch and verify the byte ranges listed in fixtures/manifest.json (I/O only).

Every range is fetched with an anonymous HTTP range request (no token is
sent), which must answer 206 with exactly the requested length, and is
checked against its sha256 before it is written to the cache that
trinity_memory.fixtures reads: build/fixtures/<repo>/<revision>/<file>/
<begin>-<end>.bin, and prefix.bin for the concatenated prefix chunks. Whole
checkpoints are never downloaded.

  python3 tools/fetch-fixtures.py              fetch what is missing, verify everything
  python3 tools/fetch-fixtures.py --offline    verify the cache only, fetch nothing
  python3 tools/fetch-fixtures.py --record REPO FILE BEGIN END --kind KIND --tensor NAME ...
                                               add one range to the manifest (explicit update)
  python3 tools/fetch-fixtures.py --write-lock regenerate fixtures/manifest.lock.json

Strict by default: a cache file that the manifest does not list is an error
(--no-strict reports it and goes on). The exit status is 1 on any missing
range, sha256 or length mismatch, HTTP error or unknown cache file.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from trinity_memory import fixtures as fx  # noqa: E402  (I/O only; loads no t27 library)


def _unknown_paths(manifest, cache: Path):
    """Cache entries under `cache` that no manifest file or range accounts for."""
    known_dirs = {}
    for entry in manifest.entries.values():
        directory = cache / entry.repo.replace("/", "--") / entry.revision / entry.name
        known_dirs[directory] = entry
    unknown = []
    if not cache.is_dir():
        return unknown
    for repo_dir in sorted(p for p in cache.iterdir()):
        for revision_dir in sorted(repo_dir.iterdir()) if repo_dir.is_dir() else [repo_dir]:
            for file_dir in sorted(revision_dir.iterdir()) if revision_dir.is_dir() else [revision_dir]:
                entry = known_dirs.get(file_dir)
                if entry is None:
                    unknown.append(file_dir)
                    continue
                for path in sorted(file_dir.iterdir()):
                    if path.name == "prefix.bin":
                        if path.stat().st_size > entry.prefix_end:
                            unknown.append(path)
                        continue
                    stem = path.name[:-4] if path.name.endswith(".bin") else ""
                    parts = stem.split("-")
                    if len(parts) != 2 or not all(p.isdigit() for p in parts) \
                            or (int(parts[0]), int(parts[1])) not in entry.ranges:
                        unknown.append(path)
    return unknown


class Run:
    def __init__(self, args):
        self.args = args
        self.errors, self.fetched, self.fetched_bytes, self.verified, self.verified_bytes = [], 0, 0, 0, 0

    def error(self, message):
        self.errors.append(message)
        print(f"FAIL {message}", file=sys.stderr)

    def one_range(self, remote, record):
        begin, end = record["begin"], record["end"]
        path = remote.cached_path(begin, end)
        if path.is_file():
            data = path.read_bytes()
            if fx.sha256(data) == record["sha256"]:
                return "verified", end - begin
            if self.args.offline:
                raise fx.FixtureError(f"{path}: sha256 {fx.sha256(data)} differs from the manifest {record['sha256']}")
            print(f"replacing {path}: sha256 differs from the manifest", file=sys.stderr)
        elif self.args.offline:
            raise fx.FixtureError(f"{remote.key}#{begin}-{end}: missing from the cache")
        data = remote.fetch(begin, end)
        digest = fx.sha256(data)
        if digest != record["sha256"]:
            raise fx.FixtureError(f"{remote.key}#{begin}-{end}: fetched sha256 {digest} differs from the "
                                  f"manifest {record['sha256']}")
        fx.write_atomic(path, data)
        return "fetched", end - begin

    def prefix(self, remote):
        """Verify prefix.bin chunk by chunk; fetch the chunks it lacks."""
        entry = remote.entry
        if not entry.prefix:
            return
        path = remote.directory / "prefix.bin"
        have = path.read_bytes() if path.is_file() else b""
        good = b""
        for chunk in entry.prefix:
            begin, end = chunk["begin"], chunk["end"]
            if len(have) >= end and fx.sha256(have[begin:end]) == chunk["sha256"]:
                good += have[begin:end]
                self.count("verified", end - begin)
                continue
            if self.args.offline:
                state = "differs from the manifest" if len(have) >= end else "is missing"
                raise fx.FixtureError(f"{path}: prefix chunk {begin}-{end} {state}")
            data = remote.fetch(begin, end)
            digest = fx.sha256(data)
            if digest != chunk["sha256"]:
                raise fx.FixtureError(f"{remote.key}#{begin}-{end}: fetched sha256 {digest} differs from the "
                                      f"manifest {chunk['sha256']}")
            good += data
            have = good + have[end:] if len(have) > end else good
            self.count("fetched", end - begin)
        if not path.is_file() or path.read_bytes() != good:
            fx.write_atomic(path, good)

    def count(self, state, size):
        if state == "fetched":
            self.fetched += 1
            self.fetched_bytes += size
        else:
            self.verified += 1
            self.verified_bytes += size

    def run(self, manifest, cache):
        remotes = [fx.Remote(entry, cache=cache, endpoint=self.args.endpoint, offline=self.args.offline)
                   for entry in manifest.entries.values()]
        jobs = []
        for remote in remotes:
            try:
                self.prefix(remote)
            except fx.FixtureError as error:
                self.error(str(error))
            jobs += [(remote, r) for r in remote.entry.ranges.values() if r["kind"] != "prefix"]

        def work(job):
            remote, record = job
            try:
                return self.one_range(remote, record), None
            except fx.FixtureError as error:
                return None, str(error)

        with ThreadPoolExecutor(max_workers=max(1, self.args.jobs)) as pool:
            for result, error in pool.map(work, jobs):
                if error:
                    self.error(error)
                else:
                    self.count(*result)
        for path in _unknown_paths(manifest, cache):
            message = f"{path}: not listed in {manifest.path.name}"
            if self.args.strict:
                self.error(message)
            else:
                print(f"note {message}", file=sys.stderr)
        mib = 1 << 20
        print(f"{len(manifest.lock())} manifest ranges: fetched {self.fetched} ({self.fetched_bytes / mib:.1f} MiB), "
              f"verified in cache {self.verified} ({self.verified_bytes / mib:.1f} MiB), {len(self.errors)} errors")
        return 1 if self.errors else 0


def record(args, manifest, cache):
    repo, name, begin, end = args.record[0], args.record[1], int(args.record[2]), int(args.record[3])
    remote = fx.Remote(manifest.file(repo, name), cache=cache, endpoint=args.endpoint, offline=args.offline)
    if (begin, end) in remote.entry.ranges:
        print(f"{remote.key}#{begin}-{end} is already in the manifest", file=sys.stderr)
        return 1
    if not args.kind:
        print("--record needs --kind", file=sys.stderr)
        return 2
    data = remote.fetch(begin, end)
    entry = {"file": name, "kind": args.kind, "tensor": args.tensor, "dtype": args.dtype,
             "ggml_type": args.ggml_type, "layout": args.layout, "shape": args.shape,
             "tensor_offset": args.tensor_offset, "begin": begin, "end": end,
             "sha256": fx.sha256(data), "used_by": args.used_by}
    entry = {k: v for k, v in entry.items() if v is not None}
    manifest.add_range(repo, entry)
    if args.kind == "prefix":
        Run(args).prefix(remote)
    else:
        fx.write_atomic(remote.cached_path(begin, end), data)
    manifest.save()
    fx.LOCK.write_text(manifest.lock_text())
    print(f"recorded {remote.key}#{begin}-{end} sha256 {entry['sha256']}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--manifest", type=Path, default=fx.MANIFEST, help="default fixtures/manifest.json")
    parser.add_argument("--cache", type=Path, default=fx.CACHE, help="default build/fixtures")
    parser.add_argument("--endpoint", help="Hub base URL (default $HF_ENDPOINT or https://huggingface.co)")
    parser.add_argument("--offline", action="store_true", help="verify the cache only; fetch nothing")
    parser.add_argument("--no-strict", dest="strict", action="store_false",
                        help="report cache files the manifest does not list instead of failing")
    parser.add_argument("--jobs", type=int, default=4, help="concurrent range requests (default 4)")
    parser.add_argument("--write-lock", action="store_true", help="regenerate fixtures/manifest.lock.json and exit")
    parser.add_argument("--record", nargs=4, metavar=("REPO", "FILE", "BEGIN", "END"),
                        help="fetch one new range and add it to the manifest")
    parser.add_argument("--kind", choices=fx.KINDS)
    parser.add_argument("--tensor")
    parser.add_argument("--dtype")
    parser.add_argument("--ggml-type", type=int)
    parser.add_argument("--layout")
    parser.add_argument("--shape", type=int, nargs="+")
    parser.add_argument("--tensor-offset", type=int)
    parser.add_argument("--used-by", nargs="+")
    args = parser.parse_args(argv)
    try:
        manifest = fx.Manifest(args.manifest)
        if args.write_lock:
            fx.LOCK.write_text(manifest.lock_text())
            print(f"wrote {fx.LOCK.relative_to(ROOT)} ({len(manifest.lock())} ranges)")
            return 0
        if args.record:
            return record(args, manifest, args.cache)
        return Run(args).run(manifest, args.cache)
    except fx.FixtureError as error:
        print(f"FAIL {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
