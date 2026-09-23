"""tools/upstream-drift.py against recorded API responses; no network.

tests/data/upstream-drift/responses-2026-09-23.json holds the GitHub and
Hugging Face answers the tool read on 2026-09-23 (`--record`, trimmed to the
fields it uses): every pinned blob was still at its branch head and every
model revision was still main. The other cases edit a copy of those answers.
"""
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
RECORDED = ROOT / "tests" / "data" / "upstream-drift" / "responses-2026-09-23.json"
TOOL = ROOT / "tools" / "upstream-drift.py"

spec = importlib.util.spec_from_file_location("upstream_drift", TOOL)
drift = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drift)


def responses():
    return json.loads(RECORDED.read_text(encoding="utf-8"))


def report_of(recorded):
    return drift.check(ROOT, drift.Fetcher(replay=recorded))


def pinned_files():
    upstream = json.loads((ROOT / drift.UPSTREAM_LOCK).read_text(encoding="utf-8"))
    llamacpp = json.loads((ROOT / drift.LLAMACPP_LOCK).read_text(encoding="utf-8"))
    return sum(len(pin["files"]) for pin in upstream["upstreams"].values()) + len(llamacpp["files"])


def url_with(recorded, fragment):
    matches = [url for url in recorded if fragment in url]
    assert len(matches) == 1, (fragment, matches)
    return matches[0]


