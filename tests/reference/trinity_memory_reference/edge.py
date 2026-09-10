"""An end-to-end signal-template demo, with explicit software/RTL evidence."""

from __future__ import annotations

import hashlib
import html
import json
import math
from pathlib import Path
import platform
import time

from .bridge import BridgeClient, BridgeServer
from .codecs import pack
from .tensorpack import Tensor, decode_tensors, encode_tensors

LABELS = ("rising", "falling", "alternating")
TEMPLATES = (
    (-1,) * 6 + (1,) * 6,
    (1,) * 6 + (-1,) * 6,
    (-1, 1) * 6,
)


def signal_model(codec: str = "dense5") -> bytes:
    """Hand-authored templates, not a trained or quantized checkpoint."""
    return encode_tensors([Tensor(
        name="signal_templates", shape=(3, 12),
        values=tuple(v for row in TEMPLATES for v in row), codec=codec,
        scales=(1.0,), axes=("class", "sample"),
    )])


def fixture_cases() -> list[dict]:
    """Fixed illustrative signals; expected labels are hand assigned."""
    return [
        {"name": "step_up", "samples": [-40] * 6 + [40] * 6, "expected": "rising"},
        {"name": "step_down", "samples": [40] * 6 + [-40] * 6, "expected": "falling"},
        {"name": "alternating", "samples": [-30, 30] * 6, "expected": "alternating"},
        {"name": "offset_step_up", "samples": [-8, -7, -9, -8, -6, -8, 33, 34, 35, 34, 32, 35], "expected": "rising"},
        {"name": "offset_step_down", "samples": [36, 35, 33, 35, 34, 36, -8, -9, -7, -8, -8, -6], "expected": "falling"},
        {"name": "noisy_alternating", "samples": [-25, 29, -27, 31, -28, 30, -26, 28, -29, 32, -25, 31], "expected": "alternating"},
    ]


def classify(client: BridgeClient, handle: str, samples: list[int]) -> dict:
    """Evaluate a loaded template matrix through the network service."""
    result = client.dot(handle, "signal_templates", samples)
    raw = result["accumulators"]
    if len(raw) != len(LABELS):
        raise ValueError("signal model must have exactly three output rows")
    scales = result["scales"]
    if len(scales) == 1:
        scales = scales * len(raw)
    if len(scales) != len(raw):
        raise ValueError("signal model requires scalar or per-row scales")
    scores = [value * scale for value, scale in zip(raw, scales)]
    if any(not math.isfinite(score) for score in scores):
        raise ValueError("scaled classifier scores must be finite")
    best = max(range(len(scores)), key=scores.__getitem__)
    tied = sum(score == scores[best] for score in scores) > 1
    return {"label": None if tied else LABELS[best], "ambiguous": tied,
            "accumulators": raw, "scores": scores, "backend": result["backend"]}


def run_edge_demo(*, rtl: bool = False, seed: int = 27) -> dict:
    """Use real loopback HTTP; optionally verify returned weights in Icarus."""
    fixtures = fixture_cases()
    modes = []
    for codec in ("dense5", "baseline2"):
        blob = signal_model(codec)
        with BridgeServer() as server:
            client = BridgeClient(server.url)
            capabilities = client.capabilities()
            start = time.perf_counter_ns()
            handle = client.upload(blob)
            fetched = client.read(handle)
            transfer_ns = time.perf_counter_ns() - start
            if fetched != blob:
                raise AssertionError("host/server container round trip differs")
            tensor = decode_tensors(fetched)[0]
            cases = []
            for case in fixtures:
                start = time.perf_counter_ns()
                result = classify(client, handle, case["samples"])
                rpc_ns = time.perf_counter_ns() - start
                expected = [sum(w * x for w, x in zip(row, case["samples"])) for row in TEMPLATES]
                if result["accumulators"] != expected or result["label"] != case["expected"]:
                    raise AssertionError(f"demo mismatch: {codec}/{case['name']}")
                rtl_results = []
                if rtl:
                    from .rtl_compute import run_rtl_dot
                    for row in range(tensor.shape[0]):
                        weights = list(tensor.values[row * 12:(row + 1) * 12])
                        hardware_layout = "dense5" if codec == "dense5" else "baseline5"
                        witness = run_rtl_dot(weights, case["samples"], codec=hardware_layout, seed=seed + row)
                        if witness["result"] != expected[row]:
                            raise AssertionError("RTL differs from independent arithmetic reference")
                        rtl_results.append(witness)
                cases.append({**case, **result, "reference": expected, "rpc_wall_ns": rpc_ns,
                              "rtl": rtl_results, "passed": True})
            client.delete(handle)
        flat = list(tensor.values)
        modes.append({"codec": codec, "container_bytes": len(blob),
                      "raw_weight_payload_bytes": len(pack(flat, codec)),
                      "container_sha256": hashlib.sha256(blob).hexdigest(),
                      "roundtrip_exact": True, "upload_read_wall_ns": transfer_ns,
                      "capabilities": capabilities, "cases": cases})
    return {"schema": "trinity.edge-report.v1", "passed": True,
            "evidence": ["software-loopback-http"] + (["rtl-simulation"] if rtl else []),
            "physical_device_tested": False, "python": platform.python_version(), "seed": seed,
            "model": "hand-authored ternary template classifier, shape [3,12]",
            "labels": list(LABELS), "fixture_count": len(fixtures), "modes": modes,
            "limitations": ["Illustrative synthetic fixtures; no measured generalization accuracy.",
                            "HTTP wall times include Python, JSON and scheduling overhead.",
                            "RTL cycles are simulation evidence, not FPGA throughput or power.",
                            "No physical board, DDR controller, trained checkpoint or ZK proof."]}


