#!/usr/bin/env python3
"""The public Ternary Check Live page and its badges (issue #53).

A static page from a scan report (trinity.ternary-check-live.v2): one row per
model with its files, layouts, verdicts, the pinned revisions and the check
date; every non-ok verdict links to its explanation; the confirmed findings
link to their ledger entries (docs/live/findings.md). A shields.io badge
endpoint per repository renders the current verdict for model cards.

The in-browser check is a separate, self-contained page (check.html) that
loads formats.wasm -- the t27/live.t27 functions exported -- and inspects
bounded GGUF file prefixes through HTTP range requests. Nothing is
uploaded; only the parsed header is evaluated. The parser runs in a worker;
remote checks require network access and a server that exposes Content-Range.

  python3 tools/live-page.py build/live/scan.json --out site/live
  python3 tools/live-page.py badge build/live/scan.json --repo owner/name

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


def badge_json(scan: dict, repo: str) -> dict | None:
    for r in scan.get("repositories", []):
        if r.get("repo") == repo:
            break
    else:
        return None
    verdicts = {m.get("verdict") for m in r.get("models", [])}
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
        f"<td><code>{e(m["file"])}</code></td>"
        f'<td class="v mid">{e(m["was"])}</td><td class="v bad">{e(m["now"])}</td></tr>'
        for m in newrej)
    rows += "".join(
        f'<tr><td><a href="https://huggingface.co/{e(m["repo"])}">{e(m["repo"])}</a></td>'
        f"<td><code>{e(m["file"])}</code></td>"
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


def render(scan: dict) -> str:
    when = scan.get("started", "")[:10] or "unknown date"
    s = scan.get("summary", {})
    runtimes = scan.get("runtimes", {})
    rt_line = ", ".join(f"{e(k)} <code>{e(str(v)[:8])}</code>"
                        for k, v in runtimes.items()) or "not replayed"
    rows = []
    for r in scan.get("repositories", []):
        for m in r.get("models", []):
            v = m.get("verdict", "?")
            files = ", ".join(f"<code>{e(f.get('file'))}</code>" for f in (m.get("files") or [])[:3])
            native = m.get("native") or "?"
            rows.append(
                f'<tr><td><a href="https://huggingface.co/{e(r["repo"])}">{e(r["repo"])}</a></td>'
                f"<td>{files}</td>"
                f'<td class="v {VERDICT_CLASS.get(v, "mid")}">{e(v)}</td>'
                f"<td><code>{e(native)}</code></td>"
                f'<td class="r">{e(v if v == "ok" else (m.get("runtimes", {}) or {}).get("llama.cpp", {}).get("verdict", "?"))}</td></tr>')
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
<br><b>Check any file yourself:</b> <a href="check.html">the browser check</a>
inspects bounded file prefixes with HTTP range requests; nothing is uploaded.
</div>
<table><thead><tr><th>repository</th><th>files</th><th>verdict</th><th>written for</th><th>llama.cpp</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>
<div class="how">Verdict explanations: {' · '.join(f'<b class="v {VERDICT_CLASS.get(k, "mid")}">{k}</b> {e(t)}' for k, t in VERDICT_TEXT.items())}
<br>Generated by <code>tools/live-page.py</code> from the committed scan report; rebuilt weekly (#54).</div>
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
