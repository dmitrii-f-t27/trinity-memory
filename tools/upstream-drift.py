#!/usr/bin/env python3
"""Drift of the pinned upstream sources and model revisions (I/O only).

Compares, by git blob SHA, every file pinned in specs/formats/upstream.lock.json
and tests/upstream/llama.cpp.lock.json with the same path at the head of the
upstream branch (and a pinned submodule gitlink with the one at the parent's
head), and, by LFS SHA-256, every model file pinned in fixtures/manifest.json
with the same file at the Hugging Face repository's current main. A head
commit or a main revision that differs from the pinned one is not drift by
itself (a model-card edit moves main too); a changed or missing file or a
moved gitlink is. Moved model revisions are reported as information.

  python3 tools/upstream-drift.py --output build/upstream-drift.json [--summary FILE]
  python3 tools/upstream-drift.py --replay RESPONSES.json    no network: answer from recorded responses
  python3 tools/upstream-drift.py --record RESPONSES.json    also save every response it read

Reads public APIs with GET only and posts nothing anywhere. GH_TOKEN (or
GITHUB_TOKEN) raises the GitHub API rate limit and is sent to api.github.com
only. The report (trinity.upstream-drift.v1) keeps its run metadata in a
separate "run" block. Exit status: 0 no drift, 1 drift, 2 a head or file could
not be read (and no drift was found in what could be read).
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = "trinity.upstream-drift.v1"
UPSTREAM_LOCK = "specs/formats/upstream.lock.json"
LLAMACPP_LOCK = "tests/upstream/llama.cpp.lock.json"
MANIFEST = "fixtures/manifest.json"
GITHUB_API = "https://api.github.com"
HF_API = "https://huggingface.co/api"
USER_AGENT = "trinity-memory-upstream-drift/1 (+https://github.com/dmitrii-f-t27/trinity-memory)"


class FetchError(Exception):
    def __init__(self, url: str, status, message: str):
        super().__init__(f"{url}: {message}")
        self.url, self.status = url, status


class Fetcher:
    """GET JSON over HTTPS; optionally records what it read, or replays it."""

    def __init__(self, token: str | None = None, replay: dict | None = None, timeout: float = 30.0):
        self.token, self.replay, self.timeout = token, replay, timeout
        self.recorded: dict = {}
        self.seen: dict = {}

    def __call__(self, url: str):
        if url not in self.seen:
            try:
                self.seen[url] = ("json", self._get(url))
            except FetchError as error:
                self.seen[url] = ("error", error)
        kind, value = self.seen[url]
        if kind == "error":
            raise value
        return value

    def _get(self, url: str):
        if self.replay is not None:
            if url not in self.replay:
                raise FetchError(url, None, "no recorded response")
            entry = self.replay[url]
            if "error" in entry:
                raise FetchError(url, entry.get("status"), entry["error"])
            return entry["json"]
        headers = {"User-Agent": USER_AGENT}
        if url.startswith(GITHUB_API + "/"):
            headers["Accept"] = "application/vnd.github.object+json"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=self.timeout) as reply:
                body = json.loads(reply.read())
        except urllib.error.HTTPError as error:
            self.recorded[url] = {"status": error.code, "error": f"HTTP {error.code}"}
            raise FetchError(url, error.code, f"HTTP {error.code}") from error
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise FetchError(url, None, str(error)) from error
        self.recorded[url] = {"json": trim(url, body)}
        return body


def trim(url: str, body):
    """The fields this tool reads, so that recorded responses stay small."""
    if not isinstance(body, dict):
        return body
    if "/git/ref/" in url:
        return {"object": {"sha": body.get("object", {}).get("sha"), "type": body.get("object", {}).get("type")}}
    if "/contents/" in url:
        return {"type": body.get("type"), "sha": body.get("sha"), "path": body.get("path")}
    if url.startswith(HF_API + "/"):
        siblings = [s for s in body.get("siblings") or [] if isinstance(s, dict) and isinstance(s.get("lfs"), dict)]
        return {"sha": body.get("sha"),
                "siblings": [{"rfilename": s.get("rfilename"),
                              "lfs": {"sha256": s["lfs"].get("sha256"), "size": s["lfs"].get("size")}}
                             for s in siblings]}
    return {"sha": body.get("sha")}


def quote(path: str) -> str:
    return urllib.parse.quote(path, safe="/")


def head_of(fetch, repo: str, branch: str) -> str:
    body = fetch(f"{GITHUB_API}/repos/{repo}/git/ref/heads/{quote(branch)}")
    sha = body.get("object", {}).get("sha")
    if not isinstance(sha, str):
        raise FetchError(f"{repo}@{branch}", None, "no commit SHA in the ref")
    return sha


def entry_at(fetch, repo: str, path: str, ref: str):
    """(type, sha) of a path at a commit; ("missing", None) when it does not exist there."""
    try:
        body = fetch(f"{GITHUB_API}/repos/{repo}/contents/{quote(path)}?ref={ref}")
    except FetchError as error:
        if error.status == 404:
            return "missing", None
        raise
    if not isinstance(body, dict) or not isinstance(body.get("sha"), str):
        return "missing", None  # a directory listing or something else, not a file
    return body.get("type"), body["sha"]


def github_targets(root: Path):
    """(lock file, key, repo, branch, pinned commit, {path: blob}, submodule or None)."""
    targets = []
    upstream = json.loads((root / UPSTREAM_LOCK).read_text(encoding="utf-8"))
    branches = {}
    for key, pin in sorted(upstream["upstreams"].items()):
        branches.setdefault(pin["repo"], pin["branch"])
        targets.append((UPSTREAM_LOCK, key, pin["repo"], pin["branch"], pin["commit"], dict(pin["files"]),
                        pin.get("submodule_of")))
    llamacpp = json.loads((root / LLAMACPP_LOCK).read_text(encoding="utf-8"))
    files = {path: entry["git_blob_sha"] for path, entry in llamacpp["files"].items()}
    targets.append((LLAMACPP_LOCK, "llama.cpp", llamacpp["repo"], branches.get(llamacpp["repo"], "master"),
                    llamacpp["commit"], files, None))
    return targets, branches


def check(root: Path, fetch) -> dict:
    """The drift report; `fetch(url)` returns parsed JSON or raises FetchError."""
    targets, branches = github_targets(root)
    heads, errors, upstreams = {}, [], []
    for lock, key, repo, branch, commit, files, submodule in targets:
        entry = {"lock": lock, "key": key, "repo": repo, "branch": branch, "pinned_commit": commit}
        try:
            if (repo, branch) not in heads:
                heads[(repo, branch)] = head_of(fetch, repo, branch)
            head = heads[(repo, branch)]
        except FetchError as error:
            errors.append(str(error))
            entry.update(head=None, status="unknown",
                         files=[{"path": path, "pinned_blob": files[path], "head_blob": None, "status": "unknown"}
                                for path in sorted(files)])
            upstreams.append(entry)
            continue
        entry["head"] = head
        results, drift, unknown = [], False, False
        for path in sorted(files):
            try:
                kind, sha = entry_at(fetch, repo, path, head)
            except FetchError as error:
                errors.append(str(error))
                results.append({"path": path, "pinned_blob": files[path], "head_blob": None, "status": "unknown"})
                unknown = True
                continue
            status = "missing" if kind == "missing" else "same" if sha == files[path] else "changed"
            drift |= status != "same"
            results.append({"path": path, "pinned_blob": files[path], "head_blob": sha, "status": status})
        entry["files"] = results
        if submodule:
            parent, parent_branch = submodule["repo"], branches.get(submodule["repo"], "main")
            record = {"repo": parent, "branch": parent_branch, "path": submodule["path"], "pinned_gitlink": commit}
            try:
                if (parent, parent_branch) not in heads:
                    heads[(parent, parent_branch)] = head_of(fetch, parent, parent_branch)
                kind, sha = entry_at(fetch, parent, submodule["path"], heads[(parent, parent_branch)])
                record["head_gitlink"] = sha if kind == "submodule" else None
                record["status"] = "same" if kind == "submodule" and sha == commit else \
                    "missing" if kind != "submodule" else "moved"
                drift |= record["status"] != "same"
            except FetchError as error:
                errors.append(str(error))
                record.update(head_gitlink=None, status="unknown")
                unknown = True
            entry["submodule_of"] = record
        entry["status"] = "drift" if drift else "unknown" if unknown else "same"
        if drift and head != commit:
            entry["compare"] = f"https://github.com/{repo}/compare/{commit}...{head}"
        upstreams.append(entry)
    models = [model_record(fetch, model, errors)
              for model in json.loads((root / MANIFEST).read_text(encoding="utf-8"))["models"]]
    files = [f for u in upstreams for f in u["files"]]
    model_files = [f for m in models for f in m["files"]]
    count = lambda items, status: sum(1 for item in items if item["status"] == status)  # noqa: E731
    submodules = [u["submodule_of"] for u in upstreams if "submodule_of" in u]
    summary = {
        "files": len(files), "files_changed": count(files, "changed"), "files_missing": count(files, "missing"),
        "files_unknown": count(files, "unknown"),
        "gitlinks": len(submodules), "gitlinks_moved": sum(1 for s in submodules if s["status"] != "same"
                                                           and s["status"] != "unknown"),
        "models": len(models), "models_unknown": count(models, "unknown"),
        "models_revision_moved": sum(1 for m in models if m["revision_moved"]),
        "model_files": len(model_files), "model_files_changed": count(model_files, "changed"),
        "model_files_missing": count(model_files, "missing"),
    }
    drift = any(u["status"] == "drift" for u in upstreams) or any(m["status"] == "drift" for m in models)
    return {"schema": SCHEMA, "sources": [UPSTREAM_LOCK, LLAMACPP_LOCK, MANIFEST], "drift": drift,
            "complete": not errors, "summary": summary, "github": upstreams, "huggingface": models,
            "errors": errors}


def model_record(fetch, model, errors) -> dict:
    """One Hugging Face model: its pinned files compared by LFS SHA-256 with the files at main."""
    record = {"repo": model["repo"], "pinned_revision": model["revision"]}
    pinned = [{"name": f["name"], "pinned_lfs_sha256": f["lfs_sha256"]} for f in model["files"]]
    try:
        body = fetch(f"{HF_API}/models/{model['repo']}/revision/main?blobs=true")
        main = body.get("sha") if isinstance(body, dict) else None
        if not isinstance(main, str):
            raise FetchError(model["repo"], None, "no revision SHA for main")
    except FetchError as error:
        errors.append(str(error))
        record.update(main=None, revision_moved=False, status="unknown",
                      files=[dict(f, main_lfs_sha256=None, status="unknown") for f in pinned])
        return record
    at_main = {s.get("rfilename"): (s.get("lfs") or {}).get("sha256") for s in body.get("siblings") or []
               if isinstance(s, dict)}
    files = []
    for f in pinned:
        sha = at_main.get(f["name"])
        status = "missing" if sha is None else "same" if sha == f["pinned_lfs_sha256"] else "changed"
        files.append(dict(f, main_lfs_sha256=sha, status=status))
    record.update(main=main, revision_moved=main != model["revision"], files=files,
                  status="same" if all(f["status"] == "same" for f in files) else "drift")
    if record["status"] == "drift":
        record["commits"] = f"https://huggingface.co/{model['repo']}/commits/main"
    return record


def exit_status(report) -> int:
    if report["drift"]:
        return 1
    return 0 if report["complete"] else 2


def markdown(report) -> str:
    s = report["summary"]
    state = "drift" if report["drift"] else "no drift" if report["complete"] else "incomplete"
    lines = [f"## Upstream drift: {state}", "",
             f"{s['files']} pinned files: {s['files_changed']} changed, {s['files_missing']} missing, "
             f"{s['files_unknown']} unreadable; {s['gitlinks']} submodule gitlinks, {s['gitlinks_moved']} moved; "
             f"{s['model_files']} pinned model files in {s['models']} models: {s['model_files_changed']} changed, "
             f"{s['model_files_missing']} missing, {s['models_unknown']} models unreadable.", ""]
    if s["models_revision_moved"]:
        moved = [m["repo"] for m in report["huggingface"] if m["revision_moved"]]
        lines += [f"Main moved past the pinned revision without drift in a pinned file (information): "
                  f"{', '.join(moved)}.", ""]
    rows = []
    for u in report["github"]:
        for f in u["files"]:
            if f["status"] != "same":
                rows.append(f"| {u['repo']}@{u['branch']} | `{f['path']}` | {f['status']} | "
                            f"{u.get('compare', '')} |")
        sub = u.get("submodule_of")
        if sub and sub["status"] != "same":
            rows.append(f"| {sub['repo']}@{sub['branch']} | `{sub['path']}` (gitlink) | {sub['status']} | |")
    for m in report["huggingface"]:
        for f in m["files"]:
            if f["status"] != "same":
                rows.append(f"| {m['repo']}@main | `{f['name']}` | {f['status']} | {m.get('commits', '')} |")
    if rows:
        lines += ["| upstream | file | status | compare |", "|---|---|---|---|", *rows, ""]
    if report["errors"]:
        lines += ["Unreadable:", *[f"- {e}" for e in report["errors"][:20]], ""]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root holding the lock files")
    parser.add_argument("--output", type=Path, help="write the JSON report here")
    parser.add_argument("--summary", type=Path, help="append the Markdown summary here")
    parser.add_argument("--replay", type=Path, help="answer every request from these recorded responses")
    parser.add_argument("--record", type=Path, help="save the responses this run read")
    args = parser.parse_args(argv)
    replay = json.loads(args.replay.read_text(encoding="utf-8")) if args.replay else None
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    fetch = Fetcher(token=None if replay is not None else token, replay=replay)
    report = check(args.root, fetch)
    report["run"] = {"note": "Run metadata: excluded from the reproducible content.",
                     "checked_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "replayed": replay is not None, "authenticated": bool(token) and replay is None}
    text = json.dumps(report, indent=1) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    summary = markdown(report)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as handle:
            handle.write(summary)
    sys.stderr.write(summary)
    if args.record:
        args.record.write_text(json.dumps(dict(sorted(fetch.recorded.items())), indent=1) + "\n", encoding="utf-8")
    return exit_status(report)


if __name__ == "__main__":
    raise SystemExit(main())
