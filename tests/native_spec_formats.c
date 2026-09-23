/* Differential harness: the sealed contracts in specs/formats/ (one .t27 per family) must agree
 * with the executable implementation t27/formats.t27 on status classes, block
 * geometry, random inputs and every vector of conformance/formats_<family>.json.
 * tools/check-specs.sh generates the spec headers (specs/<family>.h), json.h
 * and formats.h into one include directory, compiles this file with -I pointing
 * at it and runs it from the repository root. No format algorithm is
 * maintained here: both sides are generated from t27. */
#include <assert.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
double tm_json_strtod(uint8_t *);
#include "specs/llama_cpp.h"
#include "specs/prismml.h"
#include "specs/bitnet_cpp.h"
#include "specs/hf_bitnet.h"
#include "specs/mlx.h"
#include "specs/onnx.h"
#include "json.h"
#include "formats.h"

static uint32_t random_state = 2709;
static uint32_t next_random(void) {
    random_state = random_state * UINT32_C(1664525) + UINT32_C(1013904223);
    return random_state >> 8;
}

enum { MAX_WEIGHTS = 4096, MAX_BYTES = 8192, MAX_WORDS = 64 };
static int32_t got[MAX_WEIGHTS], want[MAX_WEIGHTS], expected[MAX_WEIGHTS];
static uint8_t data[MAX_BYTES], out_a[MAX_BYTES], out_b[MAX_BYTES];
static uint32_t scales_a[MAX_WORDS], scales_b[MAX_WORDS];
static int64_t flags_a[TF_FLAG_COUNT], flags_b[TF_FLAG_COUNT];

static const char *const FLAG_TOKENS[TF_FLAG_COUNT] = {
    "outside_ternary", "noncanonical_base3", "scale_negative", "scale_zero",
    "trailer_nonzero", "padding_nonzero", "affine_not_ternary"};

/* ---- constants ------------------------------------------------------------ */
static void status_classes_and_geometry(void) {
    assert(TF_ERR_FORMAT == TFS_LLAMA_ERR_FORMAT && TF_ERR_LENGTH == TFS_LLAMA_ERR_LENGTH);
    assert(TF_ERR_CAPACITY == TFS_LLAMA_ERR_CAPACITY && TF_ERR_CODE == TFS_LLAMA_ERR_CODE);
    assert(TF_ERR_PADDING == TFS_LLAMA_ERR_PADDING && TF_ERR_TRUNCATED == TFS_LLAMA_ERR_TRUNCATED);
    assert(TF_ERR_CONTAINER == TFS_LLAMA_ERR_CONTAINER && TF_ERR_NOT_FOUND == TFS_LLAMA_ERR_NOT_FOUND);
    assert(TF_ERR_SCALE_NONFINITE == TFS_LLAMA_ERR_SCALE_NONFINITE);
    assert(TF_ERR_TYPE_AMBIGUOUS == TFS_LLAMA_ERR_TYPE_AMBIGUOUS);
    assert(TF_ERR_LAYOUT_UNSUPPORTED == TFS_LLAMA_ERR_LAYOUT_UNSUPPORTED);
    assert(TF_ERR_MISALIGNED == TFS_LLAMA_ERR_MISALIGNED && TF_ERR_EXTENT == TFS_LLAMA_ERR_EXTENT);
    assert(TF_ERR_FORMAT == TFS_PRISM_ERR_FORMAT && TF_ERR_LENGTH == TFS_PRISM_ERR_LENGTH);
    assert(TF_ERR_CAPACITY == TFS_PRISM_ERR_CAPACITY && TF_ERR_CODE == TFS_PRISM_ERR_CODE);
    assert(TF_ERR_SCALE_NONFINITE == TFS_PRISM_ERR_SCALE_NONFINITE && TF_ERR_PADDING == TFS_PRISM_ERR_PADDING);
    assert(TF_ERR_CONTAINER == TFS_HF_ERR_CONTAINER && TF_ERR_EXTENT == TFS_HF_ERR_EXTENT);
    assert(TF_ERR_LENGTH == TFS_BITNET_ERR_LENGTH && TF_ERR_CAPACITY == TFS_BITNET_ERR_CAPACITY);
    assert(TF_ERR_CODE == TFS_BITNET_ERR_CODE && TF_ERR_SCALE_NONFINITE == TFS_BITNET_ERR_SCALE_NONFINITE);
    assert(TF_ERR_LENGTH == TFS_HF_ERR_LENGTH && TF_ERR_CAPACITY == TFS_HF_ERR_CAPACITY);
    assert(TF_ERR_CODE == TFS_HF_ERR_CODE && TF_ERR_SCALE_NONFINITE == TFS_HF_ERR_SCALE_NONFINITE);
    assert(TF_ERR_LENGTH == TFS_MLX_ERR_LENGTH && TF_ERR_CAPACITY == TFS_MLX_ERR_CAPACITY);
    assert(TF_ERR_CODE == TFS_MLX_ERR_CODE && TF_ERR_SCALE_NONFINITE == TFS_MLX_ERR_SCALE_NONFINITE);
    assert(TF_ERR_LENGTH == TFS_ONNX_ERR_LENGTH && TF_ERR_CAPACITY == TFS_ONNX_ERR_CAPACITY);
    assert(TF_ERR_CODE == TFS_ONNX_ERR_CODE && TF_ERR_SCALE_NONFINITE == TFS_ONNX_ERR_SCALE_NONFINITE);
    assert(TF_FLAG_COUNT == TFS_LLAMA_FLAG_COUNT && TF_FLAG_COUNT == TFS_PRISM_FLAG_COUNT);
    assert(TF_FLAG_COUNT == TFS_BITNET_FLAG_COUNT);
    assert(TF_FLAG_OUTSIDE_TERNARY == TFS_LLAMA_FLAG_OUTSIDE_TERNARY);
    assert(TF_FLAG_NONCANONICAL_BASE3 == TFS_LLAMA_FLAG_NONCANONICAL_BASE3);
    assert(TF_FLAG_SCALE_NEGATIVE == TFS_LLAMA_FLAG_SCALE_NEGATIVE && TF_FLAG_SCALE_ZERO == TFS_LLAMA_FLAG_SCALE_ZERO);
    assert(TF_FLAG_NONCANONICAL_BASE3 == TFS_PRISM_FLAG_NONCANONICAL_BASE3);
    assert(TF_FLAG_TRAILER_NONZERO == TFS_BITNET_FLAG_TRAILER_NONZERO);
    assert(TF_FLAG_SCALE_NEGATIVE == TFS_HF_FLAG_SCALE_NEGATIVE && TF_FLAG_SCALE_ZERO == TFS_MLX_FLAG_SCALE_ZERO);
    assert(TF_FLAG_AFFINE_NOT_TERNARY == TFS_MLX_FLAG_AFFINE_NOT_TERNARY);
    assert(TF_FLAG_PADDING_NONZERO == TFS_ONNX_FLAG_PADDING_NONZERO);
    assert(TF_FLAG_OUTSIDE_TERNARY == TFS_ONNX_FLAG_OUTSIDE_TERNARY && TF_FLAG_OUTSIDE_TERNARY == TFS_HF_FLAG_OUTSIDE_TERNARY);
    /* Format identifiers and block geometry. */
    assert(TF_TQ1_0 == TFS_LLAMA_TQ1_0 && TF_TQ2_0 == TFS_LLAMA_TQ2_0 && TF_Q2_0 == TFS_LLAMA_Q2_0);
    assert(TF_Q1_0 == TFS_LLAMA_Q1_0 && TF_PQ2_0 == TFS_LLAMA_PQ2_0 && TF_PTQ1_0 == TFS_LLAMA_PTQ1_0);
    assert(TF_Q2_0 == TFS_PRISM_Q2_0 && TF_PQ2_0 == TFS_PRISM_PQ2_0 && TF_PTQ1_0 == TFS_PRISM_PTQ1_0);
    assert(TF_I2_S == TFS_LLAMA_I2_S && TF_I2_S == TFS_BITNET_I2_S && TF_HF_PACKED == TFS_HF_PACKED);
    assert(TF_LINEAR2 == TFS_MLX_LINEAR2 && TF_ONNX2 == TFS_ONNX_NBITS2);
    assert(TF_KIND_F16 == TFS_MLX_KIND_F16 && TF_KIND_BF16 == TFS_MLX_KIND_BF16);
    assert(TF_KIND_F16 == TFS_ONNX_KIND_F16 && TF_KIND_BF16 == TFS_ONNX_KIND_BF16 && TF_KIND_F32 == TFS_ONNX_KIND_F32);
    for (int32_t f = -1; f <= 12; ++f) {
        bool prism = f == TF_PQ2_0 || f == TF_PTQ1_0;
        size_t per = prism ? tfs_prism_block_weights(f) : tfs_llama_block_weights(f);
        size_t bytes = prism ? tfs_prism_block_bytes(f) : tfs_llama_block_bytes(f);
        size_t at = prism ? tfs_prism_scale_at(f) : tfs_llama_scale_at(f);
        assert(tf_block_elements(f) == per && tf_block_bytes(f) == bytes);
        if (per) assert(tf_scale_offset(f) == at);
    }
    assert(tfs_prism_block_weights(TF_Q2_0) == tf_block_elements(TF_Q2_0));
    assert(tfs_prism_block_bytes(TF_Q2_0) == tf_block_bytes(TF_Q2_0));
    for (size_t n = 128; n <= 4096; n += 128) assert(tf_i2s_bytes(n) == tfs_bitnet_bytes(n));
}

