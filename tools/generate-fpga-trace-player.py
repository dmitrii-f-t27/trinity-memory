#!/usr/bin/env python3
"""Generate the on-device trace player bank for the AX7203 measurement track (#10).

Reads the cycle-exact `dot_trace`, `storage_trace` and `join_trace` vectors of
`conformance/memory_stream_compute.json`, the Edge Demo fixtures of
`conformance/memory_edge_demo.json`, and writes

  build/fpga/tms_trace_bank.v        ROMs (stimulus/expected words packed exactly as
                                     tests/tb_spec_dot_trace.v and tests/tb_spec_storage_trace.v
                                     read them), one DUT instance per distinct parameter set,
                                     and the vector table used by fpga/ax7203/tms_trace_player.v
  build/fpga/tms_trace_manifest.json vector order, packed words, counter semantics and the
                                     source hash, consumed by tools/fpga-capture.py

The packing functions are imported from tests/spec_stream_replay.py so the FPGA
player and the Icarus replay cannot drift apart.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import spec_stream_replay as replay  # noqa: E402

SOURCE = ROOT / "conformance" / "memory_stream_compute.json"
EDGE_SOURCE = ROOT / "conformance" / "memory_edge_demo.json"
MAX_VECTORS = 1023
MAX_CYCLES = 1023
EXPECTED_BITS = 40   # observed/expected words: dot and storage use 36, the joined path 38

# Throughput workload (section T of the player): 64 stored words, FRAMES frames of 64 beats
# with activations from a closed formula, so the host recomputes every frame result.
WORKLOAD_WORDS = 64
WORKLOAD_FRAMES = 16
EDGE_LABELS = ["rising", "falling", "alternating"]   # EdgeLabel enum order in specs/memory/edge_demo.t27


def dense_code(trits):
    return sum((trit + 1) * 3 ** lane for lane, trit in enumerate(trits))


def workload_trit(word, lane):
    return (word * 7 + lane * 3 + 1) % 3 - 1


def workload_activation(beat, lane, frame):
    value = (beat * 13 + lane * 29 + frame * 5 + 7) & 255
    return value - 256 if value >= 128 else value


def workload():
    codes = [dense_code([workload_trit(word, lane) for lane in range(5)]) for word in range(WORKLOAD_WORDS)]
    results = []
    for frame in range(WORKLOAD_FRAMES):
        total = 0
        for beat in range(WORKLOAD_WORDS):
            for lane in range(5):
                total += workload_trit(beat, lane) * workload_activation(beat, lane, frame)
        results.append(total)
    return {"words": WORKLOAD_WORDS, "frames": WORKLOAD_FRAMES, "codes": codes, "expected_results": results,
            "trit_rule": "trit(word, lane) = ((word*7 + lane*3 + 1) mod 3) - 1, dense5 codes",
            "activation_rule": "int8((beat*13 + lane*29 + frame*5 + 7) & 255), five lanes per beat, full masks (320 trits)"}


def edge_weight(row, column):
    if row == 2:
        return -1 if column % 2 == 0 else 1
    if row == 0:
        return -1 if column < 6 else 1
    return 1 if column < 6 else -1


def edge():
    document = json.loads(EDGE_SOURCE.read_bytes())
    rows = []
    for row in range(3):
        weights = [edge_weight(row, column) for column in range(12)]
        words = [weights[0:5], weights[5:10], weights[10:12] + [0, 0, 0]]
        rows.append({"weights": weights, "codes": [dense_code(word) for word in words]})
    fixtures = []
    for vector in document["vectors"]:
        if vector["kind"] != "fixture":
            continue
        samples = list(vector["samples"])
        accumulators = [sum(edge_weight(row, c) * samples[c] for c in range(12)) for row in range(3)]
        if accumulators != list(vector["accumulators"]):
            raise SystemExit(f"{vector['id']}: accumulators disagree with the weight rule")
        fixtures.append({"index": len(fixtures), "id": vector["id"], "name": vector.get("name"), "samples": samples,
                         "label": EDGE_LABELS.index(vector["label"]), "label_name": vector["label"],
                         "ambiguous": bool(vector.get("ambiguous", False)), "accumulators": accumulators})
    if len(fixtures) != 6 or any(len(f["samples"]) != 12 for f in fixtures):
        raise SystemExit("expected six Edge Demo fixtures of twelve samples")
    return {"source": {"path": str(EDGE_SOURCE.relative_to(ROOT)), "sha256": hashlib.sha256(EDGE_SOURCE.read_bytes()).hexdigest()},
            "rows": rows, "fixtures": fixtures, "labels": EDGE_LABELS,
            "beats": "three beats per fixture: samples 0-4, 5-9, 10-11 (lanes 2-4 zero, tail mask 00011)"}

# Counter semantics (index -> meaning), evaluated by the player at every stepped cycle
# from the stimulus word of that cycle and the outputs the core sees at the consuming
# edge (stimulus applied, state not yet advanced), i.e. the handshake values.
DOT_COUNTERS = [
    "beats accepted: in_valid && in_ready at the consuming edge",
    "results delivered: out_valid && out_ready at the consuming edge",
    "input stalls: in_valid && !in_ready at the consuming edge",
    "output holds: out_valid && !out_ready at the consuming edge",
    "error results delivered: out_valid && out_ready && out_error at the consuming edge",
    "reset cycles: reset asserted",
]
JOIN_COUNTERS = [
    "beats fired: word_valid && act_valid && in_ready at the consuming edge",
    "results delivered: out_valid && out_ready at the consuming edge",
    "activation stalls: act_valid && !act_ready at the consuming edge",
    "output holds: out_valid && !out_ready at the consuming edge",
    "loads accepted: load_en && load_ready at the consuming edge",
    "reset cycles: rst asserted",
]
STORAGE_COUNTERS = [
    "loads accepted: load_en && load_ready at the consuming edge",
    "words delivered: out_valid after the edge",
    "lanes delivered: popcount(out_lane_mask) when out_valid after the edge",
    "invalid words delivered: out_valid && !out_code_valid after the edge",
    "starts accepted: start && !busy && !rst at the consuming edge",
    "reset cycles: rst asserted",
]


def load_vectors(document):
    vectors, duts = [], []
    for vector in document["vectors"]:
        if vector["kind"] == "dot_trace":
            stimulus = [replay.dot_stimulus_word(step) for step in vector["cycles"]]
            expected = [replay.dot_expected_word(step) for step in vector["cycles"]]
            key = ("dot", bool(vector["dense"]), int(vector["acc_width"]))
            params = {"dense": bool(vector["dense"]), "acc_width": int(vector["acc_width"])}
        elif vector["kind"] == "storage_trace":
            stimulus = [replay.storage_stimulus_word(step) for step in vector["cycles"]]
            expected = [replay.storage_expected_word(step) for step in vector["cycles"]]
            key = ("storage", bool(vector["dense"]), int(vector["trit_count"]))
            params = {"dense": bool(vector["dense"]), "trit_count": int(vector["trit_count"])}
        elif vector["kind"] == "join_trace":
            stimulus = [replay.join_stimulus_word(step) for step in vector["cycles"]]
            expected = [replay.join_expected_word(step) for step in vector["cycles"]]
            key = ("join", bool(vector["dense"]), int(vector["trit_count"]), int(vector["acc_width"]))
            params = {"dense": bool(vector["dense"]), "trit_count": int(vector["trit_count"]), "acc_width": int(vector["acc_width"])}
        else:
            continue
        if key not in duts:
            duts.append(key)
        if len(stimulus) != vector["cycle_count"] or not stimulus:
            raise SystemExit(f"{vector['id']}: cycle_count disagrees with the cycles list")
        if any(word >> 64 for word in stimulus) or any(word >> EXPECTED_BITS for word in expected):
            raise SystemExit(f"{vector['id']}: packed word does not fit")
        vectors.append({
            "id": vector["id"], "kind": key[0], "dut": duts.index(key), "params": params,
            "cycles": len(stimulus), "stimulus": stimulus, "expected": expected,
        })
    if len(vectors) > MAX_VECTORS or sum(v["cycles"] for v in vectors) > MAX_CYCLES:
        raise SystemExit("too many vectors or cycles for the 10-bit player counters")
    return vectors, duts


def emit_bank(vectors, duts, source_hash):
    """Wiring only: the generated t27 ROM core (fpga_trace_rom.t27 -> TrinityFpgaTraceRomT27), one
    core-under-test wrapper per distinct parameter set, and the observation mux."""
    lines = [
        "// DO NOT EDIT - generated by tools/generate-fpga-trace-player.py from",
        f"// conformance/memory_stream_compute.json (sha256 {source_hash}).",
        "// Wiring only: tables live in the generated TrinityFpgaTraceRomT27 (fpga_trace_rom.t27),",
        "// the cores under test in build/t27/specs/rtl and fpga/ax7203/tms_dut_wrappers.v.",
        "`timescale 1ns/1ps",
        "`default_nettype none",
        "module tms_trace_bank (",
        "    input  wire        clk,",
        "    input  wire        dut_rst_n,",
        "    input  wire        step,",
        "    input  wire [9:0]  vector,",
        "    input  wire [9:0]  addr,",
        "    input  wire [63:0] stim_q,",
        "    output wire [63:0] stimulus,",
        "    output wire [39:0] expected,",
        "    output wire [31:0] entry,",
        "    output wire [9:0]  vector_count,",
        "    output wire [9:0]  cycle_count,",
        "    output wire [39:0] observed,",
        "    input  wire [5:0]  wl_addr,",
        "    output wire [7:0]  wl_code,",
        "    input  wire [3:0]  edge_ra,",
        "    output wire [7:0]  edge_code,",
        "    input  wire [4:0]  edge_fb,",
        "    output wire [39:0] edge_beat,",
        "    output wire [7:0]  workload_frames,",
        "    output wire [2:0]  edge_fixtures",
        ");",
        "    wire [63:0] rom_expected, rom_edge_beat;",
        "    wire [31:0] rom_vector_count, rom_cycle_count, rom_workload_frames, rom_edge_fixtures, rom_wl_code, rom_edge_code;",
        "    TrinityFpgaTraceRomT27 rom (",
        "        .clk(clk), .rst_n(1'b1), .en(1'b1), .ready(),",
        "        .addr({22'd0, addr}), .vector({22'd0, vector}), .wl_addr({26'd0, wl_addr}),",
        "        .edge_ra({28'd0, edge_ra}), .edge_fb({27'd0, edge_fb}),",
        "        .stimulus(stimulus), .expected(rom_expected), .entry(entry), .workload_code(rom_wl_code),",
        "        .edge_code(rom_edge_code), .edge_beat(rom_edge_beat), .vector_count(rom_vector_count),",
        "        .cycle_count(rom_cycle_count), .workload_frames(rom_workload_frames), .edge_fixtures(rom_edge_fixtures)",
        "    );",
        "    assign expected = rom_expected[39:0];",
        "    assign edge_beat = rom_edge_beat[39:0];",
        "    assign wl_code = rom_wl_code[7:0];",
        "    assign edge_code = rom_edge_code[7:0];",
        "    assign vector_count = rom_vector_count[9:0];",
        "    assign cycle_count = rom_cycle_count[9:0];",
        "    assign workload_frames = rom_workload_frames[7:0];",
        "    assign edge_fixtures = rom_edge_fixtures[2:0];",
        "    wire [3:0] vector_dut = entry[25:22];",
    ]
    for index, config in enumerate(duts):
        kind, dense, size = config[0], config[1], config[2]
        lines.append(f"    wire [39:0] obs_{index};")
        if kind == "dot":
            lines.append(f"    tms_dot_dut #(.DENSE5({1 if dense else 0}), .ACC_WIDTH({size})) dut_{index} (")
        elif kind == "storage":
            lines.append(f"    tms_storage_dut #(.DENSE5({1 if dense else 0}), .TRIT_COUNT({size})) dut_{index} (")
        else:
            lines.append(f"    tms_join_dut #(.DENSE5({1 if dense else 0}), .TRIT_COUNT({size}), .ACC_WIDTH({config[3]})) dut_{index} (")
        lines.append(f"        .clk(clk), .rst_n(dut_rst_n), .en(step && (vector_dut == 4'd{index})),")
        lines.append(f"        .stim(stim_q), .observed(obs_{index}));")
    mux = " : ".join(f"(vector_dut == 4'd{i}) ? obs_{i}" for i in range(len(duts)))
    lines.append(f"    assign observed = {mux} : 40'd0;")
    lines += ["endmodule", "`default_nettype wire", ""]
    return "\n".join(lines)


def chunked_lookup(name, rtype, entries, comment=None):
    """t27 lookup functions: yosys's AST simplifier recurses once per nested `if`, so a flat
    chain of hundreds of `if (a == k) { return v; }` overflows it; leaves of at most 32
    entries plus a dispatcher on the block index keep the nesting shallow."""
    lines = []
    if comment:
        lines.append(f"// {comment}")
    blocks = [entries[i:i + 32] for i in range(0, len(entries), 32)] or [[]]
    for b, block in enumerate(blocks):
        lines.append(f"fn {name}_block{b}(a: u32) -> {rtype} {{")
        for offset, value in enumerate(block):
            lines.append(f"    if (a == {b * 32 + offset}) {{ return {value}; }}")
        lines += ["    return 0;", "}"]
    lines.append(f"fn {name}(a: u32) -> {rtype} {{")
    for b in range(len(blocks)):
        lines.append(f"    if ((a >> 5) == {b}) {{ return {name}_block{b}(a); }}")
    lines += ["    return 0;", "}", ""]
    return lines


def emit_rom_t27(vectors, duts, source_hash):
    """The ROM bank as an executable t27 module (build/fpga/fpga_trace_rom.t27): every table is a
    function of its address, the outputs are module-level combinational values of the on_clock inputs."""
    work, demo = workload(), edge()
    kinds = {"dot": 0, "storage": 1, "join": 2}
    total = sum(v["cycles"] for v in vectors)
    lines = [
        "module TrinityFpgaTraceRomT27;",
        "",
        "// GENERATED by tools/generate-fpga-trace-player.py from",
        f"// conformance/memory_stream_compute.json (sha256 {source_hash}) and",
        f"// conformance/memory_edge_demo.json (sha256 {demo['source']['sha256']}). Do not edit.",
        "// Stimulus and expected words use the packing of tests/tb_spec_*_trace.v; the vector",
        "// table packs {dut[3:0], kind[1:0], base[9:0], cycles[9:0]}; the workload codes and",
        "// the Edge template codes and activation beats feed the throughput and Edge phases.",
        f"const VECTOR_COUNT: u32 = {len(vectors)};",
        f"const CYCLE_COUNT: u32 = {total};",
        f"const WORKLOAD_FRAMES: u32 = {work['frames']};",
        f"const EDGE_FIXTURES: u32 = {len(demo['fixtures'])};",
        "",
    ]
    stim_entries, exp_entries = [], []
    for vector in vectors:
        stim_entries += vector["stimulus"]
        exp_entries += vector["expected"]
    lines += chunked_lookup("stimulus_at", "u64", stim_entries, "stimulus words, one per flat cycle index")
    lines += chunked_lookup("expected_at", "u64", exp_entries, "expected observation words")
    table_entries = []
    base = 0
    for vector in vectors:
        table_entries.append((vector["dut"] << 22) | (kinds[vector["kind"]] << 20) | (base << 10) | vector["cycles"])
        base += vector["cycles"]
    lines += chunked_lookup("table_at", "u32", table_entries, "vector table: " + ", ".join(f"{i}={v['id']}" for i, v in enumerate(vectors)))
    lines += chunked_lookup("workload_code_at", "u32", work["codes"], "throughput workload: dense5 code of each stored word")
    edge_codes = [0] * 12
    for row in range(3):
        for address, code in enumerate(demo["rows"][row]["codes"]):
            edge_codes[row * 4 + address] = code
    lines += chunked_lookup("edge_code_at", "u32", edge_codes, "Edge template rows: {row, addr} -> dense5 code")
    edge_beats = [0] * 24
    for fixture in demo["fixtures"]:
        for beat in range(3):
            lanes = fixture["samples"][5 * beat:5 * beat + 5] + [0] * 5
            edge_beats[fixture["index"] * 4 + beat] = sum((lanes[k] & 255) << (8 * k) for k in range(5))
    lines += chunked_lookup("edge_beat_at", "u64", edge_beats, "Edge fixtures: {fixture, beat} -> five int8 lanes")
    lines += [
        "var stimulus: u64;", "var expected: u64;", "var entry: u32;", "var workload_code: u32;",
        "var edge_code: u32;", "var edge_beat: u64;", "var vector_count: u32;", "var cycle_count: u32;",
        "var workload_frames: u32;", "var edge_fixtures: u32;",
        "var strobe: bool = false;",
        "stimulus = stimulus_at(addr);", "expected = expected_at(addr);", "entry = table_at(vector);",
        "workload_code = workload_code_at(wl_addr);", "edge_code = edge_code_at(edge_ra);", "edge_beat = edge_beat_at(edge_fb);",
        "vector_count = VECTOR_COUNT;", "cycle_count = CYCLE_COUNT;", "workload_frames = WORKLOAD_FRAMES;", "edge_fixtures = EDGE_FIXTURES;",
        "",
        "fn on_clock(addr: u32, vector: u32, wl_addr: u32, edge_ra: u32, edge_fb: u32) {",
        "    strobe = !strobe;",
        "}",
        "",
        f"test first_stimulus {{ assert_eq(stimulus_at(0), {vectors[0]['stimulus'][0]}); }}",
        f"test last_vector_table {{ assert_eq(table_at({len(vectors) - 1}) & 1023, {vectors[-1]['cycles']}); }}",
        f"test workload_word_zero {{ assert_eq(workload_code_at(0), {work['codes'][0]}); }}",
        f"test edge_row_zero_word_zero {{ assert_eq(edge_code_at(0), {demo['rows'][0]['codes'][0]}); }}",
        "endmodule",
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default=str(SOURCE))
    parser.add_argument("--output-dir", default=str(ROOT / "build" / "fpga"))
    parser.add_argument("--check", action="store_true", help="fail if the outputs would change")
    args = parser.parse_args()
    source = Path(args.source)
    raw = source.read_bytes()
    source_hash = hashlib.sha256(raw).hexdigest()
    vectors, duts = load_vectors(json.loads(raw))
    bank = emit_bank(vectors, duts, source_hash)
    manifest = {
        "schema": "trinity.fpga-trace-manifest.v1",
        "source": {"path": str(source.relative_to(ROOT)) if source.is_relative_to(ROOT) else str(source),
                   "sha256": source_hash},
        "duts": [dict({"index": i, "kind": c[0], "dense": c[1]},
                      **({"acc_width": c[2]} if c[0] == "dot" else {"trit_count": c[2]} if c[0] == "storage"
                         else {"trit_count": c[2], "acc_width": c[3]}))
                 for i, c in enumerate(duts)],
        "vectors": [{
            "index": i, "id": v["id"], "kind": v["kind"], "dut": v["dut"], **v["params"],
            "cycles": v["cycles"],
            "stimulus": [f"{w:016x}" for w in v["stimulus"]],
            "expected": [f"{w:010x}" for w in v["expected"]],
        } for i, v in enumerate(vectors)],
        "counters": {"dot": DOT_COUNTERS, "storage": STORAGE_COUNTERS, "join": JOIN_COUNTERS},
        "total_cycles": sum(v["cycles"] for v in vectors),
        "workload": workload(),
        "edge": edge(),
        "line_format_v2": "tag(1) + a(8 hex) + b(10 hex) + LF; tags H V C E K F W R T X A D",
    }
    text = json.dumps(manifest, indent=1, sort_keys=True) + "\n"
    out = Path(args.output_dir)
    targets = {out / "tms_trace_bank.v": bank, out / "tms_trace_manifest.json": text,
               out / "fpga_trace_rom.t27": emit_rom_t27(vectors, duts, source_hash)}
    if args.check:
        stale = [str(p) for p, body in targets.items() if not p.is_file() or p.read_text(encoding="utf-8") != body]
        if stale:
            raise SystemExit("stale generated player files: " + ", ".join(stale))
        print("fpga trace player outputs are current")
        return
    out.mkdir(parents=True, exist_ok=True)
    for path, body in targets.items():
        path.write_text(body, encoding="utf-8")
    print(f"wrote {len(vectors)} vectors, {manifest['total_cycles']} cycles, {len(duts)} DUT instances, "
          f"{manifest['workload']['frames']} workload frames, {len(manifest['edge']['fixtures'])} edge fixtures -> {out}")


if __name__ == "__main__":
    main()
