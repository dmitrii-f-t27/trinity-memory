"""Ternary Check Live (issues #48-#51): public ternary GGUF models on the Hugging
Face Hub, checked from their headers (I/O glue only).

Discovery uses the public Hub API anonymously: no token is ever sent. Every
observation is pinned to the commit the Hub reports for the repository at the
time of the scan. A model is one GGUF file, or the parts of a split model
(<prefix>-KKKKK-of-NNNNN.gguf, read together as llama.cpp's loader reads
them). Each file's header is read with HTTP range requests at that commit,
grown until the t27 reader stops asking for bytes: the first read is 1 MiB
(the whole file when it is smaller), later reads at most double what was
read, and nothing past 256 MiB of a file is read, so a model's weights are not
downloaded. Every verdict comes from t27 (t27/live.t27 over the runtime
tables of t27/runtimes.t27): tlv_model applies each runtime's GGUF reader
and model loader to the model's headers, tlv_native names the runtime the
model is written for, tlv_file_verdict folds the verdict there. This module
only moves bytes, counts the verdicts and writes the report.

Byte-identical models (the same LFS SHA-256 for every file) are read once;
the report names the first repository that serves them. Headers are cached
under build/live/headers by their file's LFS SHA-256 (else by a hash of
repository, commit and file name), so `--recheck` can make every verdict
again offline, and `--replay` can feed them to the upstream readers.
"""
from __future__ import annotations
import argparse
import ctypes as C
import datetime as dt
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from . import _native as n
from . import formats as f
from .fixtures import _retry_after, write_atomic

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "build" / "live" / "scan.json"
HEADERS = ROOT / "build" / "live" / "headers"
COMPILER_LOCK = ROOT / "native" / "compiler.lock"
SCHEMA = "trinity.ternary-check-live.v2"
USER_AGENT = "trinity-memory-ternary-check-live/0.6 (+https://github.com/dmitrii-f-t27/trinity-memory)"
DEFAULT_ENDPOINT = "https://huggingface.co"
OFFLINE_ENV = "TRINITY_LIVE_OFFLINE"

# Hub search is a substring match, so these terms cover 1.58bit, b1.58,
# b1_58, tq1_0, ptq1_0, tq2_0 and pq2_0 too.
QUERIES = ("ternary", "bitnet", "bonsai", "1.58", "1_58", "trilm", "falcon-e", "q1_0", "q2_0", "i2_s")
_T = r"(?<![a-z0-9])"
_E = r"(?![a-z0-9])"
NAME = re.compile(rf"ternary|bitnet|1\.58|1_58|bonsai|trilm|falcon-e|"
                  rf"{_T}(?:tq1_0|tq2_0|pq2_0|ptq1_0|i2_s|q2_0|q1_0){_E}", re.IGNORECASE)
# File names that announce a layout with a contract (a model name such as
# "Ternary-Bonsai" says nothing about the file's layout).
LAYOUT_FILE = re.compile(rf"{_T}(?:tq1_0|tq2_0|pq2_0|ptq1_0|i2_s|tl1|tl2|q2_0|q2_g\d+|q2_0_g\d+|q1_0){_E}",
                         re.IGNORECASE)
SKIP_FILE = re.compile(rf"{_T}(?:mmproj|imatrix|lora(?![-_]?merged)){_E}", re.IGNORECASE)
FLOAT_FILE = re.compile(rf"{_T}(?:b?f16|f32|fp16|fp32){_E}", re.IGNORECASE)
# The names llama.cpp derives the parts of a split model from (llama_split_path).
SHARD = re.compile(r"^(.+)-(\d{5})-of-(\d{5})\.gguf$")
REVISION = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
FIRST_READ = 1 << 20
MODELS_PER_REPO = 12
UNTAGGED_MODELS = 3
GGUF_PAGES = 2
LOG_EVERY = 25

# ---- t27/live.t27 --------------------------------------------------------------------

U64 = C.c_uint64
LLAMA_CPP, PRISMML, BITNET_CPP = 1, 2, 3
RUNTIMES = {LLAMA_CPP: "llama.cpp", PRISMML: "prismml", BITNET_CPP: "bitnet.cpp"}
NATIVE = {0: "other", **RUNTIMES}
RUN = {0: "accepts", 1: "refuses", 2: "ignores_rotation"}
FILE = {0: "ok", 1: "refused", 2: "no_ternary_layout", 3: "undecided", 4: "other_runtime"}
TRUNCATED = -55
LIMIT = -84
PART_FIELDS = 5
# Status classes -63 .. -87 (specs/formats/OWNERS.md), after those of formats.t27.
TOKENS = {-63: "hadamard_type", -64: "hadamard_missing", -65: "hadamard_version", -66: "hadamard_block",
          -67: "hadamard_transform", -68: "hadamard_signs", -69: "hadamard_arch", -70: "hadamard_name",
          -71: "hadamard_tensor", -72: "offsets", -73: "magic", -74: "version", -75: "endian", -76: "key",
          -77: "alignment", -78: "name", -79: "shape", -80: "type", -81: "row", -82: "bounds", -83: "arch",
          -84: "limit", -85: "split", -86: "count", -87: "short"}
