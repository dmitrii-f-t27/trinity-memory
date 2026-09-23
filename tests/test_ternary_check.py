"""Issue #32: the Ternary Check matrix, its report, schema, HTML and reproductions.

The committed report reports/ternary-check.json must validate against
schemas/ternary-check.v1.schema.json (and each reproduction against
schemas/ternary-check.repro.v1.schema.json), meet the acceptance of #32 and
carry every number docs/ternary-check.md quotes in its Matrix section. The
Python mirror of the t27 status codes is checked against the generated
header. When build/fixtures holds the ranges and build/upstream/matrix the
llama.cpp-encoded tensors (sh tests/upstream/run-llamacpp-matrix.sh), the
report, the HTML and the reproductions are recomputed offline and must match
byte for byte; otherwise that test is skipped.
"""
import ctypes as C
import json
import os
from pathlib import Path
import re
import unittest
from unittest import mock

from trinity_memory import fixtures as fx
from trinity_memory import formats as f
from trinity_memory import matrix as mx
from trinity_memory import ternary_check as tc

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "ternary-check.json"
SCHEMA = ROOT / "schemas" / "ternary-check.v1.schema.json"
REPRO_SCHEMA = ROOT / "schemas" / "ternary-check.repro.v1.schema.json"


# ---- a JSON Schema validator for the keywords the two schemas use ------------------

class SchemaError(AssertionError):
    pass


def _type_ok(value, name):
    return {"object": isinstance(value, dict), "array": isinstance(value, list),
            "string": isinstance(value, str), "boolean": isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "null": value is None}[name]