def render_edge_report(report: dict, path: Path) -> None:
    """Render measured data only; no remote script or CDN dependency."""
    rows = []
    for mode in report["modes"]:
        for case in mode["cases"]:
            cycles = ", ".join(str(r["cycles"]) for r in case["rtl"]) or "not run"
            rows.append("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in (
                mode["codec"], case["name"], case["expected"], case["label"],
                case["accumulators"], cycles, "PASS" if case["passed"] else "FAIL")) + "</tr>")
    sizes = "".join(f"<tr><td>{html.escape(m['codec'])}</td><td>{m['raw_weight_payload_bytes']}</td>"
                    f"<td>{m['container_bytes']}</td><td>{m['upload_read_wall_ns'] / 1e6:.3f}</td></tr>"
                    for m in report["modes"])
    notes = "".join(f"<li>{html.escape(note)}</li>" for note in report["limitations"])
    data = html.escape(json.dumps(report, indent=2))
    badge = "Software + RTL simulation" if "rtl-simulation" in report["evidence"] else "Software demonstrator · RTL not run"
    page = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Trinity Memory · five directions</title>
<style>body{margin:0;background:#0d1424;color:#e6edf7;font:16px/1.6 system-ui}main{max-width:1100px;margin:auto;padding:48px 24px}h1{font-size:42px;line-height:1.15}h2{margin-top:36px}.eyebrow{color:#72dcc0;letter-spacing:.12em}.chain{padding:24px;background:#172338;border:1px solid #32455f;border-radius:12px;font-size:19px}.badge{display:inline-block;background:#184b40;border:1px solid #458b75;padding:4px 12px;border-radius:20px}.muted{color:#afc0d6}table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;border-bottom:1px solid #32455f;padding:12px}.scroll{overflow:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}a{color:#72dcc0}details{margin-top:32px}code{color:#9ee0ef}</style>
<main><div class="eyebrow">TRINITY MEMORY · V0.2</div><h1>From a tensor file<br>to a checked computation.</h1>
<p><span class="badge">""" + badge + """</span></p>
<p class="chain">TensorPack → Bridge / HTTP → emulated memory → Stream Compute → Edge Demo<br><small>Conformance Lab checks each boundary.</small></p>
<p class="muted">A hand-authored ternary signal classifier. Every result below is compared against an independent integer reference. Physical hardware has not been tested.</p>
<h2>Storage and transfer</h2><p>Raw payload counts the 36 trits only. TensorPack includes names, dimensions, scales, headers and checksums. The tiny fixture illustrates metadata overhead.</p>
<div class="scroll"><table><thead><tr><th>Codec</th><th>Payload bytes</th><th>Container bytes</th><th>HTTP upload + read (ms)</th></tr></thead><tbody>""" + sizes + """</tbody></table></div>
<h2>End-to-end checks</h2><p>RTL cycles are per output row and include the configured testbench stalls.</p><div class="scroll"><table><thead><tr><th>Codec</th><th>Signal</th><th>Expected</th><th>Predicted</th><th>Integer scores</th><th>RTL cycles</th><th>Check</th></tr></thead><tbody>""" + "".join(rows) + """</tbody></table></div><h2>Evidence boundaries</h2><ul>""" + notes + """</ul>
<p><a href="https://github.com/dmitrii-f-t27/trinity-memory">Source and reproduction instructions</a></p><details><summary>Inspect the complete report JSON</summary><pre>""" + data + "</pre></details></main></html>"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")