/* ---- exhaustive scale classes and base-3 bytes ---------------------------- */
static void scale_classes_and_base3(void) {
    for (uint32_t w = 0; w < 65536; ++w) {
        int32_t f16 = tf_scale_class(w, TF_KIND_F16);
        assert(f16 == tfs_llama_f16_class(w) && f16 == tfs_prism_f16_class(w));
        assert(f16 == tfs_mlx_class(w, TFS_MLX_KIND_F16) && f16 == tfs_onnx_scale_class(w, TFS_ONNX_KIND_F16));
        int32_t bf16 = tf_scale_class(w, TF_KIND_BF16);
        assert(bf16 == tfs_hf_bf16_class(w) && bf16 == tfs_mlx_class(w, TFS_MLX_KIND_BF16));
        assert(bf16 == tfs_onnx_scale_class(w, TFS_ONNX_KIND_BF16));
        uint32_t f32 = (w << 16) | (next_random() & 0xffff);
        assert(tf_scale_class(f32, TF_KIND_F32) == tfs_bitnet_f32_class(f32));
        assert(tf_scale_class(f32, TF_KIND_F32) == tfs_onnx_scale_class(f32, TFS_ONNX_KIND_F32));
    }
    const uint32_t specials[] = {0, 0x80000000u, 0x7f800000u, 0xff800000u, 0x7fc00000u, 1, 0x80000001u};
    for (size_t i = 0; i < sizeof specials / sizeof specials[0]; ++i)
        assert(tf_scale_class(specials[i], TF_KIND_F32) == tfs_bitnet_f32_class(specials[i]));
    for (uint32_t b = 0; b < 256; ++b) {
        for (uint32_t digits = 4; digits <= 5; ++digits) {
            bool canonical = tf_b3_canonical(b, digits);
            assert(canonical == tfs_llama_b3_canonical(b, digits) && canonical == tfs_prism_b3_canonical(b, digits));
        }
        for (uint32_t n = 0; n < 5; ++n)
            assert(tf_b3_value(b, n) == tfs_llama_b3_trit(b, n) && tf_b3_value(b, n) == tfs_prism_b3_trit(b, n));
        bool padded = tf_b3_padding_nonzero(b);
        assert(padded == tfs_llama_b3_padded(b) && padded == tfs_prism_b3_padded(b));
        if (tf_b3_canonical(b, 4)) assert(!padded);
    }
}

/* ---- random blocks --------------------------------------------------------- */
static int64_t spec_block_decode(int32_t format, bool prism, const uint8_t *bytes, size_t size, size_t count,
                                 int32_t *values, size_t capacity, uint32_t *scales, size_t scale_capacity) {
    if (prism) return tfs_prism_decode(format, (uint8_t *)bytes, size, count, values, capacity, scales, scale_capacity);
    return tfs_llama_decode(format, (uint8_t *)bytes, size, count, values, capacity, scales, scale_capacity);
}

static int64_t spec_block_flags(int32_t format, bool prism, const uint8_t *bytes, size_t size, size_t count, int64_t *flags) {
    if (prism) return tfs_prism_flags(format, (uint8_t *)bytes, size, count, flags);
    return tfs_llama_flags(format, (uint8_t *)bytes, size, count, flags);
}

static int64_t spec_block_encode(int32_t format, bool prism, int32_t *values, size_t count, uint32_t *scales,
                                 size_t scale_count, uint8_t *out, size_t capacity) {
    if (prism) return tfs_prism_encode(format, values, count, scales, scale_count, out, capacity);
    return tfs_llama_encode(format, values, count, scales, scale_count, out, capacity);
}

static void random_blocks(void) {
    static const struct { int32_t format; bool prism; } cases[] = {
        {TF_TQ1_0, false}, {TF_TQ2_0, false}, {TF_Q2_0, false}, {TF_Q1_0, false},
        {TF_Q2_0, true}, {TF_PQ2_0, true}, {TF_PTQ1_0, true}};
    for (size_t c = 0; c < sizeof cases / sizeof cases[0]; ++c) {
        int32_t format = cases[c].format;
        bool prism = cases[c].prism;
        size_t per = tf_block_elements(format), bytes = tf_block_bytes(format), blocks = 5;
        for (int round = 0; round < 300; ++round) {
            for (size_t i = 0; i < blocks * bytes; ++i) data[i] = (uint8_t)next_random();
            size_t count = blocks * per;
            int64_t a = tf_decode_blocks(format, data, blocks * bytes, count, got, MAX_WEIGHTS, scales_a, MAX_WORDS);
            int64_t b = spec_block_decode(format, prism, data, blocks * bytes, count, want, MAX_WEIGHTS, scales_b, MAX_WORDS);
            assert(a == b);
            if (a >= 0) {
                assert(memcmp(got, want, count * sizeof got[0]) == 0);
                assert(memcmp(scales_a, scales_b, blocks * sizeof scales_a[0]) == 0);
            }
            assert(tf_block_flags(format, data, blocks * bytes, count, flags_a)
                   == spec_block_flags(format, prism, data, blocks * bytes, count, flags_b));
            assert(memcmp(flags_a, flags_b, sizeof flags_a) == 0);
            /* Lengths, capacities and truncation agree too. */
            size_t size = blocks * bytes - (size_t)(next_random() % 3);
            size_t n = count - (size_t)(next_random() % 2);
            size_t capacity = count - (size_t)(next_random() % 2);
            assert(tf_decode_blocks(format, data, size, n, got, capacity, scales_a, blocks)
                   == spec_block_decode(format, prism, data, size, n, want, capacity, scales_b, blocks));
            /* Encoders: the same bytes for every allowed code, the same status otherwise. */
            for (size_t i = 0; i < count; ++i) {
                int32_t v = (int32_t)(next_random() % 4) - 1;
                if (format == TF_Q1_0) v = v >= 0 ? 1 : -1;
                if (format == TF_TQ1_0 || format == TF_PTQ1_0) v = v > 1 ? 1 : v;
                got[i] = v;
            }
            if (round % 7 == 0) got[next_random() % count] = (int32_t)(next_random() % 5) - 2;
            for (size_t i = 0; i < blocks; ++i) scales_a[i] = next_random() & 0xffff;
            memset(out_a, 0xA5, sizeof out_a);
            memset(out_b, 0xA5, sizeof out_b);
            int64_t ea = tf_encode_blocks(format, got, count, scales_a, blocks, out_a, sizeof out_a);
            int64_t eb = spec_block_encode(format, prism, got, count, scales_a, blocks, out_b, sizeof out_b);
            assert(ea == eb && memcmp(out_a, out_b, sizeof out_a) == 0);
            assert(tf_encode_blocks(format, got, count, scales_a, blocks, out_a, blocks * bytes - 1)
                   == spec_block_encode(format, prism, got, count, scales_a, blocks, out_b, blocks * bytes - 1));
        }
    }
}

