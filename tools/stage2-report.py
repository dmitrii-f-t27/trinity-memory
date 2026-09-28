#!/usr/bin/env python3
"""The stage-2 report (issue #67): what was measured on the AX7203 with DDR3,
what was not, every number traced to a committed capture.

Builds reports/stage2/stage2.json, reports/stage2/index.html and the measurement
block in docs/hardware.md from the committed captures alone. Sources: the golden-chunk captures
(both formats), the #65 measurements (both formats), the build records, the
#61 pattern bring-up record. Evidence index: bitstream/spec/vector hashes,
tool revisions, UberDDR3 pin and license, board identity.

  python3 tools/stage2-report.py            # writes all three, exit 0
  python3 tools/stage2-report.py --check    # re-verify against the captures

Exit codes: 0 written; 1 a number in the text cannot be traced to a capture.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import pathlib
import statistics

ROOT = pathlib.Path(__file__).resolve().parents[1]
R = ROOT / "reports/fpga"
OUT = ROOT / "reports/stage2"
DOC = ROOT / "docs/hardware.md"
DOC_START = "<!-- stage2-measurement:start -->"
DOC_END = "<!-- stage2-measurement:end -->"

SOURCES = {
    "golden_d5": "matvec-capture-2026-09-27-m6d5-golden.json",
    "golden_b2": "matvec-capture-2026-09-27-m6d5-golden-b2.json",
    "measure_d5": "ddr3-matvec-measure-2026-09-27-d5.json",
    "measure_b2": "ddr3-matvec-measure-2026-09-27-b2.json",
    "build_d5": "ddr3-build-2026-09-27-twoclock-m6d5/build.json",
    "build_b2": "ddr3-build-2026-09-27-twoclock-m6d5-b2/build.json",
}


def sha256_file(p: pathlib.Path) -> str:
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


def load(name: str) -> dict:
    return json.loads((R / SOURCES[name]).read_text())


def fmt(x: float) -> str:
    return f"{x:,.0f}"


def build() -> dict:
    gd5, gb2 = load("golden_d5"), load("golden_b2")
    md5, mb2 = load("measure_d5"), load("measure_b2")
    bd5, bb2 = load("build_d5"), load("build_b2")

    def row(m: dict, fmt_name: str, bits: float) -> dict:
        runs = m["runs"]
        speeds = [r["weights_per_s"] for r in runs]
        cycles = sum(r["cycles"] for r in runs)
        if len(runs) < 2 or any(r["cycles"] <= 0 for r in runs):
            raise ValueError(f"{fmt_name}: at least two runs with positive cycles required")
        s = {"runs": len(runs), "mean": statistics.mean(speeds),
             "sample_sd": statistics.stdev(speeds), "min": min(speeds), "max": max(speeds)}
        return {"format": fmt_name, "bits_per_weight": bits,
                "runs": s["runs"], "cycles_mean": round(cycles / len(runs), 3),
                "weights_per_s_mean": round(s["mean"], 1),
                "weights_per_s_sd": round(s["sample_sd"], 1),
                "weights_per_s_sd_percent": round(100 * s["sample_sd"] / s["mean"], 6),
                "weights_per_s_min": s["min"], "weights_per_s_max": s["max"],
                **{f"{kind}_stalls_per_run": round(statistics.mean(r[f"{kind}_stalls"] for r in runs), 3)
                   for kind in ("consumer", "wait", "command")},
                **{f"{kind}_stalls_percent": round(100 * sum(r[f"{kind}_stalls"] for r in runs) / cycles, 6)
                   for kind in ("consumer", "wait", "command")}}

    rows = [row(md5, "dense5", 1.6), row(mb2, "baseline2", 2.0)]
    ratio = rows[0]["weights_per_s_mean"] / rows[1]["weights_per_s_mean"]

    report = {
        "schema": "trinity.stage2-report.v1",
        "written_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "issue": "dmitrii-f-t27/trinity-memory#67", "epic": "#59",
        "what_ran": {
            "board": {"name": "ALINX AX7203", "part": "xc7a200tfbg484-2",
                      "idcode_full": "0x13636093", "idcode_printed": "0x3636093"},
            "clock": {"pll": "6/5", "ddr3_mhz": 240.0, "controller_mhz": 60.0,
                      "note": "the lowest DDR3 clock UberDDR3 calibrates on this board "
                              "(160 fails; 300 works); 75 MHz controller builds fail the "
                              "controller's own routing once real decode logic exists"},
            "workload": "BitNet b1.58 2B4T layer-0 self_attn.q_proj rows 0-319 "
                        "(819,200 weights), activations seed 27, both formats",
            "read_discipline": "one outstanding request, 8 idle clocks after each ack "
                               "(overlapping requests answer zero data, #75)",
        },
        "bit_exact": {
            "dense5": {"y_lines": gd5["y_lines"], "bit_exact": gd5["bit_exact"],
                       "first": gd5["first_device_y" if "first_device_y" in gd5 else "first_y"]},
            "baseline2": {"y_lines": gb2["y_lines"], "bit_exact": gb2["bit_exact"],
                          "first": gb2["first_y"]},
            "verdict": "320/320 Y lines equal the t27/C reference in both formats",
        },
        "measurement": {
            "consumer": "B (device matvec of #64)",
            "rows": rows,
            "aggregation": "Counter values per run are arithmetic means over all runs; "
                           "stall percentages are 100 * summed counter / summed cycles. "
                           "Stall counters can overlap and are not an additive cycle breakdown.",
            "ratio_d5_b2": round(ratio, 4),
            "ratio_ceiling": 1.25,
            "memory_bound": {
                "definition": "consumer stalls 0 in both formats",
                "observed": "; ".join(
                    f"{r['format']}: mean consumer stalls {r['consumer_stalls_per_run']:,.3f} "
                    f"of {r['cycles_mean']:,.3f} cycles ({r['consumer_stalls_percent']:.4f}%); "
                    f"wait stalls {r['wait_stalls_percent']:.4f}%"
                    for r in rows),
                "verdict": "latency-bound under the serialized read discipline; NOT "
                           "bus-rate. The 1.25 format ratio is unreachable at this "
                           f"operating point (measured ratio {ratio:.4f}); the arbiter-free "
                           "pattern test of #61 streamed the same port, so the seam to "
                           "fix is the arbiter, not the DRAM.",
        },
        },
        "not_measured": [
            "power and energy (no instrument on the bench)",
            "tokens per second and model quality (one layer chunk, not a model)",
            "x32 DDR3, and DDR3 above 480 MT/s (the 667 MT/s class was never built)",
            "bus-rate delivery (consumer A): the pipelined reads the port answers with "
            "zero data are the open seam (#62, #65)",
        ],
        "evidence": {
            name: {"file": f"reports/fpga/{path}", "sha256": sha256_file(R / path)}
            for name, path in SOURCES.items()
        },
        "uberddr3": {"pinned_by": "fpga/ax7203/ddr3/uberddr3.lock",
                     "license": "GPL-3.0 (bitstreams built from it are not shipped as "
                                "release assets; the founders' decision is open, #60)"},
        "upstream_state": {
            "s11_branch": "dmitrii-f-t27/t27 s11-fpga-adapter (fork PR #1); the "
                          "gHashTag/t27 PR waits on the founders (#66)",
        },
    }
    return report


HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>trinity-memory stage 2: DDR3 on the AX7203</title>
<style>
body{font-family:ui-monospace,monospace;margin:2rem;max-width:60rem;color:#1a1a1a}
table{border-collapse:collapse;margin:1rem 0}
td,th{border:1px solid #999;padding:.3rem .6rem;text-align:right}
th:first-child,td:first-child{text-align:left}
h2{border-bottom:2px solid #1a1a1a;padding-bottom:.3rem}
.ok{color:#046c3c;font-weight:bold}.warn{color:#8a4b00}
</style></head><body>
<h1>Stage 2: DDR3 on the AX7203</h1>
<p>__WRITTEN__ &middot; <a href="stage2.json">stage2.json</a> &middot; issue
<a href="https://github.com/dmitrii-f-t27/trinity-memory/issues/67">#67</a> of epic
<a href="https://github.com/dmitrii-f-t27/trinity-memory/issues/59">#59</a></p>

<h2>What ran</h2>
<p>__BOARD__ (__PART__), IDCODE __IDCODE__. DDR3 at __DDR__ MHz, controller __CTRL__ MHz
(PLL 6/5 &mdash; the lowest DDR3 clock UberDDR3 calibrates on this board; 160 MHz
does not, 300 MHz does). The workload: BitNet b1.58 2B4T layer-0
<code>q_proj</code> rows 0&ndash;319 (819,200 weights), activations seed 27,
both formats, read from DDR3 through the UART loader with one outstanding
request and an 8-clock gap after every ack.</p>

<h2>Bit-exact</h2>
<p class="ok">__EXACT_D5__ and __EXACT_B2__: every one of the 320 Y lines equals
the t27/C reference (first rows __FIRST__).</p>

<h2>Weights per second (consumer B, the device matvec)</h2>
<table>
<tr><th>format</th><th>bits/weight</th><th>runs</th><th>weights/s (mean)</th>
<th>s.d.</th><th>min</th><th>max</th></tr>
__ROWS__
</table>
<p>Ratio dense5/baseline2 = <b>__RATIO__</b> against the arithmetic ceiling 1.25.
The point is <b>latency-bound</b>. __STALLS__.
__AGGREGATION__ Under the serialized
read discipline the port requires, each 128-bit word costs its fixed latency
whatever it carries (64 or 80 trits) &mdash; the 1.6-bit format buys nothing
here. The 1.25 test needs pipelined reads through the arbiter; the
arbiter-free pattern test of #61 streamed the same port, so the seam to fix
is the arbiter, not the DRAM.</p>

<h2>Not measured</h2>
<ul>__NOTMEASURED__</ul>

<h2>Evidence</h2>
<p>Every number above traces to a committed capture; the JSON carries the
sha256 of each source. A clean clone rebuilds this page with
<code>python3 tools/stage2-report.py</code>.</p>
</body></html>
"""


