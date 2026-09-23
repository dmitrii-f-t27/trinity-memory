#!/bin/sh
# llama.cpp issue 15193 harness (docs/upstream/llama.cpp-15193.md).
# Fetches the pinned llama.cpp sources (sha256-checked), extracts the TQ1_0 and
# TQ2_0 storage functions and CPU vec_dot kernels, and runs them against the
# t27 decoders and encoders in build/t27 (run sh tools/build-t27.sh first) on
# random blocks and on real layer-0 tensors, once per CPU variant the host can
# execute. Exits nonzero on any mismatch.
#   LLAMACPP_REQUIRE_X86=1  fail instead of skipping when a macOS host cannot
#                           run x86_64 binaries (no Rosetta 2)
set -eu
cd "$(dirname "$0")/../.."
py=${PYTHON:-python3}
cc=${CC:-cc}
out=build/upstream/15193
for header in build/t27/formats.h build/t27/json.h; do
    [ -f "$header" ] || { echo "Missing $header: run sh tools/build-t27.sh first" >&2; exit 1; }
done
mkdir -p "$out"
sh tools/fetch-upstream.sh tests/upstream/llama.cpp.lock.json
"$py" tests/upstream/extract_llamacpp_tq.py --output "$out/llamacpp_tq.c"
"$py" tests/upstream/llamacpp_fixtures.py --output "$out/fixtures"

# name|compiler flags|runner prefix
case "$(uname -s)-$(uname -m)" in
    Darwin-arm64)
        variants="arm64-dotprod|-arch arm64|
arm64-int16|-arch arm64 -march=armv8-a|
x86_64-avx2|-arch x86_64 -mavx2 -mfma -mf16c|arch -x86_64
x86_64-generic|-arch x86_64|arch -x86_64" ;;
    Linux-x86_64 | Darwin-x86_64)
        variants="x86_64-avx2|-mavx2 -mfma -mf16c|
x86_64-generic||" ;;
    Linux-aarch64 | Linux-arm64)
        variants="arm64-native||" ;;
    *)
        variants="host||" ;;
esac

warning_flags=
if "$cc" --version | grep -qi clang; then warning_flags=-Wno-parentheses-equality; fi
status=0
summary="$out/summary.txt"
: > "$summary"
printf '%s\n' "$variants" > "$out/variants.txt"
while IFS='|' read -r name flags runner; do
    case "$name" in
    x86_64-*)
        if [ "$(uname -s)" = Darwin ] && [ -n "$runner" ] && ! arch -x86_64 /usr/bin/true 2>/dev/null; then
            if [ "${LLAMACPP_REQUIRE_X86:-0}" = 1 ]; then
                echo "FAIL $name: this Mac cannot run x86_64 binaries" | tee -a "$summary"; status=1
            else
                echo "SKIP $name: this Mac cannot run x86_64 binaries (install Rosetta 2)" | tee -a "$summary"
            fi
            continue
        fi
        if [ "$name" = x86_64-avx2 ] && [ "$(uname -s)" = Linux ] && ! grep -qw avx2 /proc/cpuinfo; then
            echo "FAIL $name: the CPU does not report AVX2" | tee -a "$summary"; status=1
            continue
        fi ;;
    esac
    for part in test real; do
        binary="$out/$part-$name"
        # shellcheck disable=SC2086
        if ! "$cc" -std=c11 -O2 $warning_flags $flags -I "$out" -I tests/upstream -I build/t27 \
            "tests/upstream/${part}_llamacpp_tq.c" -lm -o "$binary" > "$binary.build.log" 2>&1; then
            cat "$binary.build.log" >&2
            echo "FAIL $name $part: build ($cc $flags)" >> "$summary"; status=1
            continue
        fi
        if [ "$part" = test ]; then set -- "$binary"; else set -- "$binary" "$out/fixtures"; fi
        # shellcheck disable=SC2086
        if $runner "$@" > "$binary.log" 2>&1; then result=PASS; else result=FAIL; status=1; fi
        cat "$binary.log"
        echo "$result $name $part ($cc $flags${runner:+; run with $runner})" >> "$summary"
    done
done < "$out/variants.txt"
echo "--- llama.cpp issue 15193 harness summary"
cat "$summary"
exit $status