/* ---- random tensors -------------------------------------------------------- */
static void random_tensors(void) {
    uint32_t sa, sb;
    for (int round = 0; round < 300; ++round) {
        /* I2_S */
        size_t count = 128 * (1 + next_random() % 6), size = count / 4 + 32;
        for (size_t i = 0; i < size; ++i) data[i] = (uint8_t)next_random();
        if (round % 3) { data[count / 4 + 3] &= 0x3f; }
        int64_t a = tf_decode_i2s(data, size, count, got, MAX_WEIGHTS, &sa);
        assert(a == tfs_bitnet_decode(data, size, count, want, MAX_WEIGHTS, &sb));
        if (a >= 0) assert(sa == sb && memcmp(got, want, count * sizeof got[0]) == 0);
        assert(tf_i2s_flags(data, size, count, flags_a) == tfs_bitnet_flags(data, size, count, flags_b));
        assert(memcmp(flags_a, flags_b, sizeof flags_a) == 0);
        assert(tf_decode_i2s(data, size - 1, count, got, MAX_WEIGHTS, &sa) == tfs_bitnet_decode(data, size - 1, count, want, MAX_WEIGHTS, &sb));
        for (size_t i = 0; i < count; ++i) got[i] = (int32_t)(next_random() % 3) - 1;
        if (round % 5 == 0) got[next_random() % count] = 2;
        memset(out_a, 0x5A, sizeof out_a);
        memset(out_b, 0x5A, sizeof out_b);
        uint32_t scale = next_random();
        int64_t ea = tf_encode_i2s(got, count, scale, out_a, sizeof out_a);
        int64_t eb = tfs_bitnet_encode(0, got, 1, count, scale, out_b, sizeof out_b);
        assert(ea == eb && memcmp(out_a, out_b, size) == 0);

        /* HF packed */
        size_t rows = 4 * (1 + next_random() % 6), cols = 1 + next_random() % 9;
        for (size_t i = 0; i < rows / 4 * cols; ++i) data[i] = (uint8_t)next_random();
        a = tf_decode_hf_packed(data, rows / 4 * cols, rows, cols, got, MAX_WEIGHTS);
        assert(a == tfs_hf_decode(data, rows / 4 * cols, rows, cols, want, MAX_WEIGHTS));
        assert(a >= 0 && memcmp(got, want, rows * cols * sizeof got[0]) == 0);
        size_t bad_rows = rows + next_random() % 3;
        assert(tf_decode_hf_packed(data, rows / 4 * cols, bad_rows, cols, got, MAX_WEIGHTS)
               == tfs_hf_decode(data, rows / 4 * cols, bad_rows, cols, want, MAX_WEIGHTS));
        for (size_t i = 0; i < rows * cols; ++i) got[i] = (int32_t)(next_random() % 3) - 1;
        if (round % 5 == 0) got[next_random() % (rows * cols)] = -2;
        memset(out_a, 0x5A, sizeof out_a);
        memset(out_b, 0x5A, sizeof out_b);
        assert(tf_encode_hf_packed(got, rows, cols, out_a, sizeof out_a) == tfs_hf_encode(got, rows, cols, out_b, sizeof out_b));
        assert(memcmp(out_a, out_b, sizeof out_a) == 0);

        /* MLX */
        size_t group = (size_t)32 << (next_random() % 3);
        cols = group * (1 + next_random() % 3);
        rows = 1 + next_random() % 3;
        if (rows * cols > MAX_WEIGHTS) rows = 1;
        for (size_t i = 0; i < rows * cols / 4; ++i) data[i] = (uint8_t)next_random();
        size_t try_group = round % 11 == 0 ? 48 : group;
        a = tf_decode_mlx2(data, rows * cols / 4, rows, cols, try_group, got, MAX_WEIGHTS);
        assert(a == tfs_mlx_decode(data, rows * cols / 4, rows, cols, try_group, want, MAX_WEIGHTS));
        if (a >= 0) assert(memcmp(got, want, rows * cols * sizeof got[0]) == 0);
        size_t groups = rows * cols / group;
        for (size_t g = 0; g < groups; ++g) {
            scales_a[g] = next_random() & 0xffff;
            scales_b[g] = next_random() % 4 ? scales_a[g] ^ 0x8000 : next_random() & 0xffff;
        }
        int32_t kind = round % 2 ? TF_KIND_F16 : TF_KIND_BF16;
        memset(flags_a, 0, sizeof flags_a);
        memset(flags_b, 0, sizeof flags_b);
        assert(tf_affine_check(scales_a, scales_b, groups, kind, flags_a)
               == tfs_mlx_affine_check(scales_a, scales_b, groups, kind, flags_b));
        assert(memcmp(flags_a, flags_b, sizeof flags_a) == 0);
        for (size_t i = 0; i < rows * cols; ++i) got[i] = (int32_t)(next_random() % 4) - 1;
        if (round % 5 == 0) got[next_random() % (rows * cols)] = 3;
        memset(out_a, 0x5A, sizeof out_a);
        memset(out_b, 0x5A, sizeof out_b);
        assert(tf_encode_mlx2(got, rows, cols, group, out_a, sizeof out_a) == tfs_mlx_encode(got, rows, cols, group, out_b, sizeof out_b));
        assert(memcmp(out_a, out_b, sizeof out_a) == 0);

        /* ONNX */
        size_t bs = (size_t)16 << (next_random() % 3), k = 1 + next_random() % 200, n = 1 + next_random() % 4;
        size_t row = tfs_onnx_row_bytes(k, bs), zrow = tfs_onnx_zp_row_bytes(k, bs);
        if (n * k > MAX_WEIGHTS) n = 1;
        uint8_t zp[64];
        for (size_t i = 0; i < n * row; ++i) data[i] = (uint8_t)next_random();
        for (size_t i = 0; i < n * zrow; ++i) zp[i] = (uint8_t)next_random();
        size_t zp_size = round % 2 ? n * zrow : 0;
        size_t try_bs = round % 13 == 0 ? 24 : bs;
        a = tf_decode_onnx2(data, n * row, n, k, try_bs, zp, zp_size, got, MAX_WEIGHTS);
        assert(a == tfs_onnx_decode(data, n * row, n, k, try_bs, zp, zp_size, want, MAX_WEIGHTS));
        if (a >= 0) assert(memcmp(got, want, n * k * sizeof got[0]) == 0);
        assert(tf_onnx2_padding_nonzero(data, n * row, n, k, bs) == tfs_onnx_padding_nonzero(data, n * row, n, k, bs));
        assert(tf_decode_onnx2(data, n * row, n, k, bs, zp, zp_size + 1, got, MAX_WEIGHTS)
               == tfs_onnx_decode(data, n * row, n, k, bs, zp, zp_size + 1, want, MAX_WEIGHTS));
        if (a >= 0) {
            if (round % 5 == 0) got[next_random() % (n * k)] = -3;
            memset(out_a, 0x5A, sizeof out_a);
            memset(out_b, 0x5A, sizeof out_b);
            int64_t ea2 = tf_encode_onnx2(got, n, k, bs, zp, zp_size, out_a, sizeof out_a);
            assert(ea2 == tfs_onnx_encode(got, n, k, bs, zp, zp_size, out_b, sizeof out_b));
            assert(memcmp(out_a, out_b, sizeof out_a) == 0);
        }
    }
}

