"""Issue #36: the v0.4.0 release files agree with each other and with the reports.

- One version everywhere it names the current release: pyproject.toml,
  trinity_memory/__init__.py, the installed-wheel test, the top entry of
  CHANGELOG.md and the Action usage in both READMEs and ternary-check/README.md;
  setup.py takes the wheel manifest's version from the metadata.
- CHANGELOG.md names every pull request merged since v0.3.0 (checked against
  git history when the v0.3.0 tag is present).
- MANIFEST.in carries the release files into the source distribution
  (tools/build-release-assets.py checks the built archive itself).
- The Ternary Check section of README.md and README.ru.md: its result table is
  the table of reports/ternary-check.json, cell by cell, its totals are the
  report's, and every relative link resolves.
- .github/workflows/ternary-check-release-smoke.yml runs the Action from the
  tag with runtime "release" on the three runners, without secrets.
"""
import json
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "ternary-check.json"
SMOKE = ROOT / ".github" / "workflows" / "ternary-check-release-smoke.yml"
TABLE_START = "<!-- ternary-check-table: generated from reports/ternary-check.json, checked by tests/test_release.py -->"
TABLE_END = "<!-- /ternary-check-table -->"

# Pull requests merged after v0.3.0 (git log --merges v0.3.0..HEAD).
MERGED_SINCE_0_3_0 = {12, 13, 14, 16, 17, 19, 20, 21, 22, 23, 25, 26, 37, 38, 39, 40, 41, 42, 43, 45, 46}

LABELS = {
    "en": {"header": ("Format", "BitNet `q_proj`", "BitNet `down_proj`", "Bonsai `ffn_down`"),
           "status": {"match": "match", "mismatch": "mismatch", "not-representable": "not representable"},
           "provenance": {"published": "published", "t27_round_trip": "t27 round trip",
                          "upstream_encoder": "llama.cpp writer"},
           "reference": "reference"},
    "ru": {"header": ("Формат", "BitNet `q_proj`", "BitNet `down_proj`", "Bonsai `ffn_down`"),
           "status": {"match": "совпадает", "mismatch": "расходится", "not-representable": "не представим"},
           "provenance": {"published": "опубликованный файл", "t27_round_trip": "запись и чтение t27",
                          "upstream_encoder": "запись llama.cpp"},
           "reference": "эталон"},
}
TENSORS = ("bitnet-q_proj", "bitnet-down_proj", "bonsai-ffn_down")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def version() -> str:
    match = re.search(r'^version = "([^"]+)"$', read(ROOT / "pyproject.toml"), re.M)
    assert match, "pyproject.toml has no version"
    return match.group(1)


def cell_label(cell: dict, lang: str) -> str:
    labels = LABELS[lang]
    if cell["provenance"] == "reference":
        return labels["reference"]
    status = labels["status"][cell["status"]]
    if cell["provenance"] == "not_written":
        return status
    return f"{status} ({labels['provenance'][cell['provenance']]})"


def result_table(lang: str) -> list[str]:
    """The README result table, one Markdown line per row, from the committed report."""
    report = json.loads(read(REPORT))
    assert [tensor["id"] for tensor in report["tensors"]] == list(TENSORS)
    cells = {(cell["format"], cell["tensor"]): cell for cell in report["cells"]}
    header = LABELS[lang]["header"]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for column in report["formats"]:
        row = [column["name"], *(cell_label(cells[(column["id"], tensor)], lang) for tensor in TENSORS)]
        lines.append("| " + " | ".join(row) + " |")
    return lines


def readme_section(path: Path) -> str:
    match = re.search(r"^## t27 Ternary Check.*?(?=^## |\Z)", read(path), re.M | re.S)
    assert match, f"{path.name} has no '## t27 Ternary Check' section"
    return match.group(0)


class VersionTest(unittest.TestCase):
    def test_one_version(self):
        current = version()
        self.assertEqual(current, "0.4.0")
        init = re.search(r'^__version__="([^"]+)"$', read(ROOT / "trinity_memory" / "__init__.py"), re.M)
        self.assertEqual(init.group(1), current)
        self.assertIn(f"tm.__version__=='{current}'", read(ROOT / "tests" / "native" / "test_installed_wheel.py"))
        self.assertNotRegex(read(ROOT / "setup.py"), r'"version":\s*"\d')
        self.assertIn('"version": self.distribution.get_version()', read(ROOT / "setup.py"))
        for path in (ROOT / "README.md", ROOT / "README.ru.md", ROOT / "ternary-check" / "README.md"):
            refs = set(re.findall(r"dmitrii-f-t27/trinity-memory/ternary-check@(v[\d.]+)", read(path)))
            self.assertEqual(refs, {f"v{current}"}, path.name)

    def test_changelog_top_entry(self):
        text = read(ROOT / "CHANGELOG.md")
        entries = re.findall(r"^## \[([^\]]+)\] - (\d{4}-\d{2}-\d{2})$", text, re.M)
        self.assertEqual([name for name, _ in entries], [version(), "0.3.0", "0.2.0"])
        self.assertEqual(dict(entries)["0.3.0"], "2026-09-10")
        self.assertEqual(dict(entries)["0.2.0"], "2026-09-09")
        self.assertIn(f"[{version()}]: https://github.com/dmitrii-f-t27/trinity-memory/compare/v0.3.0...v{version()}",
                      text)


