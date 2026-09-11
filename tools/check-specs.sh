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
    printf '#include "%s.h"\n' "$module" > "$out/$module.main.c"
    # shellcheck disable=SC2086
    "$cc" $cflags -DT27_TEST_MAIN -I "$out" "$out/$module.main.c" -o "$out/$module-tests"
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
# Differential harness: spec constants against the executable implementation.
"$compiler" gen-c t27/codecs.t27 > "$out/codecs.h"
"$compiler" gen-c t27/container.t27 > "$out/container.h"
# shellcheck disable=SC2086
"$cc" $cflags -I "$out" tests/native_spec_types.c -o "$out/test-spec-types"
"$out/test-spec-types"
echo "PASS spec gate: $count spec(s) lexed, parsed, typechecked, generated (C, Verilog), tested, sealed; conformance validated"
