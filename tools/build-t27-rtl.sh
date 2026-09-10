#!/bin/sh
# Compiler generation and ABI wiring only; datapaths live in executable .t27.
set -eu
cd "$(dirname "$0")/.."
: "${T27_ROOT:?Set T27_ROOT to the pinned compiler checkout}"
compiler="$T27_ROOT/target/release/t27c"
out=build/t27/rtl
mkdir -p "$out/resources/generated"
"${PYTHON:-python3}" scripts/generate_rtl.py --compiler "$compiler" --output-directory "$out"
cat "$out/dot_stream.v" rtl/t27/dot_stream.v > "$out/resources/trinity_dot_stream.v"
sed 's/trinity_dot_stream #(/trinity_dot_stream_t27 #(/g' rtl/tb_dot_stream.v > "$out/resources/tb_dot_stream.v"
cp "$out/dense5_decoder.v" "$out/resources/generated/ternary_dense5_decoder.v"
cp "$out/baseline5_decoder.v" "$out/resources/ternary_baseline5_decoder.v"
