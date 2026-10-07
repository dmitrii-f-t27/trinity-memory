#!/usr/bin/env python3
"""The public Ternary Check Live page and its badges (issue #53).

A static page from a scan report (trinity.ternary-check-live.v2): one row per
model with its files, layouts, verdicts, the pinned revisions and the check
date; a model the findings ledger (docs/live/findings.md) covers links to its
entry with the entry's status, and a refusal the ledger withdrew (the file is
correct for a runtime that is not pinned here) is shown, and badged, as
other_runtime. The header counts the legacy group-128 Q2_0 files of the scan. A shields.io badge
endpoint per repository renders the current verdict for model cards.

The in-browser check is a separate, self-contained page (check.html) that
loads formats.wasm -- the t27/live.t27 functions exported -- and inspects
bounded GGUF file prefixes through HTTP range requests. Nothing is
uploaded; only the parsed header is evaluated. The parser runs in a worker;
remote checks require network access and a server that exposes Content-Range.

  python3 tools/live-page.py page build/live/scan.json --out site/live
  python3 tools/live-page.py badge build/live/scan.json owner/name

Exit 0 on success; 1 when the report is missing or not the v2 schema.
"""
from __future__ import annotations

import argparse
import datetime
import html
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

VERDICT_TEXT = {
    "ok": "every file parses, carries a ternary layout and loads in the runtime it names",
    "other_runtime": "loads, but in a runtime other than the one tried; the file names its own",
    "no_ternary_layout": "the header's tensor records carry no ternary layout the pinned readers recognise",
    "refused": "a pinned reader refuses the header (the linked explanation says where and why)",
    "limit": "a crafted header pushed a reader past its signed arithmetic; the verdict is the reader's",
}
VERDICT_CLASS = {"ok": "ok", "other_runtime": "mid", "no_ternary_layout": "mid",
                 "refused": "bad", "limit": "mid"}


def e(text) -> str:
    return html.escape(str(text))


LEDGER = ROOT / "docs/live/findings.md"
LEDGER_URL = "https://github.com/dmitrii-f-t27/trinity-memory/blob/master/docs/live/findings.md"


def ledger(path: pathlib.Path = LEDGER) -> list:
    """The entries of the findings ledger: id, status word, the repositories
    its `where` bullet names and the anchor GitHub gives its heading."""
    import re
    if not path.is_file():
        return []
    entries = []
    for block in re.split(r"(?m)^## (?=F-\d+ )", path.read_text())[1:]:
        title, _, body = block.partition("\n")
        status = re.search(r"\*\*status\*\*: (\w+)", body)
        where = re.search(r"- \*\*where\*\*: (.*?)(?=\n- \*\*|\Z)", body, re.S)
        slug = re.sub(r"[^a-z0-9 _-]", "", title.strip().lower()).replace(" ", "-")
        entries.append({"id": title.split()[0], "status": status.group(1) if status else "?",
                        "repos": set(re.findall(r"`([\w.-]+/[\w.-]+)`", where.group(1) if where else "")),
                        "anchor": slug})
    return entries


def ledger_entry(entries: list, repo: str, verdict: str) -> dict | None:
    """The ledger entry that covers a repository's model that is not ok."""
    if verdict == "ok":
        return None
    return next((x for x in entries if repo in x["repos"]), None)


def shown_verdict(verdict: str, entry: dict | None) -> str:
    """A refusal the ledger withdrew (the file is correct for a runtime that
    is not pinned here) is shown as written for another runtime."""
    return "other_runtime" if entry and entry["status"] == "withdrawn" and verdict == "refused" else verdict


def legacy_q2_0(scan: dict, entries: list) -> dict:
    """Files that declare type 42 (Q2_0) while every such record fits the
    group-128 layout (PQ2_0), without the ones the ledger withdrew."""
    rows = []
    for r in scan.get("repositories", []):
        for m in r.get("models", []):
            fits = ((m.get("problems") or {}).get("extent") or {}).get("fits") or {}
            entry = ledger_entry(entries, r["repo"], m.get("verdict", "?"))
            if "PQ2_0" in fits and any(t == 42 for t, _ in m.get("ggml_types", [])) \
                    and not (entry and entry["status"] == "withdrawn"):
                rows += [(r["repo"], f.get("lfs_sha256")) for f in m.get("files", [])]
    own = {sha for repo, sha in rows if repo.startswith("prism-ml/")}
    third = [(repo, sha) for repo, sha in rows if not repo.startswith("prism-ml/")]
    return {"files": len(rows), "third_party": len(third), "repositories": len({repo for repo, _ in third}),
            "copies": sum(sha in own for _, sha in third)}


