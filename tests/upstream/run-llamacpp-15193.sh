#!/bin/sh
# llama.cpp issue 15193 harness (docs/upstream/llama.cpp-15193.md).
# Fetches the pinned llama.cpp sources (sha256-checked), extracts the TQ1_0 and
# TQ2_0 storage functions and CPU vec_dot kernels, and runs them against the
# t27 decoders and encoders in build/t27 (run sh tools/build-t27.sh first) on
# random blocks and on real layer-0 tensors, once per CPU variant the host can
# execute. Exits nonzero on any mismatch.
#   LLAMACPP_REQUIRE_X86=1  fail instead of skipping when a macOS host cannot
#                           run x86_64 binaries (no Rosetta 2) or cannot run
#                           AVX2 under Rosetta 2 (macOS before 15)
set -eu
cd "$(dirname "$0")/../.."
py=${PYTHON:-python3}
cc=${CC:-cc}
out=build/upstream/15193
for header in build/t27/formats.h build/t27/json.h build/t27/SHA256SUMS; do
    [ -f "$header" ] || { echo "Missing $header: run sh tools/build-t27.sh first" >&2; exit 1; }
done
# The generated headers and the library the fixture glue loads must match the
# current t27 and native sources: check their lines of build/t27/SHA256SUMS
# (written by tools/build-t27.sh) so a stale build cannot pass.
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

# name|compiler flags|runner prefix
# HARNESS_EXPECT_DOTPROD and HARNESS_EXPECT_AVX2 make the build fail
# (llamacpp_harness.h) when the compiler does not select the kernel path the
# variant name promises. Before LLVM 19, clang enabled the default CPU's
# extensions under -march=armv8-a (apple-m1 on a Mac, so DOTPROD);
# +nodotprod turns it off explicitly.
case "$(uname -s)-$(uname -m)" in
    Darwin-arm64)
        variants="arm64-dotprod|-arch arm64 -DHARNESS_EXPECT_DOTPROD=1|
arm64-int16|-arch arm64 -march=armv8-a+nodotprod -DHARNESS_EXPECT_DOTPROD=0|
x86_64-avx2|-arch x86_64 -mavx2 -mfma -mf16c -DHARNESS_EXPECT_AVX2=1|arch -x86_64
x86_64-generic|-arch x86_64 -DHARNESS_EXPECT_AVX2=0|arch -x86_64" ;;
    Linux-x86_64 | Darwin-x86_64)
        variants="x86_64-avx2|-mavx2 -mfma -mf16c -DHARNESS_EXPECT_AVX2=1|
x86_64-generic|-DHARNESS_EXPECT_AVX2=0|" ;;
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
        fi
        # Rosetta 2 translates AVX2, FMA and F16C only from macOS 15; before
        # that the binaries die on their first VEX instruction. Probe with the
        # instructions the kernels use.
        if [ "$name" = x86_64-avx2 ] && [ "$(uname -s)" = Darwin ] && [ -n "$runner" ]; then
            # shellcheck disable=SC2086
            if ! { "$cc" -O1 $flags -x c tests/upstream/avx2_probe.c -o "$out/avx2-probe" > "$out/avx2-probe.log" 2>&1 \
                   && $runner "$out/avx2-probe" >> "$out/avx2-probe.log" 2>&1; }; then
                if [ "${LLAMACPP_REQUIRE_X86:-0}" = 1 ]; then
                    echo "FAIL $name: AVX2 probe failed under Rosetta 2 on macOS $(sw_vers -productVersion) (Rosetta runs AVX2 from macOS 15; see $out/avx2-probe.log)" | tee -a "$summary"; status=1
                else
                    echo "SKIP $name: AVX2 probe failed under Rosetta 2 on macOS $(sw_vers -productVersion) (Rosetta runs AVX2 from macOS 15; see $out/avx2-probe.log)" | tee -a "$summary"
                fi
                continue
            fi
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
