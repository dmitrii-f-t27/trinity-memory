#!/usr/bin/env bash
# Runs `trinity-memory ternary-check run` for ternary-check/action.yml and sets its
# outputs. Inputs arrive as environment variables; VECTORS is split on
# whitespace and its globs expand relative to the working directory.
set -uo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
python=${PYTHON:-python3}
vectors=()
if [ -n "${VECTORS:-}" ]; then
    for word in $VECTORS; do vectors+=("$word"); done
else
    vectors=("$here"/../conformance/formats_*.json)
fi
summary=${SUMMARY:-}
if [ -z "$summary" ]; then
    summary=$(mktemp "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/ternary-check-summary.XXXXXX")
fi
args=(run --decoder "$DECODER" --vectors "${vectors[@]}" --report "$REPORT" --summary "$summary"
      --fail-on "${FAIL_ON:-mismatch}" --timeout "${TIMEOUT:-60}")
if [ -n "${FORMATS:-}" ]; then args+=(--formats "$FORMATS"); fi
export PYTHONPATH="${TERNARY_CHECK_PYTHONPATH:?set by resolve-runtime.sh}${PYTHONPATH:+:$PYTHONPATH}"
"$python" -m trinity_memory ternary-check "${args[@]}"
status=$?
if [ -n "${GITHUB_STEP_SUMMARY:-}" ] && [ -f "$summary" ]; then
    cat "$summary" >> "$GITHUB_STEP_SUMMARY"
fi
if [ -f "$REPORT" ]; then
    outputs=$("$python" - "$REPORT" <<'PY'
import json, sys
s = json.load(open(sys.argv[1], encoding="utf-8"))["summary"]
print(f"passed={str(s['passed']).lower()}")
print(f"cases={s['cases']}")
print(f"failures={s['failures']}")
print(f"mismatches={s['outcomes']['mismatch']}")
print(f"silent={s['outcomes']['silent']}")
PY
)
else
    outputs="passed=false"
fi
printf '%s\n' "$outputs"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
    { printf '%s\n' "$outputs"; echo "report=$REPORT"; echo "summary=$summary"; } >> "$GITHUB_OUTPUT"
fi
exit "$status"