def badge_json(scan: dict, repo: str, entries: list | None = None) -> dict | None:
    for r in scan.get("repositories", []):
        if r.get("repo") == repo:
            break
    else:
        return None
    entries = ledger() if entries is None else entries
    verdicts = {shown_verdict(m.get("verdict"), ledger_entry(entries, repo, m.get("verdict")))
                for m in r.get("models", [])}
    if not verdicts:
        label, value, color = "ternary check", "no gguf", "lightgrey"
    elif verdicts <= {"ok"}:
        label, value, color = "ternary check", "passing", "046c3c"
    elif "refused" in verdicts:
        label, value, color = "ternary check", "refused file", "8a1c1c"
    elif verdicts <= {"ok", "other_runtime"}:
        label, value, color = "ternary check", "loads (named runtime)", "8a4b00"
    else:
        label, value, color = "ternary check", "unrecognised layout", "8a4b00"
    return {"schemaVersion": 1, "label": label, "message": value, "color": color}


def render_drift(d: dict) -> str:
    """The drift section for the page (tools/live-drift.py output)."""
    moved = d.get("verdicts_moved") or []
    adds = d.get("added") or []
    newrej = d.get("new_rejections") or []
    rows = "".join(
        f'<tr><td><a href="https://huggingface.co/{e(m["repo"])}">{e(m["repo"])}</a></td>'
        f'<td><code>{e(m["file"])}</code></td>'
        f'<td class="v mid">{e(m["was"])}</td><td class="v bad">{e(m["now"])}</td></tr>'
        for m in newrej)
    rows += "".join(
        f'<tr><td><a href="https://huggingface.co/{e(m["repo"])}">{e(m["repo"])}</a></td>'
        f'<td><code>{e(m["file"])}</code></td>'
        f'<td class="v mid">{e(m["was"])}</td><td class="v mid">{e(m["now"])}</td></tr>'
        for m in moved if m not in newrej)
    rows += "".join(
        f'<tr><td><a href="https://huggingface.co/{e(m["repo"])}">{e(m["repo"])}</a></td>'
        f'<td><code>{e(m["file"])}</code></td><td colspan="2" class="v ok">{e(m["verdict"])}</td></tr>'
        for m in adds)
    prev = (d.get("old_started") or "")[:16]
    return (f'<div class="how"><b>Drift since {e(prev)}.</b> '
            f"{d.get('files_new', '?')} files now ({d.get('files_old', '?')} before); "
            f"{len(newrej)} new rejections, {len(moved)} verdicts moved, {len(adds)} new files. "
            "The findings ledger holds the explanations.</div>")


def render(scan: dict, entries: list | None = None) -> str:
    entries = ledger() if entries is None else entries
    legacy = legacy_q2_0(scan, entries)
    when = scan.get("started", "")[:10] or "unknown date"
    s = scan.get("summary", {})
    runtimes = scan.get("runtimes", {})
    rt_line = ", ".join(f"{e(k)} <code>{e(str(v)[:8])}</code>"
                        for k, v in runtimes.items()) or "not replayed"
    rows = []
    for r in scan.get("repositories", []):
        for m in r.get("models", []):
            raw = m.get("verdict", "?")
            entry = ledger_entry(entries, r["repo"], raw)
            v = shown_verdict(raw, entry)
            note = (f' <a href="{LEDGER_URL}#{e(entry["anchor"])}">{e(entry["id"])} {e(entry["status"])}</a>'
                    if entry else "")
            files = ", ".join(f"<code>{e(f.get('file'))}</code>" for f in (m.get("files") or [])[:3])
            native = m.get("native") or "?"
            rows.append(
                f'<tr><td><a href="https://huggingface.co/{e(r["repo"])}">{e(r["repo"])}</a></td>'
                f"<td>{files}</td>"
                f'<td class="v {VERDICT_CLASS.get(v, "mid")}">{e(v)}{note}</td>'
                f"<td><code>{e('its own runtime' if v != raw else native)}</code></td>"
                f'<td class="r">{e(raw if raw == "ok" else (m.get("runtimes", {}) or {}).get("llama.cpp", {}).get("verdict", "?"))}</td></tr>')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ternary Check Live</title>
