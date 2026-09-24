"""Ternary Check Live (issues #48-#51): public ternary GGUF files on the Hugging
Face Hub, checked from their headers (I/O glue only).

Discovery uses the public Hub API anonymously: no token is ever sent. Every
observation is pinned to the commit the Hub reports for the repository at the
time of the scan. Each file's header is read with HTTP range requests at that
commit, grown until the t27 reader stops asking for bytes; a read never
reaches past MAX_HEADER, and at most twice the header's size is fetched, so a
file's weights are never downloaded. Every verdict comes from t27
(t27/live.t27 over the runtime tables of t27/runtimes.t27): tlv_walk reads the
header as each runtime's gguf.cpp reads it, tlv_runtime adds its model
loader's rules (architecture, file end, and in the PrismML fork the Hadamard
rules), tlv_file_verdict folds them for the runtime the file is written for.
This module only moves bytes, counts the verdicts and writes the report.

Byte-identical files (the same LFS SHA-256) are read once; the report names
the first repository that serves them. Headers are cached under
build/live/headers by repository, commit and file name, so `--recheck` can make
every verdict again offline.
"""
from __future__ import annotations
import argparse
import ctypes as C
import datetime as dt
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys
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
SCHEMA = "trinity.ternary-check-live.v1"
USER_AGENT = "trinity-memory-ternary-check-live/0.5 (+https://github.com/dmitrii-f-t27/trinity-memory)"
DEFAULT_ENDPOINT = "https://huggingface.co"
OFFLINE_ENV = "TRINITY_LIVE_OFFLINE"

# Hub search is a substring match, so these terms cover 1.58bit, b1.58,
# tq1_0, ptq1_0, tq2_0 and pq2_0 too.
QUERIES = ("ternary", "bitnet", "bonsai", "1.58", "b1_58", "q1_0", "q2_0", "i2_s")
_T = r"(?<![a-z0-9])"
_E = r"(?![a-z0-9])"
NAME = re.compile(rf"ternary|bitnet|1\.58|b1_58|bonsai|{_T}(?:tq1_0|tq2_0|pq2_0|ptq1_0|i2_s|q2_0|q1_0){_E}",
                  re.IGNORECASE)
# File names that announce a layout with a contract (a model name such as
# "Ternary-Bonsai" says nothing about the file's layout).
LAYOUT_FILE = re.compile(rf"{_T}(?:tq1_0|tq2_0|pq2_0|ptq1_0|i2_s|tl1|tl2|q2_0|q2_g\d+|q2_0_g\d+|q1_0){_E}",
                         re.IGNORECASE)
SKIP_FILE = re.compile(rf"{_T}(?:mmproj|imatrix|lora){_E}", re.IGNORECASE)
FLOAT_FILE = re.compile(rf"{_T}(?:b?f16|f32){_E}", re.IGNORECASE)
SHARD = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$")
REVISION = re.compile(r"^[0-9a-f]{40}$")
MAX_HEADER = 256 << 20
FIRST_READ = 1 << 20
FILES_PER_REPO = 12
UNTAGGED_FILES = 3
GGUF_PAGES = 2

# ---- t27/live.t27 --------------------------------------------------------------------

U64 = C.c_uint64
LLAMA_CPP, PRISMML, BITNET_CPP = 1, 2, 3
RUNTIMES = {LLAMA_CPP: "llama.cpp", PRISMML: "prismml", BITNET_CPP: "bitnet.cpp"}
RUN = {0: "accepts", 1: "refuses", 2: "ignores_rotation"}
FILE = {0: "ok", 1: "refused", 2: "no_ternary_layout", 3: "undecided"}
TRUNCATED = -55
BOUNDS = -82
LIMIT = -84
# Status classes -63 .. -84 (specs/formats/OWNERS.md), after those of formats.t27.
TOKENS = {-63: "hadamard_type", -64: "hadamard_missing", -65: "hadamard_version", -66: "hadamard_block",
          -67: "hadamard_transform", -68: "hadamard_signs", -69: "hadamard_arch", -70: "hadamard_name",
          -71: "hadamard_tensor", -72: "offsets", -73: "magic", -74: "version", -75: "endian", -76: "key",
          -77: "alignment", -78: "name", -79: "shape", -80: "type", -81: "row", -82: "bounds", -83: "arch",
          -84: "limit"}