/* ---- GGUF type ids --------------------------------------------------------- */
static void gguf_type_rule(void) {
    for (uint32_t t = 0; t < 300; ++t)
        for (int m = 0; m < 4; ++m) {
            bool prism = m & 1, bitnet = m & 2;
            assert(tf_format_of_gguf(t, prism, bitnet) == tfs_llama_gguf_format(t, prism, bitnet));
        }
    /* tf_gguf_check's offset and extent rules on synthetic records: a Q2_0
     * tensor of `rows` rows of 64 weights (18 bytes each). */
    for (uint64_t offset = 0; offset < 300; offset += 7)
        for (uint64_t next = 0; next < 400; next += 33)
            for (int has_next = 0; has_next < 2; ++has_next)
                for (uint64_t file = 200; file < 800; file += 150) {
                    uint64_t rows = 1 + offset % 5, bytes = 18 * rows;
                    TFTensorInfo info;
                    memset(&info, 0, sizeof info);
                    info.tensor_type = 42; info.dims = 2; info.d0 = 64; info.d1 = rows; info.d2 = 1; info.d3 = 1;
                    info.alignment = 32; info.data_start = 96; info.offset = offset;
                    info.has_next = has_next && next >= offset; info.next_offset = info.has_next ? next : 0;
                    int32_t spec = tfs_llama_gguf_offset_status(offset, 32, bytes, info.has_next, info.next_offset);
                    if (spec == 0) spec = tfs_llama_gguf_file_status(96, offset, bytes, file);
                    if (spec == 0) spec = TF_Q2_0;
                    assert(tf_gguf_check(&info, file) == spec);
                }
}

/* ---- JSON vectors ---------------------------------------------------------- */
enum { TOKENS = 200000, ARENA = 4 << 20, FILE_BYTES = 4 << 20 };
static TMJsonToken tokens[TOKENS];
static uint8_t arena[ARENA], file_bytes[FILE_BYTES];
static size_t token_count;

static int64_t member(int64_t object, const char *key) {
    return tm_json_field(tokens, token_count, object, (uint8_t *)key, strlen(key));
}

static int64_t need(int64_t object, const char *key) {
    int64_t at = member(object, key);
    if (at < 0) { fprintf(stderr, "missing %s\n", key); abort(); }
    return at;
}

static int64_t integer(int64_t object, const char *key) { return tokens[need(object, key)].integer; }
static bool boolean(int64_t object, const char *key) {
    int64_t at = member(object, key);
    return at >= 0 && tokens[at].kind == TM_JSON_TRUE;
}
static bool text_is(int64_t at, const char *text) {
    return tokens[at].kind == TM_JSON_STRING && tokens[at].text_size == strlen(text)
           && memcmp(tokens[at].text, text, tokens[at].text_size) == 0;
}

static int hex_digit(uint8_t c) {
    if (c >= '0' && c <= '9') return c - '0';
    assert(c >= 'a' && c <= 'f');
    return c - 'a' + 10;
}

static size_t hex(int64_t object, const char *key, uint8_t *outp, size_t capacity) {
    int64_t at = need(object, key);
    size_t n = tokens[at].text_size / 2;
    assert(n <= capacity && tokens[at].text_size % 2 == 0);
    for (size_t i = 0; i < n; ++i)
        outp[i] = (uint8_t)(hex_digit(tokens[at].text[2 * i]) * 16 + hex_digit(tokens[at].text[2 * i + 1]));
    return n;
}

static size_t values_hex(int64_t object, const char *key, int32_t *values) {
    static uint8_t raw[MAX_WEIGHTS];
    size_t n = hex(object, key, raw, sizeof raw);
    for (size_t i = 0; i < n; ++i) values[i] = (int8_t)raw[i];
    return n;
}

static size_t words(int64_t object, const char *key, uint32_t *outp) {
    size_t n = 0;
    for (int64_t at = tokens[need(object, key)].first; at >= 0; at = tokens[at].next) {
        assert(n < MAX_WORDS);
        outp[n++] = (uint32_t)tokens[at].uinteger;
    }
    return n;
}

static int32_t format_id(int64_t vector) {
    static const struct { const char *name; int32_t id; } names[] = {
        {"TQ1_0", TF_TQ1_0}, {"TQ2_0", TF_TQ2_0}, {"Q2_0", TF_Q2_0}, {"Q1_0", TF_Q1_0},
        {"PQ2_0", TF_PQ2_0}, {"PTQ1_0", TF_PTQ1_0}};
    int64_t at = need(vector, "format");
    for (size_t i = 0; i < sizeof names / sizeof names[0]; ++i)
        if (text_is(at, names[i].name)) return names[i].id;
    abort();
}

static int32_t kind_of(int64_t vector) {
    int64_t at = need(vector, "scale_kind");
    if (text_is(at, "F16")) return TF_KIND_F16;
    if (text_is(at, "BF16")) return TF_KIND_BF16;
    assert(text_is(at, "F32"));
    return TF_KIND_F32;
}

static void check_flags(int64_t expect, const int64_t *flags) {
    int64_t object = need(expect, "flags");
    for (size_t i = 0; i < TF_FLAG_COUNT; ++i) assert(integer(object, FLAG_TOKENS[i]) == flags[i]);
}

static void check_values(int64_t expect, const int32_t *values, size_t count) {
    size_t n = values_hex(expect, "values_hex", expected);
    assert(n == count && memcmp(values, expected, count * sizeof values[0]) == 0);
}

static void document_constants(int64_t root) {
    int64_t constants = need(root, "constants");
    int64_t errors = need(constants, "errors");
    static const struct { const char *token; int32_t status; } table[] = {
        {"format", TF_ERR_FORMAT}, {"length", TF_ERR_LENGTH}, {"capacity", TF_ERR_CAPACITY}, {"code", TF_ERR_CODE},
        {"padding", TF_ERR_PADDING}, {"truncated", TF_ERR_TRUNCATED}, {"container", TF_ERR_CONTAINER},
        {"not_found", TF_ERR_NOT_FOUND}, {"scale_nonfinite", TF_ERR_SCALE_NONFINITE},
        {"type_ambiguous", TF_ERR_TYPE_AMBIGUOUS}, {"layout_unsupported", TF_ERR_LAYOUT_UNSUPPORTED},
        {"misaligned", TF_ERR_MISALIGNED}, {"extent", TF_ERR_EXTENT}};
    size_t listed = 0;
    for (int64_t at = tokens[errors].first; at >= 0; at = tokens[at].next) ++listed;
    assert(listed == sizeof table / sizeof table[0]);
    for (size_t i = 0; i < sizeof table / sizeof table[0]; ++i) assert(integer(errors, table[i].token) == table[i].status);
    int64_t flags = need(constants, "flags");
    for (size_t i = 0; i < TF_FLAG_COUNT; ++i) assert(integer(flags, FLAG_TOKENS[i]) == (int64_t)i);
    int64_t ids = need(constants, "format_ids");
    assert(integer(ids, "TQ1_0") == TF_TQ1_0 && integer(ids, "PTQ1_0") == TF_PTQ1_0 && integer(ids, "I2_S") == TF_I2_S);
    assert(integer(ids, "HF_PACKED") == TF_HF_PACKED && integer(ids, "LINEAR2") == TF_LINEAR2 && integer(ids, "ONNX2") == TF_ONNX2);
}