<style>
body{{font:15px/1.5 -apple-system,BlinkMacSystemFont,sans-serif;margin:0;color:#1a1a1a}}
header{{padding:20px 16px;border-bottom:1px solid #ddd;background:#fafafa}}
h1{{font-size:19px;margin:0 0 4px}} .muted{{color:#667;font-size:12.5px}}
table{{border-collapse:collapse;width:100%;font-size:13.5px}}
td,th{{border-bottom:1px solid #e4e4e4;padding:7px 10px;text-align:left;vertical-align:top}}
th{{position:sticky;top:0;background:#fff;border-bottom:2px solid #1a1a1a;font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
.v{{font-weight:600}} .ok{{color:#046c3c}} .mid{{color:#8a4b00}} .bad{{color:#8a1c1c}}
.badge{{display:inline-block;margin-top:8px;font:12px ui-monospace,monospace;color:#667}}
code{{font:12.5px ui-monospace,monospace;background:#f3f3f3;padding:1px 5px;border-radius:3px}}
.how{{padding:14px 16px;border-bottom:1px solid #e4e4e4;font-size:13.5px}}
@media(max-width:640px){{td:nth-child(2),th:nth-child(2){{display:none}}}}
</style></head><body>
<header>
<h1>Ternary Check Live</h1>
<div class="muted">{e(when)} · {s.get("repositories", "?")} repositories, {s.get("files", "?")} GGUF files,
{s.get("models", "?")} models · replayed against {rt_line}</div>
<div class="badge">Badge:
<code>https://dmitrii-f-t27.github.io/trinity-memory/badge/{e("OWNER--REPO")}.json</code> (shields.io endpoint)</div>
</header>
<div class="how">
<b>What a verdict means.</b> A t27 walk of the file's GGUF header — the same
bytes every reader parses first — checked against the pinned revisions above.
<b>ok</b> means {e(VERDICT_TEXT["ok"])}; the others link below. It does not
measure model quality or speed, and it says nothing about weights the header
does not describe. Rejections link to
<a href="https://github.com/dmitrii-f-t27/trinity-memory/blob/master/docs/live/findings.md">the findings ledger</a>.
<br><b>Legacy Q2_0.</b> {legacy["files"]} files declare type 42 (<code>Q2_0</code>) but store the older
128-weight layout, so the pinned llama.cpp and PrismML readers refuse them: {legacy["files"] - legacy["third_party"]} of PrismML's own and
{legacy["third_party"]} third-party files in {legacy["repositories"]} repositories, {legacy["copies"]} of them byte-identical
copies of PrismML's. A file the ledger withdrew as a defect is shown as <b>other_runtime</b>.
<br><b>Check any file yourself:</b> <a href="check.html">the browser check</a>
inspects bounded file prefixes with HTTP range requests; nothing is uploaded.
</div>
<table><thead><tr><th>repository</th><th>files</th><th>verdict</th><th>written for</th><th>llama.cpp</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>
<div class="how">Verdict explanations: {' · '.join(f'<b class="v {VERDICT_CLASS.get(k, "mid")}">{k}</b> {e(t)}' for k, t in VERDICT_TEXT.items())}
<br>Generated by <code>tools/live-page.py</code> from the scan report; rebuilt daily (#54).</div>
</body></html>"""


CHECK_ASSETS = ("check.html", "check.mjs", "check-worker.mjs", "header-fetch.mjs", "wasm-live.mjs")


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    page = sub.add_parser("page", help="generate index.html and check.html")
    page.add_argument("scan")
    page.add_argument("--out", default=str(ROOT / "site/live"))
    page.add_argument("--drift", default="", help="drift JSON (tools/live-drift.py) to embed")
    bd = sub.add_parser("badge", help="the shields.io badge JSON of one repository")
    bd.add_argument("scan")
    bd.add_argument("repo")
    args = ap.parse_args()
    if args.cmd is None:
        ap.print_usage()
        return 2
    if args.cmd == "badge":
        scan = json.loads(pathlib.Path(args.scan).read_text())
        badge = badge_json(scan, args.repo)
        if badge is None:
            print(f"repository {args.repo} not in the scan", file=sys.stderr)
            return 1
        print(json.dumps(badge))
        return 0
    path = pathlib.Path(args.scan)
    if not path.is_file():
        print(f"no scan at {path}", file=sys.stderr)
        return 1
    scan = json.loads(path.read_text())
    if scan.get("schema") != "trinity.ternary-check-live.v2":
        print(f"not a v2 scan: {scan.get('schema')}", file=sys.stderr)
        return 1
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    page = render(scan)
    if args.drift:
        import re
        drift_html = render_drift(json.loads(pathlib.Path(args.drift).read_text()))
        page = page.replace("<table><thead>", drift_html + "\n<table><thead>", 1)
    (out / "index.html").write_text(page)
    for name in CHECK_ASSETS:
        source = ROOT / "site/live" / name
        if source.resolve() != (out / name).resolve():
            (out / name).write_bytes(source.read_bytes())
    print(f"wrote {out}/index.html and check.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
