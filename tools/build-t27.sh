#!/bin/sh
# Rebuild native algorithms from a clean, explicitly pinned t27 compiler tree.
set -eu
cd "$(dirname "$0")/.."
: "${T27_ROOT:?Set T27_ROOT to a checkout of gHashTag/t27 at native/compiler.lock}"
T27_ROOT=$(cd "$T27_ROOT" && pwd)
export T27_ROOT
if [ -n "${CARGO_BUILD_TARGET:-}" ]; then
    echo "Unset CARGO_BUILD_TARGET: this build requires a host t27c executable" >&2
    exit 1
fi
out=build/t27
mkdir -p "$out"
# Remove only obsolete scratch outputs produced by the previous test runner.
rm -f "$out"/*.regenerated.h
pin=$(cat native/compiler.lock)
actual=$(git -C "$T27_ROOT" rev-parse HEAD)
if [ "$actual" != "$pin" ]; then
    echo "Compiler revision mismatch: expected $pin, got $actual" >&2
    exit 1
fi
if ! git -C "$T27_ROOT" diff --quiet HEAD -- bootstrap Cargo.toml Cargo.lock; then
    echo "Compiler checkout contains tracked modifications" >&2
    exit 1
fi
# Rebuild, including on local runs: HEAD alone cannot attest a preexisting binary.
if ! (cd "$T27_ROOT" && cargo +1.94.0 build --locked --release -p t27c --bin t27c --target-dir target) > "$out/compiler-build.log" 2>&1; then
    tail -80 "$out/compiler-build.log" >&2
    exit 1
fi
compiler="$T27_ROOT/target/release/t27c"
# parse-complete observes parser tokens, not characters lost by the lexer.
# lex-dropped exits zero even when characters were discarded: inspect its total.
"$compiler" lex-dropped --specs-dir t27 > "$out/lexer.log" 2>&1
if ! grep -Eq '^[[:space:]]*0[[:space:]]+TOTAL across 0 spec\(s\)[[:space:]]*$' "$out/lexer.log"; then
    echo "Source lexer discarded characters; refusing code generation" >&2
    cat "$out/lexer.log" >&2
    exit 1
fi
for module in codecs container compute sparsity json json_writer formats ternary_contract tensorpack tensorpack_json tensorpack_cli http fpga_link bridge client random matvec matrix rtl_driver experiments reports python_api cli; do
    "$compiler" parse-complete --show "t27/$module.t27" > "$out/$module.parse.log" 2>&1
    if ! grep -q 'nothing discarded' "$out/$module.parse.log"; then
        cat "$out/$module.parse.log" >&2
        exit 1
    fi
    "$compiler" gen-c "t27/$module.t27" > "$out/$module.h.tmp"
    if grep -E 'TODO|ENTRY POINT REFUSED' "$out/$module.h.tmp"; then
        echo "Incomplete generated C: $module" >&2
        exit 1
    fi
    mv "$out/$module.h.tmp" "$out/$module.h"
    # Extract declarations for separate OS translation units. No algorithm is
    # changed: the original generated header remains the implementation.
    awk '/Function implementations/ { exit }
        { if (NR > 2) print lines[(NR - 2) % 3]; lines[NR % 3] = $0 }
        END { print "#endif" }' "$out/$module.h" > "$out/$module.abi.h"
done
sh tools/build-t27-rtl.sh
sh tools/build-t27-wasm.sh
cc=${CC:-cc}
cxx=${CXX:-c++}
# Intentional word splitting permits SANITIZERS and platform CFLAGS from CI.
# shellcheck disable=SC2086
"$cc" -std=c11 -Wall -Wextra -Werror -O2 ${CFLAGS:-} -fPIC -I "$out" -c native/core.c -o "$out/core.o"
# shellcheck disable=SC2086
"$cc" -std=c11 -Wall -Wextra -Werror -O2 ${CFLAGS:-} -I "$out" -c native/host.c -o "$out/host.o"
# shellcheck disable=SC2086
"$cc" -std=c11 -Wall -Wextra -Werror -O2 ${CFLAGS:-} -fPIC -c native/platform.c -o "$out/platform.o"
# shellcheck disable=SC2086
"$cc" -std=c11 -Wall -Wextra -Werror -O2 ${CFLAGS:-} -fPIC -pthread -I "$out" -c native/runtime.c -o "$out/runtime.o"
"$cc" -std=c11 -Wall -Wextra -Werror -O2 ${CFLAGS:-} -fPIC -c native/process.c -o "$out/process.o"
"$cc" -std=c11 -Wall -Wextra -Werror -O2 ${CFLAGS:-} -fPIC -c "$out/wasm_asset.c" -o "$out/wasm_asset.o"
# shellcheck disable=SC2086
"$cxx" -std=c++17 -Wall -Wextra -Werror -O2 ${CXXFLAGS:-} -fPIC -c native/float.cpp -o "$out/float.o"
case $(uname -s) in
    Darwin) shared_flags=-dynamiclib; extension=dylib; crypto_flags= ;;
    *) shared_flags=-shared; extension=so; crypto_flags="-lcrypto -ldl" ;;
esac
# shellcheck disable=SC2086
"$cxx" ${LDFLAGS:-} "$out/host.o" "$out/core.o" "$out/float.o" "$out/platform.o" "$out/runtime.o" "$out/process.o" "$out/wasm_asset.o" $crypto_flags -lm -pthread -o "$out/trinity-memory-t27"
# shellcheck disable=SC2086
"$cxx" ${LDFLAGS:-} $shared_flags "$out/core.o" "$out/float.o" "$out/platform.o" "$out/runtime.o" "$out/process.o" "$out/wasm_asset.o" $crypto_flags -lm -pthread -o "$out/libtrinity_memory_t27.$extension"
printf '%s\n' "$pin" > "$out/compiler.revision"
"$cc" --version > "$out/c-compiler.txt"
"$cxx" --version > "$out/cxx-compiler.txt"
printf 'CFLAGS=%s\nCXXFLAGS=%s\nLDFLAGS=%s\n' "${CFLAGS:-}" "${CXXFLAGS:-}" "${LDFLAGS:-}" > "$out/build-flags.txt"
if command -v sha256sum >/dev/null 2>&1; then
    sha256sum t27/*.t27 t27/rtl/*.t27 native/*.c native/*.cpp native/*.h native/wasm-include/*.h native/compiler.lock tools/*.sh tools/*.py rtl/t27/*.v rtl/tb_dot_stream.v scripts/generate_rtl.py "$out"/*.h "$out"/codecs.wasm "$out"/formats.wasm "$out"/rtl/*.v "$out"/rtl/resources/*.v "$out"/rtl/resources/generated/*.v "$out/trinity-memory-t27" "$out/libtrinity_memory_t27.$extension" > "$out/SHA256SUMS"
    sha256sum < "$compiler" > "$out/compiler.sha256"
else
    shasum -a 256 t27/*.t27 t27/rtl/*.t27 native/*.c native/*.cpp native/*.h native/wasm-include/*.h native/compiler.lock tools/*.sh tools/*.py rtl/t27/*.v rtl/tb_dot_stream.v scripts/generate_rtl.py "$out"/*.h "$out"/codecs.wasm "$out"/formats.wasm "$out"/rtl/*.v "$out"/rtl/resources/*.v "$out"/rtl/resources/generated/*.v "$out/trinity-memory-t27" "$out/libtrinity_memory_t27.$extension" > "$out/SHA256SUMS"
    shasum -a 256 < "$compiler" > "$out/compiler.sha256"
fi
echo "Built $out/trinity-memory-t27 from executable .t27 sources"