/* The status class a vector declares must be the status it expects. */
static int32_t error_status(int64_t vector) {
    static const struct { const char *token; int32_t status; } table[] = {
        {"format", TF_ERR_FORMAT}, {"length", TF_ERR_LENGTH}, {"capacity", TF_ERR_CAPACITY}, {"code", TF_ERR_CODE},
        {"padding", TF_ERR_PADDING}, {"truncated", TF_ERR_TRUNCATED}, {"container", TF_ERR_CONTAINER},
        {"not_found", TF_ERR_NOT_FOUND}, {"scale_nonfinite", TF_ERR_SCALE_NONFINITE},
        {"type_ambiguous", TF_ERR_TYPE_AMBIGUOUS}, {"layout_unsupported", TF_ERR_LAYOUT_UNSUPPORTED},
        {"misaligned", TF_ERR_MISALIGNED}, {"extent", TF_ERR_EXTENT}};
    int64_t at = member(vector, "error_class");
    if (at < 0) return 0;
    for (size_t i = 0; i < sizeof table / sizeof table[0]; ++i)
        if (text_is(at, table[i].token)) return table[i].status;
    abort();
}

static void block_vector(int64_t vector, bool prism) {
    int32_t format = format_id(vector);
    size_t count = (size_t)integer(vector, "count");
    size_t size = hex(vector, "data_hex", data, sizeof data);
    int64_t expect = need(vector, "expect");
    int64_t status = integer(expect, "status");
    int64_t a = tf_decode_blocks(format, data, size, count, got, MAX_WEIGHTS, scales_a, MAX_WORDS);
    int64_t b = spec_block_decode(format, prism, data, size, count, want, MAX_WEIGHTS, scales_b, MAX_WORDS);
    assert(a == status && b == status);
    if (error_status(vector)) assert(status == error_status(vector));
    int64_t fa = tf_block_flags(format, data, size, count, flags_a);
    int64_t fb = spec_block_flags(format, prism, data, size, count, flags_b);
    assert(fa == fb && memcmp(flags_a, flags_b, sizeof flags_a) == 0);
    if (status < 0) {
        assert(fa == status);
        /* Silent output: the trits of the upstream loop, which reads neither
         * the scale nor a padding digit, from both the implementation and the
         * spec, and the raw scale words. */
        for (int64_t entry = tokens[need(vector, "silent_output")].first; entry >= 0; entry = tokens[entry].next) {
            if (member(entry, "values_hex") < 0) continue;
            size_t per = tf_block_elements(format), bytes = tf_block_bytes(format);
            assert(size == count / per * bytes);
            for (size_t e = 0; e < count; ++e) {
                got[e] = tf_block_value(format, data, e / per * bytes, e % per);
                want[e] = prism ? tfs_prism_weight(format, data, e / per * bytes, e % per)
                                : tfs_llama_weight(format, data, e / per * bytes, e % per);
            }
            check_values(entry, got, count);
            assert(memcmp(got, want, count * sizeof got[0]) == 0);
            size_t n = words(entry, "scale_words", scales_b);
            assert(n == count / per);
            for (size_t blk = 0; blk < n; ++blk)
                assert(scales_b[blk] == ((uint32_t)data[blk * bytes + tf_scale_offset(format)]
                                         | (uint32_t)data[blk * bytes + tf_scale_offset(format) + 1] << 8));
        }
        return;
    }
    assert(fa == 0);
    check_values(expect, got, count);
    assert(memcmp(got, want, count * sizeof got[0]) == 0);
    size_t blocks = words(expect, "scale_words", scales_b);
    assert(memcmp(scales_a, scales_b, blocks * sizeof scales_a[0]) == 0);
    check_flags(expect, flags_a);
    if (boolean(vector, "encode")) {
        memset(out_a, 0, sizeof out_a);
        memset(out_b, 0, sizeof out_b);
        assert(tf_encode_blocks(format, got, count, scales_a, blocks, out_a, sizeof out_a) == (int64_t)size);
        assert(spec_block_encode(format, prism, got, count, scales_a, blocks, out_b, sizeof out_b) == (int64_t)size);
        assert(memcmp(out_a, data, size) == 0 && memcmp(out_b, data, size) == 0);
    }
    int64_t intended = member(vector, "intended");
    if (intended >= 0) {
        int32_t intended_format = format_id(intended);
        size_t intended_count = (size_t)integer(intended, "count");
        size_t n = values_hex(intended, "values_hex", expected);
        assert(n == intended_count);
        static int32_t intent[MAX_WEIGHTS];
        static uint32_t intent_scales[MAX_WORDS];
        memcpy(intent, expected, n * sizeof intent[0]);
        size_t per = tf_block_elements(intended_format);
        size_t sw = words(intended, "scale_words", intent_scales);
        assert(sw == n / per);
        if (member(intended, "data_hex") >= 0) {
            /* Corruption: the original bytes decode to the intended values; the
             * corrupted bytes decode without error to other values or scales. */
            static uint8_t original[MAX_BYTES];
            size_t original_size = hex(intended, "data_hex", original, sizeof original);
            assert(original_size == size && memcmp(original, data, size) != 0);
            assert(tf_decode_blocks(intended_format, original, size, n, want, MAX_WEIGHTS, scales_b, MAX_WORDS) >= 0);
            assert(memcmp(want, intent, n * sizeof want[0]) == 0 && memcmp(scales_b, intent_scales, sw * sizeof scales_b[0]) == 0);
            assert(memcmp(intent, got, n * sizeof got[0]) != 0 || memcmp(intent_scales, scales_a, sw * sizeof scales_a[0]) != 0);
        } else {
            /* Layout confusion: the intended layout reproduces the same bytes
             * from other values. */
            assert(tf_encode_blocks(intended_format, intent, n, intent_scales, sw, out_a, sizeof out_a) == (int64_t)size);
            assert(memcmp(out_a, data, size) == 0);
            assert(memcmp(intent, got, (n < count ? n : count) * sizeof got[0]) != 0);
        }
    }
}

static void block_encode_vector(int64_t vector, bool prism) {
    int32_t format = format_id(vector);
    size_t count = values_hex(vector, "values_hex", got);
    assert(count == (size_t)integer(vector, "count"));
    size_t n = words(vector, "scale_words", scales_a);
    int64_t status = integer(need(vector, "expect"), "status");
    memset(out_a, 0x77, sizeof out_a);
    memset(out_b, 0x77, sizeof out_b);
    assert(tf_encode_blocks(format, got, count, scales_a, n, out_a, sizeof out_a) == status);
    assert(spec_block_encode(format, prism, got, count, scales_a, n, out_b, sizeof out_b) == status);
    assert(status == error_status(vector));
    for (size_t i = 0; i < sizeof out_a; ++i) assert(out_a[i] == 0x77 && out_b[i] == 0x77);
}

