"""Ternary Check Live (issue #48): public ternary GGUF files on the Hugging
Face Hub, checked from their headers (I/O glue only).

Discovery uses the public Hub API anonymously: no token is ever sent. Every
observation is pinned to the commit the Hub reports for the repository at the
time of the scan. Header bytes are read with HTTP range requests at that
commit and grown until the t27 reader stops asking for more (the pattern of
fixtures.Remote.header_walk); tensor data is never downloaded. Every verdict
comes from t27 through trinity_memory.formats: tf_gguf_nth for each tensor
record, tf_format_of_gguf under the file's namespace markers, and
tf_gguf_check for the type id, alignment, row and extent rules. This module
only moves bytes, counts the verdicts and writes reports/live/scan.json.

Byte-identical files (same LFS sha256) are read once; the report lists every
repository that serves them. Headers are cached under build/live/headers by
repository, commit and file name, so a repeated scan of an unchanged commit
touches only the API.
"""
from __future__ import annotations
import argparse
import datetime as dt
import http.client
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import ctypes as C

from . import _native as n
from . import formats as f
from .fixtures import _retry_after, write_atomic

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "live" / "scan.json"
HEADERS = ROOT / "build" / "live" / "headers"
SCHEMA = "trinity.ternary-check-live.v1"
USER_AGENT = "trinity-memory-ternary-check-live/0.5 (+https://github.com/dmitrii-f-t27/trinity-memory)"
DEFAULT_ENDPOINT = "https://huggingface.co"
OFFLINE_ENV = "TRINITY_LIVE_OFFLINE"

# Hub search terms and the name filter applied to their results. The filter
# keeps repositories whose name says they hold a ternary (or 1-bit Bonsai)
# layout; the header scan then decides what the files really hold.
QUERIES = ("ternary", "bitnet", "bonsai", "1.58", "b1.58", "1.58bit", "1.58-bit", "tq1_0", "tq2_0",
           "pq2_0", "ptq1_0", "i2_s", "q2_0", "q1_0")
NAME = re.compile(r"ternary|bitnet|1\.58|tq1_0|tq2_0|pq2_0|ptq1_0|i2_s|bonsai|(?<![a-z0-9])q2_0|(?<![a-z0-9])q1_0",
                  re.IGNORECASE)
# File names that announce a layout this repository has a contract for (a model
# name such as "Ternary-Bonsai" says nothing about the file's layout).
LAYOUT_FILE = re.compile(r"tq1_0|tq2_0|pq2_0|ptq1_0|i2_s|tl1|tl2|(?<![a-z0-9])q2_(?:0|g\d+)|(?<![a-z0-9])q1_0",
                         re.IGNORECASE)
SKIP_FILE = re.compile(r"mmproj|imatrix|lora", re.IGNORECASE)
FLOAT_FILE = re.compile(r"(?<![a-z0-9])(?:b?f16|f32)(?![a-z0-9])", re.IGNORECASE)
UNTAGGED_FILES = 3
SHARD = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$")
MAX_HEADER = 256 << 20
FIRST_READ = 1 << 20
FILES_PER_REPO = 12


class LiveError(RuntimeError):
    pass


# ---- t27/live.t27 --------------------------------------------------------------------

U64 = C.c_uint64
# Status classes -63 .. -71 (specs/formats/OWNERS.md): the Hadamard metadata
# of the PrismML fork, in the order its loader checks it.
HADAMARD_TOKENS = {-63: "hadamard_type", -64: "hadamard_missing", -65: "hadamard_version",
                   -66: "hadamard_block", -67: "hadamard_transform", -68: "hadamard_signs",
                   -69: "hadamard_arch", -70: "hadamard_name", -71: "hadamard_tensor", -72: "offsets"}


class Key(C.Structure):
    _fields_ = [("found", C.c_bool), ("vtype", C.c_uint32), ("at", U64), ("elem", C.c_uint32),
                ("count", U64), ("items", U64)]


