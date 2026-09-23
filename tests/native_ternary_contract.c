/* Acceptance harness for t27/ternary_contract.t27, the Ternary Check CLI
 * contract v1 (ternary-check/CONTRACT.md). The expected tokens are spelled out
 * here from conformance/formats_*.json constants rather than taken from the
 * module; the reader is checked against the t27/formats.t27 functions it
 * composes, and the verdict against the table in CONTRACT.md. The whole
 * vector set runs through tests/test_ternary_check_run.py. */
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
double tm_json_strtod(uint8_t *);
#include "json.h"
#include "formats.h"
#include "ternary_contract.h"

static const struct { int32_t status; const char *token; } errors[] = {
    {-50, "format"}, {-51, "length"}, {-52, "capacity"}, {-53, "code"}, {-54, "padding"},
    {-55, "truncated"}, {-56, "container"}, {-57, "not_found"}, {-58, "scale_nonfinite"},
    {-59, "type_ambiguous"}, {-60, "layout_unsupported"}, {-61, "misaligned"}, {-62, "extent"},
};
static const char *flag_tokens[7] = {"outside_ternary", "noncanonical_base3", "scale_negative", "scale_zero",
                                     "trailer_nonzero", "padding_nonzero", "affine_not_ternary"};
static const char *formats[11] = {"", "TQ1_0", "TQ2_0", "Q2_0", "Q1_0", "PQ2_0", "PTQ1_0", "I2_S",
                                  "HF_PACKED", "MLX2", "ONNX2"};

static uint32_t state = 2709;
static uint32_t next_random(void) {
    state = state * UINT32_C(1664525) + UINT32_C(1013904223);
    return state >> 8;
}

static int64_t parse(const char *text, int64_t *flags) {
    return tk_parse_flags((uint8_t *)text, strlen(text), flags);
}

static void tokens(void) {
    uint8_t out[64];
    for (size_t i = 0; i < sizeof errors / sizeof errors[0]; ++i) {
        size_t n = strlen(errors[i].token);
        assert(tk_error_token(errors[i].status, out, sizeof out) == (int64_t)n);
        assert(memcmp(out, errors[i].token, n) == 0);
        assert(tk_error_status((uint8_t *)errors[i].token, n) == errors[i].status);
        char line[64];
        snprintf(line, sizeof line, "%s \r\n\t", errors[i].token);
        assert(tk_error_status((uint8_t *)line, strlen(line)) == errors[i].status);
        snprintf(line, sizeof line, " %s", errors[i].token);  /* leading space: not a token */
        assert(tk_error_status((uint8_t *)line, strlen(line)) == 0);
        assert(tk_error_status((uint8_t *)errors[i].token, n - 1) == 0);
        assert(tk_error_token(errors[i].status, out, n - 1) == TF_ERR_CAPACITY);
    }
    assert(tk_error_token(0, out, sizeof out) == 0);
    assert(tk_error_token(-49, out, sizeof out) == 0);
    assert(tk_error_token(-63, out, sizeof out) == 0);
    assert(tk_error_status((uint8_t *)"", 0) == 0);
    assert(tk_error_status((uint8_t *)"Padding", 7) == 0);
    assert(tk_error_status((uint8_t *)"padding padding", 15) == 0);
    for (size_t slot = 0; slot < 7; ++slot) {
        size_t n = strlen(flag_tokens[slot]);
        assert(tk_flag_token(slot, out, sizeof out) == (int64_t)n);
        assert(memcmp(out, flag_tokens[slot], n) == 0);
    }
    assert(tk_flag_token(7, out, sizeof out) == 0);
    for (int32_t id = 1; id <= 10; ++id) {
        size_t n = strlen(formats[id]);
        assert(tk_format_token(id, out, sizeof out) == (int64_t)n);
        assert(tk_format_of_name((uint8_t *)formats[id], n) == id);
    }
    assert(tk_format_of_name((uint8_t *)"LINEAR2", 7) == 0);
    assert(tk_format_of_name((uint8_t *)"Q2_", 3) == 0);
    assert(tk_format_of_name((uint8_t *)"TQ1_0 ", 6) == 0);
    const char *outcomes[9] = {"match", "rejected", "match_unflagged", "mismatch", "wrong_class",
                               "unexpected_reject", "unclassified", "silent", "crashed"};
    for (int32_t o = 0; o < 9; ++o) {
        size_t n = strlen(outcomes[o]);
        assert(tk_outcome_token(o, out, sizeof out) == (int64_t)n);
        assert(memcmp(out, outcomes[o], n) == 0);
    }
    assert(tk_outcome_token(9, out, sizeof out) == 0);
}