static uint64_t spec_tensor_bytes(int32_t format, uint64_t ne0, uint64_t count) {
    if (format == TF_I2_S) return tfs_bitnet_bytes((size_t)count);
    if (format == TF_PQ2_0 || format == TF_PTQ1_0) {
        uint64_t per = tfs_prism_block_weights(format);
        return ne0 % per ? 0 : count / per * tfs_prism_block_bytes(format);
    }
    return tfs_llama_tensor_bytes(format, ne0, count);
}

static void gguf_vector(int64_t vector) {
    static uint8_t g[4096];
    size_t size = hex(vector, "gguf_hex", g, sizeof g);
    int64_t name = need(vector, "tensor");
    int64_t expect = need(vector, "expect");
    TFTensorInfo info;
    assert(tf_gguf_find(g, size, tokens[name].text, tokens[name].text_size, &info) == integer(expect, "find"));
    assert(info.tensor_type == (uint32_t)integer(expect, "ggml_type") && info.offset == (uint64_t)integer(expect, "offset"));
    assert(info.prism == boolean(expect, "prism") && info.bitnet == boolean(expect, "bitnet"));
    assert(info.alignment == (uint64_t)integer(expect, "alignment") && info.next_offset == (uint64_t)integer(expect, "next_offset"));
    assert(info.has_next == boolean(expect, "has_next") && info.data_start == (uint64_t)integer(expect, "data_start"));
    uint64_t file_size = (uint64_t)integer(vector, "file_size");
    int64_t check = integer(expect, "check");
    assert(tf_gguf_check(&info, file_size) == check);
    /* The same decision from the spec's rules. */
    int32_t spec = tfs_llama_gguf_format(info.tensor_type, info.prism, info.bitnet);
    if (spec >= 0) {
        int32_t aligned = tfs_llama_gguf_offset_status(info.offset, info.alignment, 0, false, 0);
        uint64_t bytes = spec > 0 ? spec_tensor_bytes(spec, info.d0, info.d0 * info.d1 * info.d2 * info.d3) : 0;
        if (aligned) spec = aligned;
        else if (spec > 0 && bytes == 0) spec = TFS_LLAMA_ERR_LENGTH;
        else if (spec > 0) {
            int32_t extent = tfs_llama_gguf_offset_status(info.offset, info.alignment, bytes, info.has_next, info.next_offset);
            if (!extent) extent = tfs_llama_gguf_file_status(info.data_start, info.offset, bytes, file_size);
            if (extent) spec = extent;
            if (member(expect, "tensor_bytes") >= 0) assert(bytes == (uint64_t)integer(expect, "tensor_bytes"));
        }
    }
    assert(spec == check);
    if (error_status(vector)) assert(check == error_status(vector));
}

static void i2s_vector(int64_t vector) {
    size_t count = (size_t)integer(vector, "count");
    size_t size = hex(vector, "data_hex", data, sizeof data);
    int64_t expect = need(vector, "expect");
    int64_t status = integer(expect, "status");
    uint32_t sa = 0, sb = 0;
    assert(tf_decode_i2s(data, size, count, got, MAX_WEIGHTS, &sa) == status);
    assert(tfs_bitnet_decode(data, size, count, want, MAX_WEIGHTS, &sb) == status);
    int64_t fa = tf_i2s_flags(data, size, count, flags_a), fb = tfs_bitnet_flags(data, size, count, flags_b);
    assert(fa == fb && memcmp(flags_a, flags_b, sizeof flags_a) == 0);
    if (error_status(vector)) assert(status == error_status(vector));
    if (status < 0) {
        for (int64_t entry = tokens[need(vector, "silent_output")].first; entry >= 0; entry = tokens[entry].next) {
            if (member(entry, "values_hex") < 0) continue;
            /* The codes do not depend on the scale: read them with a scale of 1.0. */
            assert(status == TF_ERR_SCALE_NONFINITE);
            static uint8_t patched[MAX_BYTES];
            memcpy(patched, data, size);
            patched[count / 4] = 0; patched[count / 4 + 1] = 0; patched[count / 4 + 2] = 0x80; patched[count / 4 + 3] = 0x3f;
            assert(tf_decode_i2s(patched, size, count, got, MAX_WEIGHTS, &sa) >= 0);
            check_values(entry, got, count);
            uint32_t word = (uint32_t)data[count / 4] | (uint32_t)data[count / 4 + 1] << 8
                            | (uint32_t)data[count / 4 + 2] << 16 | (uint32_t)data[count / 4 + 3] << 24;
            assert(word == (uint32_t)integer(entry, "scale_word"));
        }
        return;
    }
    check_values(expect, got, count);
    assert(memcmp(got, want, count * sizeof got[0]) == 0 && sa == sb);
    assert(sa == (uint32_t)integer(expect, "scale_word"));
    check_flags(expect, flags_a);
    if (boolean(vector, "encode")) {
        assert(tf_encode_i2s(got, count, sa, out_a, sizeof out_a) == (int64_t)size && memcmp(out_a, data, size) == 0);
        assert(tfs_bitnet_encode(0, got, 1, count, sa, out_b, sizeof out_b) == (int64_t)size && memcmp(out_b, data, size) == 0);
    }
    int64_t intended = member(vector, "intended");
    if (intended >= 0) {
        int64_t layout_at = need(intended, "layout");
        uint32_t layout = text_is(layout_at, "arm") ? 1 : 2;
        size_t rows = (size_t)integer(intended, "rows"), cols = (size_t)integer(intended, "cols");
        size_t n = values_hex(intended, "values_hex", expected);
        static int32_t intent[MAX_WEIGHTS];
        memcpy(intent, expected, n * sizeof intent[0]);
        assert(n == count && rows * cols == count);
        assert(tfs_bitnet_encode(layout, intent, rows, cols, sa, out_b, sizeof out_b) == (int64_t)size);
        assert(memcmp(out_b, data, size) == 0 && memcmp(intent, got, count * sizeof got[0]) != 0);
    }
}

static void i2s_encode_vector(int64_t vector) {
    size_t count = values_hex(vector, "values_hex", got);
    uint32_t scale = (uint32_t)integer(vector, "scale_word");
    int64_t status = integer(need(vector, "expect"), "status");
    memset(out_a, 0x77, sizeof out_a);
    memset(out_b, 0x77, sizeof out_b);
    assert(tf_encode_i2s(got, count, scale, out_a, sizeof out_a) == status);
    assert(tfs_bitnet_encode(0, got, 1, count, scale, out_b, sizeof out_b) == status);
    assert(status == error_status(vector));
    for (size_t i = 0; i < sizeof out_a; ++i) assert(out_a[i] == 0x77 && out_b[i] == 0x77);
}

static void hf_vector(int64_t vector) {
    size_t rows = (size_t)integer(vector, "rows"), cols = (size_t)integer(vector, "cols");
    size_t size = hex(vector, "data_hex", data, sizeof data);
    uint32_t scale = (uint32_t)integer(vector, "scale_word");
    int64_t expect = need(vector, "expect");
    int64_t status = integer(expect, "status");
    int64_t a = tf_decode_hf_packed(data, size, rows, cols, got, MAX_WEIGHTS);
    assert(a == status && tfs_hf_decode(data, size, rows, cols, want, MAX_WEIGHTS) == status);
    if (status < 0) { assert(status == error_status(vector)); return; }
    memset(flags_a, 0, sizeof flags_a);
    int64_t scale_status = tf_scales_check(&scale, 1, kind_of(vector), flags_a);
    int32_t spec_class = tfs_hf_bf16_class(scale);
    assert(scale_status == integer(expect, "scale_status") && scale_status == (spec_class < 0 ? spec_class : 0));
    if (error_status(vector)) assert(scale_status == error_status(vector));
    flags_a[TF_FLAG_OUTSIDE_TERNARY] = a;
    check_values(expect, got, rows * cols);
    assert(memcmp(got, want, rows * cols * sizeof got[0]) == 0);
    check_flags(expect, flags_a);
    if (boolean(vector, "encode")) {
        assert(tf_encode_hf_packed(got, rows, cols, out_a, sizeof out_a) == (int64_t)size && memcmp(out_a, data, size) == 0);
        assert(tfs_hf_encode(got, rows, cols, out_b, sizeof out_b) == (int64_t)size && memcmp(out_b, data, size) == 0);
    }
}