# Ternary layouts bitnet.cpp reads without a storage contract in formats.t27.
LAYOUTS = {101: "TL1", 102: "TL2"}


def status_token(status: int) -> str:
    return TOKENS.get(status) or f.TOKENS.get(status) or f"status {status}"


def layout_name(layout: int) -> str:
    return LAYOUTS.get(layout) or f.NAMES.get(layout, str(layout))


class LiveError(RuntimeError):
    pass


class Walk(C.Structure):
    _fields_ = [("status", C.c_int32), ("reader", C.c_int32), ("record", U64), ("expected", U64), ("found", U64),
                ("needed", U64), ("version", C.c_uint32), ("keys", U64), ("tensors", U64), ("records_at", U64),
                ("data_start", U64), ("alignment", U64), ("ternary", U64), ("ternary_ok", U64), ("read", U64),
                ("prism", C.c_bool), ("bitnet", C.c_bool)]


class Key(C.Structure):
    _fields_ = [("found", C.c_bool), ("vtype", C.c_uint32), ("at", U64), ("elem", C.c_uint32),
                ("count", U64), ("items", U64)]


class Hadamard(C.Structure):
    _fields_ = [("present", C.c_bool), ("block_size", C.c_uint32), ("names", U64), ("inverse", U64),
                ("widths", U64), ("signs", U64), ("explicit_signs", C.c_bool), ("gdn_v_grouped", C.c_bool),
                ("index", U64)]


class Run(C.Structure):
    _fields_ = [("verdict", C.c_int32), ("status", C.c_int32), ("part", U64), ("record", U64), ("expected", U64),
                ("found", U64)]


_WALK_ARGS = [n.U8, U64, n.SZ, U64, C.c_int32, n.I32, n.I32, C.POINTER(C.c_uint32), C.POINTER(U64),
              C.POINTER(U64), n.SZ, C.POINTER(Walk)]
_TABLE_ARGS = [n.U8, n.SZ, C.POINTER(U64), n.SZ]


def max_header() -> int:
    """TLV_MAX_HEADER: nothing past it is read (the verdict is `limit`)."""
    return int(n.call("tlv_max_header", U64, []))


def walk(header: bytes, file_size: int, runtime: int, records: bool = False, data=None):
    """(Walk, per-record arrays or None) from tlv_walk on one file's header;
    `data` may pass octets already made."""
    data = n.octets(header) if data is None else data
    out = Walk()
    one_i, one_f, one_t = (C.c_int32 * 1)(), (C.c_int32 * 1)(), (C.c_uint32 * 1)()
    one_a, one_s = (U64 * 1)(), (U64 * 1)()
    n.call("tlv_walk", C.c_int32, _WALK_ARGS, data, 0, len(header), file_size, runtime, one_i, one_f, one_t,
           one_a, one_s, 0, C.byref(out))
    if not records or out.read == 0:
        return out, None
    count = int(out.read)
    arrays = ((C.c_int32 * count)(), (C.c_int32 * count)(), (C.c_uint32 * count)(), (U64 * count)(), (U64 * count)())
    out = Walk()
    n.call("tlv_walk", C.c_int32, _WALK_ARGS, data, 0, len(header), file_size, runtime, *arrays, count, C.byref(out))
    return out, arrays


class Model:
    """The headers of one model's files in the part-table layout of
    t27/live.t27: the first file first, the others in part order."""

    def __init__(self, parts: list):
        # parts: (file name, header bytes, file size)
        self.parts = parts
        self.bytes = b"".join(header for _, header, _ in parts)
        self.data = n.octets(self.bytes)
        rows, at = [], 0
        for name, header, size in parts:
            shard = SHARD.match(name)
            rows += [at, len(header), size, int(shard.group(2)) if shard else 0, int(shard.group(3)) if shard else 0]
            at += len(header)
        self.table = (U64 * len(rows))(*rows)

    def args(self):
        return self.data, len(self.bytes), self.table, len(self.parts)

    def verdict(self, runtime: int):
        out = Run()
        value = n.call("tlv_model", C.c_int32, _TABLE_ARGS + [C.c_int32, C.POINTER(Run)], *self.args(), runtime,
                       C.byref(out))
        return value, out

    def native(self) -> int:
        return n.call("tlv_native", C.c_int32, _TABLE_ARGS, *self.args())

    def loaded(self) -> int:
        return int(n.call("tlv_loaded", U64, _TABLE_ARGS, *self.args()))

    def ternary(self, runtime: int) -> int:
        return int(n.call("tlv_ternary_count", U64, _TABLE_ARGS + [C.c_int32], *self.args(), runtime))

    def hadamard(self, rows: int):
        out = Hadamard()
        status = n.call("tlv_hadamard", C.c_int32, _TABLE_ARGS + [C.POINTER(Hadamard)], self.data, len(self.bytes),
                        self.table, rows, C.byref(out))
        return status, out

    def text(self, key: str) -> str | None:
        """A string value of the first file for display (moving bytes, not a verdict), up to its first NUL."""
        out, text = Key(), key.encode()
        header = self.parts[0][1]
        status = n.call("tlv_key", C.c_int32, [n.U8, n.SZ, C.c_char_p, n.SZ, C.POINTER(Key)],
                        self.data, len(header), text, len(text), C.byref(out))
        if status < 0 or not out.found or out.vtype != 8:
            return None
        length = int.from_bytes(header[out.at: out.at + 8], "little")
        return header[out.at + 8: out.at + 8 + length].split(b"\0", 1)[0].decode("utf-8", "replace")


