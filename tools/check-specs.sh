#!/bin/sh
# Specification gate. Every specs/**/*.t27 must lex without loss, parse
# completely, typecheck, generate C and Verilog on the pinned compiler, pass its
# executed test blocks and compile-time invariants, and match its saved seal.
# Conformance files must validate and stay in sync with their generator, and
# the differential harness ties the spec constants to the executable modules.
set -eu
cd "$(dirname "$0")/.."
: "${T27_ROOT:?Set T27_ROOT to a checkout of gHashTag/t27 at native/compiler.lock}"
T27_ROOT=$(cd "$T27_ROOT" && pwd)
pin=$(cat native/compiler.lock)
actual=$(git -C "$T27_ROOT" rev-parse HEAD)
if [ "$actual" != "$pin" ]; then
    echo "Compiler revision mismatch: expected $pin, got $actual" >&2
    exit 1
fi
compiler="$T27_ROOT/target/release/t27c"
if [ ! -x "$compiler" ]; then
    echo "Build the pinned compiler first: tools/build-t27.sh, or in \$T27_ROOT:" >&2
    echo "  cargo +1.94.0 build --locked --release -p t27c --bin t27c --target-dir target" >&2
    exit 1
fi
command -v iverilog >/dev/null 2>&1 || { echo "iverilog is required for the Verilog gate" >&2; exit 1; }
out=build/t27/specs
mkdir -p "$out"
cc=${CC:-cc}
warning_flags=
if "$cc" --version | grep -qi clang; then warning_flags=-Wno-parentheses-equality; fi
cflags="-std=c11 -Wall -Wextra -Werror $warning_flags -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer"

# lex-dropped exits zero even when characters were discarded: inspect its total.
"$compiler" lex-dropped --specs-dir specs > "$out/lexer.log" 2>&1
if ! grep -Eq '^[[:space:]]*0[[:space:]]+TOTAL across 0 spec\(s\)[[:space:]]*$' "$out/lexer.log"; then
    echo "Spec lexer discarded characters" >&2
    cat "$out/lexer.log" >&2
    exit 1
fi
specs=$(find specs -name '*.t27' | sort)
if [ -z "$specs" ]; then
    echo "No .t27 specifications under specs/" >&2
    exit 1
fi
count=0
# Pass 1: every spec must lex, parse, typecheck and generate C before any runner
# is built, because a spec's test runner includes the foundation header first.
for spec in $specs; do
    module=$(basename "$spec" .t27)
    "$compiler" parse-complete --show "$spec" > "$out/$module.parse.log" 2>&1
    if ! grep -q 'nothing discarded' "$out/$module.parse.log"; then
        cat "$out/$module.parse.log" >&2
        exit 1
    fi
    if ! "$compiler" typecheck "$spec" > "$out/$module.typecheck.log" 2>&1; then
        cat "$out/$module.typecheck.log" >&2
        exit 1
    fi
    "$compiler" gen-c "$spec" > "$out/$module.h.tmp"
    # An invariant that is not a constant expression is emitted as a comment,
    # never checked; an assert beginning with "(" is folded into a call.
    if grep -E 'TODO|ENTRY POINT REFUSED|not a C constant expression|^[[:space:]]*\(assert\(' "$out/$module.h.tmp"; then
        echo "Incomplete generated C: $spec" >&2
        exit 1
    fi
    mv "$out/$module.h.tmp" "$out/$module.h"
done
# Pass 2: execute the tests, generate Verilog and verify the seal of each spec.
for spec in $specs; do
    module=$(basename "$spec" .t27)
    {
        if [ "$module" != types ]; then echo '#include "types.h"'; fi
        echo '#define T27_TEST_MAIN'
        printf '#include "%s.h"\n' "$module"
    } > "$out/$module.main.c"
    # shellcheck disable=SC2086
    "$cc" $cflags -I "$out" "$out/$module.main.c" -o "$out/$module-tests"
    if ! "$out/$module-tests" > "$out/$module.tests.log" 2>&1; then
        cat "$out/$module.tests.log" >&2
        exit 1
    fi
    printf '%s: %s; %s static assertions\n' "$spec" "$(cat "$out/$module.tests.log")" "$(grep -c '_Static_assert' "$out/$module.h")"
    "$compiler" gen-verilog "$spec" > "$out/$module.v.tmp"
    if grep -E 'REFUSED|TODO' "$out/$module.v.tmp"; then
        echo "Incomplete generated Verilog: $spec" >&2
        exit 1
    fi
    mv "$out/$module.v.tmp" "$out/$module.v"
    iverilog -t null -o /dev/null "$out/$module.v"
    if ! "$compiler" seal --verify "$spec" > "$out/$module.seal.log" 2>&1; then
        cat "$out/$module.seal.log" >&2
        echo "Seal missing or stale for $spec: review the change, then run" >&2
        echo "  $compiler seal --save $spec   # and commit .trinity/seals/" >&2
        exit 1
    fi
    count=$((count + 1))