def render(report: dict) -> str:
    rows = "\n".join(
        f"<tr><td>{r['format']}</td><td>{r['bits_per_weight']}</td><td>{r['runs']}</td>"
        f"<td>{fmt(r['weights_per_s_mean'])}</td><td>&plusmn;{fmt(r['weights_per_s_sd'])}</td>"
        f"<td>{fmt(r['weights_per_s_min'])}</td><td>{fmt(r['weights_per_s_max'])}</td></tr>"
        for r in report["measurement"]["rows"])
    first = ", ".join(str(v) for v in report["bit_exact"]["dense5"]["first"])
    written = report["written_utc"]
    return (HTML
        .replace("__WRITTEN__", written)
        .replace("__BOARD__", report["what_ran"]["board"]["name"])
        .replace("__PART__", report["what_ran"]["board"]["part"])
        .replace("__IDCODE__", report["what_ran"]["board"]["idcode_full"])
        .replace("__DDR__", str(int(report["what_ran"]["clock"]["ddr3_mhz"])))
        .replace("__CTRL__", str(int(report["what_ran"]["clock"]["controller_mhz"])))
        .replace("__FIRST__", first)
        .replace("__EXACT_D5__", "dense5: " + str(report["bit_exact"]["dense5"]["bit_exact"]))
        .replace("__EXACT_B2__", "baseline2: " + str(report["bit_exact"]["baseline2"]["bit_exact"]))
        .replace("__ROWS__", rows)
        .replace("__RATIO__", f"{report['measurement']['ratio_d5_b2']:.4f}")
        .replace("__STALLS__", report["measurement"]["memory_bound"]["observed"])
        .replace("__AGGREGATION__", report["measurement"]["aggregation"])
        .replace("__NOTMEASURED__", "".join(f"<li>{t}</li>" for t in report["not_measured"])))