def file_verdict(run: int, status: int, ternary: int, native: int) -> int:
    return n.call("tlv_file_verdict", C.c_int32, [C.c_int32, C.c_int32, U64, C.c_int32], run, status, ternary, native)


# ---- the Hub ---------------------------------------------------------------------------

class Hub:
    """The public Hub API and range reads, anonymously, one request at a time
    with at least `interval` seconds between requests. 429 and 5xx answers,
    and connections that fail before the whole body is read, are retried
    (after the time the Hub asks for: fixtures._retry_after)."""

    def __init__(self, endpoint: str | None = None, interval: float = 1.5, attempts: int = 6,
                 offline: bool | None = None, log=None):
        self.endpoint = (endpoint or os.environ.get("HF_ENDPOINT") or DEFAULT_ENDPOINT).rstrip("/")
        self.interval, self.attempts = interval, attempts
        self.offline = os.environ.get(OFFLINE_ENV) == "1" if offline is None else offline
        self.log = log or (lambda message: None)
        self._last = 0.0
        self.requests = 0

    def _fetch(self, url: str, headers: dict | None = None, check=None, limit: int | None = None):
        """(status, headers, body) after throttling and retries. The body is
        read inside the retry loop; `check(status, headers)` raises LiveError
        for an answer no retry can mend; at most `limit` + 1 bytes are read."""
        if self.offline:
            raise LiveError(f"{url}: network access is off ({OFFLINE_ENV}=1)")
        if not url.startswith(self.endpoint + "/"):
            raise LiveError(f"{url}: not under {self.endpoint}")
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
        for attempt in range(self.attempts):
            wait = self._last + self.interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.requests += 1
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    status, answer = response.status, response.headers
                    if check:
                        check(status, answer)
                    body = response.read() if limit is None else response.read(limit + 1)
                    length = answer.get("Content-Length", "")
                    if length.isdigit() and len(body) < int(length) and (limit is None or int(length) <= limit + 1):
                        raise http.client.IncompleteRead(body, int(length) - len(body))
                return status, answer, body
            except urllib.error.HTTPError as error:
                error.close()
                if (error.code == 429 or error.code >= 500) and attempt + 1 < self.attempts:
                    delay = _retry_after(error, attempt)
                    self.log(f"HTTP {error.code}: waiting {delay:.0f} s")
                    time.sleep(delay)
                    continue
                raise LiveError(f"{url}: HTTP {error.code} {error.reason}") from error
            except LiveError:
                raise
            except (OSError, http.client.HTTPException, ValueError) as error:
                if attempt + 1 < self.attempts:
                    time.sleep(2.0 * 2 ** attempt)
                    continue
                raise LiveError(f"{url}: {type(error).__name__}: {error}") from error
        raise LiveError(f"{url}: no answer after {self.attempts} attempts")

    def api(self, path: str, params: dict | None = None, pages: int = 20):
        url = f"{self.endpoint}/api/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        results, seen = [], 0
        while url:
            _, answer, body = self._fetch(url)
            try:
                value = json.loads(body)
            except ValueError as error:
                raise LiveError(f"{url}: not JSON") from error
            if not isinstance(value, list):
                return value
            results.extend(value)
            seen += 1
            url = self._next(answer.get("Link", ""), url) if seen < pages else None
        return results

    def _next(self, header: str, current: str) -> str | None:
        link = _next_link(header)
        if not link:
            return None
        link = urllib.parse.urljoin(current, link)
        return link if link.startswith(self.endpoint + "/") else None

    def search(self, query: str) -> list:
        return self.api("models", {"search": query, "filter": "gguf", "limit": 1000})

    def gguf_models(self, pages: int) -> list:
        """The most downloaded GGUF repositories, with their file names."""
        return self.api("models", {"filter": "gguf", "sort": "downloads", "direction": -1, "limit": 1000,
                                   "full": "true"}, pages=pages)

    def model(self, repo: str) -> dict:
        return self.api(f"models/{repo}", {"blobs": "true"})

    def read(self, repo: str, revision: str, filename: str, begin: int, end: int, size: int) -> bytes:
        """Bytes [begin, end) of a file at a commit. The answer must be 206 with
        Content-Range "bytes begin-(end - 1)/size", or 200 only for a request
        of the whole file."""
        url = f"{self.endpoint}/{repo}/resolve/{revision}/{urllib.parse.quote(filename)}"
        where = f"{repo}@{revision}/{filename}"

        def check(status, answer):
            if status == 200:
                if not (begin == 0 and end == size):
                    raise LiveError(f"{where}: server ignored the range request (HTTP 200)")
            elif status == 206:
                content = answer.get("Content-Range", "")
                if content != f"bytes {begin}-{end - 1}/{size}":
                    raise LiveError(f"{where}: Content-Range {content!r} for bytes {begin}-{end - 1} of {size}")
            else:
                raise LiveError(f"{where}: HTTP {status}")

        _, _, data = self._fetch(url, {"Range": f"bytes={begin}-{end - 1}"}, check, limit=end - begin)
        if len(data) != end - begin:
            raise LiveError(f"{where}: expected {end - begin} bytes, received {len(data)}")
        return data


