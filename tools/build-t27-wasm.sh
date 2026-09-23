#!/bin/sh
# Compile the same generated codec algorithms for the standalone browser UI,
# and the external format readers and writers with the real-layer product of
# t27/matvec.t27 and the matrix cells of t27/matrix.t27 (formats.wasm) for
# JavaScript consumers, tests/spec_formats_wasm_replay.mjs,
# tests/matvec_wasm.mjs and tests/matrix_wasm.mjs.
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
wasm_cc() {
    if "$clang" --print-targets | grep -q wasm32; then
        "$clang" --target=wasm32 -std=c11 -O2 -ffreestanding -fno-builtin -nostdlib -Wno-parentheses-equality "$@"
    else
        # Apple's clang omits wasm; Zig supplies the same C compilation target.
        zig cc -target wasm32-freestanding -std=c11 -O2 -ffreestanding -fno-builtin -nostdlib -Wno-parentheses-equality "$@"
    fi
}
wasm_cc -I "$out" -c native/wasm.c -o "$out/codecs.wasm.o"
"$linker" --no-entry --export=tm_encode --export=tm_decode --export=tm_wasm_values --export=tm_wasm_output --export-memory \
    --initial-memory=131072 --max-memory=131072 --stack-first -z stack-size=65536 "$out/codecs.wasm.o" -o "$out/codecs.wasm"
# formats.h includes <assert.h> for its test block; native/wasm-include has a
# freestanding stand-in. Memory grows to 4 GiB so whole real tensors fit.
# The readers zero-initialise local arrays, which clang lowers to a memset call
# unless bulk memory lets it emit memory.fill; LLVM enables bulk memory by
# default only from version 20, so older clang (18 on ubuntu-24.04) would leave
# memset undefined in this freestanding link. The feature set is spelled out
# (LLVM 18's generic CPU plus bulk memory) so that clang 18 on CI and clang 20
# or zig here compile the same module.
wasm_cc -mcpu=mvp -msign-ext -mmutable-globals -mbulk-memory -I native/wasm-include -I "$out" \
    -c native/wasm_formats.c -o "$out/formats.wasm.o"
formats_exports=
for name in tf_block_elements tf_block_bytes tf_scale_offset tf_scale_class tf_scales_check tf_b3_canonical \
    tf_decode_blocks tf_encode_blocks tf_block_flags tf_decode_i2s tf_encode_i2s tf_i2s_flags tf_i2s_trailer_nonzero \
    tf_decode_hf_packed tf_encode_hf_packed tf_decode_linear2 tf_encode_linear2 tf_decode_mlx2 tf_encode_mlx2 \
    tf_affine_check tf_affine_not_ternary tf_decode_onnx2 tf_encode_onnx2 tf_onnx2_padding_nonzero \
    tf_format_of_gguf tf_gguf_find tf_gguf_nth tf_gguf_check tf_gguf_tensor_bytes tf_wasm_info_size tf_wasm_info_field \
    tf_block_value tf_b3_padding_nonzero tf_safetensors_find tf_safetensors_check tf_wasm_json_token_size \
    tf_wasm_safe_info_size tf_wasm_safe_info_field tf_scale_value \
    tm_dot_i64 tmv_activations tmv_outside_ternary tmv_matvec tmv_max_abs tmv_mismatches_i64 tmv_mismatches_f64 \
    tmv_difference tmv_rows_nonzero tmv_residual tmv_scale_tensor tmv_scale_groups tmv_f16_scaled \
    tmv_exact_groups_f16 tmv_exact_mismatches \
    tmx_float_bits tmx_bf16_round tmx_scale_count tmx_scale_kind tmx_scale_group tmx_representable \
    tmx_convert_scales tmx_encode tmx_decode tmx_compare tmx_explain_ties tmx_round_trip tmx_bytes_differ \
    tmx_reencode tmx_locate tmx_mismatch_indices tmx_word_indices tmx_bits_per_weight tmx_gguf_metadata_bytes \
    tf_absmean_bf16; do
    formats_exports="$formats_exports --export=$name"
done
# shellcheck disable=SC2086
"$linker" --no-entry $formats_exports --export=__heap_base --export-memory \
    --initial-memory=4194304 --max-memory=4294967296 --stack-first -z stack-size=65536 "$out/formats.wasm.o" -o "$out/formats.wasm"
"${PYTHON:-python3}" tools/embed-wasm.py "$out/codecs.wasm" "$out/wasm_asset.c"