static void flags_text(void) {
    int64_t flags[7], back[7];
    assert(parse("", flags) == 0);
    for (int i = 0; i < 7; ++i) assert(flags[i] == 0);
    assert(parse("noncanonical_base3 3\n", flags) == 0 && flags[1] == 3);
    assert(parse("trailer_nonzero 28\r\n\nscale_zero 1", flags) == 0 && flags[4] == 28 && flags[3] == 1);
    assert(parse("outside_ternary 0\n", flags) == 0 && flags[0] == 0);
    assert(parse("outside_ternary 999999999999999999\n", flags) == 0 && flags[0] == INT64_C(999999999999999999));
    const char *bad[] = {"outside_ternary\n", "outside_ternary  1\n", " outside_ternary 1\n", "outside_ternary 1 \n",
                         "outside_ternary -1\n", "outside_ternary 1x\n", "unknown_flag 1\n",
                         "outside_ternary 1\noutside_ternary 2\n", "outside_ternary 1000000000000000000\n",
                         "Outside_ternary 1\n", "outside_ternary\t1\n"};
    for (size_t i = 0; i < sizeof bad / sizeof bad[0]; ++i) {
        flags[0] = 77;
        assert(parse(bad[i], flags) == -1);
    }
    uint8_t text[512];
    for (int round = 0; round < 200; ++round) {
        for (int i = 0; i < 7; ++i) flags[i] = next_random() % 3 == 0 ? 0 : (int64_t)(next_random() % 100000);
        if (round == 0) for (int i = 0; i < 7; ++i) flags[i] = 0;
        if (round == 1) for (int i = 0; i < 7; ++i) flags[i] = INT64_MAX;
        int64_t n = tk_flags_text(flags, text, sizeof text);
        assert(n >= 0);
        assert(tk_parse_flags(text, (size_t)n, back) == (round == 1 ? -1 : 0));  /* 19 digits do not parse */
        if (round != 1) for (int i = 0; i < 7; ++i) assert(back[i] == flags[i]);
    }
    flags[0] = 12; flags[1] = 0; flags[2] = 0; flags[3] = 0; flags[4] = 0; flags[5] = 0; flags[6] = 5;
    int64_t n = tk_flags_text(flags, text, sizeof text);
    assert(n == 40 && memcmp(text, "outside_ternary 12\naffine_not_ternary 5\n", 40) == 0);
    for (size_t cap = 0; cap < 40; ++cap) assert(tk_flags_text(flags, text, cap) == TF_ERR_CAPACITY);
    flags[3] = -1;
    assert(tk_flags_text(flags, text, sizeof text) == TF_ERR_LENGTH);
}