def _next_link(header: str) -> str | None:
    for part in header.split(","):
        match = re.match(r'\s*<([^>]+)>\s*;\s*rel="?next"?', part)
        if match:
            return match.group(1)
    return None


# ---- discovery -------------------------------------------------------------------------

def discover(hub: Hub, queries=QUERIES, min_downloads: int = 100, gguf_pages: int = GGUF_PAGES,
             log=None) -> tuple[dict, dict]:
    """(repository id -> 30-day downloads, what discovery saw): search hits
    whose name passes NAME, and GGUF repositories among the most downloaded
    whose file names announce a ternary layout (a generic repository name may
    hold a TQ1_0 file). A query that fails is recorded and skipped."""
    log = log or (lambda message: None)
    found, seen = {}, {"hits": {}, "gguf_repositories": 0, "errors": []}

    def keep(model):
        repo = model.get("id") or model.get("modelId")
        downloads = int(model.get("downloads") or 0)
        if repo and downloads >= min_downloads:
            found[repo] = max(downloads, found.get(repo, 0))

    for query in queries:
        try:
            hits = hub.search(query)
        except LiveError as error:
            seen["errors"].append({"query": query, "error": str(error)})
            log(f"search {query!r} failed")
            continue
        seen["hits"][query] = len(hits)
        for model in hits:
            if NAME.search(model.get("id") or model.get("modelId") or ""):
                keep(model)
    if gguf_pages:
        try:
            listed = hub.gguf_models(gguf_pages)
        except LiveError as error:
            seen["errors"].append({"query": "filter=gguf", "error": str(error)})
            log("GGUF listing failed")
            listed = []
        seen["gguf_repositories"] = len(listed)
        for model in listed:
            siblings = model.get("siblings")
            if not isinstance(siblings, list):
                continue
            names = [s.get("rfilename", "") for s in siblings if isinstance(s, dict)]
            if any(name.lower().endswith(".gguf") and LAYOUT_FILE.search(name) for name in names):
                keep(model)
    return found, seen


def gguf_models(info: dict) -> tuple[list, int, int]:
    """(models worth a header read, GGUF files, GGUF models in the repository).
    A model is a list of (name, size, sha256): one file, or the files of a
    split model (<prefix>-KKKKK-of-NNNNN.gguf with the same prefix and count)
    in part order. Models named after a layout with a contract, up to
    MODELS_PER_REPO; when none is, up to UNTAGGED_MODELS not named as float
    weights, else the smallest. Projectors, imatrix files and LoRA adapters
    are skipped."""
    files = []
    for sibling in info.get("siblings") or []:
        if not isinstance(sibling, dict):
            continue
        name = sibling.get("rfilename", "")
        if not name.lower().endswith(".gguf") or SKIP_FILE.search(name):
            continue
        lfs = sibling.get("lfs") or {}
        size = int(sibling.get("size") or lfs.get("size") or 0)
        sha = lfs.get("sha256") or lfs.get("oid")
        files.append((name, size, sha if isinstance(sha, str) and SHA256.match(sha) else None))
    groups, models = {}, []
    for entry in files:
        shard = SHARD.match(entry[0])
        if shard:
            groups.setdefault((shard.group(1), shard.group(3)), []).append((int(shard.group(2)), entry))
        else:
            models.append([entry])
    models += [[entry for _, entry in sorted(group)] for group in groups.values()]

    def label(model):
        return model[0][0]

    named = [model for model in models if LAYOUT_FILE.search(label(model))]
    if named:
        return sorted(named, key=label)[:MODELS_PER_REPO], len(files), len(models)
    by_size = sorted(models, key=lambda model: (sum(entry[1] for entry in model), label(model)))
    quantized = [model for model in by_size if not FLOAT_FILE.search(label(model))]
    return sorted(quantized[:UNTAGGED_MODELS] or by_size[:1], key=label), len(files), len(models)


# ---- one header ------------------------------------------------------------------------

def header_path(cache: Path, repo: str, revision: str, name: str, sha256: str | None = None) -> Path:
    """Where a file's header is cached: by the file's LFS SHA-256, else by a
    hash of repository, commit and file name (never by the name itself, which
    a case-insensitive file system could confuse with another)."""
    if sha256:
        if not SHA256.match(sha256):
            raise LiveError(f"{repo}/{name}: {sha256!r} is not a SHA-256")
        return cache / "sha256" / sha256[:2] / f"{sha256}.header"
    if not REVISION.match(revision or ""):
        raise LiveError(f"{repo}: revision {revision!r} is not a commit")
    if "/" not in repo or ".." in repo or repo.startswith("/"):
        raise LiveError(f"{repo}: not a repository id")
    key = hashlib.sha256("\0".join((repo, revision, name)).encode()).hexdigest()
    return cache / "name" / key[:2] / f"{key}.header"


