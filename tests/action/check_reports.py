#!/usr/bin/env python3
"""Checks the two reports of the Action self-test in .github/workflows/ci.yml.

  python3 tests/action/check_reports.py OWN.json WRONG.json

OWN.json (the reference decoder) must pass with every call ending as match or
rejected. WRONG.json (tests/action/wrong_group_decoder.py, PQ2_0 bytes read as
group-64 Q2_0) must fail, only on PQ2_0 calls, with at least one value
mismatch that names its first differing weight.
"""
import json
import sys


def load(path):
    with open(path, encoding="utf-8") as handle:
        report = json.load(handle)
    assert report["schema"] == "trinity.ternary-check-run.v1", path
    return report


def main(own_path, wrong_path):
    own, wrong = load(own_path), load(wrong_path)
    assert own["summary"]["passed"], f"{own_path}: the reference decoder failed"
    assert own["summary"]["cases"] > 0 and own["summary"]["failures"] == 0, own["summary"]
    outcomes = {case["outcome"] for case in own["cases"]}
    assert outcomes == {"match", "rejected"}, f"{own_path}: outcomes {sorted(outcomes)}"
    assert not wrong["summary"]["passed"], f"{wrong_path}: the wrong decoder passed"
    failing = [case for case in wrong["cases"] if case["fails"]]
    assert {case["format"] for case in failing} == {"PQ2_0"}, sorted({case["format"] for case in failing})
    mismatches = [case for case in failing if case["outcome"] == "mismatch" and case["values"]["differ"] > 0
                  and case["values"]["first"] >= 0]
    assert mismatches, f"{wrong_path}: no value mismatch reported"
    first = mismatches[0]
    print(f"reference decoder: {own['summary']['cases']} calls, all match or rejected; wrong decoder: "
          f"{len(failing)} failing PQ2_0 calls, {len(mismatches)} value mismatches, e.g. {first['id']} "
          f"first differs at weight {first['values']['first']}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