def validate(value, schema, root=None, path="$"):
    """Draft 2020-12 subset: $ref (local), type, const, enum, required, properties,
    additionalProperties, minProperties, items, minItems, maxItems, pattern,
    minimum, maximum, exclusiveMinimum, allOf, anyOf, not, if/then/else."""
    root = root or schema
    if "$ref" in schema:
        target = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            target = target[part]
        validate(value, target, root, path)
    if "type" in schema and not _type_ok(value, schema["type"]):
        raise SchemaError(f"{path}: expected {schema['type']}, got {type(value).__name__}")
    if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
        raise SchemaError(f"{path}: expected {schema['const']!r}, got {value!r}")
    if "enum" in schema and not any(value == e and type(value) is type(e) for e in schema["enum"]):
        raise SchemaError(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise SchemaError(f"{path}: missing {key!r}")
        if len(value) < schema.get("minProperties", 0):
            raise SchemaError(f"{path}: fewer than {schema['minProperties']} properties")
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key in properties:
                validate(item, properties[key], root, f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                raise SchemaError(f"{path}: unexpected property {key!r}")
            elif isinstance(schema.get("additionalProperties"), dict):
                validate(item, schema["additionalProperties"], root, f"{path}.{key}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            raise SchemaError(f"{path}: {len(value)} items outside the bounds")
        if "items" in schema:
            for i, item in enumerate(value):
                validate(item, schema["items"], root, f"{path}[{i}]")
    if isinstance(value, str) and "pattern" in schema and not re.search(schema["pattern"], value):
        raise SchemaError(f"{path}: {value!r} does not match {schema['pattern']}")
    if _type_ok(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaError(f"{path}: {value} < {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaError(f"{path}: {value} > {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise SchemaError(f"{path}: {value} <= {schema['exclusiveMinimum']}")
    for sub in schema.get("allOf", []):
        validate(value, sub, root, path)
    if "anyOf" in schema:
        errors = []
        for sub in schema["anyOf"]:
            try:
                validate(value, sub, root, path)
                break
            except SchemaError as error:
                errors.append(str(error))
        else:
            raise SchemaError(f"{path}: no anyOf branch holds: {errors}")
    if "not" in schema:
        try:
            validate(value, schema["not"], root, path)
        except SchemaError:
            pass
        else:
            raise SchemaError(f"{path}: matches a schema it must not match")
    if "if" in schema:
        try:
            validate(value, schema["if"], root, path)
            holds = True
        except SchemaError:
            holds = False
        branch = schema.get("then") if holds else schema.get("else")
        if branch:
            validate(value, branch, root, path)


def _jsonschema():
    try:
        import jsonschema
    except ImportError:
        return None
    return jsonschema


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.report = json.loads(REPORT.read_text())
        self.schema = json.loads(SCHEMA.read_text())
        self.repro_schema = json.loads(REPRO_SCHEMA.read_text())

    def test_report_and_reproductions_validate(self):
        validate(self.report, self.schema)
        paths = sorted((ROOT / tc.REPRO).glob("*.json"))
        self.assertTrue(paths)
        for path in paths:
            validate(json.loads(path.read_text()), self.repro_schema)
        checker = _jsonschema()
        if checker:  # the reference implementation, where it is installed
            checker.Draft202012Validator.check_schema(self.schema)
            checker.Draft202012Validator(self.schema).validate(self.report)
            for path in paths:
                checker.Draft202012Validator(self.repro_schema).validate(json.loads(path.read_text()))

    def test_the_schema_rejects_broken_reports(self):
        def broken(change):
            report = json.loads(REPORT.read_text())
            change(report)
            with self.assertRaises(SchemaError):
                validate(report, self.schema)
        broken(lambda r: r.update(schema="trinity.ternary-check.v2"))
        broken(lambda r: r["cells"][1].update(status="differs"))
        broken(lambda r: r["cells"][1].pop("repro"))                          # a mismatch without its repro
        broken(lambda r: r["cells"][0]["trits"].update(differ=3))             # a match with differing trits
        broken(lambda r: r["cells"][8].pop("reasons"))                        # not representable without reasons
        broken(lambda r: r["cells"][2].update(evidence="third-party"))        # a t27 round trip as evidence
        broken(lambda r: r.update(generated="2026-09-23T00:00:00Z"))          # run metadata inside the report

    def test_deterministic_content(self):
        for path in [REPORT, ROOT / tc.HTML, *(ROOT / tc.REPRO).glob("*.json")]:
            text = path.read_text()
            for word in ("generated", "seconds", "timestamp", "duration"):
                self.assertNotIn(f'"{word}"', text, path)
        self.assertEqual(self.report["compiler"]["commit"], (ROOT / "native" / "compiler.lock").read_text().strip())


class TaxonomyTest(unittest.TestCase):
    def test_python_mirror_equals_the_generated_header(self):
        header = ROOT / "build" / "t27" / "matrix.h"
        if not header.is_file():
            self.skipTest("build/t27/matrix.h missing: sh tools/build-t27.sh")
        defines = dict(re.findall(r"^#define (TMX_\w+) (-?\d+)$", header.read_text(), re.M))
        self.assertEqual({int(defines[f"TMX_{name.upper().replace('-', '_')}"]): name for name in mx.STATUS.values()},
                         mx.STATUS)
        explain = {int(defines[k]): k[len("TMX_EXPLAIN_"):].lower() for k in defines if k.startswith("TMX_EXPLAIN_")}
        self.assertEqual({code: (None if token == "none" else token) for code, token in explain.items()}, mx.EXPLAIN)
        reasons = sorted((int(v), k[len("TMX_REASON_"):].lower()) for k, v in defines.items()
                         if k.startswith("TMX_REASON_") and k != "TMX_REASON_COUNT")
        self.assertEqual(tuple(token for _, token in reasons), mx.REASONS)
        self.assertEqual(int(defines["TMX_REASON_COUNT"]), len(mx.REASONS))
        for prefix, names in (("TMX_R_", mx.RESULT), ("TMX_T_", mx.TIES), ("TMX_L_", mx.LOCATE)):
            slots = sorted((int(v), k[len(prefix):].lower()) for k, v in defines.items()
                           if k.startswith(prefix) and k != prefix + "COUNT")
            self.assertEqual(tuple(name for _, name in slots), names, prefix)
        self.assertEqual(int(defines["TMX_REPRESENTABLE"]), mx.REPRESENTABLE)

    def test_report_taxonomy_reuses_the_formats_tokens(self):
        report = json.loads(REPORT.read_text())
        constants = json.loads((ROOT / "conformance" / "formats_llama_cpp.json").read_text())["constants"]
        self.assertEqual(report["taxonomy"]["errors"], constants["errors"])
        self.assertEqual(report["taxonomy"]["flags"], constants["flags"])


class BindingsTest(unittest.TestCase):
    def test_exact_words_and_representability(self):
        self.assertEqual(mx.float_bits(1.21875, f.F16), 0x3CE0)
        self.assertEqual(mx.float_bits(1.21875, f.BF16), 0x3F9C)
        self.assertEqual(mx.float_bits(1.0 + 2 ** -10, f.BF16), -1)
        self.assertEqual(mx.bf16_round(0x3F9C036F), 0x3F9C)
        rows, cols = 4, 256
        values = (C.c_int32 * (rows * cols))(*[((i * 7919) % 3) - 1 for i in range(rows * cols)])
        self.assertEqual(mx.representable(f.Q1_0, rows, cols, 0, values, [0x3F9C], f.BF16, 0)[0], "binary_only")
        # Two different scales per row, one group each: TQ1_0 (one scale per 256) cannot hold them.
        scales = [0x3C00, 0x3800] * rows
        primary, reasons = mx.representable(f.TQ1_0, rows, cols, 0, values, scales, f.F16, 128)
        self.assertEqual((primary, reasons), ("group_scales_differ", [("group_scales_differ", 4, 128)]))
        cell, reasons, written, words = mx.round_trip(f.Q2_0, rows, cols, 0, values, scales, f.F16, 128)
        self.assertEqual((mx.STATUS[cell["status"]], cell["trits_differ"], cell["scales_differ"]), ("match", 0, 0))
        self.assertEqual(len(written), rows * cols // 64 * 18)
        self.assertEqual(words, [0x3C00, 0x3C00, 0x3800, 0x3800] * rows)
        with self.assertRaises(f.FormatError) as caught:
            mx.round_trip(f.Q2_0, rows, cols, 0, values, [0x7E00] * 8, f.F16, 128)
        self.assertEqual(caught.exception.token, "scale_nonfinite")


class AcceptanceTest(unittest.TestCase):
    """#32: at least 5 formats x 2 model families; every cell match, mismatch (first index and an
    explanation, with a reproduction) or not representable (with reasons)."""

    def setUp(self):
        self.report = json.loads(REPORT.read_text())

    def test_every_cell_is_decided(self):
        tensors = {t["id"]: t for t in self.report["tensors"]}
        columns = [c["id"] for c in self.report["formats"]]
        self.assertGreaterEqual(len(columns), 5)
        self.assertEqual({t["family"] for t in tensors.values()}, {"bitnet", "bonsai"})
        cells = {(c["tensor"], c["format"]): c for c in self.report["cells"]}
        self.assertEqual(set(cells), {(t, c) for t in tensors for c in columns})
        for cell in self.report["cells"] + self.report["derived"]:
            if cell["status"] == "mismatch":
                differs = [part for part in ("trits", "scales") if cell[part].get("differ", 0) > 0]
                self.assertTrue(differs, cell["id"])
                for part in differs:
                    self.assertGreaterEqual(cell[part]["first"], 0)
                    self.assertIn(cell[part]["explanation"], ("scale_bf16_rounding", "tie_split", "scale_zero_weights"))
                repro = json.loads((ROOT / cell["repro"]).read_text())
                self.assertEqual(repro["cell"], cell["id"])
            elif cell["status"] == "not-representable":
                self.assertTrue(all(r["count"] >= 1 and r["of"] >= r["count"] for r in cell["reasons"]))
        repro_files = {p.name for p in (ROOT / tc.REPRO).glob("*.json")}
        self.assertEqual(repro_files, {Path(c["repro"]).name for c in self.report["cells"] + self.report["derived"]
                                       if "repro" in c})
        # Third-party evidence per family, not only t27 round trips.
        families = self.report["summary"]["families"]
        self.assertEqual(families["bitnet"]["third_party_formats"], ["hf_packed", "i2_s", "tq1_0_llamacpp", "tq2_0_llamacpp"])
        self.assertEqual(families["bonsai"]["third_party_formats"], ["mlx_2bit", "pq2_0", "ptq1_0", "q2_0"])

    def test_matvec_section_is_the_committed_matvec_report(self):
        matvec = json.loads((ROOT / "reports" / "ternary-check" / "matvec-2026-09-23.json").read_text())
        self.assertEqual(self.report["matvec"], matvec)

    def test_html_is_self_contained(self):
        html = (ROOT / tc.HTML).read_text()
        self.assertNotRegex(html, r"<script|<link|<img|<iframe|@import|url\(")
        self.assertNotRegex(html, r"(src|href)=")
        for cell in self.report["cells"]:
            self.assertIn(cell["status"], html)
        self.assertIn('<meta name="viewport"', html)
        self.assertIn("prefers-color-scheme:dark", html)


class NumbersInTheDocsTest(unittest.TestCase):
    """Everything the Matrix section of docs/ternary-check.md states, read from the committed report."""

    def setUp(self):
        self.report = json.loads(REPORT.read_text())
        self.cells = {c["id"]: c for c in self.report["cells"] + self.report["derived"]}

    def test_summary(self):
        summary = self.report["summary"]
        self.assertEqual((summary["tensors"], summary["columns"], summary["cells"]), (3, 12, 36))
        self.assertEqual(summary["status"], {"match": 23, "mismatch": 4, "not-representable": 9})
        self.assertEqual(summary["provenance"], {"published": 5, "reference": 3, "t27_round_trip": 22,
                                                 "upstream_encoder": 6})
        self.assertEqual(summary["derived_cells"], 2)

    def test_bitnet(self):
        for tensor, weights, differ, ties, tie, product in (
                ("bitnet-q_proj", 6553600, 79719, (39708, 41560, 40011, 41944), 0.609375, 0.49996),
                ("bitnet-down_proj", 17694720, 101673, (50812, 453819, 50861, 453976), 1.078125, 0.49841)):
            i2s = self.cells[f"{tensor}--i2_s"]
            self.assertEqual((i2s["status"], i2s["trits"]["differ"], i2s["scales"]["differ"]), ("mismatch", 0, weights))
            self.assertEqual(i2s["scales"]["explanation"], "scale_bf16_rounding")
            self.assertEqual((i2s["flags"]["trailer_nonzero"], i2s["t27_reencode"]["differ_bytes"]), (28, 28))
            absmean = self.cells[f"{tensor}--bf16_absmean"]
            self.assertEqual((absmean["trits"]["differ"], absmean["trits"]["explanation"]), (differ, "tie_split"))
            t = absmean["ties"]
            self.assertEqual((t["positive"]["packed_nonzero"], t["positive"]["packed_zero"],
                              t["negative"]["packed_nonzero"], t["negative"]["packed_zero"]), ties)
            self.assertEqual(t["positive"]["packed_nonzero"] + t["negative"]["packed_nonzero"], differ)
            self.assertEqual((t["tie_value"], round(t["tie_times_s"], 5)), (tie, product))
            self.assertEqual((t["differ_elsewhere"], t["derived_nonzero_at_ties"]), (0, 0))
            triple = absmean["triple_check"]
            self.assertEqual(triple["holds"], {"packed_equals_i2_s": True, "absmean_equals_packed": False})
            self.assertEqual(triple["bf16_absmean_vs_i2_s"]["explanation"], "tie_split")
            for column in ("ptq1_0", "pq2_0", "q2_0", "mlx_2bit", "tq1_0", "tq2_0", "onnx_2bit"):
                self.assertEqual(self.cells[f"{tensor}--{column}"]["status"], "match", column)
            self.assertEqual(self.cells[f"{tensor}--q1_0"]["reasons"][0]["reason"], "binary_only")
        self.assertEqual(self.cells["bitnet-q_proj--q1_0"]["reasons"][0]["count"], 3251715)
        self.assertEqual(self.cells["bitnet-down_proj--q1_0"]["reasons"][0]["count"], 6761794)
        # llama.cpp's quantizers: same trits; d = 0 in the 50 blocks of zero weights of q_proj.
        for ext in ("tq1_0", "tq2_0"):
            q = self.cells[f"bitnet-q_proj--{ext}_llamacpp"]
            self.assertEqual((q["status"], q["trits"]["differ"], q["scales"]["differ"], q["scales"]["first"]),
                             ("mismatch", 0, 12800, 3934464))
            self.assertEqual(q["scales"]["explanation"], "scale_zero_weights")
            self.assertEqual((q["t27_reencode"]["differ_bytes"], q["t27_round_trip_bytes"]["differ_bytes"]), (0, 100))
            # 50 whole blocks of 256 weights, and the first differing byte is a block's fp16 scale.
            block, scale_at = {"tq1_0": (54, 52), "tq2_0": (66, 64)}[ext]
            self.assertEqual((q["scales"]["differ"] % 256, q["scales"]["differ"] // 256, q["scales"]["first"] % 256), (0, 50, 0))
            self.assertEqual(q["t27_round_trip_bytes"]["first"] % block, scale_at)
            self.assertEqual(q["t27_round_trip_bytes"]["first"] // block, q["scales"]["first"] // 256)
            d = self.cells[f"bitnet-down_proj--{ext}_llamacpp"]
            self.assertEqual((d["status"], d["t27_reencode"]["differ_bytes"], d["t27_round_trip_bytes"]["differ_bytes"]),
                             ("match", 0, 0))

    def test_bonsai(self):
        for column in ("pq2_0", "q2_0", "mlx_2bit"):
            cell = self.cells[f"bonsai-ffn_down--{column}"]
            self.assertEqual((cell["status"], cell["provenance"], cell["t27_reencode"]["differ_bytes"]),
                             ("match", "published", 0))
        self.assertEqual(self.cells["bonsai-ffn_down--ptq1_0"]["t27_reencode"]["differ_bytes"], 0)
        self.assertEqual(self.cells["bonsai-ffn_down--mlx_2bit"]["flags"]["affine_not_ternary"], 0)
        for column in ("tq1_0", "tq2_0", "tq1_0_llamacpp", "tq2_0_llamacpp"):
            reason = self.cells[f"bonsai-ffn_down--{column}"]["reasons"]
            self.assertEqual([(r["reason"], r["count"], r["of"]) for r in reason],
                             [("group_scales_differ", 347140, 348160)])
        hf = self.cells["bonsai-ffn_down--hf_packed"]["reasons"]
        self.assertEqual([(r["reason"], r["count"], r["of"]) for r in hf],
                         [("group_scales_differ", 1, 1), ("scale_precision", 608946, 696320)])
        self.assertEqual([r["reason"] for r in self.cells["bonsai-ffn_down--i2_s"]["reasons"]], ["group_scales_differ"])
        q1 = self.cells["bonsai-ffn_down--q1_0"]["reasons"][0]
        self.assertEqual((q1["reason"], q1["count"], q1["of"]), ("binary_only", 29214453, 89128960))
        self.assertEqual(self.cells["bonsai-ffn_down--onnx_2bit"]["status"], "match")
        self.assertTrue(self.report["tensors"][2]["runtime_requirements"]["hadamard_rotation"])


class RecomputeFromCacheTest(unittest.TestCase):
    """Recomputes every committed file offline; skipped when a range or an upstream tensor is missing."""

    def setUp(self):
        self._offline = os.environ.get(fx.OFFLINE_ENV)
        os.environ[fx.OFFLINE_ENV] = "1"
        tc.BITNET._remotes.clear()
        tc.BONSAI._remotes.clear()

    def tearDown(self):
        if self._offline is None:
            os.environ.pop(fx.OFFLINE_ENV, None)
        else:
            os.environ[fx.OFFLINE_ENV] = self._offline
        tc.BITNET._remotes.clear()
        tc.BONSAI._remotes.clear()

    def test_reports_reproduce_byte_for_byte(self):
        if not all((tc.UPSTREAM / name).is_file() for name in
                   ("bitnet-q_proj.tq1_0", "bitnet-q_proj.tq2_0", "bitnet-down_proj.tq1_0", "bitnet-down_proj.tq2_0")):
            self.skipTest("llama.cpp-encoded tensors missing: sh tests/upstream/run-llamacpp-matrix.sh")
        seen = set()
        read, prefix = fx.Remote.read, fx.Remote.prefix

        def reading(remote, begin, end):
            seen.add((remote.key, begin, end))
            return read(remote, begin, end)

        def prefixing(remote, needed):
            for chunk in remote.entry.prefix:
                seen.add((remote.key, chunk["begin"], chunk["end"]))
                if chunk["end"] >= needed:
                    break
            return prefix(remote, needed)
        try:
            with mock.patch.object(fx.Remote, "read", reading), mock.patch.object(fx.Remote, "prefix", prefixing):
                report, repro = tc.run()
        except fx.FixtureError as error:
            self.skipTest(f"fixture cache incomplete (python3 tools/fetch-fixtures.py): {error}")
        self.assertEqual(tc.check(tc.outputs(report, repro)), [])
        # The manifest tags exactly the ranges the matrix reads with its consumer.
        manifest = fx.Manifest(fx.MANIFEST)
        tagged = {(entry.key, begin, end) for entry in manifest.entries.values()
                  for (begin, end), record in entry.ranges.items() if "ternary_check" in record["used_by"]}
        self.assertEqual(seen, tagged)
        self.assertEqual(len(seen), 36)


if __name__ == "__main__":
    unittest.main()