def _complete(prefix: bytes, size: int) -> tuple[bool, bytes, int]:
    """(complete, header bytes to keep, bytes still needed): complete when no
    runtime's walk asks for more bytes. The header ends at the data section
    once the records are read; a walk that stops before that point keeps what
    was read, which is enough to make the same verdict again."""
    needed, keep = 0, 0
    for runtime in RUNTIMES:
        walked, _ = walk(prefix, size, runtime)
        if walked.status == TRUNCATED:
            needed = max(needed, int(walked.needed))
        elif walked.data_start:
            keep = max(keep, int(walked.data_start))
    if needed:
        return False, prefix, needed
    if keep and keep <= len(prefix):
        return True, prefix[:keep], 0
    return True, prefix, 0


def read_header(hub: Hub, repo: str, revision: str, name: str, size: int, sha256: str | None,
                cache: Path | None) -> bytes:
    """The header of one file (its first bytes up to the data section),
    cached. t27 decides when it is complete: a header that runs past the end
    of the file or past TLV_MAX_HEADER is a verdict, not a read."""
    path = header_path(cache, repo, revision, name, sha256) if cache is not None else None
    if path is not None and path.is_file():
        cached = path.read_bytes()
        complete, _, _ = _complete(cached, size)
        if complete:
            return cached
    limit = min(size, max_header())
    have = b""
    needed = min(FIRST_READ, limit)
    while True:
        if needed > len(have):
            end = min(limit, max(needed, 2 * len(have)))
            if end <= len(have):
                raise LiveError(f"{repo}/{name}: the reader asked for {needed} bytes, at most {limit} can be read")
            have += hub.read(repo, revision, name, len(have), end, size)
        complete, header, needed = _complete(have, size)
        if complete:
            if path is not None:
                write_atomic(path, header)
            return header
        if needed <= len(have):
            raise LiveError(f"{repo}/{name}: the reader asked for {needed} bytes with {len(have)} read")


def _runtime_entry(verdict: int, run: Run, names: dict, files: list) -> dict:
    entry = {"verdict": RUN.get(verdict, "no_verdict")}
    if verdict < 0:
        entry["status"] = status_token(verdict)
    if verdict == 1:
        token = status_token(run.status)
        entry["status"] = token
        if len(files) > 1 or run.part:
            entry["file"] = files[int(run.part)] if int(run.part) < len(files) else int(run.part)
        if token.startswith("hadamard"):
            entry["entry"] = int(run.record)
        elif (int(run.part), int(run.record)) in names:
            entry["record"] = names[(int(run.part), int(run.record))]
        if token in ("offsets", "bounds", "split"):
            entry.update(expected=int(run.expected), found=int(run.found))
    return entry


def check_model(parts: list) -> dict:
    """The t27 verdicts for one model, `parts` being (file name, header,
    file size) with the first file first: each runtime's verdict, and the
    per-record diagnosis in the runtime the model is written for."""
    model = Model(parts)
    first_name, first_header, first_size = parts[0]
    first, _ = walk(first_header, first_size, LLAMA_CPP)
    native = model.native()
    runtime = native or LLAMA_CPP
    loaded = model.loaded()
    record = {"version": int(first.version), "keys": int(first.keys),
              "architecture": model.text("general.architecture"),
              "native": NATIVE[native]}
    names, tensors = {}, 0
    record["ggml_types"], record["layouts"], record["problems"] = {}, {}, {}
    for index, (name, header, size) in enumerate(parts[:max(loaded, 1)]):
        walked, arrays = walk(header, size, runtime, records=True)
        tensors += int(walked.tensors)
        if arrays is None:
            continue
        status, fit, types, name_at, name_size = arrays
        for i in range(int(walked.read)):
            label = header[name_at[i]: name_at[i] + name_size[i]].decode("utf-8", "replace")
            names[(index, i)] = label
            record["ggml_types"][int(types[i])] = record["ggml_types"].get(int(types[i]), 0) + 1
            if status[i] > 0:
                layout = layout_name(status[i])
                record["layouts"][layout] = record["layouts"].get(layout, 0) + 1
            elif status[i] < 0:
                problem = record["problems"].setdefault(status_token(status[i]), {"count": 0, "examples": []})
                problem["count"] += 1
                if len(problem["examples"]) < 3:
                    problem["examples"].append(label)
                if fit[i]:
                    problem.setdefault("fits", {})
                    problem["fits"][layout_name(fit[i])] = problem["fits"].get(layout_name(fit[i]), 0) + 1
    record["tensors"] = tensors
    record["ggml_types"] = [[key, value] for key, value in sorted(record["ggml_types"].items())]
    record["runtimes"] = {}
    native_run, native_status = -1, 0
    files = [name for name, _, _ in parts]
    for key_runtime, key in RUNTIMES.items():
        verdict, run = model.verdict(key_runtime)
        record["runtimes"][key] = _runtime_entry(verdict, run, names, files)
        if key_runtime == runtime:
            native_run, native_status = verdict, run.status
    record["ternary_tensors"] = model.ternary(runtime)
    status, rotation = model.hadamard(max(loaded, 1))
    record["hadamard"] = {"present": bool(rotation.present), "status": "ok" if status == 0 else status_token(status)}
    if rotation.present:
        record["hadamard"].update(block_size=int(rotation.block_size), weight_names=int(rotation.names),
                                  inverse_names=int(rotation.inverse), explicit_signs=bool(rotation.explicit_signs),
                                  sign_widths=int(rotation.widths), sign_values=int(rotation.signs),
                                  gdn_v_grouped=bool(rotation.gdn_v_grouped))
    record["verdict"] = FILE[file_verdict(native_run, native_status, record["ternary_tensors"], native)]
    return record


