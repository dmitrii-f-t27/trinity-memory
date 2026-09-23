#!/bin/sh
# Upstream-encoded TQ1_0/TQ2_0 cells of the Ternary Check matrix (issue #32).
# Fetches the pinned llama.cpp sources (sha256-checked, tests/upstream/
# llama.cpp.lock.json), extracts the reference quantizers, and stores the
# BitNet b1.58 2B4T layer-0 weights with them into build/upstream/matrix/,
# where python3 -m trinity_memory.ternary_check reads them with the t27
# decoders. Needs the generated headers of sh tools/build-t27.sh and the
# fixture ranges (python3 tools/fetch-fixtures.py).
#   TRINITY_UPSTREAM_OFFLINE=1  use the cached sources only (no download)
set -eu
cd "$(dirname "$0")/../.."
py=${PYTHON:-python3}
cc=${CC:-cc}
out=build/upstream/matrix
for header in build/t27/formats.h build/t27/json.h build/t27/SHA256SUMS; do
    [ -f "$header" ] || { echo "Missing $header: run sh tools/build-t27.sh first" >&2; exit 1; }
done
if command -v sha256sum > /dev/null 2>&1; then sha="sha256sum"; else sha="shasum -a 256"; fi
if ! awk '$2 ~ /^(t27\/[^\/]+\.t27|native\/|build\/t27\/[^\/]+\.h$|build\/t27\/libtrinity_memory_t27\.)/' build/t27/SHA256SUMS \
    | $sha -c - > /dev/null 2>&1; then
    echo "build/t27 does not match the t27 or native sources: run sh tools/build-t27.sh first" >&2
    exit 1
fi
mkdir -p "$out"
sh tools/fetch-upstream.sh tests/upstream/llama.cpp.lock.json
"$py" tests/upstream/extract_llamacpp_tq.py --output "$out/llamacpp_tq.c"
"$py" tests/upstream/llamacpp_fixtures.py --output "$out/fixtures"
warning_flags=
if "$cc" --version | grep -qi clang; then warning_flags=-Wno-parentheses-equality; fi
# The quantizers use float products and roundings only; -ffp-contract=off
# keeps a compiler from fusing them, so every host writes the same bytes.
# shellcheck disable=SC2086
"$cc" -std=c11 -O2 -ffp-contract=off $warning_flags -I "$out" -I tests/upstream -I build/t27 \
    tests/upstream/encode_llamacpp_tq.c -lm -o "$out/encode"
"$out/encode" "$out/fixtures" "$out"
