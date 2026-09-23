"""Byte-range fixtures from pinned public checkpoints (I/O glue only).

fixtures/manifest.json is the single source of truth: it pins the revision
and size of every model file and lists every byte range this repository
reads, with its sha256. Ranges are fetched with HTTP range requests
(tools/fetch-fixtures.py fetches all of them), cached under build/fixtures/
and re-hashed on every read. Reading a range that the manifest does not list
is an error; `tools/fetch-fixtures.py --record` adds one explicitly. Whole
checkpoints are never downloaded. Container parsing is done by the t27
readers in trinity_memory.formats.
"""
from __future__ import annotations
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.request

from . import formats as f

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "build" / "fixtures"
MANIFEST = ROOT / "fixtures" / "manifest.json"
LOCK = ROOT / "fixtures" / "manifest.lock.json"
SCHEMA = "trinity.fixtures-manifest.v1"
KINDS = ("prefix", "header", "tensor", "scale", "trailer")
RANGE_KEYS = ("file", "kind", "tensor", "dtype", "ggml_type", "layout", "shape", "tensor_offset",
              "begin", "end", "sha256", "used_by")
USER_AGENT = "trinity-memory-ternary-check/0.4 (+https://github.com/dmitrii-f-t27/trinity-memory)"
DEFAULT_ENDPOINT = "https://huggingface.co"
OFFLINE_ENV = "TRINITY_FIXTURES_OFFLINE"
ATTEMPTS = 4