def status_token(status: int) -> str:
    return TOKENS.get(status) or f.TOKENS.get(status) or f"status {status}"


class LiveError(RuntimeError):
    pass


class Walk(C.Structure):
    _fields_ = [("status", C.c_int32), ("record", U64), ("expected", U64), ("found", U64), ("needed", U64),
                ("version", C.c_uint32), ("keys", U64), ("tensors", U64), ("records_at", U64),
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
    _fields_ = [("verdict", C.c_int32), ("status", C.c_int32), ("record", U64), ("expected", U64),
                ("found", U64)]


_WALK_ARGS = [n.U8, n.SZ, U64, C.c_int32, n.I32, n.I32, C.POINTER(C.c_uint32), C.POINTER(U64),
              C.POINTER(U64), n.SZ, C.POINTER(Walk)]


def walk(header: bytes, file_size: int, runtime: int, records: bool = False, data=None):
    """(Walk, per-record arrays or None) from tlv_walk; `data` may pass octets already made."""
    data = n.octets(header) if data is None else data
    out = Walk()
    one_i, one_f, one_t = (C.c_int32 * 1)(), (C.c_int32 * 1)(), (C.c_uint32 * 1)()
    one_a, one_s = (U64 * 1)(), (U64 * 1)()
    n.call("tlv_walk", C.c_int32, _WALK_ARGS, data, len(header), file_size, runtime, one_i, one_f, one_t,
           one_a, one_s, 0, C.byref(out))
    if not records or out.read == 0:
        return out, None
    count = int(out.tensors)
    arrays = ((C.c_int32 * count)(), (C.c_int32 * count)(), (C.c_uint32 * count)(), (U64 * count)(), (U64 * count)())
    out = Walk()
    n.call("tlv_walk", C.c_int32, _WALK_ARGS, data, len(header), file_size, runtime, *arrays, count, C.byref(out))
    return out, arrays


def runtime_verdict(data, size: int, file_size: int, runtime: int):
    out = Run()
    verdict = n.call("tlv_runtime", C.c_int32, [n.U8, n.SZ, U64, C.c_int32, C.POINTER(Run)],
                     data, size, file_size, runtime, C.byref(out))
    return verdict, out


def hadamard(data, size: int):
    out = Hadamard()
    status = n.call("tlv_hadamard", C.c_int32, [n.U8, n.SZ, C.POINTER(Hadamard)], data, size, C.byref(out))
    return status, out


def text_value(header: bytes, data, name: str) -> str | None:
    """A string value for display (moving bytes, not a verdict), up to its first NUL."""
    out, text = Key(), name.encode()
    status = n.call("tlv_key", C.c_int32, [n.U8, n.SZ, C.c_char_p, n.SZ, C.POINTER(Key)],
                    data, len(header), text, len(text), C.byref(out))
    if status < 0 or not out.found or out.vtype != 8:
        return None
    length = int.from_bytes(header[out.at: out.at + 8], "little")
    return header[out.at + 8: out.at + 8 + length].split(b"\0", 1)[0].decode("utf-8", "replace")


def native_runtime(data, size: int, walked: Walk) -> int:
    return n.call("tlv_native", C.c_int32, [n.U8, n.SZ, C.POINTER(Walk)], data, size, C.byref(walked))


def file_verdict(run: int, ternary: int) -> int:
    return n.call("tlv_file_verdict", C.c_int32, [C.c_int32, U64], run, ternary)


# ---- the Hub ---------------------------------------------------------------------------

class Hub:
    """The public Hub API and range reads, anonymously, one request at a time
    with at least `interval` seconds between requests. 429 and 5xx answers
    are retried after the time the Hub asks for (fixtures._retry_after)."""

    def __init__(self, endpoint: str | None = None, interval: float = 1.5, attempts: int = 6,
                 offline: bool | None = None, log=None):
        self.endpoint = (endpoint or os.environ.get("HF_ENDPOINT") or DEFAULT_ENDPOINT).rstrip("/")
        self.interval, self.attempts = interval, attempts
        self.offline = os.environ.get(OFFLINE_ENV) == "1" if offline is None else offline
        self.log = log or (lambda message: None)
        self._last = 0.0
        self.requests = 0

    def _open(self, url: str, headers: dict | None = None):
        """An open response, after throttling and retries; the caller reads it."""
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
                return urllib.request.urlopen(request, timeout=120)
            except urllib.error.HTTPError as error:
                error.close()
                if (error.code == 429 or error.code >= 500) and attempt + 1 < self.attempts:
                    delay = _retry_after(error, attempt)
                    self.log(f"HTTP {error.code} on {url}: waiting {delay:.0f} s")
                    time.sleep(delay)
                    continue
                raise LiveError(f"{url}: HTTP {error.code} {error.reason}") from error
            except (OSError, http.client.HTTPException) as error:
                if attempt + 1 < self.attempts:
                    time.sleep(2.0 * 2 ** attempt)
                    continue
                raise LiveError(f"{url}: {error}") from error
        raise LiveError(f"{url}: no answer after {self.attempts} attempts")

    def api(self, path: str, params: dict | None = None, pages: int = 20):
        url = f"{self.endpoint}/api/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        results, seen = [], 0
        while url:
            with self._open(url) as response:
                body = response.read()
                link = response.headers.get("Link", "")
            try:
                value = json.loads(body)
            except ValueError as error:
                raise LiveError(f"{url}: not JSON") from error
            if not isinstance(value, list):
                return value
            results.extend(value)
            seen += 1
            url = self._next(link, url) if seen < pages else None
        return results

    def _next(self, header: str, current: str) -> str | None:
        link = _next_link(header)
        if not link:
            return None
        link = urllib.parse.urljoin(current, link)
        return link if link.startswith(self.endpoint + "/") else None

    def search(self, query: str) -> list:
        return self.api("models", {"search": query, "limit": 1000})

    def gguf_models(self, pages: int) -> list:
        """The most downloaded GGUF repositories, with their file names."""
        return self.api("models", {"filter": "gguf", "sort": "downloads", "direction": -1, "limit": 1000,
                                   "full": "true"}, pages=pages)

    def model(self, repo: str) -> dict:
        return self.api(f"models/{repo}", {"blobs": "true"})

    def read(self, repo: str, revision: str, filename: str, begin: int, end: int, size: int) -> bytes:
        """Bytes [begin, end) of a file at a commit. The answer must be 206 with
        a matching Content-Range, or 200 only for a request of the whole file."""
        url = f"{self.endpoint}/{repo}/resolve/{revision}/{urllib.parse.quote(filename)}"
        with self._open(url, {"Range": f"bytes={begin}-{end - 1}"}) as response:
            whole = begin == 0 and end == size
            if response.status == 200 and not whole:
                raise LiveError(f"{repo}@{revision}/{filename}: server ignored the range request (HTTP 200)")
            if response.status == 206:
                content = response.headers.get("Content-Range", "")
                if content and not content.startswith(f"bytes {begin}-{end - 1}/"):
                    raise LiveError(f"{repo}@{revision}/{filename}: Content-Range {content!r} for {begin}-{end - 1}")
            elif response.status != 200:
                raise LiveError(f"{repo}@{revision}/{filename}: HTTP {response.status}")
            data = response.read(end - begin + 1)
        if len(data) != end - begin:
            raise LiveError(f"{repo}@{revision}/{filename}: expected {end - begin} bytes, received {len(data)}")
        return data


def _next_link(header: str) -> str | None:
    for part in header.split(","):
        match = re.match(r'\s*<([^>]+)>\s*;\s*rel="?next"?', part)
        if match:
            return match.group(1)
    return None


# ---- discovery -------------------------------------------------------------------------

def discover(hub: Hub, queries=QUERIES, min_downloads: int = 100, gguf_pages: int = GGUF_PAGES) -> dict:
    """Repository id -> 30-day downloads: search hits whose name passes NAME,
    and GGUF repositories among the most downloaded whose file names announce
    a ternary layout (a generic repository name may hold a TQ1_0 file)."""
    found = {}

    def keep(model):
        repo = model.get("id") or model.get("modelId")
        downloads = int(model.get("downloads") or 0)
        if repo and downloads >= min_downloads:
            found[repo] = max(downloads, found.get(repo, 0))

    for query in queries:
        for model in hub.search(query):
            if NAME.search(model.get("id") or model.get("modelId") or ""):
                keep(model)
    if gguf_pages:
        for model in hub.gguf_models(gguf_pages):
            names = [s.get("rfilename", "") for s in model.get("siblings") or []]
            if any(name.endswith(".gguf") and LAYOUT_FILE.search(name) for name in names):
                keep(model)
    return found


def gguf_files(info: dict) -> tuple[list, int]:
    """((name, size, sha256) of the GGUF files worth a header read, number of
    GGUF files in the repository). Files named after a layout with a
    contract; when none is, up to UNTAGGED_FILES files not named as float
    weights, else the smallest. Projectors, imatrix files and LoRA adapters
    are skipped."""
    files = []
    for sibling in info.get("siblings") or []:
        name = sibling.get("rfilename", "")
        if not name.endswith(".gguf") or SKIP_FILE.search(name):
            continue
        lfs = sibling.get("lfs") or {}
        size = int(sibling.get("size") or lfs.get("size") or 0)
        files.append((name, size, lfs.get("sha256") or lfs.get("oid")))
    named = [entry for entry in files if LAYOUT_FILE.search(entry[0])]
    if named:
        return sorted(named)[:FILES_PER_REPO], len(files)
    by_size = sorted(files, key=lambda entry: (entry[1], entry[0]))
    quantized = [entry for entry in by_size if not FLOAT_FILE.search(entry[0])]
    return sorted(quantized[:UNTAGGED_FILES] or by_size[:1]), len(files)


# ---- one header ------------------------------------------------------------------------

def header_path(cache: Path, repo: str, revision: str, name: str) -> Path:
    if not REVISION.match(revision or ""):
        raise LiveError(f"{repo}: revision {revision!r} is not a commit")
    if "/" not in repo or ".." in repo or repo.startswith("/"):
        raise LiveError(f"{repo}: not a repository id")
    return cache / repo.replace("/", "--") / revision / (urllib.parse.quote(name, safe="") + ".header")


def _complete(prefix: bytes, size: int) -> tuple[bool, bytes, int]:
    """(complete, header bytes to keep, bytes still needed): the header ends
    at the data section once the records are read; a refusal before that
    point keeps what was read, which is enough to make the same refusal again."""
    walked, _ = walk(prefix, size, LLAMA_CPP)
    if walked.status == TRUNCATED:
        return False, prefix, int(walked.needed)
    if walked.data_start and walked.data_start <= len(prefix):
        return True, prefix[: walked.data_start], 0
    return True, prefix, 0


def read_header(hub: Hub, repo: str, revision: str, name: str, size: int, cache: Path | None) -> bytes:
    """The header of one file, cached by repository, commit and file name."""
    path = header_path(cache, repo, revision, name) if cache is not None else None
    if path is not None and path.is_file():
        cached = path.read_bytes()
        complete, _, _ = _complete(cached, size)
        if complete:
            return cached
    have = b""
    needed = min(FIRST_READ, size)
    while True:
        if needed > MAX_HEADER:
            raise LiveError(f"{repo}/{name}: header larger than {MAX_HEADER} bytes")
        if needed > size:
            raise LiveError(f"{repo}/{name}: the header needs {needed} bytes, the file has {size}")
        if needed > len(have):
            end = min(size, MAX_HEADER, max(needed, 2 * len(have)))
            have += hub.read(repo, revision, name, len(have), end, size)
        complete, header, needed = _complete(have, size)
        if not complete:
            if needed <= len(have):
                raise LiveError(f"{repo}/{name}: reader asked for {needed} bytes with {len(have)} read")
            continue
        if path is not None:
            write_atomic(path, header)
        return header


def check_header(header: bytes, size: int) -> dict:
    """The t27 verdicts for one header: each runtime's, and the per-record
    diagnosis in the runtime the file is written for."""
    data = n.octets(header)
    walked, _ = walk(header, size, LLAMA_CPP, data=data)
    record = {"version": int(walked.version), "tensors": int(walked.tensors), "keys": int(walked.keys)}
    if walked.status in (TRUNCATED, LIMIT):
        record.update(verdict=FILE[3], reason=status_token(walked.status))
        return record
    parsed = walked.records_at > 0
    record["architecture"] = text_value(header, data, "general.architecture") if parsed else None
    native = native_runtime(data, len(header), walked) if parsed else LLAMA_CPP
    record["native"] = RUNTIMES[native]
    walked, arrays = walk(header, size, native, records=True, data=data)
    names = {}
    if arrays is not None:
        name_at, name_size = arrays[3], arrays[4]
        for i in range(int(walked.read)):
            names[i] = header[name_at[i]: name_at[i] + name_size[i]].decode("utf-8", "replace")
    record["runtimes"] = {}
    native_run = -1
    for runtime, key in RUNTIMES.items():
        verdict, run = runtime_verdict(data, len(header), size, runtime)
        entry = {"verdict": RUN.get(verdict, status_token(verdict))}
        if verdict == 1:
            entry["status"] = status_token(run.status)
            if entry["status"].startswith("hadamard"):
                entry["entry"] = int(run.record)
            elif run.record in names:
                entry["record"] = names[run.record]
            if entry["status"] in ("offsets", "bounds"):
                entry.update(expected=int(run.expected), found=int(run.found))
        if runtime == native:
            native_run = verdict
        record["runtimes"][key] = entry
    record["ggml_types"], record["layouts"], record["problems"] = {}, {}, {}
    if arrays is not None:
        status, fit, types = arrays[0], arrays[1], arrays[2]
        for i in range(int(walked.read)):
            key = str(types[i])
            record["ggml_types"][key] = record["ggml_types"].get(key, 0) + 1
            if status[i] > 0:
                label = f.NAMES.get(status[i], str(status[i]))
                record["layouts"][label] = record["layouts"].get(label, 0) + 1
            elif status[i] < 0:
                problem = record["problems"].setdefault(status_token(status[i]), {"count": 0, "examples": []})
                problem["count"] += 1
                if len(problem["examples"]) < 3:
                    problem["examples"].append(names[i])
                if fit[i]:
                    problem.setdefault("fits", {})
                    label = f.NAMES.get(fit[i], str(fit[i]))
                    problem["fits"][label] = problem["fits"].get(label, 0) + 1
    record["ggml_types"] = dict(sorted(record["ggml_types"].items(), key=lambda item: int(item[0])))
    record["ternary_tensors"] = int(walked.ternary)
    status, rotation = hadamard(data, len(header)) if parsed else (0, Hadamard())
    record["hadamard"] = {"present": bool(rotation.present), "status": "ok" if status == 0 else status_token(status)}
    if rotation.present:
        record["hadamard"].update(block_size=int(rotation.block_size), weight_names=int(rotation.names),
                                  inverse_names=int(rotation.inverse), explicit_signs=bool(rotation.explicit_signs),
                                  sign_widths=int(rotation.widths), sign_values=int(rotation.signs),
                                  gdn_v_grouped=bool(rotation.gdn_v_grouped))
    record["verdict"] = FILE[file_verdict(native_run, int(walked.ternary))]
    return record


# ---- the scan --------------------------------------------------------------------------

KEEP = ("file", "size", "lfs_sha256", "shard", "same_as")


def _check_file(hub: Hub, repo: str, revision: str, name: str, size: int, cache: Path | None) -> dict:
    try:
        header = read_header(hub, repo, revision, name, size, cache)
    except LiveError as error:
        return {"verdict": "unread", "error": str(error)}
    return check_header(header, size)


def scan(hub: Hub, repos: dict, cache: Path | None = HEADERS, log=None, save=None) -> list:
    """One entry per repository (most downloaded first), each file checked
    once per LFS sha256. `save(entries)` is called after every repository."""
    log = log or (lambda message: None)
    by_blob: dict = {}
    entries = []
    for repo, downloads in sorted(repos.items(), key=lambda item: (-item[1], item[0])):
        entry = {"repo": repo, "downloads_30d": downloads}
        try:
            info = hub.model(repo)
            entry["revision"] = info.get("sha")
            entry["last_modified"] = info.get("lastModified")
            if info.get("gated"):
                entry["state"] = "gated"
            else:
                files, total = gguf_files(info)
                entry["gguf_files"] = total
                entry["state"] = "scanned" if files else "no_gguf"
                if files:
                    entry["files"] = []
                for name, size, blob in files:
                    item = {"file": name, "size": size, "lfs_sha256": blob}
                    shard = SHARD.search(name)
                    if shard:
                        item["shard"] = [int(shard.group(1)), int(shard.group(2))]
                    if blob and blob in by_blob:
                        item.update(by_blob[blob]["result"], same_as=by_blob[blob]["first"])
                    else:
                        try:
                            item.update(_check_file(hub, repo, entry["revision"], name, size, cache))
                        except Exception as error:  # a crafted file must not end the whole scan
                            item.update(verdict="error", error=f"{type(error).__name__}: {error}")
                        if blob and item.get("verdict") not in ("unread", "error"):
                            result = {k: v for k, v in item.items() if k not in KEEP}
                            by_blob[blob] = {"result": result, "first": {"repo": repo, "file": name}}
                    entry["files"].append(item)
                    log(f"{repo}/{name}: {item.get('verdict')} {item.get('runtimes', '')}")
        except LiveError as error:
            entry.update(state="unavailable", error=str(error))
            log(f"{repo}: {error}")
        except Exception as error:
            entry.update(state="error", error=f"{type(error).__name__}: {error}")
            log(f"{repo}: {entry['error']}")
        entries.append(entry)
        if save:
            save(entries)
    return entries


def recheck(previous: dict, cache: Path = HEADERS) -> list:
    """The repositories of an earlier report with every file's verdicts made
    again from the cached headers, offline. A file without a cached header
    keeps its earlier result."""
    entries = json.loads(json.dumps(previous["repositories"]))
    revisions = {entry["repo"]: entry.get("revision") for entry in entries}
    for entry in entries:
        for item in entry.get("files", []):
            repo, name = entry["repo"], item["file"]
            if isinstance(item.get("same_as"), dict):
                repo, name = item["same_as"]["repo"], item["same_as"]["file"]
            try:
                path = header_path(cache, repo, revisions.get(repo) or "", name)
            except LiveError:
                continue
            if not path.is_file():
                continue
            fields = {k: item[k] for k in KEEP if k in item}
            item.clear()
            item.update(fields)
            try:
                item.update(check_header(path.read_bytes(), fields["size"]))
            except Exception as error:
                item.update(verdict="error", error=f"{type(error).__name__}: {error}")
    return entries


def replay(entries: list, cache: Path, binaries: dict, log=None) -> dict:
    """Feed every cached header to the pinned upstream GGUF readers
    (tools/live-replay.sh builds them) and compare with tlv_walk in the same
    runtime. gguf_init_from_file reads no tensor data, so the loader's
    file-end rule (`bounds`) is not part of the comparison. Adds `upstream`
    to each file and returns the counts."""
    log = log or (lambda message: None)
    counts = {"files": 0, "agree": 0, "disagree": 0, "disagreements": []}
    keys = {name: runtime for runtime, name in RUNTIMES.items()}
    for entry in entries:
        for item in entry.get("files", []):
            if item.get("same_as") or item.get("verdict") in ("unread", "error", None):
                continue
            try:
                path = header_path(cache, entry["repo"], entry.get("revision") or "", item["file"])
            except LiveError:
                continue
            if not path.is_file():
                continue
            header = path.read_bytes()
            counts["files"] += 1
            item["upstream"] = {}
            for name, binary in sorted(binaries.items()):
                done = subprocess.run([str(binary), str(path), str(item["size"])], capture_output=True, text=True,
                                      timeout=120)
                accepted = done.returncode == 0 and done.stdout.startswith("ACCEPTED")
                message = next((line.strip() for line in done.stderr.splitlines()
                                if "gguf_init" in line and "failed to read" not in line), None)
                walked, _ = walk(header, item["size"], keys[name])
                ours = walked.status in (0, BOUNDS)
                result = {"accepted": accepted, "agrees": ours == accepted}
                if not accepted and message:
                    result["message"] = message[:300]
                item["upstream"][name] = result
                if ours == accepted:
                    counts["agree"] += 1
                else:
                    counts["disagree"] += 1
                    counts["disagreements"].append({"repo": entry["repo"], "file": item["file"], "runtime": name,
                                                    "upstream": accepted, "t27": status_token(walked.status)})
                    log(f"DISAGREE {entry['repo']}/{item['file']} {name}: upstream {accepted}, t27 {walked.status}")
    return counts


def summary(entries: list) -> dict:
    files = [item for entry in entries for item in entry.get("files", [])]
    verdicts, runtimes = {}, {key: {} for key in RUNTIMES.values()}
    for item in files:
        verdicts[item.get("verdict")] = verdicts.get(item.get("verdict"), 0) + 1
        for key, entry in (item.get("runtimes") or {}).items():
            runtimes[key][entry["verdict"]] = runtimes[key].get(entry["verdict"], 0) + 1
    states = {}
    for entry in entries:
        states[entry.get("state")] = states.get(entry.get("state"), 0) + 1
    refused = sorted({e["repo"] for e in entries if any(i.get("verdict") == "refused" for i in e.get("files", []))})
    return {"repositories": len(entries), "states": dict(sorted(states.items(), key=lambda kv: str(kv[0]))),
            "files": len(files), "verdicts": dict(sorted(verdicts.items(), key=lambda kv: str(kv[0]))),
            "runtimes": {k: dict(sorted(v.items())) for k, v in runtimes.items()},
            "repositories_with_refused_files": len(refused),
            "downloads_30d_scanned": sum(e["downloads_30d"] for e in entries if e.get("state") == "scanned")}


def report(entries: list, discovery: dict, started: str) -> dict:
    return {"schema": SCHEMA, "started": started, "compiler": COMPILER_LOCK.read_text().strip(),
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
    log = (lambda message: None) if args.quiet else (lambda message: print(message, file=sys.stderr, flush=True))
    started = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    if args.recheck:
        previous = json.loads(args.recheck.read_text())
        if previous.get("schema") != SCHEMA:
            parser.error(f"{args.recheck}: not a {SCHEMA} report")
        out = report(recheck(previous, HEADERS), previous["discovery"], previous["started"])
        out["rechecked"] = started
        _write(args.out, out)
        log(f"{out['summary']} -> {args.out} (offline recheck)")
        return 0
    if args.replay:
        current = json.loads(args.out.read_text())
        binaries = {RUNTIMES[runtime]: args.replay / f"gguf_replay_{name}"
                    for runtime, name in ((LLAMA_CPP, "llama_cpp"), (PRISMML, "prismml"), (BITNET_CPP, "bitnet_cpp"))
                    if (args.replay / f"gguf_replay_{name}").is_file()}
        if not binaries:
            parser.error(f"{args.replay}: no gguf_replay_* binaries (run sh tools/live-replay.sh)")
        current["replay"] = replay(current["repositories"], HEADERS, binaries, log)
        _write(args.out, current)
        log(f"replay: {current['replay']['agree']} agree, {current['replay']['disagree']} disagree "
            f"over {current['replay']['files']} files")
        return 0
    hub = Hub(interval=args.interval, log=log)
    discovery = {"endpoint": hub.endpoint, "queries": list(QUERIES), "name_filter": NAME.pattern,
                 "file_filter": LAYOUT_FILE.pattern, "gguf_pages": args.gguf_pages,
                 "min_downloads_30d": args.min_downloads, "limit": args.limit, "repos": args.repo}
    if args.repo:
        repos = {}
        for repo in args.repo:
            try:
                repos[repo] = int(hub.api(f"models/{repo}").get("downloads") or 0)
            except LiveError as error:
                log(f"{repo}: {error}")
                repos[repo] = 0
    else:
        repos = discover(hub, QUERIES, args.min_downloads, args.gguf_pages)
    if args.limit:
        repos = dict(sorted(repos.items(), key=lambda item: (-item[1], item[0]))[: args.limit])
    log(f"{len(repos)} repositories to scan")
    cache = None if args.no_cache else HEADERS
    entries = scan(hub, repos, cache, log, save=lambda done: _write(args.out, report(done, discovery, started)))
    out = report(entries, discovery, started)
    _write(args.out, out)
    log(f"{out['summary']} -> {args.out} ({hub.requests} requests)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
