#!/usr/bin/env python3
"""Render a portable, dependency-free benchmark report from measured JSON."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def validate_report(data: object) -> dict:
    """Reject malformed input rather than display invented or incomplete results."""
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("Expected a benchmark object with schema_version=1")
    if not isinstance(data.get("datasets"), list) or not data["datasets"]:
        raise ValueError("The benchmark must contain at least one measured dataset")
    for dataset in data["datasets"]:
        if not isinstance(dataset, dict) or not isinstance(dataset.get("name"), str):
            raise ValueError("Each dataset needs a name")
        for field in ("count", "zero_fraction", "entropy_bpw"):
            _number(dataset, field)
        if not 0 <= dataset["zero_fraction"] <= 1:
            raise ValueError("zero_fraction must be in [0,1]")
        if not isinstance(dataset.get("results"), list) or not dataset["results"]:
            raise ValueError(f"Dataset {dataset['name']} has no measured results")
        for result in dataset["results"]:
            if not isinstance(result, dict) or not isinstance(result.get("codec"), str):
                raise ValueError("Each result needs a codec name")
            for field in (
                "group_size", "group_bits", "payload_bytes", "container_bytes",
                "payload_bpw", "container_bpw", "encode_ms", "decode_ms",
                "ideal_payload_ratio_vs_baseline",
            ):
                _number(result, field)
            if not isinstance(result.get("roundtrip"), bool):
                raise ValueError("roundtrip must be a boolean")
    return data


def _number(obj: dict, key: str) -> None:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Missing or invalid numeric field: {key}")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"Field {key} must be finite and nonnegative")


def render_report(data: dict) -> str:
    """Safely embed data, including user-controlled strings, in a standalone page."""
    payload = json.dumps(validate_report(data), ensure_ascii=False, allow_nan=False)
    payload = payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(
        ">", "\\u003e"
    ).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return TEMPLATE.replace("__BENCHMARK_DATA__", payload)


TEMPLATE = r'''<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>TRINITY · Ternary Memory Lab</title>
  <style>
    :root{--paper:#f5f4ed;--card:#fffef8;--ink:#192b29;--muted:#5b6c65;--line:#d8ddd3;--accent:#c3e65d;--green:#27594c;--orange:#c16b32;--red:#a53631;--mono:ui-monospace,SFMono-Regular,Consolas,monospace}
    *{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}a{color:inherit;text-underline-offset:4px}a:hover{color:var(--green)}button,select{font:inherit}button{cursor:pointer}button:focus-visible,select:focus-visible,a:focus-visible{outline:3px solid var(--orange);outline-offset:4px}.wrap{max-width:1260px;margin:auto;padding:0 40px}.top{display:flex;align-items:center;justify-content:space-between;gap:20px;border-bottom:1px solid var(--line);padding:24px 0}.brand{font-weight:800;letter-spacing:.1em;font-size:17px;display:flex;align-items:center;gap:12px}.triangle{width:20px;height:20px;background:var(--green);clip-path:polygon(50% 0,100% 100%,0 100%)}.top nav{display:flex;gap:24px;font-size:13px}.eyebrow{font:11px/1.5 var(--mono);text-transform:uppercase;letter-spacing:.1em}.hero{display:grid;grid-template-columns:1.45fr 1fr;gap:64px;padding:62px 0 44px;align-items:center}.hero h1{font-size:clamp(35px,4.8vw,62px);font-weight:560;line-height:1.06;letter-spacing:-.055em;margin:19px 0 23px}.hero p{max-width:570px;color:var(--muted);font-size:17px;margin:0}.pill{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:30px;padding:7px 11px;font:11px var(--mono);background:var(--card)}.dot{width:6px;height:6px;border-radius:50%;background:var(--green)}.hero-graphic{background:var(--ink);color:var(--paper);border-radius:4px;padding:32px;min-height:244px;position:relative;overflow:hidden}.hero-graphic .eyebrow{color:#bacbbd}.ternary{display:flex;gap:10px;padding:22px 0}.ternary span{flex:1;border:1px solid #59736a;display:grid;place-items:center;aspect-ratio:1.12;font:clamp(32px,5vw,54px) var(--mono);border-radius:3px}.ternary span:nth-child(2){background:var(--accent);color:var(--ink);border-color:var(--accent)}.graphic-foot{display:flex;justify-content:space-between;gap:12px;font:11px var(--mono);color:#c7d0c8}.metrics{display:grid;grid-template-columns:repeat(3,1fr);border:1px solid var(--line);background:var(--card);margin:0 0 44px;border-radius:4px}.metric{padding:22px 24px;min-width:0}.metric+.metric{border-left:1px solid var(--line)}.metric-value{font:32px/1.1 var(--mono);letter-spacing:-.06em;margin:10px 0}.metric-detail{font-size:12px;color:var(--muted)}.section-head{display:flex;justify-content:space-between;gap:20px;align-items:flex-end;margin:0 0 20px}h2{font-size:26px;letter-spacing:-.03em;font-weight:550;margin:4px 0 0}h3{font-size:17px;margin:0 0 12px;font-weight:600}.section-head p{font-size:13px;color:var(--muted);margin:6px 0 0}.tabs{display:flex;gap:5px;flex-wrap:wrap}.tabs button{border:1px solid var(--line);border-radius:3px;background:transparent;padding:9px 15px;font:12px var(--mono);color:var(--muted)}.tabs button[aria-pressed=true]{background:var(--ink);border-color:var(--ink);color:var(--paper)}.panel{border:1px solid var(--line);border-radius:4px;background:var(--card);padding:26px}.measurement{display:grid;grid-template-columns:1.6fr 1fr;gap:20px}.chart-title{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:24px}.chart-title span{font:11px var(--mono);color:var(--muted)}.chart{display:grid;gap:15px}.bar-row{display:grid;grid-template-columns:80px 1fr 75px;gap:13px;align-items:center;font:12px var(--mono)}.bar-track{height:27px;background:#eef0e8;border-radius:2px;overflow:hidden}.bar-fill{height:100%;background:var(--green);width:0;transition:width .25s ease}.bar-fill.baseline{background:#aab5a6}.bar-fill.best{background:var(--accent)}.bar-value{text-align:right}.chart-note{font-size:12px;color:var(--muted);margin:21px 0 0}.dataset-card{background:#e9eddd;display:flex;flex-direction:column;justify-content:space-between}.dataset-card dl{margin:13px 0}.dataset-card dl>div{display:flex;justify-content:space-between;gap:15px;padding:11px 0;border-bottom:1px solid #cfd7c3;font-size:13px}.dataset-card dd{font:13px var(--mono);margin:0;text-align:right}.dataset-card p{font-size:12px;color:#455748;margin:8px 0 0}.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:4px;margin-top:20px;background:var(--card)}table{width:100%;border-collapse:collapse;white-space:nowrap}th{font:10px/1.6 var(--mono);color:var(--muted);text-transform:uppercase;letter-spacing:.04em;text-align:right;background:#eef0e7;padding:16px 17px;border-bottom:1px solid var(--line)}th:first-child,td:first-child{text-align:left}td{font:12px var(--mono);text-align:right;padding:15px 17px;border-bottom:1px solid #e8eadf}tbody tr:last-child td{border:0}td:first-child{font-weight:600}.ok{color:var(--green)}.fail{color:var(--red);font-weight:700}.table-caption{font-size:12px;color:var(--muted);margin:12px 0 43px}.experiment{margin-top:24px;padding:30px;background:var(--ink);border-radius:4px;color:var(--paper);display:grid;grid-template-columns:1.25fr 1fr;gap:50px}.experiment .eyebrow{color:var(--accent)}.experiment h2{margin:10px 0 12px}.experiment p{font-size:13px;color:#c7d0c8}.trits{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin:24px 0 18px}.trit label{display:block;color:#b7c6b9;font:10px var(--mono);margin-bottom:7px}.trit select{width:100%;min-width:0;border:1px solid #677e70;border-radius:3px;color:var(--paper);background:#283e35;padding:12px 8px;font:22px var(--mono)}.trit small{display:block;text-align:center;font:10px var(--mono);margin-top:8px;color:#c8d2c6}.formula{font:12px/1.8 var(--mono);overflow-wrap:anywhere;min-height:43px}.byte-card{align-self:center;background:var(--accent);color:var(--ink);padding:24px;border-radius:3px}.byte-top{display:flex;justify-content:space-between;align-items:center;gap:16px}.byte-value{font:76px/1.2 var(--mono);letter-spacing:-.06em}.byte-hex{font:18px var(--mono)}.binary{display:grid;grid-template-columns:repeat(8,1fr);gap:5px;margin:15px 0}.binary span{text-align:center;padding:7px 0;background:#b2d250;border-radius:2px;font:18px var(--mono)}.byte-card p{color:#344a2d;font-size:12px;margin:12px 0 0}.byte-card code{font-family:var(--mono);font-size:11px}.note-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;margin:32px 0 40px}.note{border-top:2px solid var(--ink);padding-top:17px}.note h3{font-size:15px}.note p{font-size:13px;color:var(--muted);margin:0}.tag{font:10px var(--mono);letter-spacing:.06em;display:block;margin-bottom:10px;color:var(--green)}footer{padding:23px 0 35px;border-top:1px solid var(--line);display:flex;justify-content:space-between;gap:25px;font:11px/1.8 var(--mono);color:var(--muted)}footer .meta{max-width:75%;overflow-wrap:anywhere}.foot-title{font-weight:700;color:var(--ink)}.empty{padding:30px;background:var(--card);border:1px solid var(--line)}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
    @media(max-width:900px){.wrap{padding:0 24px}.hero{gap:28px;padding-top:40px}.hero-graphic{padding:24px;min-height:0}.section-head{align-items:flex-start;flex-direction:column}.measurement{grid-template-columns:1fr 1fr}.experiment{gap:25px;padding:24px}.trit select{font-size:18px;padding:10px 5px}.note-grid{gap:24px}.metric{padding:20px}.metric-value{font-size:27px}}
    @media(max-width:660px){.wrap{padding:0 18px}.top{padding:18px 0}.top nav{gap:14px;font-size:11px}.brand{font-size:13px;gap:7px}.triangle{width:16px;height:16px}.hero{grid-template-columns:1fr;padding:36px 0 24px;gap:24px}.hero h1{font-size:44px}.hero p{font-size:15px}.hero-graphic{display:none}.metrics{grid-template-columns:1fr;margin-bottom:33px}.metric{padding:17px 20px;display:grid;grid-template-columns:1fr auto;gap:4px 12px;align-items:center}.metric+.metric{border-left:0;border-top:1px solid var(--line)}.metric-value{grid-column:2;grid-row:1/3;font-size:29px;margin:0}.metric-detail{font-size:11px}.measurement,.experiment,.note-grid{grid-template-columns:1fr}.panel{padding:20px}.bar-row{grid-template-columns:67px 1fr 63px;gap:9px;font-size:11px}.chart-title{margin-bottom:18px}.chart-title span{font-size:10px}.dataset-card dl>div{padding:8px 0}.experiment{padding:24px 20px;gap:22px}.trits{gap:7px}.byte-card{padding:20px}.byte-value{font-size:63px}.note-grid{gap:25px}.table-caption{margin-bottom:30px}footer{flex-direction:column;gap:8px}footer .meta{max-width:100%}}
    @media(prefers-reduced-motion:reduce){.bar-fill{transition:none}}
    @media print{body{background:white}.wrap{max-width:none;padding:0}.top nav,.tabs,.trits{display:none}.hero{padding:24px 0}.hero h1{font-size:38px}.hero-graphic{display:none}.hero{grid-template-columns:1fr}.measurement{grid-template-columns:1.5fr 1fr}.panel,.table-wrap,.experiment,.note{break-inside:avoid}.experiment{color:black;background:#eee}.experiment p{color:#333}.experiment .eyebrow{color:#333}.formula{color:black}table{white-space:normal}th,td{padding:9px 6px}.note-grid{gap:18px}footer{padding-bottom:0}}
  </style>
</head>
<body>
<div class="wrap">
  <header class="top"><div class="brand"><span class="triangle" aria-hidden="true"></span>TRINITY <span style="font-weight:400;letter-spacing:0;color:var(--muted)">/ memory lab</span></div><nav aria-label="Документация"><a href="../README.md">README ↗</a><a href="../docs/research.md">Research ↗</a><a href="../docs/hardware.md">RTL ↗</a></nav></header>
  <main>
    <section class="hero" aria-labelledby="title">
      <div><span class="pill"><span class="dot" aria-hidden="true"></span>PYTHON BENCHMARK · LOCAL ARTIFACT</span><h1 id="title">Троичная память.<br>Измеряем в битах.</h1><p>Упаковка весов −1, 0, +1 в двоичную память: размер данных, обратимость и время кодирования. Результаты одного воспроизводимого запуска.</p></div>
      <div class="hero-graphic" aria-hidden="true"><div class="eyebrow">Three values · one representation</div><div class="ternary"><span>−1</span><span>0</span><span>+1</span></div><div class="graphic-foot"><span>log₂(3) ≈ 1.585 bit / trit</span><span>DENSE LIMIT</span></div></div>
    </section>
    <div class="metrics" aria-label="Показатели выбранного набора">
      <div class="metric"><div class="eyebrow">Минимальный payload</div><div class="metric-value" id="best-bpw">—</div><div class="metric-detail" id="best-detail">bits / weight · выбранный набор</div></div>
      <div class="metric"><div class="eyebrow">Энтропия одного веса</div><div class="metric-value" id="entropy">—</div><div class="metric-detail">Эмпирическая H₁ = −Σ p · log₂(p)</div></div>
      <div class="metric"><div class="eyebrow">Обратимость</div><div class="metric-value" id="roundtrip">—</div><div class="metric-detail">decode(encode(weights)) = weights</div></div>
    </div>
    <section aria-labelledby="benchmark-title">
      <div class="section-head"><div><div class="eyebrow">01 / Measured benchmark</div><h2 id="benchmark-title">Сколько памяти занимает набор</h2><p>Выберите распределение весов. Короткая полоса означает меньший payload.</p></div><div class="tabs" id="datasets" aria-label="Выбор набора данных"></div></div>
      <div class="measurement">
        <div class="panel"><div class="chart-title"><h3 style="margin:0">Payload</h3><span>bit / weight ↓</span></div><div class="chart" id="chart" role="img" aria-label="Размер данных по кодекам"></div><p class="chart-note">Размер payload рассчитан из фактической длины байтового потока: bytes × 8 / weights. Заголовок контейнера учтён отдельно.</p></div>
        <aside class="panel dataset-card" aria-labelledby="dataset-title"><div><div class="eyebrow">Dataset profile</div><h3 id="dataset-title" style="margin-top:9px">—</h3><dl><div><dt>Весов</dt><dd id="count">—</dd></div><div><dt>Доля нулей</dt><dd id="zeros">—</dd></div><div><dt>Seed</dt><dd id="seed">—</dd></div><div><dt>Повторов замера</dt><dd id="repeats">—</dd></div></dl></div><p id="dataset-note"></p></aside>
      </div>
      <div class="table-wrap"><table><caption class="sr-only">Измеренные размеры и время работы кодеков</caption><thead><tr><th scope="col">Codec<br>group → bits</th><th scope="col">Payload<br>bytes</th><th scope="col">Container<br>bytes</th><th scope="col">Container<br>bit / weight</th><th scope="col">Encode<br>ms</th><th scope="col">Decode<br>ms</th><th scope="col">Payload ratio¹<br>vs baseline</th><th scope="col">Roundtrip</th></tr></thead><tbody id="results"></tbody></table></div>
      <p class="table-caption">¹ Payload ratio = фактические bytes baseline2 / bytes кодека, включая padding и исключая заголовки. При неизменной пропускной способности это расчётный предел ускорения передачи payload; он не измеряет FPGA. Время Encode / Decode относится к Python на указанной машине.</p>
    </section>
    <section class="experiment" aria-labelledby="encoder-title">
      <div><div class="eyebrow">02 / Interactive codec</div><h2 id="encoder-title">Пять тритов → один байт</h2><p>Каждый вес t превращается в цифру d = t + 1. Первый трит занимает младший разряд: byte = Σ dᵢ × 3ⁱ.</p><div class="trits" id="trits"></div><div class="formula" id="formula" aria-live="polite"></div></div>
      <div class="byte-card"><div class="eyebrow" style="color:var(--ink)">Encoded byte · 0…242</div><div class="byte-top"><output class="byte-value" id="byte-decimal" aria-label="Байт в десятичной форме">—</output><span class="byte-hex" id="byte-hex">—</span></div><div class="binary" id="binary" aria-label="Байт в двоичной форме"></div><code id="rtl-lanes"></code><p>RTL lane: 00 = 0, 01 = +1, 10 = −1; 11 недопустимо. Это представление трита на выходе RTL-декодера. Байты 243…255 не кодируют пять тритов.</p></div>
    </section>
    <section class="note-grid" aria-label="Границы выводов">
      <div class="note"><span class="tag">MEASURED / SOFTWARE</span><h3>Байты и Python timing</h3><p id="scope"></p></div>
      <div class="note"><span class="tag">CALCULATED / MODEL</span><h3>Теоретическая плотность</h3><p>Для равновероятных независимых тритов предел — log₂(3) ≈ 1.585 bit / weight. Плотность фиксированной группы — group_bits / group_size. Эмпирическая H₁ описывает частоты одного веса; она не учитывает связи между весами структурированного набора.</p></div>
      <div class="note"><span class="tag">UNMEASURED / HARDWARE</span><h3>Результаты FPGA впереди</h3><p>DDR bandwidth, LUT, Fmax, энергопотребление и LLM inference здесь не измерены. В Python dense17/dense22 хранятся непрерывным потоком битов. Физическая укладка в BRAM и измерения на плате требуют отдельной проверки.</p></div>
    </section>
  </main>
  <footer><div class="meta"><span class="foot-title">SOURCE: <a href="benchmark.json">benchmark.json</a></span><br><span id="generated"></span><br><span id="environment"></span></div><div>TRINITY / TERNARY MEMORY<br>Self-contained report · schema v1</div></footer>
  <noscript><p class="empty">Для интерактивного отчёта включите JavaScript. Исходные измерения доступны в <a href="benchmark.json">benchmark.json</a>.</p></noscript>
</div>
<script type="application/json" id="benchmark-data">__BENCHMARK_DATA__</script>
<script>
"use strict";
const data = JSON.parse(document.getElementById("benchmark-data").textContent);
const $ = id => document.getElementById(id);
const num = (v, digits = 3) => new Intl.NumberFormat("en-US", {minimumFractionDigits: digits, maximumFractionDigits: digits}).format(v);
const int = v => new Intl.NumberFormat("en-US", {maximumFractionDigits: 0}).format(v);
function node(tag, text, className) { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (className) n.className = className; return n; }
const datasetDescriptions = {
  uniform: "Синтетический набор с равновероятной генерацией −1, 0, +1. Фактическая доля нулей и эмпирическая энтропия показаны выше.",
  sparse_4_1: "Структурированный набор: в каждой группе из 4 весов допускается не более 1 ненулевого веса. Специализированный кодек применим только к совместимой структуре.",
  sparse_8_2: "Структурированный набор: в каждой группе из 8 весов допускается не более 2 ненулевых весов. Специализированный кодек применим только к совместимой структуре."
};
const buttons = data.datasets.map((dataset, index) => {
  const button = node("button", dataset.name); button.type = "button"; button.setAttribute("aria-pressed", "false"); button.addEventListener("click", () => showDataset(index)); $("datasets").appendChild(button); return button;
});
function showDataset(index) {
  const dataset = data.datasets[index];
  buttons.forEach((button, i) => button.setAttribute("aria-pressed", String(i === index)));
  const best = dataset.results.reduce((a, b) => a.payload_bpw <= b.payload_bpw ? a : b);
  $("best-bpw").textContent = num(best.payload_bpw);
  $("best-detail").textContent = `${best.codec} · bit / weight · payload`;
  $("entropy").textContent = num(dataset.entropy_bpw);
  const passed = dataset.results.filter(r => r.roundtrip).length;
  $("roundtrip").textContent = `${passed} / ${dataset.results.length}`;
  $("roundtrip").classList.toggle("fail", passed !== dataset.results.length);
  $("dataset-title").textContent = dataset.name;
  $("count").textContent = int(dataset.count);
  $("zeros").textContent = `${num(dataset.zero_fraction * 100, 2)}%`;
  $("seed").textContent = data.seed ?? "Не указан";
  $("repeats").textContent = data.repeats ?? "Не указаны";
  $("dataset-note").textContent = datasetDescriptions[dataset.name] ?? "Параметры этого набора сохранены в исходном benchmark.json.";
  $("chart").replaceChildren(); $("results").replaceChildren();
  const ceiling = Math.max(2, ...dataset.results.map(r => r.payload_bpw));
  const labels = [];
  dataset.results.forEach(result => {
    const bar = node("div", undefined, "bar-row");
    bar.appendChild(node("span", result.codec));
    const track = node("div", undefined, "bar-track");
    const fill = node("div", undefined, "bar-fill");
    fill.classList.toggle("baseline", result.codec === "baseline2");
    fill.classList.toggle("best", result.payload_bpw === best.payload_bpw && result.codec !== "baseline2");
    fill.style.width = `${100 * result.payload_bpw / ceiling}%`;
    track.appendChild(fill); bar.appendChild(track); bar.appendChild(node("span", num(result.payload_bpw), "bar-value")); $("chart").appendChild(bar);
    labels.push(`${result.codec}: ${num(result.payload_bpw)} bit / weight`);
    const row = node("tr");
    const codec = node("td"); codec.appendChild(node("span", result.codec)); codec.appendChild(node("br")); const group = node("small", `${result.group_size} → ${result.group_bits}`); group.style.color = "var(--muted)"; codec.appendChild(group); row.appendChild(codec);
    [int(result.payload_bytes), int(result.container_bytes), num(result.container_bpw), num(result.encode_ms), num(result.decode_ms), `${num(result.ideal_payload_ratio_vs_baseline, 2)}×`].forEach(value => row.appendChild(node("td", value)));
    row.appendChild(node("td", result.roundtrip ? "PASS" : "FAIL", result.roundtrip ? "ok" : "fail")); $("results").appendChild(row);
  });
  $("chart").setAttribute("aria-label", `Payload, ${dataset.name}. ${labels.join(". ")}`);
}
$("scope").textContent = data.measurement_scope || "Измерены фактические размеры сериализованного потока и время encode/decode на локальной машине. Область замера не уточнена в исходном файле.";
$("generated").textContent = `Generated: ${data.generated_at || "не указано"} · seed ${data.seed ?? "не указан"} · repeats ${data.repeats ?? "не указаны"}`;
$("environment").textContent = `Python ${data.environment?.python || "не указано"} · ${data.environment?.platform || "платформа не указана"}`;
const startingTrits = [-1, 0, 1, 0, -1];
const selects = startingTrits.map((value, index) => {
  const holder = node("div", undefined, "trit");
  const label = node("label", `t${index} · 3^${index}`); label.htmlFor = `trit-${index}`; holder.appendChild(label);
  const select = node("select"); select.id = `trit-${index}`;
  [-1, 0, 1].forEach(trit => { const option = node("option", trit === 1 ? "+1" : trit === -1 ? "−1" : "0"); option.value = trit; select.appendChild(option); });
  select.value = value; select.addEventListener("change", encodeExample); holder.appendChild(select); holder.appendChild(node("small", `× ${3 ** index}`)); $("trits").appendChild(holder); return select;
});
function encodeExample() {
  const trits = selects.map(select => Number(select.value)); const digits = trits.map(t => t + 1); const byte = digits.reduce((sum, digit, index) => sum + digit * 3 ** index, 0);
  $("byte-decimal").textContent = byte; $("byte-hex").textContent = `0x${byte.toString(16).toUpperCase().padStart(2, "0")}`;
  $("formula").textContent = digits.map((digit, index) => `${digit} × ${3 ** index}`).join(" + ") + ` = ${byte}`;
  const bits = byte.toString(2).padStart(8, "0"); $("binary").replaceChildren(...[...bits].map(bit => node("span", bit))); $("binary").setAttribute("aria-label", `Двоичная запись: ${bits}`);
  const mapping = {"-1": "10", "0": "00", "1": "01"}; $("rtl-lanes").textContent = "RTL lanes t0 → t4: " + trits.map(t => mapping[String(t)]).join(" · ");
}
showDataset(0); encodeExample();
</script>
</body>
</html>
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input", nargs="?", type=Path, default=PROJECT_ROOT / "reports/benchmark.json",
        help="Measured benchmark JSON (default: reports/benchmark.json)",
    )
    parser.add_argument(
        "--output", type=Path, default=PROJECT_ROOT / "reports/index.html",
        help="Destination HTML (default: reports/index.html)",
    )
    args = parser.parse_args()
    try:
        data = json.loads(args.input.read_text(encoding="utf-8"))
        html = render_report(data)
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(1, f"Cannot render benchmark: {exc}\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8")
    print(f"Rendered {args.output}")


if __name__ == "__main__":
    main()