class FixtureError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_atomic(path: Path, data: bytes):
    """Write through a temporary file and rename, so that concurrent readers of
    a shared cache never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


class FileEntry:
    """One file of one pinned model revision, with its manifest ranges."""

    def __init__(self, model: dict, name: str, size: int):
        self.repo, self.revision, self.license = model["repo"], model["revision"], model["license"]
        self.name, self.size = name, size
        self.key = f"{self.repo}@{self.revision}/{name}"
        self.ranges: dict[tuple[int, int], dict] = {}
        self.prefix: list[dict] = []

    def add(self, record: dict):
        span = record["begin"], record["end"]
        if span in self.ranges:
            raise FixtureError(f"{self.key}: range {span[0]}-{span[1]} is listed twice")
        self.ranges[span] = record
        if record["kind"] == "prefix":
            self.prefix = sorted(self.prefix + [record], key=lambda r: r["begin"])

    @property
    def prefix_end(self) -> int:
        return self.prefix[-1]["end"] if self.prefix else 0


class Manifest:
    """fixtures/manifest.json, validated on load."""

    def __init__(self, path: Path = MANIFEST):
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError) as error:
            raise FixtureError(f"{self.path}: {error}") from error
        if self.data.get("schema") != SCHEMA:
            raise FixtureError(f"{self.path}: schema is {self.data.get('schema')!r}, expected {SCHEMA}")
        self.entries: dict[tuple[str, str], FileEntry] = {}
        repos = set()
        for model in self.data["models"]:
            for field in ("repo", "revision", "license", "files", "ranges"):
                if field not in model:
                    raise FixtureError(f"{self.path}: model {model.get('repo')!r} has no {field!r}")
            if model["repo"] in repos:
                raise FixtureError(f"{self.path}: {model['repo']} is listed twice (one revision per repository)")
            repos.add(model["repo"])
            if not re.fullmatch(r"[0-9a-f]{40}", model["revision"]):
                raise FixtureError(f"{model['repo']}: revision must be a full 40-hex commit sha")
            for item in model["files"]:
                self.entries[model["repo"], item["name"]] = FileEntry(model, item["name"], int(item["size"]))
            for record in model["ranges"]:
                entry = self.entries.get((model["repo"], record.get("file")))
                if entry is None:
                    raise FixtureError(f"{model['repo']}: range names unknown file {record.get('file')!r}")
                self._check_record(entry, record)
                entry.add(record)
        for entry in self.entries.values():
            at = 0
            for chunk in entry.prefix:
                if chunk["begin"] != at:
                    raise FixtureError(f"{entry.key}: prefix chunks must be contiguous from 0 (gap at {at})")
                at = chunk["end"]

    @staticmethod
    def _check_record(entry: FileEntry, record: dict):
        where = f"{entry.key}#{record.get('begin')}-{record.get('end')}"
        unknown = set(record) - set(RANGE_KEYS)
        if unknown:
            raise FixtureError(f"{where}: unknown fields {sorted(unknown)}")
        if record.get("kind") not in KINDS:
            raise FixtureError(f"{where}: kind must be one of {', '.join(KINDS)}")
        begin, end = record.get("begin"), record.get("end")
        if not (isinstance(begin, int) and isinstance(end, int) and 0 <= begin < end <= entry.size):
            raise FixtureError(f"{where}: range outside the file of {entry.size} bytes")
        if not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256"))):
            raise FixtureError(f"{where}: sha256 must be 64 lowercase hex digits")
        if record["kind"] != "prefix" and "tensor" not in record:
            raise FixtureError(f"{where}: a {record['kind']} range must name its tensor")

    def file(self, repo: str, name: str) -> FileEntry:
        try:
            return self.entries[repo, name]
        except KeyError:
            raise FixtureError(f"{repo}/{name} is not in {self.path}") from None

    def lock(self) -> dict:
        """The flat {"repo@revision/file#begin-end": sha256} view (fixtures/manifest.lock.json)."""
        return {f"{e.key}#{b}-{end}": r["sha256"] for e in self.entries.values()
                for (b, end), r in e.ranges.items()}

    def lock_text(self) -> str:
        return json.dumps(self.lock(), indent=1, sort_keys=True) + "\n"

    def dumps(self) -> str:
        """The manifest text: one line per file and per range, so diffs stay readable."""
        def dump(value, indent):
            pad = " " * indent
            if isinstance(value, dict) and not ({"begin", "end"} <= set(value) or set(value) >= {"name", "size"}):
                items = [f"{pad} {json.dumps(k)}: {dump(v, indent + 1).lstrip()}" for k, v in value.items()]
                return pad + "{\n" + ",\n".join(items) + "\n" + pad + "}"
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return pad + "[\n" + ",\n".join(dump(v, indent + 1) for v in value) + "\n" + pad + "]"
            return pad + json.dumps(value)
        return dump(self.data, 0) + "\n"

    def add_range(self, repo: str, record: dict):
        """Insert one range record in file and offset order (the explicit --record path)."""
        entry = self.file(repo, record["file"])
        record = {k: record[k] for k in RANGE_KEYS if k in record}
        self._check_record(entry, record)
        entry.add(record)
        model = next(m for m in self.data["models"] if m["repo"] == repo)
        order = [item["name"] for item in model["files"]]
        model["ranges"].append(record)
        model["ranges"].sort(key=lambda r: (order.index(r["file"]), r["begin"], r["end"]))

    def save(self):
        write_atomic(self.path, self.dumps().encode())


@lru_cache(maxsize=None)
def _manifest(path: str) -> Manifest:
    return Manifest(Path(path))


def manifest(path: Path | None = None) -> Manifest:
    return _manifest(str(path or MANIFEST))


def remote(repo: str, filename: str, **options) -> "Remote":
    """The pinned file `filename` of `repo`, as the manifest records it."""
    return Remote(manifest(options.pop("manifest_path", None)).file(repo, filename), **options)


def _retry_after(error: urllib.error.HTTPError, attempt: int) -> float:
    """Seconds to wait before retrying a 429 or 5xx: the RateLimit header's
    t=<seconds> or Retry-After when the Hub sends them, else a short backoff."""
    header = error.headers.get("RateLimit", "") if error.headers else ""
    match = re.search(r"t=(\d+)", header)
    if match:
        return min(float(match.group(1)) + 1, 310.0)
    retry = error.headers.get("Retry-After") if error.headers else None
    if retry and retry.isdigit():
        return min(float(retry), 310.0)
    return 2.0 * 2 ** attempt


class Remote:
    """One file at one pinned revision of a Hugging Face repository.

    Anonymous access only: no token is sent. Set TRINITY_FIXTURES_OFFLINE=1 (or
    offline=True) to read the cache and never touch the network."""

    _verified: dict = {}

    def __init__(self, entry: FileEntry, cache: Path | None = None, endpoint: str | None = None,
                 offline: bool | None = None):
        self.entry = entry
        self.repo, self.revision, self.filename, self.size = entry.repo, entry.revision, entry.name, entry.size
        self.key = entry.key
        self.cache = Path(cache) if cache else CACHE
        self.directory = self.cache / self.repo.replace("/", "--") / self.revision / self.filename
        self.endpoint = (endpoint or os.environ.get("HF_ENDPOINT") or DEFAULT_ENDPOINT).rstrip("/")
        self.offline = os.environ.get(OFFLINE_ENV) == "1" if offline is None else offline

    @property
    def url(self):
        return f"{self.endpoint}/{self.repo}/resolve/{self.revision}/{self.filename}"

    def fetch(self, begin: int, end: int) -> bytes:
        """Bytes [begin, end) over HTTP: the answer must be 206 with exactly that length."""
        if self.offline:
            raise FixtureError(f"{self.key}#{begin}-{end}: not in the cache and fetching is off "
                               f"(run python3 tools/fetch-fixtures.py)")
        if not 0 <= begin < end <= self.size:
            raise FixtureError(f"{self.key}: range {begin}-{end} outside file of {self.size} bytes")
        request = urllib.request.Request(self.url, headers={"Range": f"bytes={begin}-{end - 1}",
                                                            "User-Agent": USER_AGENT})
        for attempt in range(ATTEMPTS):
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    if response.status != 206:
                        raise FixtureError(f"{self.key}: server ignored the range request (HTTP {response.status})")
                    data = response.read()
                break
            except urllib.error.HTTPError as error:
                if (error.code == 429 or error.code >= 500) and attempt + 1 < ATTEMPTS:
                    time.sleep(_retry_after(error, attempt))
                    continue
                raise FixtureError(f"{self.key}#{begin}-{end}: HTTP {error.code} {error.reason}") from error
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                if attempt + 1 < ATTEMPTS:
                    time.sleep(2.0 * 2 ** attempt)
                    continue
                raise FixtureError(f"{self.key}#{begin}-{end}: {error}") from error
        if len(data) != end - begin:
            raise FixtureError(f"{self.key}: expected {end - begin} bytes, received {len(data)}")
        return data

    def record(self, begin: int, end: int) -> dict:
        record = self.entry.ranges.get((begin, end))
        if record is None:
            raise FixtureError(f"{self.key}#{begin}-{end} is not in fixtures/manifest.json; add it with "
                               f"python3 tools/fetch-fixtures.py --record {self.repo} {self.filename} {begin} {end} "
                               f"--kind KIND --tensor NAME")
        return record

    def _check(self, record: dict, data: bytes, origin: str) -> bytes:
        digest = sha256(data)
        if digest != record["sha256"]:
            raise FixtureError(f"{self.key}#{record['begin']}-{record['end']} ({origin}): sha256 {digest} "
                               f"differs from the manifest {record['sha256']}")
        return data

    def cached_path(self, begin: int, end: int) -> Path:
        return self.directory / f"{begin}-{end}.bin"

    def read(self, begin: int, end: int) -> bytes:
        """Bytes [begin, end) of a manifest range, from the cache or the network,
        sha256-checked on every read."""
        record = self.record(begin, end)
        if record["kind"] == "prefix":
            return self.prefix(end)[begin:end]
        path = self.cached_path(begin, end)
        if path.is_file():
            return self._check(record, path.read_bytes(), str(path))
        data = self._check(record, self.fetch(begin, end), self.url)
        write_atomic(path, data)
        return data

    def read_within(self, begin: int, end: int) -> bytes:
        """Bytes [begin, end) cut from the smallest manifest range that holds them."""
        if end <= self.entry.prefix_end:
            return self.prefix(end)[begin:end]
        holders = [span for span, r in self.entry.ranges.items()
                   if r["kind"] != "prefix" and span[0] <= begin and end <= span[1]]
        if not holders:
            self.record(begin, end)
        low, high = min(holders, key=lambda span: span[1] - span[0])
        return self.read(low, high)[begin - low: end - low]

    def prefix(self, needed: int) -> bytes:
        """The manifest prefix chunks up to the first that ends at or after
        `needed`, concatenated. Cached chunks are re-hashed; missing ones are
        fetched and appended to prefix.bin."""
        chunks = []
        for chunk in self.entry.prefix:
            chunks.append(chunk)
            if chunk["end"] >= needed:
                break
        else:
            raise FixtureError(f"{self.key}: {needed} header bytes needed, the manifest pins a prefix of "
                               f"{self.entry.prefix_end}; add a prefix chunk with --record")
        path = self.directory / "prefix.bin"
        have = path.read_bytes() if path.is_file() else b""
        grown = False
        for chunk in chunks:
            begin, end = chunk["begin"], chunk["end"]
            if len(have) >= end:
                key = (str(path), begin, end, chunk["sha256"], len(have), path.stat().st_mtime_ns)
                if key not in self._verified:
                    self._check(chunk, have[begin:end], str(path))
                    self._verified[key] = True
                continue
            if len(have) != begin:
                raise FixtureError(f"{path}: {len(have)} bytes do not end at a manifest chunk boundary; delete it")
            have += self._check(chunk, self.fetch(begin, end), self.url)
            grown = True
        if grown:
            write_atomic(path, have)
        return have[: chunks[-1]["end"]]

    def header_walk(self, find):
        """Grows the prefix until the t27 reader `find(prefix)` stops asking."""
        needed = 1
        while True:
            prefix = self.prefix(needed)
            status, info = find(prefix)[:2]
            if status != f.TRUNCATED:
                return status, info, prefix
            if info.needed <= len(prefix):
                raise FixtureError(f"{self.key}: reader asked for {info.needed} bytes with {len(prefix)} read")
            needed = info.needed


def gguf_tensor(remote: Remote, name: str):
    """(TensorInfo, stored bytes) of one GGUF tensor."""
    status, info, _ = remote.header_walk(lambda p: f.gguf_find(p, name))
    if status != 0:
        raise f.FormatError(status)
    size = f.gguf_tensor_bytes(info)
    if size == 0:
        raise FixtureError(f"{remote.key}: {name} has ggml type {info.tensor_type}, not a ternary layout")
    begin = info.data_start + info.offset
    return info, remote.read(begin, begin + size)


def gguf_header(remote: Remote):
    status, info, prefix = remote.header_walk(lambda p: f.gguf_find(p, "\0"))
    if status not in (0, f.NOT_FOUND):
        raise f.FormatError(status)
    return prefix[: info.data_start], info


def safetensors_tensor(remote: Remote, name: str):
    """(SafeInfo, dtype, bytes) of one safetensors tensor."""
    status, info, prefix = remote.header_walk(lambda p: f.safetensors_find(p, name))
    if status != 0:
        raise f.FormatError(status)
    _, info, dtype = f.safetensors_find(prefix, name)
    return info, dtype, remote.read(info.begin, info.end)


def safetensors_names(remote: Remote):
    """Tensor names of a safetensors header (listing only, for choosing fixtures)."""
    status, info, prefix = remote.header_walk(lambda p: f.safetensors_find(p, "\0"))
    if status not in (0, f.NOT_FOUND):
        raise f.FormatError(status)
    header = int.from_bytes(prefix[:8], "little")
    return sorted(k for k in json.loads(prefix[8: 8 + header]) if k != "__metadata__")