static void comparison(void) {
    int64_t first[1];
    uint8_t a[6] = {1, 0xff, 0, 1, 2, 0xfe}, b[6] = {1, 0xff, 0, 1, 2, 0xfe};
    assert(tk_compare_bytes(a, 6, b, 6, first) == 0 && first[0] == -1);
    b[4] = 0;
    assert(tk_compare_bytes(a, 6, b, 6, first) == 1 && first[0] == 4);
    assert(tk_compare_bytes(a, 3, b, 6, first) == 3 && first[0] == 3);
    assert(tk_compare_bytes(a, 6, b, 4, first) == 2 && first[0] == 4);
    assert(tk_compare_bytes(a, 0, b, 0, first) == 0 && first[0] == -1);
    assert(tk_compare_bytes(a, 0, b, 2, first) == 2 && first[0] == 0);
    b[0] = 9;
    assert(tk_compare_bytes(a, 3, b, 6, first) == 4 && first[0] == 0);
    uint32_t words[3] = {0x3c00, 0x0001, 0xfbff};
    uint8_t w2[6] = {0x00, 0x3c, 0x01, 0x00, 0xff, 0xfb};
    assert(tk_compare_words(w2, 6, 2, words, 3, first) == 0 && first[0] == -1);
    assert(tk_compare_words(w2, 5, 2, words, 3, first) == 1 && first[0] == 2);
    assert(tk_compare_words(w2, 4, 2, words, 3, first) == 1 && first[0] == 2);
    assert(tk_compare_words(w2, 6, 2, words, 2, first) == 1 && first[0] == 2);
    assert(tk_compare_words(w2, 5, 2, words, 2, first) == 1 && first[0] == 2);  /* one surplus partial word */
    w2[2] = 2;
    assert(tk_compare_words(w2, 6, 2, words, 3, first) == 1 && first[0] == 1);
    assert(tk_compare_words(w2, 6, 3, words, 2, first) == TF_ERR_LENGTH);
    uint32_t f32[1] = {0x3f800000};
    uint8_t w4[4] = {0x00, 0x00, 0x80, 0x3f};
    assert(tk_compare_words(w4, 4, 4, f32, 1, first) == 0);
    assert(tk_compare_words(w4, 4, 2, f32, 1, first) == 2 && first[0] == 0);
    assert(tk_compare_words(w4, 0, 4, f32, 0, first) == 0 && first[0] == -1);

    int64_t zero[7] = {0}, some[7] = {0, 3, 0, 0, 0, 0, 0}, other[7] = {0, 2, 0, 0, 0, 0, 0};
    assert(tk_flag_state(false, 0, zero, zero) == TK_FLAGS_EQUAL);
    assert(tk_flag_state(false, 0, zero, some) == TK_FLAGS_UNREPORTED);
    assert(tk_flag_state(true, 0, some, some) == TK_FLAGS_EQUAL);
    assert(tk_flag_state(true, 0, other, some) == TK_FLAGS_DIFFER);
    assert(tk_flag_state(true, 0, zero, some) == TK_FLAGS_DIFFER);
    assert(tk_flag_state(true, -1, some, some) == TK_FLAGS_DIFFER);
    assert(tk_flag_state(true, -1, zero, zero) == TK_FLAGS_DIFFER);
}

/* The verdict table of ternary-check/CONTRACT.md, written out case by case. */
static int32_t table(int32_t expected, int32_t ended, int32_t exit_code, int32_t error_status, int64_t values,
                     int64_t scales, int32_t flags) {
    if (ended) return 8;
    if (exit_code != 0) {
        if (error_status == 0) return 6;
        if (expected == 0) return 5;
        return error_status == expected ? 1 : 4;
    }
    if (expected != 0) return 7;
    if (values || scales || flags == 2) return 3;
    return flags == 1 ? 2 : 0;
}

static void verdicts(void) {
    const int32_t expected[] = {0, -51, -53, -58};
    const int32_t exits[] = {0, 1, 2, 255};
    const int32_t errors_seen[] = {0, -51, -53, -58, -62};
    const int64_t diffs[] = {0, 1, 1000};
    size_t cases = 0;
    for (size_t e = 0; e < 4; ++e)
        for (int32_t ended = 0; ended < 2; ++ended)
            for (size_t x = 0; x < 4; ++x)
                for (size_t s = 0; s < 5; ++s)
                    for (size_t v = 0; v < 3; ++v)
                        for (size_t c = 0; c < 3; ++c)
                            for (int32_t f = 0; f < 3; ++f) {
                                int32_t got = tk_verdict(expected[e], ended, exits[x], errors_seen[s], diffs[v],
                                                         diffs[c], f);
                                assert(got == table(expected[e], ended, exits[x], errors_seen[s], diffs[v], diffs[c], f));
                                ++cases;
                            }
    for (int32_t o = 0; o < 9; ++o) {
        bool pass = o == TK_MATCH || o == TK_MATCH_UNFLAGGED || o == TK_REJECTED;
        assert(tk_fails(o, TK_FAIL_MISMATCH) == !pass);
        assert(tk_fails(o, TK_FAIL_SILENT) == (o == TK_SILENT));
        assert(!tk_fails(o, TK_FAIL_NEVER));
    }
    printf("verdict table: %zu cases\n", cases);
}