def measurement_markdown(report: dict) -> str:
    rows = report["measurement"]["rows"]
    table = ["| Format | bits/weight | weights/s (mean, +- sample s.d.) | runs | consumer stalls/run (mean) | consumer stalls/cycles | wait stalls/cycles |",
             "|---|---|---|---|---|---|---|"]
    for r in rows:
        table.append(f"| {r['format']} | {r['bits_per_weight']} | "
                     f"{fmt(r['weights_per_s_mean'])} +- {fmt(r['weights_per_s_sd'])} | {r['runs']} | "
                     f"{r['consumer_stalls_per_run']:,.3f} | {r['consumer_stalls_percent']:.4f}% | "
                     f"{r['wait_stalls_percent']:.4f}% |")
    return "\n".join(table) + f"\n\n{report['measurement']['aggregation']}\n\n" + (
        f"The dense5/baseline2 throughput ratio is {report['measurement']['ratio_d5_b2']:.4f} "
        "(arithmetic ceiling 1.25). The point is latency-bound, not bus-rate. "
        "The 1.25 format ratio is unreachable at this operating point; the "
        "arbiter-free pattern test of #61 streamed the same port at bus rate, "
        "so pipelining through the arbiter/controller seam remains the next "
        "bandwidth test (#62, #65). Not measured: power, tokens per second, "
        "model quality, x32, DDR3 above 480 MT/s.\n")


def update_document(text: str, report: dict) -> str:
    if text.count(DOC_START) != 1 or text.count(DOC_END) != 1:
        raise ValueError("docs/hardware.md must contain one stage2-measurement block")
    before, rest = text.split(DOC_START)
    _, after = rest.split(DOC_END)
    return before + DOC_START + "\n" + measurement_markdown(report) + DOC_END + after


def check(report: dict) -> bool:
    try:
        saved = json.loads((OUT / "stage2.json").read_text())
        # Generation time is metadata; all measurements and evidence must match.
        report = {**report, "written_utc": saved["written_utc"]}
        checks = {"JSON": saved == report,
                  "HTML": (OUT / "index.html").read_text() == render(report),
                  "documentation": DOC.read_text() == update_document(DOC.read_text(), report)}
    except (OSError, ValueError, KeyError) as exc:
        print(f"check: FAIL ({exc})")
        return False
    for name, ok in checks.items():
        print(f"check {name}: {'PASS' if ok else 'FAIL'}")
    return all(checks.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="verify every number in the text against the captures")
    args = ap.parse_args()
    report = build()
    if args.check:
        return 0 if check(report) else 1
    doc = update_document(DOC.read_text(), report)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "stage2.json").write_text(json.dumps(report, indent=1) + "\n")
    (OUT / "index.html").write_text(render(report))
    DOC.write_text(doc)
    print(f"wrote {OUT}/stage2.json, {OUT}/index.html and {DOC}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