done
if ! "$compiler" validate-conformance --repo-root . > "$out/conformance.log" 2>&1; then
    cat "$out/conformance.log" >&2
    exit 1
fi
if grep -q 'WARN' "$out/conformance.log"; then
    echo "A conformance file has no vectors" >&2
    cat "$out/conformance.log" >&2
    exit 1
fi
"${PYTHON:-python3}" tools/generate-spec-vectors.py --check
# A seal alone does not pass for the format contracts: each spec under
# specs/formats needs its vectors (conformance/formats_<name>.json naming the
# spec) and a differential harness that includes its header; after the
# harnesses run, one of them must report replaying those vectors.
format_specs=
if [ -d specs/formats ]; then format_specs=$(find specs/formats -name '*.t27' | sort); fi
for spec in $format_specs; do
    name=$(basename "$spec" .t27)
    vectors="conformance/formats_$name.json"
    if [ ! -f "$vectors" ] || ! grep -q "\"spec_path\": \"$spec\"" "$vectors"; then
        echo "No vectors for $spec: generate $vectors with tools/generate-spec-vectors.py" >&2
        exit 1
    fi
    if ! grep -l "#include \"specs/$name.h\"" tests/native_spec_*.c >/dev/null; then
        echo "No differential harness includes specs/$name.h" >&2
        exit 1
    fi
done
# Differential harnesses: spec constants and rules against the executable
# implementation. Implementation headers are generated next to the spec headers
# (build/t27/specs/impl) so both can be included from one translation unit.
impl="$out/impl"
mkdir -p "$impl/specs"
for module in codecs container tensorpack json json_writer tensorpack_json bridge client compute formats; do
    "$compiler" gen-c "t27/$module.t27" > "$impl/$module.h"
done
# The dot pipeline source is generated to C as well, so the harness compares the
# spec against the same functions that produce the RTL.
"$compiler" gen-c t27/rtl/dot_stream.t27 > "$impl/rtl_dot_stream.h"
"$compiler" gen-c t27/rtl/stream_join.t27 > "$impl/rtl_stream_join.h"
for spec in $specs; do
    cp "$out/$(basename "$spec" .t27).h" "$impl/specs/"
done
cxx=${CXX:-c++}
case $(uname -s) in Darwin) crypto_flags= ;; *) crypto_flags="-lcrypto -ldl" ;; esac
"$cxx" -std=c++17 -Wall -Wextra -Werror -O1 -g -fPIC -fsanitize=address,undefined -c native/float.cpp -o "$out/float-spec.o"
"$cc" -std=c11 -Wall -Wextra -Werror -O1 -g -fPIC -fsanitize=address,undefined -c native/platform.c -o "$out/platform-spec.o"
for harness in tests/native_spec_*.c; do
    name=$(basename "$harness" .c)
    # shellcheck disable=SC2086
    "$cc" $cflags -I "$impl" -c "$harness" -o "$out/$name.o"
    # shellcheck disable=SC2086
    "$cxx" -fsanitize=address,undefined "$out/$name.o" "$out/float-spec.o" "$out/platform-spec.o" $crypto_flags -lm -o "$out/$name"
    "$out/$name" > "$out/$name.log" 2>&1 || { cat "$out/$name.log" >&2; exit 1; }
    cat "$out/$name.log"
done
for spec in $format_specs; do
    vectors="conformance/formats_$(basename "$spec" .t27).json"
    if ! grep -Eq "^replayed $vectors: [1-9][0-9]* vectors" "$out"/native_spec_*.log; then
        echo "No differential harness replayed $vectors" >&2
        exit 1
    fi
done
# Cycle traces: generate the RTL modules from their executable sources and
# replay every trace of the stream compute spec in Icarus Verilog.
mkdir -p "$out/rtl"
for module in dot_stream stream_storage stream_view stream_join; do
    "$compiler" gen-verilog "t27/rtl/$module.t27" > "$out/rtl/$module.v"
    if grep -E 'ENTRY POINT REFUSED|NO DATA PORTS|TODO' "$out/rtl/$module.v"; then
        echo "Incomplete generated RTL: t27/rtl/$module.t27" >&2
        exit 1
    fi
done
"${PYTHON:-python3}" tests/spec_stream_replay.py --rtl-dir "$out/rtl" --work "$out/traces" --output "$out/traces.json"
echo "PASS spec gate: $count spec(s) lexed, parsed, typechecked, generated (C, Verilog), tested, sealed; conformance validated; differential harnesses passed"