/* The reader and writer against the t27/formats.t27 functions they compose. */
static void reader_blocks(void) {
    enum { COUNT = 1024 };
    static int32_t codes[COUNT], work[COUNT], direct[COUNT];
    static uint8_t values[COUNT], back[COUNT], data[8192], again[8192];
    uint32_t scales[16], direct_scales[16];
    int64_t flags[7], direct_flags[7];
    for (int32_t format = 1; format <= 6; ++format) {
        size_t per = tf_block_elements(format), blocks = COUNT / per;
        for (int round = 0; round < 20; ++round) {
            for (size_t i = 0; i < COUNT; ++i) {
                int32_t v = (int32_t)(next_random() % 3) - 1;
                if (format == TF_Q1_0) v = next_random() % 2 ? 1 : -1;
                codes[i] = v;
                values[i] = (uint8_t)v;
            }
            for (size_t b = 0; b < blocks; ++b) scales[b] = next_random() & 0x7bff;
            int64_t size = tk_encode(format, values, COUNT, 0, 0, 0, scales, blocks, NULL, 0, work, COUNT, data,
                                     sizeof data);
            assert(size == (int64_t)(blocks * tf_block_bytes(format)));
            assert((size_t)size == tk_encoded_bytes(format, COUNT, 0, 0, 0));
            assert(tf_encode_blocks(format, codes, COUNT, scales, blocks, again, sizeof again) == size);
            assert(memcmp(data, again, (size_t)size) == 0);
            uint32_t words[16];
            int64_t got = tk_decode(format, data, (size_t)size, COUNT, 0, 0, 0, 0, NULL, 0, NULL, 0, NULL, 0, work,
                                    back, COUNT, words, 16, flags);
            assert(got == (int64_t)blocks);
            assert(tk_scale_count(format, COUNT, 0, 0, 0) == blocks && tk_scale_width(format, TF_KIND_F32) == 2);
            assert(memcmp(back, values, COUNT) == 0);
            assert(tf_decode_blocks(format, data, (size_t)size, COUNT, direct, COUNT, direct_scales, 16) == 0);
            assert(memcmp(words, direct_scales, blocks * sizeof words[0]) == 0);
            assert(tf_block_flags(format, data, (size_t)size, COUNT, direct_flags) == 0);
            assert(memcmp(flags, direct_flags, sizeof flags) == 0);
        }
        /* A rejection writes nothing. */
        memset(back, 0x5a, sizeof back);
        uint32_t words[16] = {0};
        size_t size = blocks * tf_block_bytes(format);
        assert(tk_decode(format, data, size - 1, COUNT, 0, 0, 0, 0, NULL, 0, NULL, 0, NULL, 0, work, back, COUNT,
                         words, 16, flags) == TF_ERR_LENGTH);
        for (size_t i = 0; i < COUNT; ++i) assert(back[i] == 0x5a);
        for (int i = 0; i < 7; ++i) assert(flags[i] == 0);
        assert(tk_decode(format, data, size, COUNT, 0, 0, 0, 0, NULL, 0, NULL, 0, NULL, 0, work, back, COUNT - 1,
                         words, 16, flags) == TF_ERR_CAPACITY);
        values[3] = 2;
        if (format == TF_TQ1_0 || format == TF_PTQ1_0 || format == TF_Q1_0)
            assert(tk_encode(format, values, COUNT, 0, 0, 0, scales, blocks, NULL, 0, work, COUNT, data,
                             sizeof data) == TF_ERR_CODE);
        values[3] = 0xfe;  /* -2 */
        assert(tk_encode(format, values, COUNT, 0, 0, 0, scales, blocks, NULL, 0, work, COUNT, data,
                         sizeof data) == TF_ERR_CODE);
    }
}

