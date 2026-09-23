"""Byte-range fixtures from pinned public checkpoints (I/O glue only).

Reads the header prefix and selected tensors of large model files over HTTP
range requests, caches every range under build/fixtures/ and records its
sha256 in fixtures/manifest.lock.json. Whole checkpoints are never downloaded.
Container parsing is done by the t27 readers in trinity_memory.formats.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import urllib.request

from . import formats as f

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "build" / "fixtures"
LOCK = ROOT / "fixtures" / "manifest.lock.json"
USER_AGENT = "trinity-memory-ternary-check/0.4 (+https://github.com/dmitrii-f-t27/trinity-memory)"
FIRST_PREFIX = 1 << 20


class FixtureError(RuntimeError):
    pass


def _lock():
    return json.loads(LOCK.read_text()) if LOCK.is_file() else {}


def _save_lock(lock):
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(json.dumps(lock, indent=1, sort_keys=True) + "\n")


class Remote:
    """One file at one pinned revision of a Hugging Face repository."""

    def __init__(self, repo: str, revision: str, filename: str, size: int):
        self.repo, self.revision, self.filename, self.size = repo, revision, filename, size
        self.key = f"{repo}@{revision}/{filename}"
        self.directory = CACHE / repo.replace("/", "--") / revision / filename

    @property
    def url(self):
        return f"https://huggingface.co/{self.repo}/resolve/{self.revision}/{self.filename}"

    def _fetch(self, begin: int, end: int) -> bytes:
        if not 0 <= begin < end <= self.size:
            raise FixtureError(f"{self.key}: range {begin}-{end} outside file of {self.size} bytes")
        request = urllib.request.Request(self.url, headers={"Range": f"bytes={begin}-{end - 1}",
                                                            "User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=120) as response:
            if response.status != 206:
                raise FixtureError(f"{self.key}: server ignored the range request (HTTP {response.status})")
            data = response.read()
        if len(data) != end - begin:
            raise FixtureError(f"{self.key}: expected {end - begin} bytes, received {len(data)}")
        return data

    def _checked(self, begin: int, end: int, data: bytes) -> bytes:
        record = f"{self.key}#{begin}-{end}"
        digest = hashlib.sha256(data).hexdigest()
        lock = _lock()
        if record in lock and lock[record] != digest:
            raise FixtureError(f"{record}: sha256 {digest} differs from the lock {lock[record]}")
        if record not in lock:
            lock[record] = digest
            _save_lock(lock)
        return data

    def read(self, begin: int, end: int) -> bytes:
        """Bytes [begin, end), from the cache or the network, sha256-checked."""
        path = self.directory / f"{begin}-{end}.bin"
        if path.is_file():
            return self._checked(begin, end, path.read_bytes())
        data = self._checked(begin, end, self._fetch(begin, end))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data

    def prefix(self, needed: int) -> bytes:
        """A prefix of at least `needed` bytes. The cached prefix grows in powers
        of two, fetching only the bytes it lacks; each chunk is sha256-checked."""
        length = FIRST_PREFIX
        while length < needed:
            length *= 2
        length = min(length, self.size)
        path = self.directory / "prefix.bin"
        have = path.read_bytes() if path.is_file() else b""
        if len(have) < length:
            chunk = self._checked(len(have), length, self._fetch(len(have), length))
            have += chunk
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(have)
        return have[:length]

    def header_walk(self, find):
        """Grows the prefix until the t27 reader `find(prefix)` stops asking."""
        needed = FIRST_PREFIX
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
    f.gguf_check(info, remote.size)
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
    f.safetensors_check(info, remote.size)
    return info, dtype, remote.read(info.begin, info.end)


def safetensors_names(remote: Remote):
    """Tensor names of a safetensors header (listing only, for choosing fixtures)."""
    status, info, prefix = remote.header_walk(lambda p: f.safetensors_find(p, "\0"))
    if status not in (0, f.NOT_FOUND):
        raise f.FormatError(status)
    header = int.from_bytes(prefix[:8], "little")
    return sorted(k for k in json.loads(prefix[8: 8 + header]) if k != "__metadata__")
