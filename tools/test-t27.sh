#!/bin/sh
set -eu
# Sanitizer findings fail the gate: the C objects are built with
# -fno-sanitize-recover=all, and UBSan halts on its first report.
UBSAN_OPTIONS=${UBSAN_OPTIONS:-halt_on_error=1:print_stacktrace=1}
export UBSAN_OPTIONS
cd "$(dirname "$0")/.."
sh tools/build-t27.sh
cc=${CC:-cc}
cxx=${CXX:-c++}
warning_flags=
if "$cc" --version | grep -qi clang; then warning_flags=-Wno-parentheses-equality; fi
"$cxx" -std=c++17 -Wall -Wextra -Werror -O1 -g -fPIC -fsanitize=address,undefined -fno-sanitize-recover=all -c native/float.cpp -o build/t27/float-test.o
"$cc" -std=c11 -Wall -Wextra -Werror -O1 -g -fPIC -fsanitize=address,undefined -fno-sanitize-recover=all -c native/platform.c -o build/t27/platform-test.o
"$cc" -std=c11 -Wall -Wextra -Werror -O1 -g -fPIC -fsanitize=address,undefined -fno-sanitize-recover=all -c native/process.c -o build/t27/process-test.o
case $(uname -s) in Darwin) crypto_flags=; shared_flags=-dynamiclib; extension=dylib;; *) crypto_flags="-lcrypto -ldl"; shared_flags=-shared; extension=so;; esac
for module in codecs compute tensorpack json formats ternary_contract tensorpack_json tensorpack_cli http bridge client random matvec matrix rtl_driver; do
    # Test executables always keep assertions, ASan and UBSan enabled.
    # shellcheck disable=SC2086
    "$cc" -std=c11 -Wall -Wextra -Werror $warning_flags -O1 -g \
        -fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer -I build/t27 -I native \
        -c "tests/native_$module.c" -o "build/t27/test-$module.o"
    platform_object=
    if [ "$module" = bridge ]; then platform_object=build/t27/platform-test.o; fi
    if [ "$module" = rtl_driver ]; then platform_object=build/t27/process-test.o; fi
    "$cxx" -fsanitize=address,undefined -fno-sanitize-recover=all "build/t27/test-$module.o" build/t27/float-test.o $platform_object $crypto_flags -lm -o "build/t27/test-$module"
    "build/t27/test-$module" > "build/t27/test-$module.log" 2>&1 || {
        cat "build/t27/test-$module.log" >&2; exit 1;
    }
    cat "build/t27/test-$module.log"
done
# Run the integrated OS/network/experiment path with instrumented algorithms.
"$cc" -std=c11 -O1 -g -fPIC -fsanitize=address,undefined -fno-sanitize-recover=all -I build/t27 -I native -c native/core.c -o build/t27/core-test.o
"$cc" -std=c11 -O1 -g -fPIC -pthread -fsanitize=address,undefined -fno-sanitize-recover=all -I build/t27 -I native -c native/runtime.c -o build/t27/runtime-test.o
"$cc" -std=c11 -O1 -g -fsanitize=address,undefined -fno-sanitize-recover=all -I build/t27 -I native -c tests/native_experiments.c -o build/t27/experiments-test.o
"$cxx" -fsanitize=address,undefined -fno-sanitize-recover=all build/t27/experiments-test.o build/t27/core-test.o build/t27/runtime-test.o build/t27/platform-test.o build/t27/process-test.o build/t27/float-test.o build/t27/wasm_asset.o $crypto_flags -lm -pthread -o build/t27/test-experiments
build/t27/test-experiments
# A second generation must match byte-for-byte, without editing emitted C.
mkdir -p build/t27/regenerated
for source in t27/*.t27; do
    module=$(basename "$source" .t27)
    "$T27_ROOT/target/release/t27c" gen-c "t27/$module.t27" > "build/t27/regenerated/$module.h"
    cmp "build/t27/$module.h" "build/t27/regenerated/$module.h"
done
"$cc" -std=c11 -O2 -fPIC $warning_flags -I build/t27 -c tests/native_bridge.c -o build/t27/bridge-harness.o
"$cxx" $shared_flags build/t27/bridge-harness.o build/t27/float.o build/t27/platform.o $crypto_flags -lm -o "build/t27/bridge-harness.$extension"
"${PYTHON:-python3}" tests/native/test_bridge_parity.py --library "build/t27/bridge-harness.$extension" --output build/t27/bridge-parity.json
"${PYTHON:-python3}" tests/native/test_spec_bridge_vectors.py --library "build/t27/bridge-harness.$extension" --output build/t27/spec-bridge-vectors.json
"${PYTHON:-python3}" tests/native/test_cli_complete.py --binary build/t27/trinity-memory-t27 --output build/t27/cli-parity.json
node tests/native/test_wasm.mjs
node tests/spec_formats_wasm_replay.mjs
node tests/ternary_contract_wasm_replay.mjs
node tests/matvec_wasm.mjs
node tests/matrix_wasm.mjs
"${PYTHON:-python3}" -m unittest discover -s tests/native -v
"${PYTHON:-python3}" tests/t27_rtl.py --compiler "$T27_ROOT/target/release/t27c"
"${PYTHON:-python3}" tests/t27_storage.py --compiler "$T27_ROOT/target/release/t27c"
"${PYTHON:-python3}" -m unittest tests.test_bram_trit_packing -v
"${PYTHON:-python3}" -m unittest tests.test_ddr3_pattern tests.test_ddr3_uart_debug tests.test_ddr3_reader tests.test_uart_loader -v
"${PYTHON:-python3}" -m unittest tests.test_bridge_link tests.test_bridge_fpga -v
sh tools/check-specs.sh
echo "PASS native C regeneration and reference/CLI parity"