static void reader_tensors(void) {
    enum { ROWS = 8, COLS = 64, COUNT = ROWS * COLS };
    static int32_t work[COUNT], direct[COUNT];
    static uint8_t values[COUNT], back[COUNT], data[4096];
    uint32_t words[64];
    int64_t flags[7];
    for (size_t i = 0; i < COUNT; ++i) values[i] = (uint8_t)((int32_t)(next_random() % 3) - 1);

    /* I2_S: codes, f32 scale, 28 trailer bytes. */
    uint32_t f32[1] = {0x3fc00000};
    int64_t size = tk_encode(TF_I2_S, values, COUNT, 0, 0, 0, f32, 1, NULL, 0, work, COUNT, data, sizeof data);
    assert(size == (int64_t)(COUNT / 4 + 32) && (size_t)size == tk_encoded_bytes(TF_I2_S, COUNT, 0, 0, 0));
    data[COUNT / 4 + 10] = 7;
    assert(tk_decode(TF_I2_S, data, (size_t)size, COUNT, 0, 0, 0, 0, NULL, 0, NULL, 0, NULL, 0, work, back, COUNT,
                     words, 1, flags) == 1);
    assert(words[0] == 0x3fc00000 && memcmp(back, values, COUNT) == 0 && flags[TF_FLAG_TRAILER_NONZERO] == 1);
    assert(tk_scale_width(TF_I2_S, TF_KIND_F16) == 4);
    assert(tk_decode(TF_I2_S, data, (size_t)size, COUNT, 0, 0, 0, 0, NULL, 0, NULL, 0, NULL, 0, work, back, COUNT,
                     words, 0, flags) == TF_ERR_CAPACITY);
    assert(tk_encode(TF_I2_S, values, COUNT, 0, 0, 0, f32, 2, NULL, 0, work, COUNT, data, sizeof data) ==
           TF_ERR_LENGTH);

    /* HF packed with a separate bf16 scale: echoed, checked after the payload. */
    size = tk_encode(TF_HF_PACKED, values, COUNT, ROWS, COLS, 0, NULL, 0, NULL, 0, work, COUNT, data, sizeof data);
    assert(size == ROWS / 4 * COLS && (size_t)size == tk_encoded_bytes(TF_HF_PACKED, COUNT, ROWS, COLS, 0));
    uint32_t bf16[1] = {0x3f80};
    assert(tk_decode(TF_HF_PACKED, data, (size_t)size, COUNT, ROWS, COLS, 0, TF_KIND_BF16, bf16, 1, NULL, 0, NULL, 0,
                     work, back, COUNT, words, 64, flags) == 1);
    assert(words[0] == 0x3f80 && memcmp(back, values, COUNT) == 0);
    assert(tf_decode_hf_packed(data, (size_t)size, ROWS, COLS, direct, COUNT) == 0);
    for (size_t i = 0; i < COUNT; ++i) assert((int8_t)back[i] == direct[i]);
    bf16[0] = 0x7fc0;
    assert(tk_decode(TF_HF_PACKED, data, (size_t)size, COUNT, ROWS, COLS, 0, TF_KIND_BF16, bf16, 1, NULL, 0, NULL, 0,
                     work, back, COUNT, words, 64, flags) == TF_ERR_SCALE_NONFINITE);
    for (int i = 0; i < 7; ++i) assert(flags[i] == 0);
    bf16[0] = 0x8000;
    assert(tk_decode(TF_HF_PACKED, data, (size_t)size, COUNT, ROWS, COLS, 0, TF_KIND_BF16, bf16, 1, NULL, 0, NULL, 0,
                     work, back, COUNT, words, 64, flags) == 1);
    assert(flags[TF_FLAG_SCALE_ZERO] == 1);
    assert(tk_decode(TF_HF_PACKED, data, (size_t)size, COUNT, ROWS, COLS, 0, TF_KIND_BF16, bf16, 2, NULL, 0, NULL, 0,
                     work, back, COUNT, words, 64, flags) == TF_ERR_LENGTH);
    assert(tk_decode(TF_HF_PACKED, data, (size_t)size, COUNT - 1, ROWS, COLS, 0, TF_KIND_BF16, bf16, 1, NULL, 0, NULL,
                     0, work, back, COUNT, words, 64, flags) == TF_ERR_LENGTH);
    assert(tk_decode(TF_HF_PACKED, data, (size_t)size, COUNT, ROWS, COLS, 0, 0, bf16, 1, NULL, 0, NULL, 0, work, back,
                     COUNT, words, 64, flags) == TF_ERR_FORMAT);

    /* MLX2, group 32: one scale and bias per group; bias == -scale is ternary. */
    size = tk_encode(TF_LINEAR2, values, COUNT, ROWS, COLS, 32, NULL, 0, NULL, 0, work, COUNT, data, sizeof data);
    assert(size == ROWS * COLS / 4);
    uint32_t mscales[16], mbiases[16];
    for (size_t g = 0; g < 16; ++g) { mscales[g] = 0x3c00; mbiases[g] = 0xbc00; }
    mbiases[5] = 0xbe00;
    assert(tk_scale_count(TF_LINEAR2, COUNT, ROWS, COLS, 32) == 16);
    assert(tk_decode(TF_LINEAR2, data, (size_t)size, COUNT, ROWS, COLS, 32, TF_KIND_F16, mscales, 16, mbiases, 16,
                     NULL, 0, work, back, COUNT, words, 64, flags) == 16);
    assert(memcmp(back, values, COUNT) == 0 && flags[TF_FLAG_AFFINE_NOT_TERNARY] == 1);
    assert(tk_decode(TF_LINEAR2, data, (size_t)size, COUNT, ROWS, COLS, 32, TF_KIND_F16, mscales, 16, mbiases, 15,
                     NULL, 0, work, back, COUNT, words, 64, flags) == TF_ERR_LENGTH);
    assert(tk_decode(TF_LINEAR2, data, (size_t)size, COUNT, ROWS, COLS, 48, TF_KIND_F16, mscales, 16, mbiases, 16,
                     NULL, 0, work, back, COUNT, words, 64, flags) == TF_ERR_LENGTH);
    mbiases[7] = 0x7c00;
    assert(tk_decode(TF_LINEAR2, data, (size_t)size, COUNT, ROWS, COLS, 32, TF_KIND_F16, mscales, 16, mbiases, 16,
                     NULL, 0, work, back, COUNT, words, 64, flags) == TF_ERR_SCALE_NONFINITE);

    /* ONNX2 with K not a multiple of the block: padding codes are a flag. */
    enum { N = 3, K = 70, BS = 32 };
    uint8_t zp[3] = {0x2a, 0x15, 0x2a};  /* zero points 2, 1, 2 per row */
    static uint8_t ov[N * K];
    for (size_t i = 0; i < N * K; ++i) ov[i] = values[i];
    size = tk_encode(TF_ONNX2, ov, N * K, N, K, BS, NULL, 0, zp, 3, work, COUNT, data, sizeof data);
    assert(size == N * 3 * BS / 4 && (size_t)size == tk_encoded_bytes(TF_ONNX2, N * K, N, K, BS));
    data[K / 4 + 2] |= 0xc0;  /* a padding code of row 0 */
    uint32_t oscales[9];
    for (size_t i = 0; i < 9; ++i) oscales[i] = 0x3c00;
    assert(tk_scale_count(TF_ONNX2, N * K, N, K, BS) == 9);
    assert(tk_decode(TF_ONNX2, data, (size_t)size, N * K, N, K, BS, TF_KIND_F16, oscales, 9, NULL, 0, zp, 3, work,
                     back, COUNT, words, 64, flags) == 9);
    assert(memcmp(back, ov, N * K) == 0 && flags[TF_FLAG_PADDING_NONZERO] == 1);
    assert(tk_decode(TF_ONNX2, data, (size_t)size, N * K, N, K, BS, TF_KIND_F16, oscales, 8, NULL, 0, zp, 3, work,
                     back, COUNT, words, 64, flags) == TF_ERR_LENGTH);
    assert(tk_decode(99, data, (size_t)size, N * K, N, K, BS, TF_KIND_F16, oscales, 9, NULL, 0, zp, 3, work, back,
                     COUNT, words, 64, flags) == TF_ERR_FORMAT);
    assert(tk_encode(99, ov, N * K, N, K, BS, NULL, 0, zp, 3, work, COUNT, data, sizeof data) == TF_ERR_FORMAT);
    assert(tk_encoded_bytes(TF_ONNX2, N * K, N, K, 0) == 0 && tk_encoded_bytes(99, 4, 2, 2, 0) == 0);
}

int main(void) {
    test_tk_tokens_verdicts_and_comparison(); /* the test block of t27/ternary_contract.t27 */
    tokens();
    flags_text();
    comparison();
    verdicts();
    reader_blocks();
    reader_tensors();
    puts("PASS ternary_contract: tokens, flags text, comparison, verdict table, reader and writer");
    return 0;
}
