#!/bin/sh
# Ternary Check (issue #32) from a clean clone, in one command (make ternary-check):
#   1. fetch every fixture range of fixtures/manifest.json into build/fixtures
#      (strict: HTTP 206, exact length, sha256; a cache file the manifest does
#      not list fails);
#   2. build the t27 library with the compiler pinned by native/compiler.lock
#      ($T27_ROOT, or a fetch of gHashTag/t27 at the pin into build/compiler;
#      needs cargo +1.94.0);
#   3. store the BitNet layer-0 tensors with the pinned llama.cpp quantizers
#      (tests/upstream/run-llamacpp-matrix.sh; sources sha256-checked);
#   4. write reports/ternary-check.json, reports/ternary-check.html and
#      reports/ternary-check/repro/ (python3 -m trinity_memory.ternary_check).
# Step 4 writes the committed files (--write); with --check it compares with
# them instead. OFFLINE=1: no network at all; the fixture cache, the llama.cpp
# sources in build/upstream and the compiler checkout must already be there.
set -eu
cd "$(dirname "$0")/.."
py=${PYTHON:-python3}
offline=${OFFLINE:-0}
if [ "$offline" = 1 ]; then
    TRINITY_FIXTURES_OFFLINE=1
    TRINITY_UPSTREAM_OFFLINE=1
    export TRINITY_FIXTURES_OFFLINE TRINITY_UPSTREAM_OFFLINE
    "$py" tools/fetch-fixtures.py --offline
else
    "$py" tools/fetch-fixtures.py
fi
if [ -z "${T27_ROOT:-}" ]; then
    T27_ROOT=$(pwd)/build/compiler
    pin=$(cat native/compiler.lock)
    if [ ! -d "$T27_ROOT/.git" ]; then
        if [ "$offline" = 1 ]; then
            echo "OFFLINE=1: set T27_ROOT to a checkout of gHashTag/t27 at $pin (or fetch build/compiler once)" >&2
            exit 1
        fi
        git init -q "$T27_ROOT"
        git -C "$T27_ROOT" remote add origin https://github.com/gHashTag/t27.git
    fi
    if [ "$(git -C "$T27_ROOT" rev-parse -q --verify HEAD 2>/dev/null || true)" != "$pin" ]; then
        [ "$offline" = 1 ] && { echo "OFFLINE=1: $T27_ROOT is not at $pin" >&2; exit 1; }
        git -C "$T27_ROOT" fetch -q --depth 1 origin "$pin"
        git -C "$T27_ROOT" checkout -q --detach FETCH_HEAD
    fi
    export T27_ROOT
fi
sh tools/build-t27.sh
PYTHON=$py sh tests/upstream/run-llamacpp-matrix.sh
mode=--write
if [ "${1:-}" = --check ]; then mode=--check; fi
TRINITY_FIXTURES_OFFLINE=1 "$py" -m trinity_memory.ternary_check "$mode"