class UpstreamDrift(unittest.TestCase):
    def test_recorded_heads_have_the_pinned_blobs(self):
        report = report_of(responses())
        self.assertEqual(report["schema"], "trinity.upstream-drift.v1")
        self.assertFalse(report["drift"])
        self.assertTrue(report["complete"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(drift.exit_status(report), 0)
        s = report["summary"]
        self.assertEqual(s["files"], pinned_files())
        self.assertEqual((s["files_changed"], s["files_missing"], s["files_unknown"]), (0, 0, 0))
        self.assertEqual((s["gitlinks"], s["gitlinks_moved"]), (1, 0))
        manifest = json.loads((ROOT / drift.MANIFEST).read_text(encoding="utf-8"))
        self.assertEqual((s["models"], s["models_moved"]), (len(manifest["models"]), 0))
        keys = {(u["lock"], u["key"]) for u in report["github"]}
        self.assertIn((drift.LLAMACPP_LOCK, "llama.cpp"), keys)
        self.assertIn((drift.UPSTREAM_LOCK, "bitnet.cpp-llama.cpp"), keys)
        for u in report["github"]:
            self.assertEqual(u["status"], "same", u["key"])
            self.assertNotIn("compare", u)
        # A head that moved past the pinned commit is not drift by itself.
        moved = [u for u in report["github"] if u["head"] != u["pinned_commit"]]
        self.assertTrue(moved)

    def test_report_is_deterministic(self):
        self.assertEqual(json.dumps(report_of(responses())), json.dumps(report_of(responses())))

    def test_changed_blob_is_drift_with_a_compare_link(self):
        recorded = responses()
        url = url_with(recorded, "ggml-org/llama.cpp/contents/ggml/src/ggml-quants.c")
        recorded[url]["json"]["sha"] = "0" * 40
        report = report_of(recorded)
        self.assertTrue(report["drift"])
        self.assertEqual(drift.exit_status(report), 1)
        # The file is pinned by both lock files; both report it.
        self.assertEqual(report["summary"]["files_changed"], 2)
        changed = [u for u in report["github"] if u["status"] == "drift"]
        self.assertEqual(sorted(u["lock"] for u in changed), sorted([drift.UPSTREAM_LOCK, drift.LLAMACPP_LOCK]))
        for u in changed:
            self.assertTrue(u["compare"].startswith(f"https://github.com/ggml-org/llama.cpp/compare/{u['pinned_commit']}..."))
            self.assertEqual([f["path"] for f in u["files"] if f["status"] == "changed"], ["ggml/src/ggml-quants.c"])
        self.assertIn("ggml-quants.c", drift.markdown(report))

    def test_missing_file_is_drift(self):
        recorded = responses()
        url = url_with(recorded, "ml-explore/mlx/contents/mlx/ops.cpp")
        recorded[url] = {"status": 404, "error": "HTTP 404"}
        report = report_of(recorded)
        self.assertTrue(report["drift"])
        self.assertEqual(report["summary"]["files_missing"], 1)
        self.assertTrue(report["complete"])

    def test_moved_gitlink_and_model_revision_are_drift(self):
        recorded = responses()
        url = url_with(recorded, "microsoft/BitNet/contents/3rdparty/llama.cpp")
        recorded[url]["json"]["sha"] = "1" * 40
        report = report_of(recorded)
        self.assertTrue(report["drift"])
        self.assertEqual(report["summary"]["gitlinks_moved"], 1)
        recorded = responses()
        url = url_with(recorded, "huggingface.co/api/models/prism-ml/Ternary-Bonsai-2-27B-gguf/")
        recorded[url]["json"]["sha"] = "2" * 40
        report = report_of(recorded)
        self.assertTrue(report["drift"])
        self.assertEqual(report["summary"]["models_moved"], 1)
        self.assertEqual([m["repo"] for m in report["huggingface"] if m["status"] == "moved"],
                         ["prism-ml/Ternary-Bonsai-2-27B-gguf"])

    def test_unreadable_head_is_incomplete_not_drift(self):
        recorded = responses()
        url = url_with(recorded, "huggingface/transformers/git/ref/heads/main")
        recorded[url] = {"status": 403, "error": "HTTP 403"}
        report = report_of(recorded)
        self.assertFalse(report["drift"])
        self.assertFalse(report["complete"])
        self.assertEqual(drift.exit_status(report), 2)
        transformers = [u for u in report["github"] if u["key"] == "transformers"][0]
        self.assertEqual(transformers["status"], "unknown")
        self.assertEqual({f["status"] for f in transformers["files"]}, {"unknown"})
        self.assertEqual(report["summary"]["files"], pinned_files())
        # Drift found elsewhere still wins over an unreadable head.
        url = url_with(recorded, "huggingface.co/api/models/microsoft/bitnet-b1.58-2B-4T-bf16/")
        recorded[url]["json"]["sha"] = "3" * 40
        self.assertEqual(drift.exit_status(report_of(recorded)), 1)

    def test_token_goes_to_the_github_api_only(self):
        sent = []

        class Reply:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(self.body).encode()

        def fake_urlopen(request, timeout):
            sent.append((request.full_url, request.get_header("Authorization")))
            return Reply({"sha": "a" * 40, "object": {"sha": "b" * 40}})

        fetch = drift.Fetcher(token="secret-token")
        with mock.patch.object(drift.urllib.request, "urlopen", fake_urlopen):
            fetch("https://api.github.com/repos/o/r/git/ref/heads/main")
            fetch("https://huggingface.co/api/models/o/r/revision/main")
            fetch("https://api.github.com/repos/o/r/git/ref/heads/main")  # answered from memory
        self.assertEqual(sent, [("https://api.github.com/repos/o/r/git/ref/heads/main", "Bearer secret-token"),
                                ("https://huggingface.co/api/models/o/r/revision/main", None)])
        self.assertNotIn("secret-token", json.dumps(fetch.recorded))

    def test_command_line_replay_writes_report_and_exit_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorded = responses()
            url = url_with(recorded, "microsoft/onnxruntime/contents/docs/ContribOperators.md")
            recorded[url]["json"]["sha"] = "4" * 40
            replay = Path(tmp) / "replay.json"
            replay.write_text(json.dumps(recorded), encoding="utf-8")
            output, summary = Path(tmp) / "drift.json", Path(tmp) / "summary.md"
            env = {key: value for key, value in os.environ.items() if key not in ("GH_TOKEN", "GITHUB_TOKEN")}
            result = subprocess.run([sys.executable, str(TOOL), "--replay", str(replay), "--output", str(output),
                                     "--summary", str(summary)], capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 1, result.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(report["drift"])
            self.assertTrue(report["run"]["replayed"])
            self.assertIn("## Upstream drift: drift", summary.read_text(encoding="utf-8"))
            clean = Path(tmp) / "clean.json"
            clean.write_text(json.dumps(responses()), encoding="utf-8")
            result = subprocess.run([sys.executable, str(TOOL), "--replay", str(clean), "--output", str(output)],
                                    capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            again = json.loads(output.read_text(encoding="utf-8"))
            again.pop("run")
            self.assertEqual(again, report_of(responses()))

    def test_every_request_of_a_run_was_recorded(self):
        recorded = responses()
        asked = []

        def fetch(url):
            asked.append(url)
            return drift.Fetcher(replay=recorded)(url)

        drift.check(ROOT, fetch)
        self.assertEqual(set(asked), set(recorded))
        for url in asked:
            self.assertTrue(url.startswith(("https://api.github.com/", "https://huggingface.co/api/")), url)


if __name__ == "__main__":
    unittest.main()