def check_header(header: bytes, size: int, name: str = "model.gguf") -> dict:
    """check_model for a model of one file."""
    return check_model([(name, header, size)])


# ---- the scan --------------------------------------------------------------------------

def _model_item(model: list) -> dict:
    item = {"file": model[0][0], "size": sum(size for _, size, _ in model),
            "files": [{"file": name, "size": size, "lfs_sha256": sha} for name, size, sha in model]}
    if SHARD.match(model[0][0]):
        item["split"] = int(SHARD.match(model[0][0]).group(3))
    return item


def _check(hub: Hub, repo: str, revision: str, model: list, cache: Path | None) -> dict:
    parts, hashes = [], []
    for name, size, sha in model:
        try:
            header = read_header(hub, repo, revision, name, size, sha, cache)
        except LiveError as error:
            return {"verdict": "unread", "error": str(error)}
        parts.append((name, header, size))
        hashes.append(hashlib.sha256(header).hexdigest())
    result = check_model(parts)
    result["header_sha256"] = hashes
    return result


def _blob_key(model: list):
    shas = tuple(sha for _, _, sha in model)
    return shas if all(shas) else None


KEEP = ("file", "size", "files", "split", "same_as")


def scan(hub: Hub, repos: dict, cache: Path | None = HEADERS, log=None, save=None) -> list:
    """One entry per repository (most downloaded first), each model checked
    once per set of LFS SHA-256s. `save(entries)` is called after every
    repository; the log gets counts only."""
    log = log or (lambda message: None)
    by_blob: dict = {}
    entries = []
    ordered = sorted(repos.items(), key=lambda item: (-item[1], item[0]))
    models_seen = 0
    for index, (repo, downloads) in enumerate(ordered, 1):
        entry = {"repo": repo, "downloads_30d": downloads}
        try:
            info = hub.model(repo)
            if not isinstance(info, dict):
                raise LiveError(f"{repo}: unexpected answer")
            entry["revision"] = info.get("sha")
            entry["last_modified"] = info.get("lastModified")
            if info.get("gated"):
                entry["state"] = "gated"
            else:
                models, files, total = gguf_models(info)
                entry.update(gguf_files=files, gguf_models=total, models_checked=len(models))
                entry["state"] = "scanned" if models else "no_gguf"
                if models:
                    entry["models"] = []
                for model in models:
                    item = _model_item(model)
                    key = _blob_key(model)
                    if key and key in by_blob:
                        item.update(by_blob[key]["result"], same_as=by_blob[key]["first"])
                    else:
                        try:
                            item.update(_check(hub, repo, entry["revision"], model, cache))
                        except Exception as error:  # a crafted file must not end the whole scan
                            item.update(verdict="error", error=f"{type(error).__name__}: {error}")
                        if key and item.get("verdict") not in ("unread", "error"):
                            result = {k: v for k, v in item.items() if k not in KEEP}
                            by_blob[key] = {"result": result, "first": {"repo": repo, "file": model[0][0]}}
                    entry["models"].append(item)
                    models_seen += 1
        except LiveError as error:
            entry.update(state="unavailable", error=str(error))
        except Exception as error:
            entry.update(state="error", error=f"{type(error).__name__}: {error}")
        entries.append(entry)
        if index % LOG_EVERY == 0 or index == len(ordered):
            log(f"[{index}/{len(ordered)}] repositories, {models_seen} models")
        if save:
            save(entries)
    return entries


def _cached(cache: Path, repo: str, revision: str, item: dict) -> list | None:
    """The cached headers of a model item as (name, header, size), or None
    when one is missing or differs from the header_sha256 the item records."""
    parts = []
    hashes = item.get("header_sha256") or []
    for i, file in enumerate(item.get("files") or []):
        try:
            path = header_path(cache, repo, revision or "", file["file"], file.get("lfs_sha256"))
        except LiveError:
            return None
        if not path.is_file():
            return None
        header = path.read_bytes()
        if i < len(hashes) and hashlib.sha256(header).hexdigest() != hashes[i]:
            return None
        parts.append((file["file"], header, file["size"]))
    return parts or None


def recheck(previous: dict, cache: Path = HEADERS) -> list:
    """The repositories of an earlier report with every model's verdicts made
    again from the cached headers, offline. A model whose headers are not all
    cached keeps its earlier result and is marked `recheck: kept`."""
    entries = json.loads(json.dumps(previous["repositories"]))
    revisions = {entry["repo"]: entry.get("revision") for entry in entries}
    for entry in entries:
        for item in entry.get("models", []):
            repo = entry["repo"]
            if isinstance(item.get("same_as"), dict):
                repo = item["same_as"].get("repo", repo)
            parts = _cached(cache, repo, revisions.get(repo) or "", item)
            if parts is None:
                item["recheck"] = "kept"
                continue
            fields = {k: item[k] for k in KEEP if k in item}
            for file in fields.get("files", []):
                file.pop("upstream", None)          # the replay answered earlier verdicts
            item.clear()
            item.update(fields)
            try:
                item.update(check_model(parts))
                item["header_sha256"] = [hashlib.sha256(header).hexdigest() for _, header, _ in parts]
            except Exception as error:
                item.update(verdict="error", error=f"{type(error).__name__}: {error}")
    return entries