class Hadamard(C.Structure):
    _fields_ = [("present", C.c_bool), ("block_size", C.c_uint32), ("names", U64), ("inverse", U64),
                ("widths", U64), ("signs", U64), ("explicit_signs", C.c_bool), ("index", U64)]


def key(header: bytes, name: str) -> Key:
    """The first key-value pair `name` of a complete header (tlv_key); raises on a bad header."""
    out, text = Key(), name.encode()
    status = n.call("tlv_key", C.c_int32, [n.U8, n.SZ, C.c_char_p, n.SZ, C.POINTER(Key)],
                    n.octets(header), len(header), text, len(text), C.byref(out))
    if status < 0:
        raise f.FormatError(status)
    return out


def text_value(header: bytes, name: str) -> str | None:
    """A string value, for display only (moving bytes, not a verdict)."""
    found = key(header, name)
    if not found.found or found.vtype != 8:
        return None
    length = int.from_bytes(header[found.at: found.at + 8], "little")
    return header[found.at + 8: found.at + 8 + length].decode("utf-8", "replace")


def hadamard(header: bytes):
    """(status, Hadamard): 0 or a negative status of tlv_hadamard."""
    out = Hadamard()
    status = n.call("tlv_hadamard", C.c_int32, [n.U8, n.SZ, C.POINTER(Hadamard)],
                    n.octets(header), len(header), C.byref(out))
    return status, out


def layout_fit(info, file_size: int) -> int:
    """Format id whose size fills the space a rejected record has (tlv_layout_fit), or 0."""
    return n.call("tlv_layout_fit", C.c_int32, [C.POINTER(f.TensorInfo), U64], C.byref(info), file_size)


def prism_unmarked(info) -> bool:
    return bool(n.call("tlv_prism_unmarked", C.c_bool, [C.POINTER(f.TensorInfo)], C.byref(info)))


def record_check(info, file_size: int) -> int:
    """tlv_record_check: format id, 0 (not a ternary layout) or a negative status."""
    return n.call("tlv_record_check", C.c_int32, [C.POINTER(f.TensorInfo), U64], C.byref(info), file_size)


def status_token(status: int) -> str:
    return HADAMARD_TOKENS.get(status) or f.TOKENS.get(status) or f"status {status}"


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
        if self.offline:
            raise LiveError(f"{url}: network access is off ({OFFLINE_ENV}=1)")
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
        for attempt in range(self.attempts):
            wait = self._last + self.interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.requests += 1
            try:
                response = urllib.request.urlopen(request, timeout=120)
                return response, response.read()
            except urllib.error.HTTPError as error:
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

    def api(self, path: str, params: dict | None = None):
        url = f"{self.endpoint}/api/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        results, pages = [], 0
        while url:
            response, body = self._open(url)
            value = json.loads(body)
            if not isinstance(value, list):
                return value
            results.extend(value)
            pages += 1
            url = _next_link(response.headers.get("Link", "")) if pages < 20 else None
        return results

    def search(self, query: str) -> list:
        return self.api("models", {"search": query, "limit": 1000})

    def model(self, repo: str) -> dict:
        return self.api(f"models/{repo}", {"blobs": "true"})

    def read(self, repo: str, revision: str, filename: str, begin: int, end: int) -> bytes:
        """Bytes [begin, end) of a file at a commit: HTTP 206 with exactly that length."""
        url = f"{self.endpoint}/{repo}/resolve/{revision}/{urllib.parse.quote(filename)}"
        response, data = self._open(url, {"Range": f"bytes={begin}-{end - 1}"})
        if response.status != 206:
            raise LiveError(f"{repo}@{revision}/{filename}: server ignored the range request (HTTP {response.status})")
        if len(data) != end - begin:
            raise LiveError(f"{repo}@{revision}/{filename}: expected {end - begin} bytes, received {len(data)}")
        return data


