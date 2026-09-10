#!/bin/sh
# Compile the same generated codec algorithms for the standalone browser UI.
set -eu
cd "$(dirname "$0")/.."
out=build/t27
clang=${WASM_CC:-clang}
if [ -n "${WASM_LD:-}" ]; then
    linker=$WASM_LD
elif command -v wasm-ld >/dev/null 2>&1; then
    linker=$(command -v wasm-ld)
else
    sysroot=$(rustc +1.94.0 --print sysroot)
    host=$(rustc +1.94.0 -vV | sed -n 's/^host: //p')
    linker="$sysroot/lib/rustlib/$host/bin/gcc-ld/wasm-ld"
fi
if "$clang" --print-targets | grep -q wasm32; then
    "$clang" --target=wasm32 -std=c11 -O2 -ffreestanding -fno-builtin -nostdlib -Wno-parentheses-equality -I "$out" -c native/wasm.c -o "$out/codecs.wasm.o"
else
    # Apple's clang omits wasm; Zig supplies the same C compilation target.
    zig cc -target wasm32-freestanding -std=c11 -O2 -ffreestanding -fno-builtin -nostdlib -Wno-parentheses-equality -I "$out" -c native/wasm.c -o "$out/codecs.wasm.o"
fi
"$linker" --no-entry --export=tm_encode --export=tm_decode --export=tm_wasm_values --export=tm_wasm_output --export-memory \
    --initial-memory=131072 --max-memory=131072 --stack-first -z stack-size=65536 "$out/codecs.wasm.o" -o "$out/codecs.wasm"
"${PYTHON:-python3}" tools/embed-wasm.py "$out/codecs.wasm" "$out/wasm_asset.c"