def replay(entries: list, cache: Path, binaries: dict, log=None) -> dict:
    """Feed every cached header to the pinned upstream GGUF readers
    (tools/live-replay.sh builds them) and compare their answer with
    tlv_walk's `reader` in the same runtime. gguf_init_from_file reads no
    tensor data, so the loader's file-end rule is not part of the comparison;
    a t27 walk with no verdict (limit, truncated) is counted apart; a reader
    that crashes (a signal, as GGML_ASSERT does) is counted as refusing and
    also apart. Adds `upstream` to each file and returns the counts."""
    log = log or (lambda message: None)
    counts = {"runtimes": sorted(binaries), "files": 0, "agree": 0, "disagree": 0, "no_verdict": 0, "crashes": 0,
              "timeouts": 0, "disagreements": []}
    keys = {name: runtime for runtime, name in RUNTIMES.items()}
    revisions = {entry["repo"]: entry.get("revision") for entry in entries}
    work = Path(tempfile.mkdtemp(prefix="live-replay-"))
    try:
        for entry in entries:
            for item in entry.get("models", []):
                if item.get("same_as") or item.get("verdict") in ("unread", "error", None):
                    continue
                for file in item.get("files") or []:
                    try:
                        path = header_path(cache, entry["repo"], revisions.get(entry["repo"]) or "", file["file"],
                                           file.get("lfs_sha256"))
                    except LiveError:
                        continue
                    if not path.is_file():
                        continue
                    header = path.read_bytes()
                    counts["files"] += 1
                    file["upstream"] = {}
                    for name, binary in sorted(binaries.items()):
                        result = {}
                        try:
                            done = subprocess.run([str(binary), str(path), str(file["size"])], capture_output=True,
                                                  timeout=120, env={**os.environ, "TMPDIR": str(work)})
                        except subprocess.TimeoutExpired:
                            counts["timeouts"] += 1
                            file["upstream"][name] = {"timeout": True}
                            continue
                        finally:
                            for leftover in work.iterdir():
                                leftover.unlink(missing_ok=True)
                        stdout = done.stdout.decode("utf-8", "replace")
                        stderr = done.stderr.decode("utf-8", "replace")
                        accepted = done.returncode == 0 and stdout.startswith("ACCEPTED")
                        result["accepted"] = accepted
                        if done.returncode < 0:
                            result["signal"] = -done.returncode
                            counts["crashes"] += 1
                        message = next((line.strip() for line in stderr.splitlines()
                                        if "gguf_init" in line and "failed to read" not in line), None)
                        if not accepted and message:
                            result["message"] = message[:300]
                        walked, _ = walk(header, file["size"], keys[name])
                        if walked.reader in (TRUNCATED, LIMIT):
                            result["t27"] = status_token(walked.reader)
                            counts["no_verdict"] += 1
                        else:
                            ours = walked.reader == 0
                            result["agrees"] = ours == accepted
                            if ours == accepted:
                                counts["agree"] += 1
                            else:
                                counts["disagree"] += 1
                                counts["disagreements"].append({"repo": entry["repo"], "file": file["file"],
                                                                "runtime": name, "upstream": accepted,
                                                                "t27": status_token(walked.reader) if walked.reader else "ok"})
                        file["upstream"][name] = result
    finally:
        shutil.rmtree(work, ignore_errors=True)
    log(f"replay over {counts['files']} files: {counts['agree']} agree, {counts['disagree']} disagree, "
        f"{counts['no_verdict']} without a t27 verdict, {counts['crashes']} crashes, {counts['timeouts']} timeouts")
    return counts


def summary(entries: list) -> dict:
    models = [item for entry in entries for item in entry.get("models", [])]
    verdicts, runtimes = {}, {key: {} for key in RUNTIMES.values()}
    for item in models:
        verdicts[item.get("verdict")] = verdicts.get(item.get("verdict"), 0) + 1
        for key, entry in (item.get("runtimes") or {}).items():
            runtimes[key][entry["verdict"]] = runtimes[key].get(entry["verdict"], 0) + 1
    states = {}
    for entry in entries:
        states[entry.get("state")] = states.get(entry.get("state"), 0) + 1
    refused = sorted({e["repo"] for e in entries if any(i.get("verdict") == "refused" for i in e.get("models", []))})
    return {"repositories": len(entries), "states": dict(sorted(states.items(), key=lambda kv: str(kv[0]))),
            "models": len(models), "files": sum(len(item.get("files") or []) for item in models),
            "split_models": sum(1 for item in models if item.get("split")),
            "models_not_checked": sum(e.get("gguf_models", 0) - e.get("models_checked", 0) for e in entries),
            "verdicts": dict(sorted(verdicts.items(), key=lambda kv: str(kv[0]))),
            "runtimes": {k: dict(sorted(v.items())) for k, v in runtimes.items()},
            "repositories_with_refused_models": len(refused),
            "downloads_30d_scanned": sum(e["downloads_30d"] for e in entries if e.get("state") == "scanned")}


