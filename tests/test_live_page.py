"""The live page reads the findings ledger (tools/live-page.py, #53, #55)."""
import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("live_page", ROOT / "tools/live-page.py")
live_page = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live_page)

LEDGER = """# Findings
## F-1 — `Q2_0` files of a/own

- **status**: reported (link)
- **where**: `prism-ml/A-gguf` → `A-Q2_0.gguf`
- **answer**: —

## F-2 — `b/other`: written for x.cpp, not a defect

- **status**: withdrawn (as a defect report)
- **where**: `b/other` @ `abc` → `b.gguf`
- **why withdrawn**: `c/not-where` is only mentioned here
"""


def model(verdict, sha, legacy=True):
    problems = {"extent": {"fits": {"PQ2_0": 1}}} if legacy else {}
    return {"verdict": verdict, "ggml_types": [[42, 1]] if legacy else [[35, 1]], "problems": problems,
            "files": [{"file": "f.gguf", "lfs_sha256": sha}], "runtimes": {"llama.cpp": {"verdict": "refuses"}}}


SCAN = {"schema": "trinity.ternary-check-live.v2", "started": "2026-09-28T12:00", "summary": {}, "runtimes": {},
        "repositories": [
            {"repo": "prism-ml/A-gguf", "models": [model("refused", "aa")]},
            {"repo": "x/copy", "models": [model("refused", "aa")]},
            {"repo": "y/own", "models": [model("refused", "bb"), model("ok", "cc", legacy=False)]},
            {"repo": "b/other", "models": [model("refused", "dd")]}]}


class LivePage(unittest.TestCase):
    def setUp(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "findings.md"
            path.write_text(LEDGER)
            self.entries = live_page.ledger(path)

    def test_ledger_entries(self):
        self.assertEqual([(x["id"], x["status"], x["repos"]) for x in self.entries],
                         [("F-1", "reported", {"prism-ml/A-gguf"}), ("F-2", "withdrawn", {"b/other"})])
        self.assertEqual(self.entries[1]["anchor"], "f-2--bother-written-for-xcpp-not-a-defect")

    def test_withdrawn_refusal_is_shown_as_other_runtime(self):
        entry = live_page.ledger_entry(self.entries, "b/other", "refused")
        self.assertEqual(live_page.shown_verdict("refused", entry), "other_runtime")
        self.assertEqual(live_page.shown_verdict("refused", self.entries[0]), "refused")
        self.assertIsNone(live_page.ledger_entry(self.entries, "b/other", "ok"))
        self.assertEqual(live_page.badge_json(SCAN, "b/other", self.entries)["message"], "loads (named runtime)")
        self.assertEqual(live_page.badge_json(SCAN, "y/own", self.entries)["message"], "refused file")

    def test_legacy_counts_leave_out_withdrawn(self):
        self.assertEqual(live_page.legacy_q2_0(SCAN, self.entries),
                         {"files": 3, "third_party": 2, "repositories": 2, "copies": 1})

    def test_page_links_the_entry(self):
        page = live_page.render(SCAN, self.entries)
        self.assertIn("#f-2--bother-written-for-xcpp-not-a-defect\">F-2 withdrawn</a>", page)
        self.assertIn("3 files declare type 42", page)


if __name__ == "__main__":
    unittest.main()