class ChangelogTest(unittest.TestCase):
    def section(self) -> str:
        text = read(ROOT / "CHANGELOG.md")
        return re.search(rf"^## \[{re.escape(version())}\].*?(?=^## \[)", text, re.M | re.S).group(0)

    def test_every_merged_pull_request_is_named(self):
        named = {int(n) for n in re.findall(r"trinity-memory/pull/(\d+)", self.section())}
        self.assertEqual(named, MERGED_SINCE_0_3_0)

    def test_against_git_history_when_available(self):
        tag = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "-q", "--verify", "v0.3.0^{commit}"],
                             capture_output=True, text=True)
        if tag.returncode:
            self.skipTest("the v0.3.0 tag is not in this clone")
        log = subprocess.run(["git", "-C", str(ROOT), "log", "--merges", "--format=%s", "v0.3.0..HEAD"],
                             capture_output=True, text=True, check=True).stdout
        merged = {int(n) for n in re.findall(r"^Merge pull request #(\d+) ", log, re.M)}
        self.assertLessEqual(MERGED_SINCE_0_3_0, merged)
        self.assertLessEqual(merged, MERGED_SINCE_0_3_0 | {self.release_pull_request(merged)})

    @staticmethod
    def release_pull_request(merged):
        # The release pull request itself merges after this file is written.
        later = sorted(n for n in merged if n > max(MERGED_SINCE_0_3_0))
        return later[0] if len(later) == 1 else -1


class ManifestTest(unittest.TestCase):
    def test_release_files_are_in_the_sdist_template(self):
        lines = {line.strip() for line in read(ROOT / "MANIFEST.in").splitlines()}
        for line in ("include README.ru.md CHANGELOG.md LICENSE NOTICE",
                     "recursive-include fixtures *.json",
                     "recursive-include schemas *.json",
                     "include reports/ternary-check.json reports/ternary-check.html",
                     "recursive-include reports/ternary-check *.json",
                     "recursive-include ternary-check *.yml *.md *.sh",
                     "recursive-include conformance *.json *.md",
                     "recursive-include specs *.t27 *.md *.json"):
            self.assertIn(line, lines)


class ReadmeTest(unittest.TestCase):
    def test_result_table_is_the_report(self):
        for path, lang in ((ROOT / "README.md", "en"), (ROOT / "README.ru.md", "ru")):
            section = readme_section(path)
            self.assertIn(TABLE_START, section, path.name)
            table = section.split(TABLE_START, 1)[1].split(TABLE_END, 1)[0]
            lines = [line for line in table.strip().splitlines() if line.strip()]
            self.assertEqual(lines, result_table(lang), path.name)

    def test_totals_are_the_report(self):
        summary = json.loads(read(REPORT))["summary"]
        status = summary["status"]
        third_party = sum(summary["provenance"][key] for key in ("reference", "published", "upstream_encoder"))
        self.assertEqual((summary["cells"], status["match"], status["mismatch"], status["not-representable"],
                          third_party, summary["provenance"]["t27_round_trip"]), (36, 23, 4, 9, 12, 15))
        en, ru = readme_section(ROOT / "README.md"), readme_section(ROOT / "README.ru.md")
        self.assertIn("36 cells: 23 match, 4 mismatch, 9 not representable", en)
        self.assertIn("12 cells", en)
        self.assertIn("15 t27 round trips", en)
        self.assertIn("36 ячеек: 23 совпадают, 4 расходятся, 9 не представимы", ru)
        self.assertIn("12 ячеек", ru)
        self.assertIn("15 записей и чтений t27", ru)

    def test_findings_and_links(self):
        docs = read(ROOT / "docs" / "ternary-check.md")
        self.assertRegex(docs, r"(?m)^## Results$")
        for path in (ROOT / "README.md", ROOT / "README.ru.md"):
            section = readme_section(path)
            self.assertEqual(section.count("(docs/ternary-check.md#results)"), 4, path.name)
            self.assertIn("make ternary-check", section)
            for target in ("reports/ternary-check.html", "reports/ternary-check.json", "ternary-check/README.md",
                           "ternary-check/CONTRACT.md", "docs/ternary-check.md"):
                self.assertIn(f"]({target}", section, (path.name, target))
            for link in re.findall(r"\]\(([^)#:]+)(?:#[^)]*)?\)", section):
                self.assertTrue((ROOT / link).exists(), (path.name, link))
            badge = "actions/workflows/ternary-check-weekly.yml/badge.svg"
            self.assertIn(badge, read(path).split("\n## ", 1)[0], path.name)


class ReleaseSmokeWorkflowTest(unittest.TestCase):
    def test_triggers_matrix_and_runtime(self):
        text = read(SMOKE)
        self.assertRegex(text, r"(?m)^  release:\n    types: \[published\]$")
        self.assertRegex(text, r"(?m)^  workflow_dispatch:\n    inputs:\n      version:$")
        self.assertRegex(text, r"(?m)^permissions:\n  contents: read$")
        self.assertIn("os: [ubuntu-latest, macos-14, macos-15]", text)
        self.assertEqual(re.findall(r"uses: (\./\S*ternary-check)$", text, re.M),
                         ["./trinity-memory/ternary-check"] * 2)
        self.assertEqual(text.count("runtime: release"), 2)
        self.assertNotIn("secrets.", text)
        self.assertIn("decoder: python3 -m trinity_memory ternary-check", text)


if __name__ == "__main__":
    unittest.main()