def _next_link(header: str) -> str | None:
    for part in header.split(","):
        match = re.match(r'\s*<([^>]+)>\s*;\s*rel="next"', part)
        if match:
            return match.group(1)
    return None


# ---- discovery -------------------------------------------------------------------

def discover(hub: Hub, queries=QUERIES, min_downloads: int = 100) -> dict:
    """Repository id -> 30-day downloads, for search hits whose name passes NAME."""
    found = {}
    for query in queries:
        for model in hub.search(query):
            repo = model.get("id") or model.get("modelId")
            if not repo or not NAME.search(repo):
                continue
            downloads = int(model.get("downloads") or 0)
            if downloads >= min_downloads:
                found[repo] = max(downloads, found.get(repo, 0))
    return found


def gguf_files(info: dict) -> list:
    """(name, size, sha256) of the GGUF files worth a header read: files named
    after a layout with a contract; when none is, up to UNTAGGED_FILES files
    not named as float weights (a repository may call its ternary file
    model.gguf or Full-Ternary.gguf), else the smallest GGUF file. Projectors,
    imatrix files and LoRA adapters are skipped."""
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
        return sorted(named)[:FILES_PER_REPO]
    by_size = sorted(files, key=lambda entry: (entry[1], entry[0]))
    quantized = [entry for entry in by_size if not FLOAT_FILE.search(entry[0])]
    return sorted(quantized[:UNTAGGED_FILES] or by_size[:1])


# ---- one header --------------------------------------------------------------------

def read_header(hub: Hub, repo: str, revision: str, name: str, size: int, cache: Path | None) -> bytes:
    """The GGUF header of one file: bytes up to the start of tensor data, grown
    until tf_gguf_find (asked for a name no tensor has) stops reporting
    TRUNCATED. Cached by repository, commit and file name."""
    path = None
    if cache is not None:
        path = header_path(cache, repo, revision, name)
        if path.is_file():
            return path.read_bytes()
    have = b""
    needed = min(FIRST_READ, size)
    while True:
        if needed > MAX_HEADER:
            raise LiveError(f"{repo}/{name}: header larger than {MAX_HEADER} bytes")
        if needed > size:
            raise LiveError(f"{repo}/{name}: the header needs {needed} bytes, the file has {size}")
        if needed > len(have):
            end = min(size, max(needed, 2 * len(have)))
            have += hub.read(repo, revision, name, len(have), end)
        status, info = f.gguf_find(have, "\0")
        if status == f.TRUNCATED:
            if info.needed <= len(have):
                raise LiveError(f"{repo}/{name}: reader asked for {info.needed} bytes with {len(have)} read")
            needed = info.needed
            continue
        header = have[: info.data_start] if status == f.NOT_FOUND else have
        if path is not None and status == f.NOT_FOUND:
            write_atomic(path, header)
        return header


