#!/usr/bin/env python3
"""The drift between two Ternary Check Live reports (issue #54).

new files, changed revisions, new rejections, verdicts that moved — the diff
the weekly run shows on the page. The previous report is committed at
reports/live/scan.json by the publishing workflow; the fresh report is the
scan's output. A failed or rate-limited scan never reaches this tool: the
caller publishes nothing and the last good page stays.

  python3 tools/live-drift.py reports/live/scan.json build/live/scan.json [--html]

Exit 0 always (a drift is information, not a failure); 2 when a report is
missing or not the v2 schema — the caller must then publish nothing.
"""
from __future__ import annotations

import argparse
import datetime
import html
import json
import pathlib
import sys


def load(path: str) -> dict:
    p = pathlib.Path(path)
    if not p.is_file():
        raise SystemExit(f"no report at {p} (2)")
    d = json.loads(p.read_text())
    if d.get("schema") != "trinity.ternary-check-live.v2":
        raise SystemExit(f"not a v2 report: {p} (2)")
    return d


def key(report: dict) -> dict:
    """(repo, file) -> (revision, verdict, lfs sha)."""
    out = {}
    for r in report.get("repositories", []):
        for m in r.get("models", []):
            for f in m.get("files", []):
                out[(r["repo"], f.get("file"))] = (r.get("revision", ""),
                                                   m.get("verdict", "?"),
                                                   f.get("lfs_sha256", ""))
    return out


def drift(old: dict, new: dict) -> dict:
    a, b = key(old), key(new)
    gone = sorted(k for k in a if k not in b)
    added = sorted(k for k in b if k not in a)
    moved, revision_changed, content_changed = [], [], []
    for k in sorted(set(a) & set(b)):
        if a[k][1] != b[k][1]:
            moved.append({"repo": k[0], "file": k[1], "was": a[k][1], "now": b[k][1]})
        if a[k][0] != b[k][0]:
            revision_changed.append({"repo": k[0], "file": k[1],
                                    "was": a[k][0][:12], "now": b[k][0][:12]})
        if a[k][2] and b[k][2] and a[k][2] != b[k][2]:
            content_changed.append({"repo": k[0], "file": k[1]})
    new_rejections = [m for m in moved if m["now"] in ("refused", "no_ternary_layout")]
    return {"old_started": old.get("started", ""), "new_started": new.get("started", ""),
            "files_old": len(a), "files_new": len(b),
            "added": [{"repo": r, "file": f, "verdict": b[(r, f)][1]} for r, f in added],
            "gone": [{"repo": r, "file": f} for r, f in gone],
            "verdicts_moved": moved, "new_rejections": new_rejections,
            "revision_changed": revision_changed, "content_changed": content_changed}


def render_html(d: dict) -> str:
    def rows(items, cols):
        return "".join(f"<tr><td>{html.escape(str(i.get(c, '')))}</td></tr>"
                       for i in items for c in [cols]) if False else "".join(
            "<tr>" + "".join(f"<td>{html.escape(str(i.get(c, '')))}</td>" for c in cols) + "</tr>"
            for i in items)

    def section(title, items, cols):
        n = len(items)
        inner = rows(items, cols) if n else '<tr><td class="m">none</td></tr>'
        return f"<h3>{title} ({n})</h3><table><tbody>{inner}</tbody></table>"

    when = (d.get("new_started") or "")[:16]
    prev = (d.get("old_started") or "")[:16]
    return f"""<section id="drift">
<h2>Drift since {html.escape(prev)}</h2>
<p class="muted">fresh scan {html.escape(when)} · {d['files_new']} files now
({d['files_old']} before)</p>
{section("New files", d["added"], ["repo", "file", "verdict"])}
{section("Files gone", d["gone"], ["repo", "file"])}
{section("Verdicts moved", d["verdicts_moved"], ["repo", "file", "was", "now"])}
{section("New rejections", d["new_rejections"], ["repo", "file", "was", "now"])}
{section("Revision changed, verdict held", d["revision_changed"], ["repo", "file", "was", "now"])}
{section("File content changed (LFS sha)", d["content_changed"], ["repo", "file"])}
</section>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--html", action="store_true", help="print the drift section for the page")
    args = ap.parse_args()
    d = drift(load(args.old), load(args.new))
    if args.html:
        print(render_html(d))
        return 0
    d["computed_utc"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(json.dumps(d, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