def _commit() -> str | None:
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"]
    try:
        done = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def report(entries: list, discovery: dict, started: str) -> dict:
    return {"schema": SCHEMA, "started": started, "trinity_memory": _commit(),
            "compiler": COMPILER_LOCK.read_text().strip(),
            "runtimes": {key: json.loads((ROOT / "specs" / "runtimes" / f"{name}.json").read_text())["commit"]
                         for key, name in (("llama.cpp", "llama_cpp"), ("prismml", "prismml"),
                                           ("bitnet.cpp", "bitnet_cpp"))},
            "discovery": discovery, "summary": summary(entries), "repositories": entries}


def _write(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, (json.dumps(value, indent=1, ensure_ascii=False, sort_keys=True) + "\n").encode())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m trinity_memory.live", description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=REPORT, help="report path (default build/live/scan.json)")
    parser.add_argument("--min-downloads", type=int, default=100, help="30-day downloads (default 100)")
    parser.add_argument("--limit", type=int, default=0, help="scan only the N most downloaded repositories")
    parser.add_argument("--repo", action="append", default=[], help="scan these repositories instead of discovering")
    parser.add_argument("--gguf-pages", type=int, default=GGUF_PAGES,
                        help="pages of 1000 most downloaded GGUF repositories to search by file name")
    parser.add_argument("--interval", type=float, default=1.5, help="seconds between requests (default 1.5)")
    parser.add_argument("--no-cache", action="store_true", help="do not read or write build/live/headers")
    parser.add_argument("--recheck", type=Path, help="make the verdicts of this report again from cached headers, offline")
    parser.add_argument("--replay", type=Path, help="feed the cached headers of the report at --out to the upstream "
                        "readers built by tools/live-replay.sh in this directory and record their answers")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    if not COMPILER_LOCK.is_file():
        parser.error(f"{COMPILER_LOCK} is missing: run the scan from a source checkout")
    try:
        max_header()
    except n.NativeLibraryError as error:
        parser.error(str(error))
    log = (lambda message: None) if args.quiet else (lambda message: print(message, file=sys.stderr, flush=True))
    started = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    if args.recheck:
        previous = json.loads(args.recheck.read_text())
        if previous.get("schema") != SCHEMA:
            parser.error(f"{args.recheck}: not a {SCHEMA} report")
        out = report(recheck(previous, HEADERS), previous["discovery"], previous["started"])
        out["rechecked"] = started
        _write(args.out, out)
        log(f"{out['summary']['models']} models rechecked -> {args.out}")
        return 0
    if args.replay:
        if not args.out.is_file():
            parser.error(f"{args.out}: no report to replay (run the scan first)")
        current = json.loads(args.out.read_text())
        if current.get("schema") != SCHEMA:
            parser.error(f"{args.out}: not a {SCHEMA} report")
        binaries = {RUNTIMES[runtime]: args.replay / f"gguf_replay_{name}"
                    for runtime, name in ((LLAMA_CPP, "llama_cpp"), (PRISMML, "prismml"), (BITNET_CPP, "bitnet_cpp"))
                    if (args.replay / f"gguf_replay_{name}").is_file()}
        if not binaries:
            parser.error(f"{args.replay}: no gguf_replay_* binaries (run sh tools/live-replay.sh)")
        current["replay"] = replay(current["repositories"], HEADERS, binaries, log)
        _write(args.out, current)
        return 0
    hub = Hub(interval=args.interval, log=log)
    discovery = {"endpoint": hub.endpoint, "min_downloads_30d": args.min_downloads, "limit": args.limit}
    if args.repo:
        discovery["repos"] = args.repo
        repos = {}
        for repo in args.repo:
            try:
                answer = hub.api(f"models/{repo}")
                repos[repo] = int(answer.get("downloads") or 0) if isinstance(answer, dict) else 0
            except LiveError as error:
                log(f"a repository given with --repo is unavailable: {error}")
                repos[repo] = 0
    else:
        discovery.update(queries=list(QUERIES), name_filter=NAME.pattern, file_filter=LAYOUT_FILE.pattern,
                         gguf_pages=args.gguf_pages)
        repos, seen = discover(hub, QUERIES, args.min_downloads, args.gguf_pages, log)
        discovery.update(seen)
    if args.limit:
        repos = dict(sorted(repos.items(), key=lambda item: (-item[1], item[0]))[: args.limit])
    log(f"{len(repos)} repositories to scan")
    cache = None if args.no_cache else HEADERS
    entries = scan(hub, repos, cache, log, save=lambda done: _write(args.out, report(done, discovery, started)))
    out = report(entries, discovery, started)
    _write(args.out, out)
    counts = out["summary"]
    log(f"{counts['repositories']} repositories, {counts['models']} models, verdicts {counts['verdicts']} "
        f"({hub.requests} requests) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