def check_header(header: bytes, size: int) -> dict:
    """The t27 verdicts for every tensor record of one GGUF header, and for its
    prism.hadamard.* metadata."""
    status, info = f.gguf_find(header, "\0")
    if status != f.NOT_FOUND:
        return {"verdict": "container", "container": status_token(status), "tensors": None}
    record = {"tensors": int(info.tensors), "architecture": text_value(header, "general.architecture"),
              "namespace": {"prism": bool(info.prism), "bitnet": bool(info.bitnet)},
              "data_start": int(info.data_start), "ggml_types": {}, "layouts": {}, "rejections": {}}
    ternary = unmarked = 0

    def reject(token, name, fit=None):
        entry = record["rejections"].setdefault(token, {"count": 0, "examples": []})
        entry["count"] += 1
        if len(entry["examples"]) < 3:
            entry["examples"].append(name)
        if fit is not None:
            label = f.NAMES.get(fit, "none") if fit else "none"
            entry.setdefault("fits", {})
            entry["fits"][label] = entry["fits"].get(label, 0) + 1

    for name, tensor in f.gguf_names(header):
        type_key = str(tensor.tensor_type)
        record["ggml_types"][type_key] = record["ggml_types"].get(type_key, 0) + 1
        checked = record_check(tensor, size)
        if checked == 0:
            continue
        ternary += 1
        if prism_unmarked(tensor):
            unmarked += 1
        if checked > 0:
            label = f.NAMES.get(checked, str(checked))
            record["layouts"][label] = record["layouts"].get(label, 0) + 1
        else:
            reason = status_token(checked)
            reject(reason, name, layout_fit(tensor, size) if reason in ("extent", "offsets") else None)
    status, rotation = hadamard(header)
    record["hadamard"] = {"present": bool(rotation.present),
                          "status": "ok" if status == 0 else status_token(status)}
    if rotation.present:
        record["hadamard"].update(block_size=int(rotation.block_size), weight_names=int(rotation.names),
                                  inverse_names=int(rotation.inverse), explicit_signs=bool(rotation.explicit_signs),
                                  sign_widths=int(rotation.widths), sign_values=int(rotation.signs))
        if status < 0:
            record["hadamard"]["index"] = int(rotation.index)
    if status < 0:
        reject(record["hadamard"]["status"], "prism.hadamard")
    record["ggml_types"] = dict(sorted(record["ggml_types"].items(), key=lambda item: int(item[0])))
    record["ternary_tensors"] = ternary
    record["prism_unmarked"] = unmarked
    if record["rejections"]:
        record["verdict"] = "rejected"
    elif ternary:
        record["verdict"] = "ok"
    else:
        record["verdict"] = "no_ternary_layout"
    return record


# ---- the scan ----------------------------------------------------------------------

def scan(hub: Hub, repos: dict, cache: Path | None = HEADERS, log=None) -> list:
    """One entry per repository (most downloaded first), each file checked once
    per LFS sha256."""
    log = log or (lambda message: None)
    by_blob: dict = {}
    entries = []
    for repo, downloads in sorted(repos.items(), key=lambda item: (-item[1], item[0])):
        entry = {"repo": repo, "downloads_30d": downloads}
        try:
            info = hub.model(repo)
        except LiveError as error:
            entry.update(state="unavailable", error=str(error))
            entries.append(entry)
            log(f"{repo}: {error}")
            continue
        entry["revision"] = info.get("sha")
        entry["last_modified"] = info.get("lastModified")
        if info.get("gated"):
            entry["state"] = "gated"
            entries.append(entry)
            continue
        files = gguf_files(info)
        if not files:
            entry["state"] = "no_gguf"
            entries.append(entry)
            continue
        entry["state"] = "scanned"
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
                    header = read_header(hub, repo, entry["revision"], name, size, cache)
                    item.update(check_header(header, size))
                except (LiveError, f.FormatError) as error:
                    item.update(verdict="unread", error=str(error))
                if blob and item.get("verdict") != "unread":
                    result = {k: v for k, v in item.items() if k not in ("file", "size", "lfs_sha256", "shard")}
                    by_blob[blob] = {"result": result, "first": f"{repo}/{name}"}
            entry["files"].append(item)
            log(f"{repo}/{name}: {item.get('verdict')} {item.get('layouts', '')} {item.get('rejections', '')}")
        entries.append(entry)
    return entries


def header_path(cache: Path, repo: str, revision: str, name: str) -> Path:
    return cache / repo.replace("/", "--") / revision / (name.replace("/", "--") + ".header")


