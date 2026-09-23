"""HTML compatibility table of the Ternary Check report (rendering only).

render(report) turns the trinity.ternary-check.v1 report into one
self-contained page: inline CSS, no scripts, no external assets, light and
dark themes, readable on a phone. Every number on the page is read from the
report; nothing is computed here but formatting.
"""
from __future__ import annotations
from html import escape

STYLE = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#5f5e58;--line:#dedbd2;--card:#ffffff;
--match:#1f7a4d;--match-bg:#e3f3ea;--mismatch:#a23b12;--mismatch-bg:#fbe9df;
--nr:#555a66;--nr-bg:#eceef2;--accent:#3b4cc0}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ecebe6;--muted:#a6a49b;--line:#34332f;
--card:#1c1c1a;--match:#6fd3a0;--match-bg:#16301f;--mismatch:#f3a27c;--mismatch-bg:#3a1f12;
--nr:#b8bdc8;--nr-bg:#262a31;--accent:#9aa6ff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,
"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:1180px;margin:0 auto;padding:28px 16px 48px}
h1{font-size:1.6rem;margin:0 0 4px}h2{font-size:1.2rem;margin:36px 0 8px}
p{margin:6px 0}.muted{color:var(--muted)}code{font:0.9em ui-monospace,SFMono-Regular,Menlo,monospace}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--card)}
table{border-collapse:collapse;width:100%;min-width:720px}
th,td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top;text-align:left}
thead th{font-size:0.82rem;color:var(--muted);font-weight:600;position:sticky;top:0;background:var(--card)}
tbody tr:last-child td,tbody tr:last-child th{border-bottom:0}
th.fmt{font-weight:600;min-width:190px}th.fmt small{display:block;color:var(--muted);font-weight:400}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:0.8rem;font-weight:600}
.match{color:var(--match);background:var(--match-bg)}
.mismatch{color:var(--mismatch);background:var(--mismatch-bg)}
.not-representable{color:var(--nr);background:var(--nr-bg)}
.prov{font-size:0.78rem;color:var(--muted);margin-left:6px}
.detail{font-size:0.84rem;margin-top:3px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:14px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.card b{display:block;font-size:1.35rem}
dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;margin:8px 0}
dt{font-weight:600}dd{margin:0}
"""

PROVENANCE_LABEL = {"reference": "reference, published", "published": "published",
                    "t27_round_trip": "t27 round trip", "upstream_encoder": "llama.cpp writer",
                    "not_written": "not written, decided by t27", "derived": "derived"}
REASON_LABEL = {"shape": "shape", "binary_only": "binary only (no code for 0)",
                "code_outside": "codes outside the format", "group_scales_differ": "group scales differ",
                "scale_precision": "scale precision"}


def _n(value) -> str:
    return f"{value:,}"


def _e(value) -> str:
    return escape(str(value), quote=True)


def _cell_html(cell: dict) -> str:
    status = cell["status"]
    parts = [f'<span class="badge {status}">{_e(status)}</span>'
             f'<span class="prov">{_e(PROVENANCE_LABEL[cell["provenance"]])}</span>']
    if status == "not-representable":
        reasons = "; ".join(f"{REASON_LABEL[r['reason']]}: {_n(r['count'])} of {_n(r['of'])} {r['unit']}"
                            if r["reason"] != "shape" else REASON_LABEL[r["reason"]] for r in cell["reasons"])
        parts.append(f'<div class="detail">{_e(reasons)}</div>')
        if cell.get("note"):
            parts.append(f'<div class="detail">{_e(cell["note"])}</div>')
    else:
        trits, scales = cell["trits"], cell["scales"]
        lines = []
        if trits["differ"]:
            lines.append(f"trits differ: {_n(trits['differ'])} from index {_n(trits['first'])} "
                         f"({trits['explanation']})")
        if scales.get("differ"):
            lines.append(f"{scales['dtype']} scales differ for {_n(scales['differ'])} weights from index "
                         f"{_n(scales['first'])} ({scales['explanation']})")
        if not lines:
            lines.append(f"trits and {scales['dtype']} scales identical")
        bits = f"{cell['bits_per_weight']:.4f} bits/weight"
        metadata = cell["metadata"]
        if metadata["container"] != "none":
            bits += (f"; {metadata['bits_per_weight']:.6f} with the {metadata['container']} metadata share "
                     f"({_n(metadata['bytes'])} bytes)")
        lines.append(bits)
        if cell.get("repro"):
            lines.append(f"repro: {cell['repro']}")
        parts.append("".join(f'<div class="detail">{_e(line)}</div>' for line in lines))
    return "".join(parts)


def _matrix(report: dict) -> str:
    tensors = report["tensors"]
    cells = {(c["tensor"], c["format"]): c for c in report["cells"]}
    head = "".join(f'<th>{_e(t["model"])}<br><code>{_e(t["names"].get("gguf", ""))}</code><br>'
                   f'{_n(t["shape"][0])} × {_n(t["shape"][1])}</th>' for t in tensors)
    rows = []
    for fmt in report["formats"]:
        writer = "" if fmt["writer"] == "t27" else f'<small>writer: {_e(fmt["writer"])}</small>'
        scale = f'{fmt["scale_dtype"]} scale per {fmt["scale_group"] or "tensor"}'
        tds = "".join(f"<td>{_cell_html(cells[(t['id'], fmt['id'])])}</td>" for t in tensors)
        rows.append(f'<tr><th class="fmt">{_e(fmt["name"])}<small>{_e(scale)}</small>{writer}</th>{tds}</tr>')
    return (f'<div class="wrap"><table><thead><tr><th>Format</th>{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def _triple(report: dict) -> str:
    rows = []
    for cell in report["derived"]:
        t = cell["triple_check"]
        ties = cell["ties"]
        pos, neg = ties["positive"], ties["negative"]
        rows.append(
            f"<tr><td><code>{_e(cell['tensor'])}</code></td>"
            f"<td>{_n(t['hf_packed_vs_i2_s']['differ'])}</td>"
            f"<td>{_n(t['bf16_absmean_vs_hf_packed']['differ'])} ({_e(t['bf16_absmean_vs_hf_packed']['explanation'])})</td>"
            f"<td>{_n(t['bf16_absmean_vs_i2_s']['differ'])}</td>"
            f"<td>{ties['tie_value']} (|w·s| = {ties['tie_times_s']:.5f}): "
            f"+t {_n(pos['packed_nonzero'])} ±1 / {_n(pos['packed_zero'])} 0, "
            f"−t {_n(neg['packed_nonzero'])} ±1 / {_n(neg['packed_zero'])} 0</td>"
            f"<td>{_e(cell.get('repro', ''))}</td></tr>")
    return ('<div class="wrap"><table><thead><tr><th>Tensor</th><th>packed vs I2_S (trits differ)</th>'
            '<th>bf16 absmean vs packed</th><th>bf16 absmean vs I2_S</th><th>Tie value t = 0.5 × weight_scale: '
            'packed trits at ±t</th><th>Reproduction</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def _matvec(report: dict) -> str:
    rows = []
    for tensor in report["matvec"]["tensors"]:
        names = tensor["tensor"].get("gguf", "")
        for comparison in tensor["comparisons"]:
            if "accumulator_rows" not in comparison:
                continue
            acc = comparison["accumulator_rows"]["differ"]
            partials = comparison["partials"]["differ"]
            rows.append(f"<tr><td>{_e(tensor['model'])} <code>{_e(names)}</code></td>"
                        f"<td>{_e(comparison['a'])} vs {_e(comparison['b'])}</td>"
                        f"<td>{_n(acc)} of {_n(tensor['tensor']['shape'][0])}</td><td>{_n(partials)}</td></tr>")
    return ('<div class="wrap"><table><thead><tr><th>Tensor</th><th>Forms compared</th>'
            '<th>Rows whose integer accumulators differ</th><th>Differing partial sums</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def render(report: dict) -> str:
    summary = report["summary"]
    status = summary["status"]
    cards = "".join(f'<div class="card"><b>{_n(value)}</b>{_e(label)}</div>' for label, value in (
        ("real tensors", summary["tensors"]), ("storage formats", summary["storage_formats"]),
        ("columns (TQ1_0, TQ2_0 by two writers)", summary["columns"]), ("cells", summary["cells"]),
        ("match", status.get("match", 0)), ("mismatch", status.get("mismatch", 0)),
        ("not representable", status.get("not-representable", 0))))
    taxonomy = report["taxonomy"]
    definitions = "".join(f"<dt>{_e(REASON_LABEL[token])}</dt><dd>{_e(entry['unit'])}</dd>"
                          for token, entry in taxonomy["reasons"].items())
    definitions += "".join(f"<dt>{_e(token)}</dt><dd>{_e(entry['text'])}</dd>"
                           for token, entry in taxonomy["explanations"].items())
    provenance = "".join(f"<dt>{_e(PROVENANCE_LABEL[token])}</dt><dd>{_e(text)}</dd>"
                         for token, text in taxonomy["provenance"].items())
    sources = []
    for tensor in report["tensors"]:
        for cell in report["cells"]:
            if cell["tensor"] == tensor["id"] and cell["provenance"] in ("reference", "published"):
                for src in cell["source"]:
                    sources.append(f"{src['repo']}@{src['revision'][:8]} {src['file']} bytes "
                                   f"{_n(src['range'][0])}–{_n(src['range'][1])}")
    for cell in report["derived"]:
        src = cell["source"]
        sources.append(f"{src['repo']}@{src['revision'][:8]} {src['file']} bytes "
                       f"{_n(src['range'][0])}–{_n(src['range'][1])}")
    upstream = report["upstream"]["llama.cpp"]
    sources.append(f"{upstream['repo']}@{upstream['commit'][:8]}: {', '.join(upstream['functions'])}")
    source_list = "".join(f"<li><code>{_e(s)}</code></li>" for s in dict.fromkeys(sources))
    families = summary["families"]
    evidence = "; ".join(f"{family}: {', '.join(entry['third_party_formats'])}"
                         for family, entry in families.items())
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>t27 Ternary Check</title>
<style>{STYLE}</style>
</head>
<body>
<main>
<h1>t27 Ternary Check</h1>
<p class="muted">Do the public ternary weight formats store the same trits and scales? Every cell below is decided by
executable t27 (<code>t27/matrix.t27</code> over the readers and writers of <code>t27/formats.t27</code>) on real
layer-0 tensors of BitNet b1.58 2B4T and Ternary Bonsai 2 27B, read by HTTP range requests from pinned revisions.
Report <code>reports/ternary-check.json</code> (schema <code>{_e(report['schema'])}</code>), compiler
<code>{_e(report['compiler']['repo'])}@{_e(report['compiler']['commit'][:8])}</code>, issue
<code>{_e(report['issue'])}</code>.</p>
<div class="cards">{cards}</div>
<h2>Compatibility matrix</h2>
<p class="muted">A cell is <b>match</b> when trits and every weight's scale are identical to the tensor's reference
(the chosen reference: HF packed for BitNet, PTQ1_0 for Bonsai), <b>mismatch</b> with the first differing index,
the count and an explanation, or <b>not representable</b> with the reasons. Third-party evidence: {_e(evidence)}.
t27 round trips show that a format can hold a tensor; they are not third-party evidence. A not-representable cell
is decided by t27 before anything is written, so no writer runs for it.</p>
{_matrix(report)}
<h2>BitNet triple check: bf16 master → absmean, packed, I2_S</h2>
<p class="muted">The packed checkpoint and the I2_S file hold the same trits. Ternarizing the published bf16 master
weights with transformers <code>WeightQuant</code> does not give them: every difference sits at the bf16 value
0.5 × weight_scale, where the packed file stores both 0 and ±1, so the packed trits cannot be recomputed from the
published bf16 weights.</p>
{_triple(report)}
<h2>Real layer: int8 matvec on decoded weights</h2>
<p class="muted">Integer accumulators <code>y = W·x</code> of the stored forms, and of the BitNet trits derived
from the bf16 master weights (<code>bf16_absmean</code>, not a stored form), with one int8 activation vector
(<code>t27/matvec.t27</code>, issue #33); the float step and its arithmetic are in the report's <code>matvec</code>
section.</p>
{_matvec(report)}
<h2>Definitions</h2>
<dl>{definitions}</dl>
<dl>{provenance}</dl>
<h2>Sources</h2>
<ul>{source_list}</ul>
<p class="muted">Reproduce from a clean clone: <code>make ternary-check</code> (<code>OFFLINE=1</code> uses the caches
only). Mismatches have minimal reproductions under <code>reports/ternary-check/repro/</code>. Storage and integer
arithmetic only: no model was run, and nothing here measures model quality or speed.</p>
</main>
</body>
</html>
"""