static void hf_encode_vector(int64_t vector) {
    size_t rows = (size_t)integer(vector, "rows"), cols = (size_t)integer(vector, "cols");
    size_t count = values_hex(vector, "values_hex", got);
    assert(count == rows * cols);
    int64_t status = integer(need(vector, "expect"), "status");
    memset(out_a, 0x77, sizeof out_a);
    memset(out_b, 0x77, sizeof out_b);
    assert(tf_encode_hf_packed(got, rows, cols, out_a, sizeof out_a) == status);
    assert(tfs_hf_encode(got, rows, cols, out_b, sizeof out_b) == status && status == error_status(vector));
    for (size_t i = 0; i < sizeof out_a; ++i) assert(out_a[i] == 0x77 && out_b[i] == 0x77);
}

static void mlx_vector(int64_t vector) {
    size_t rows = (size_t)integer(vector, "rows"), cols = (size_t)integer(vector, "cols");
    size_t group = (size_t)integer(vector, "group");
    size_t size = hex(vector, "data_hex", data, sizeof data);
    int64_t expect = need(vector, "expect");
    int64_t status = integer(expect, "status");
    int64_t a = tf_decode_mlx2(data, size, rows, cols, group, got, MAX_WEIGHTS);
    assert(a == status && tfs_mlx_decode(data, size, rows, cols, group, want, MAX_WEIGHTS) == status);
    if (status < 0) { assert(status == error_status(vector)); return; }
    size_t n = words(vector, "scale_words", scales_a);
    assert(words(vector, "bias_words", scales_b) == n && n == rows * cols / group);
    int32_t kind = kind_of(vector);
    memset(flags_a, 0, sizeof flags_a);
    memset(flags_b, 0, sizeof flags_b);
    int64_t affine = tf_affine_check(scales_a, scales_b, n, kind, flags_a);
    assert(affine == tfs_mlx_affine_check(scales_a, scales_b, n, kind, flags_b) && affine == integer(expect, "affine_status"));
    assert(memcmp(flags_a, flags_b, sizeof flags_a) == 0);
    if (error_status(vector)) assert(affine == error_status(vector));
    flags_a[TF_FLAG_OUTSIDE_TERNARY] = a;
    check_values(expect, got, rows * cols);
    assert(memcmp(got, want, rows * cols * sizeof got[0]) == 0);
    check_flags(expect, flags_a);
    if (boolean(vector, "encode")) {
        assert(tf_encode_mlx2(got, rows, cols, group, out_a, sizeof out_a) == (int64_t)size && memcmp(out_a, data, size) == 0);
        assert(tfs_mlx_encode(got, rows, cols, group, out_b, sizeof out_b) == (int64_t)size && memcmp(out_b, data, size) == 0);
    }
}

static void mlx_encode_vector(int64_t vector) {
    size_t rows = (size_t)integer(vector, "rows"), cols = (size_t)integer(vector, "cols");
    size_t group = (size_t)integer(vector, "group");
    size_t count = values_hex(vector, "values_hex", got);
    assert(count == rows * cols);
    int64_t status = integer(need(vector, "expect"), "status");
    memset(out_a, 0x77, sizeof out_a);
    memset(out_b, 0x77, sizeof out_b);
    assert(tf_encode_mlx2(got, rows, cols, group, out_a, sizeof out_a) == status);
    assert(tfs_mlx_encode(got, rows, cols, group, out_b, sizeof out_b) == status && status == error_status(vector));
    for (size_t i = 0; i < sizeof out_a; ++i) assert(out_a[i] == 0x77 && out_b[i] == 0x77);
}

static void onnx_vector(int64_t vector) {
    size_t n = (size_t)integer(vector, "n"), k = (size_t)integer(vector, "k");
    size_t bs = (size_t)integer(vector, "block_size");
    size_t size = hex(vector, "data_hex", data, sizeof data);
    static uint8_t zp[256];
    size_t zp_size = hex(vector, "zero_points_hex", zp, sizeof zp);
    int64_t expect = need(vector, "expect");
    int64_t status = integer(expect, "status");
    int64_t a = tf_decode_onnx2(data, size, n, k, bs, zp, zp_size, got, MAX_WEIGHTS);
    assert(a == status && tfs_onnx_decode(data, size, n, k, bs, zp, zp_size, want, MAX_WEIGHTS) == status);
    if (status < 0) { assert(status == error_status(vector)); return; }
    size_t count = words(vector, "scale_words", scales_a);
    assert(count == n * tfs_onnx_blocks(k, bs));
    memset(flags_a, 0, sizeof flags_a);
    int64_t scale_status = tf_scales_check(scales_a, count, kind_of(vector), flags_a);
    assert(scale_status == integer(expect, "scale_status"));
    for (size_t i = 0; i < count; ++i)
        if (scale_status == 0) assert(tfs_onnx_scale_class(scales_a[i], kind_of(vector)) >= 0);
    if (error_status(vector)) assert(scale_status == error_status(vector));
    flags_a[TF_FLAG_OUTSIDE_TERNARY] = a;
    flags_a[TF_FLAG_PADDING_NONZERO] = tf_onnx2_padding_nonzero(data, size, n, k, bs);
    assert(flags_a[TF_FLAG_PADDING_NONZERO] == tfs_onnx_padding_nonzero(data, size, n, k, bs));
    check_values(expect, got, n * k);
    assert(memcmp(got, want, n * k * sizeof got[0]) == 0);
    check_flags(expect, flags_a);
    if (boolean(vector, "encode")) {
        assert(tf_encode_onnx2(got, n, k, bs, zp, zp_size, out_a, sizeof out_a) == (int64_t)size && memcmp(out_a, data, size) == 0);
        assert(tfs_onnx_encode(got, n, k, bs, zp, zp_size, out_b, sizeof out_b) == (int64_t)size && memcmp(out_b, data, size) == 0);
    }
}

static void onnx_encode_vector(int64_t vector) {
    size_t n = (size_t)integer(vector, "n"), k = (size_t)integer(vector, "k");
    size_t bs = (size_t)integer(vector, "block_size");
    static uint8_t zp[256];
    size_t zp_size = hex(vector, "zero_points_hex", zp, sizeof zp);
    size_t count = values_hex(vector, "values_hex", got);
    assert(count == n * k);
    int64_t status = integer(need(vector, "expect"), "status");
    memset(out_a, 0x77, sizeof out_a);
    memset(out_b, 0x77, sizeof out_b);
    assert(tf_encode_onnx2(got, n, k, bs, zp, zp_size, out_a, sizeof out_a) == status);
    assert(tfs_onnx_encode(got, n, k, bs, zp, zp_size, out_b, sizeof out_b) == status && status == error_status(vector));
    for (size_t i = 0; i < sizeof out_a; ++i) assert(out_a[i] == 0x77 && out_b[i] == 0x77);
}