def recheck(previous: dict, cache: Path = HEADERS) -> list:
    """The repositories of an earlier report with every file's verdicts made
    again from the cached headers, without network access: the same revisions,
    the current t27 checks."""
    entries = json.loads(json.dumps(previous["repositories"]))
    revisions = {entry["repo"]: entry.get("revision") for entry in entries}
    keep = ("file", "size", "lfs_sha256", "shard", "same_as")
    for entry in entries:
        for item in entry.get("files", []):
            repo, name = entry["repo"], item["file"]
            if "same_as" in item:
                owner, model, name = item["same_as"].split("/", 2)
                repo = f"{owner}/{model}"
            path = header_path(cache, repo, revisions.get(repo) or "", name)
            fields = {k: item[k] for k in keep if k in item}
            item.clear()
            item.update(fields)
            if path.is_file():
                item.update(check_header(path.read_bytes(), fields["size"]))
            else:
                item.update(verdict="unread", error="header not in the cache")
    return entries


def summary(entries: list) -> dict:
    files = [item for entry in entries for item in entry.get("files", [])]
    verdicts: dict = {}
    for item in files:
        verdicts[item.get("verdict")] = verdicts.get(item.get("verdict"), 0) + 1
    rejected = [e["repo"] for e in entries if any(i.get("verdict") == "rejected" for i in e.get("files", []))]
    return {"repositories": len(entries), "scanned": sum(e.get("state") == "scanned" for e in entries),
            "files": len(files), "verdicts": dict(sorted(verdicts.items())),
            "repositories_with_rejections": len(rejected),
            "downloads_30d_scanned": sum(e["downloads_30d"] for e in entries if e.get("state") == "scanned")}


def report(entries: list, queries, min_downloads: int, started: str) -> dict:
    return {"schema": SCHEMA, "started": started,
            "compiler": (ROOT / "native" / "compiler.lock").read_text().strip(),
            "discovery": {"endpoint": DEFAULT_ENDPOINT, "queries": list(queries), "name_filter": NAME.pattern,
                          "min_downloads_30d": min_downloads},
            "summary": summary(entries), "repositories": entries}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m trinity_memory.live", description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=REPORT)
    parser.add_argument("--min-downloads", type=int, default=100, help="30-day downloads (default 100)")
    parser.add_argument("--limit", type=int, default=0, help="scan only the N most downloaded repositories")
    parser.add_argument("--repo", action="append", default=[], help="scan these repositories instead of discovering")
    parser.add_argument("--interval", type=float, default=1.5, help="seconds between requests (default 1.5)")
    parser.add_argument("--no-cache", action="store_true", help="do not read or write build/live/headers")
    parser.add_argument("--recheck", type=Path, help="make the verdicts of this report again from cached headers, offline")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    log = (lambda message: None) if args.quiet else (lambda message: print(message, file=sys.stderr, flush=True))
    hub = Hub(interval=args.interval, log=log)
    started = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    if args.recheck:
        previous = json.loads(args.recheck.read_text())
        entries = recheck(previous, HEADERS)
        discovery = previous["discovery"]
        out = report(entries, discovery["queries"], discovery["min_downloads_30d"], previous["started"])
        out["rechecked"] = started
        args.out.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(args.out, (json.dumps(out, indent=1, ensure_ascii=False) + "\n").encode())
        log(f"{out['summary']} -> {args.out} (offline recheck)")
        return 0
    if args.repo:
        repos = {repo: 0 for repo in args.repo}
        for repo in args.repo:
            try:
                repos[repo] = int(hub.api(f"models/{repo}").get("downloads") or 0)
            except LiveError as error:
                log(f"{repo}: {error}")
    else:
        repos = discover(hub, QUERIES, args.min_downloads)
    if args.limit:
        repos = dict(sorted(repos.items(), key=lambda item: (-item[1], item[0]))[: args.limit])
    log(f"{len(repos)} repositories to scan")
    entries = scan(hub, repos, None if args.no_cache else HEADERS, log)
    out = report(entries, QUERIES, args.min_downloads, started)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(args.out, (json.dumps(out, indent=1, ensure_ascii=False) + "\n").encode())
    log(f"{out['summary']} -> {args.out} ({hub.requests} requests)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