static void safetensors_vector(int64_t vector) {
    static uint8_t header[4096], st_arena[4096];
    static TMJsonToken st_tokens[512];
    size_t size = hex(vector, "safetensors_hex", header, sizeof header);
    int64_t name = need(vector, "tensor");
    int64_t expect = need(vector, "expect");
    uint64_t file_size = (uint64_t)integer(vector, "file_size");
    TFSafeInfo info;
    assert(tf_safetensors_find(header, size, tokens[name].text, tokens[name].text_size, st_tokens, 512, st_arena,
                               sizeof st_arena, &info) == integer(expect, "find"));
    int64_t dtype = need(expect, "dtype");
    assert(info.dtype_size == tokens[dtype].text_size && memcmp(info.dtype, tokens[dtype].text, info.dtype_size) == 0);
    uint64_t dims[4] = {info.d0, info.d1, info.d2, info.d3}, numel = 1;
    size_t n = 0;
    for (int64_t at = tokens[need(expect, "shape")].first; at >= 0; at = tokens[at].next, ++n) {
        assert(n < 4 && dims[n] == tokens[at].uinteger);
        numel = dims[n] && numel > UINT64_MAX / dims[n] ? UINT64_MAX : numel * dims[n]; /* saturating */
    }
    assert(n == info.dims);
    assert(info.begin == (uint64_t)integer(expect, "begin") && info.end == (uint64_t)integer(expect, "end"));
    assert(info.dtype_bits == (uint64_t)integer(expect, "dtype_bits") && info.has_next == boolean(expect, "has_next"));
    assert(info.next_begin == (uint64_t)integer(expect, "next_begin"));
    int64_t check = integer(expect, "check");
    assert(tf_safetensors_check(&info, file_size) == check);
    /* The spec states the widths of the checkpoints' dtypes and the extent rule. */
    uint64_t spec_bits = tfs_hf_dtype_bits(info.dtype, info.dtype_size);
    if (spec_bits) assert(spec_bits == info.dtype_bits);
    assert(tfs_hf_safetensors_status(info.dtype_bits, numel, info.begin, info.end, info.has_next, info.next_begin,
                                     file_size) == check);
    if (error_status(vector)) assert(check == error_status(vector));
}

/* Negative vectors: a declared reject class needs kind "reject"; every
 * reject, flag or silent vector carries its upstream readers' view, each
 * citing lines of a file pinned for that upstream in the document. */
static bool known_cite(int64_t upstreams, int64_t key, const TMJsonToken *cite) {
    int64_t up = tm_json_field(tokens, token_count, upstreams, tokens[key].text, tokens[key].text_size);
    assert(up >= 0);
    size_t colon = 0;
    while (colon < cite->text_size && cite->text[colon] != ':') ++colon;
    if (colon == 0 || colon + 1 >= cite->text_size) return false;
    size_t dash = 0;
    for (size_t i = colon + 1; i < cite->text_size; ++i) {
        if (cite->text[i] == '-' && !dash && i > colon + 1) { dash = i; continue; }
        if (cite->text[i] < '0' || cite->text[i] > '9') return false;
    }
    if (dash + 1 == cite->text_size) return false;
    for (int64_t f = tokens[need(up, "files")].first; f >= 0; f = tokens[f].next)
        if (tokens[f].text_size == colon && memcmp(tokens[f].text, cite->text, colon) == 0) return true;
    return false;
}

static void negative_shape(int64_t root, int64_t vector) {
    bool reject = text_is(need(vector, "kind"), "reject");
    assert(reject == (member(vector, "error_class") >= 0));
    bool negative = reject || member(vector, "flag_class") >= 0 || member(vector, "silent_class") >= 0;
    int64_t views = member(vector, "silent_output");
    assert(negative == (views >= 0));
    if (!negative) return;
    int64_t upstreams = need(root, "upstream");
    size_t entries = 0;
    for (int64_t entry = tokens[views].first; entry >= 0; entry = tokens[entry].next, ++entries) {
        int64_t behaviour = need(entry, "behaviour");
        bool unknown = text_is(behaviour, "unknown");
        assert(unknown || text_is(behaviour, "decodes") || text_is(behaviour, "rejects")
               || text_is(behaviour, "not_applicable"));
        bool marked = false;
        size_t cites = 0;
        for (int64_t cite = tokens[need(entry, "cite")].first; cite >= 0; cite = tokens[cite].next, ++cites) {
            if (text_is(cite, "UNKNOWN")) { marked = true; continue; }
            assert(known_cite(upstreams, need(entry, "upstream"), &tokens[cite]));
        }
        assert(cites > 0 && marked == unknown && tokens[need(entry, "note")].text_size > 0);
    }
    assert(entries > 0);
}

static size_t replay(const char *family) {
    char path[128];
    snprintf(path, sizeof path, "conformance/formats_%s.json", family);
    FILE *file = fopen(path, "rb");
    if (!file) { fprintf(stderr, "cannot open %s\n", path); abort(); }
    size_t size = fread(file_bytes, 1, sizeof file_bytes, file);
    assert(size < sizeof file_bytes && feof(file));
    fclose(file);
    TMJsonResult result;
    assert(tm_json_parse(file_bytes, size, tokens, TOKENS, arena, ARENA, 16, &result) == 0);
    token_count = result.token_count;
    int64_t root = result.root;
    char spec_path[128];
    snprintf(spec_path, sizeof spec_path, "specs/formats/%s.t27", family);
    assert(text_is(need(root, "spec_path"), spec_path));
    document_constants(root);
    bool prism = strcmp(family, "prismml") == 0;
    size_t vectors = 0;
    for (int64_t v = tokens[need(root, "vectors")].first; v >= 0; v = tokens[v].next, ++vectors) {
        negative_shape(root, v);
        int64_t kind = need(v, "kind");
        if (text_is(kind, "reject")) kind = need(v, "reader");
        if (text_is(kind, "block")) block_vector(v, prism);
        else if (text_is(kind, "block_encode")) block_encode_vector(v, prism);
        else if (text_is(kind, "gguf")) gguf_vector(v);
        else if (text_is(kind, "i2s")) i2s_vector(v);
        else if (text_is(kind, "i2s_encode")) i2s_encode_vector(v);
        else if (text_is(kind, "hf_packed")) hf_vector(v);
        else if (text_is(kind, "hf_encode")) hf_encode_vector(v);
        else if (text_is(kind, "mlx")) mlx_vector(v);
        else if (text_is(kind, "mlx_encode")) mlx_encode_vector(v);
        else if (text_is(kind, "onnx")) onnx_vector(v);
        else if (text_is(kind, "onnx_encode")) onnx_encode_vector(v);
        else if (text_is(kind, "safetensors")) safetensors_vector(v);
        else { fprintf(stderr, "%s: unknown vector kind\n", path); abort(); }
    }
    assert(vectors > 0);
    printf("replayed %s: %zu vectors\n", path, vectors);
    return vectors;
}

int main(void) {
    status_classes_and_geometry();
    scale_classes_and_base3();
    random_blocks();
    random_tensors();
    gguf_type_rule();
    static const char *const families[] = {"llama_cpp", "prismml", "bitnet_cpp", "hf_bitnet", "mlx", "onnx"};
    size_t total = 0;
    for (size_t i = 0; i < sizeof families / sizeof families[0]; ++i) total += replay(families[i]);
    printf("PASS spec/formats differential harness: status classes, geometry, 65536 f16/bf16 scale words, "
           "256 base-3 bytes, random blocks and tensors in all formats, GGUF type ids and extents, %zu vectors in "
           "6 files with their reject classes and upstream views (spec and implementation)\n", total);
    return 0;
}
