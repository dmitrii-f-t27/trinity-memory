/* Independent acceptance harness for t27/matrix.t27 (issue #32). The oracles
 * are written from the definitions, not from the t27 source: exact scale words
 * against the compiler's _Float16 and IEEE binary32 through memcpy,
 * bf16 rounding against the nearer of the two neighbouring bf16 values,
 * representability, comparisons and tie counts against direct loops, and the
 * code positions of tmx_locate against the bit and base-3 digit layouts of the
 * pinned upstream readers. Built by tools/test-t27.sh with ASan and UBSan. */
#include <assert.h>
#include <math.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
double tm_json_strtod(uint8_t *);
#include "json.h"
#include "formats.h"
#include "matrix.h"

static uint64_t state = 32;
static uint32_t next_u32(void) {
    state ^= state << 13;
    state ^= state >> 7;
    state ^= state << 17;
    return (uint32_t)state;
}
static int32_t trit(void) { return (int32_t)(next_u32() % 3) - 1; }

static double f16_value(uint16_t w) { _Float16 h; memcpy(&h, &w, 2); return (double)h; }
static double f32_value(uint32_t w) { float f; memcpy(&f, &w, 4); return (double)f; }
static double bf16_value(uint16_t w) { return f32_value((uint32_t)w << 16); }

static void test_exact_words(void) {
    /* Every finite f16 and bf16 word round-trips; -0 maps to +0. */
    for (uint32_t w = 0; w < 65536; w++) {
        if (((w >> 10) & 31) != 31) {
            int64_t got = tmx_float_bits(f16_value((uint16_t)w), TF_KIND_F16);
            assert(got == ((w & 32767) == 0 ? 0 : (int64_t)w));
        }
        if (((w >> 7) & 255) != 255) {
            int64_t got = tmx_float_bits(bf16_value((uint16_t)w), TF_KIND_BF16);
            assert(got == ((w & 32767) == 0 ? 0 : (int64_t)w));
            /* A bf16 value is an f16 value exactly when _Float16 holds it. */
            double v = bf16_value((uint16_t)w);
            _Float16 h = (_Float16)v;
            int64_t as_f16 = tmx_float_bits(v, TF_KIND_F16);
            if ((double)h == v) {
                uint16_t hw;
                memcpy(&hw, &h, 2);
                assert(as_f16 == (v == 0.0 ? 0 : hw));
            } else {
                assert(as_f16 == -1);
            }
            /* Every bf16 value is an f32 value. */
            assert(tmx_float_bits(v, TF_KIND_F32) == (v == 0.0 ? 0 : (int64_t)w << 16));
        }
    }
    for (int i = 0; i < 1000000; i++) {
        uint32_t w = next_u32();
        if (((w >> 23) & 255) == 255) continue;
        double v = f32_value(w);
        assert(tmx_float_bits(v, TF_KIND_F32) == ((w & 0x7FFFFFFFu) == 0 ? 0 : (int64_t)w));
        /* Between two neighbouring f32 values: not an f32 value. */
        if ((w & 0x7FFFFFFFu) != 0 && (w & 0x7F800000u) != 0x7F000000u) {
            double next = f32_value(w + 1);
            if (isfinite(next)) assert(tmx_float_bits((v + next) / 2, TF_KIND_F32) == -1);
        }
    }
    assert(tmx_float_bits(NAN, TF_KIND_F16) == -1);
    assert(tmx_float_bits(INFINITY, TF_KIND_F32) == -1);
    assert(tmx_float_bits(65520.0, TF_KIND_F16) == -1);
    assert(tmx_float_bits(ldexp(1.0, -25), TF_KIND_F16) == -1);
    assert(tmx_float_bits(ldexp(1.0, -149), TF_KIND_F32) == 1);
    assert(tmx_float_bits(ldexp(1.0, -133), TF_KIND_BF16) == 1);
    assert(tmx_float_bits(1.0, 9) == -1);
}

/* Oracle: the nearer bf16 neighbour, ties to the even word. */
static void test_bf16_round(void) {
    for (int i = 0; i < 2000000; i++) {
        uint32_t w = next_u32();
        if (i < 1000) w = (w & 0xFFFF0000u) | 0x8000u; /* exact ties */
        if (((w >> 23) & 255) >= 254) continue;       /* finite, and the upper neighbour too */
        uint16_t lo = (uint16_t)(w >> 16), hi = (uint16_t)(lo + 1);
        double v = f32_value(w), dl = fabs(v - bf16_value(lo)), dh = fabs(bf16_value(hi) - v);
        uint16_t expect = dl < dh ? lo : dh < dl ? hi : ((lo & 1) ? hi : lo);
        assert(tmx_bf16_round(w) == expect);
    }
    /* The BitNet case: bf16 1.21875 (0x3F9C) is the rounding of an f32 near 1.2188548. */
    float f = 1.2188548f;
    uint32_t word;
    memcpy(&word, &f, 4);
    assert(tmx_bf16_round(word) == 0x3F9C);
    assert(tmx_rounding_pair(0x3F9C, TF_KIND_BF16, word, TF_KIND_F32));
    assert(tmx_rounding_pair(word, TF_KIND_F32, 0x3F9C, TF_KIND_BF16));
    assert(!tmx_rounding_pair(0x3F9D, TF_KIND_BF16, word, TF_KIND_F32));
    assert(!tmx_rounding_pair(0x3CE0, TF_KIND_F16, word, TF_KIND_F32));
    assert(tmx_bf16_half(0x3F9C) == 0x3F1C && bf16_value(0x3F1C) == 0.609375);
    assert(tmx_bf16_half(0x400A) == 0x3F8A && bf16_value(0x3F8A) == 1.078125);
    assert(tmx_bf16_half(0x0080) == -1 && tmx_bf16_half(0x7F80) == -1);
}

static void test_layouts(void) {
    for (size_t cols = 1; cols < 40; cols++)
        for (size_t group = 0; group < 12; group++)
            for (size_t i = 0; i < 3 * cols; i++) {
                size_t per_row = group ? (cols + group - 1) / group : 0;
                size_t expect = group ? (i / cols) * per_row + (i % cols) / group : 0;
                assert(tmx_scale_index(i, cols, group) == expect);
                size_t s = tmx_scale_index(i, cols, group);
                size_t first = tmx_scale_first(s, cols, group);
                assert(first <= i && tmx_scale_index(first, cols, group) == s);
                assert(first == 0 || tmx_scale_index(first - 1, cols, group) != s);
            }
    assert(tmx_scale_count(3, 10, 4) == 9 && tmx_scale_count(3, 10, 0) == 1);
    assert(tmx_scale_kind(TF_HF_PACKED) == TF_KIND_BF16 && tmx_scale_kind(TF_I2_S) == TF_KIND_F32);
    assert(tmx_scale_kind(TF_TQ1_0) == TF_KIND_F16 && tmx_scale_kind(TF_LINEAR2) == TF_KIND_F16);
    assert(tmx_scale_group(TF_Q2_0, 7) == 64 && tmx_scale_group(TF_ONNX2, 32) == 32 && tmx_scale_group(TF_I2_S, 32) == 0);
}

enum { ROWS = 8, COLS = 256, N = ROWS * COLS };
static const int32_t formats[] = {TF_TQ1_0, TF_TQ2_0, TF_Q2_0, TF_Q1_0, TF_PQ2_0, TF_PTQ1_0,
                                  TF_I2_S, TF_HF_PACKED, TF_LINEAR2, TF_ONNX2};

/* A [ROWS, COLS] reference with per-128 f16 scales; `same_pairs` makes both
 * 128-groups of every 256 block share a scale. */
static void reference(int32_t *t, uint32_t *s, bool binary, bool same_pairs) {
    for (size_t i = 0; i < N; i++) { t[i] = trit(); if (binary && t[i] == 0) t[i] = 1; }
    for (size_t g = 0; g < N / 128; g++) {
        s[g] = 0x2000u + next_u32() % 0x1000u; /* positive normal f16 */
        if (same_pairs && g % 2) s[g] = s[g - 1];
    }
}

static void test_representable_and_round_trips(void) {
    static int32_t t[N], back[N];
    static uint32_t s[N / 128], words[N], back_words[N];
    static uint8_t out[N];
    int64_t reasons[2 * TMX_REASON_COUNT], result[TMX_R_COUNT];
    for (int round = 0; round < 8; round++) {
        bool binary = round & 1, same = round & 2;
        reference(t, s, binary, same);
        if (round & 4) s[3] = 0x3C01; /* 1 + 2^-10: not a bf16 value */
        /* Oracle counts of the per-format reasons. */
        long zeros = 0, pairs_differ = 0, first_pair = -1;
        long first_zero = -1;
        for (size_t i = 0; i < N; i++) if (t[i] == 0) { if (!zeros) first_zero = (long)i; zeros++; }
        for (size_t p = 0; p < N / 256; p++)
            if (f16_value((uint16_t)s[2 * p]) != f16_value((uint16_t)s[2 * p + 1])) {
                if (!pairs_differ) first_pair = (long)(256 * p + 128);
                pairs_differ++;
            }
        long groups_not_tensor = 0, first_not_tensor = -1;
        for (size_t g = 1; g < N / 128; g++)
            if (f16_value((uint16_t)s[g]) != f16_value((uint16_t)s[0])) { groups_not_tensor = 1; first_not_tensor = (long)(128 * g); break; }
        long not_bf16 = 0, first_not_bf16 = -1;
        for (size_t g = 0; g < N / 128; g++) {
            double v = f16_value((uint16_t)s[g]);
            uint32_t fw; float fv = (float)v; memcpy(&fw, &fv, 4);
            if ((fw & 0xFFFF) != 0) { if (!not_bf16) first_not_bf16 = (long)(128 * g); not_bf16++; }
        }
        for (size_t k = 0; k < sizeof formats / sizeof *formats; k++) {
            int32_t fmt = formats[k];
            size_t group = fmt == TF_LINEAR2 || fmt == TF_ONNX2 ? 128 : 0;
            int64_t primary = tmx_representable(fmt, ROWS, COLS, group, t, s, N / 128, TF_KIND_F16, 128, reasons);
            int64_t expect = TMX_REPRESENTABLE;
            if (fmt == TF_Q1_0 && zeros) {
                expect = TMX_REASON_BINARY_ONLY;
                assert(reasons[2 * TMX_REASON_BINARY_ONLY] == zeros && reasons[2 * TMX_REASON_BINARY_ONLY + 1] == first_zero);
            } else if ((fmt == TF_TQ1_0 || fmt == TF_TQ2_0) && pairs_differ) {
                expect = TMX_REASON_GROUP_SCALES_DIFFER;
                assert(reasons[2 * TMX_REASON_GROUP_SCALES_DIFFER] == pairs_differ);
                assert(reasons[2 * TMX_REASON_GROUP_SCALES_DIFFER + 1] == first_pair);
            } else if ((fmt == TF_I2_S || fmt == TF_HF_PACKED) && groups_not_tensor) {
                expect = TMX_REASON_GROUP_SCALES_DIFFER;
                assert(reasons[2 * TMX_REASON_GROUP_SCALES_DIFFER] == 1);
                assert(reasons[2 * TMX_REASON_GROUP_SCALES_DIFFER + 1] == first_not_tensor);
            }
            if (fmt == TF_HF_PACKED) {
                assert(reasons[2 * TMX_REASON_SCALE_PRECISION] == not_bf16);
                assert(reasons[2 * TMX_REASON_SCALE_PRECISION + 1] == first_not_bf16);
            } else {
                assert(reasons[2 * TMX_REASON_SCALE_PRECISION] == 0);
            }
            assert(primary == expect);
            memset(result, 0x55, sizeof result);
            int32_t status = tmx_round_trip(fmt, ROWS, COLS, group, t, s, N / 128, TF_KIND_F16, 128, words, N,
                                            out, sizeof out, back, N, back_words, reasons, result);
            assert(status == 0);
            assert(result[TMX_R_REASON] == expect);
            if (expect != TMX_REPRESENTABLE) {
                assert(result[TMX_R_STATUS] == TMX_NOT_REPRESENTABLE);
                continue;
            }
            assert(result[TMX_R_STATUS] == TMX_MATCH && result[TMX_R_TRITS_DIFFER] == 0 && result[TMX_R_SCALES_DIFFER] == 0);
            assert(memcmp(t, back, sizeof t) == 0);
            size_t code_bytes = fmt == TF_I2_S ? N / 4 + 32 : fmt == TF_HF_PACKED || fmt == TF_LINEAR2 || fmt == TF_ONNX2
                ? N / 4 : (N / tf_block_elements(fmt)) * tf_block_bytes(fmt);
            size_t external = fmt == TF_HF_PACKED ? 2 : fmt == TF_LINEAR2 ? 4 * (N / 128) : fmt == TF_ONNX2 ? 2 * (N / 128) : 0;
            assert(result[TMX_R_CODE_BYTES] == (int64_t)code_bytes);
            assert(result[TMX_R_STORED_BYTES] == (int64_t)(code_bytes + external));
            /* Every code sits where tmx_locate says, in the upstream layout. */
            int64_t at[TMX_L_COUNT];
            for (int probe = 0; probe < 200; probe++) {
                size_t i = next_u32() % N;
                assert(tmx_locate(fmt, ROWS, COLS, group, i, at) == 0);
                uint8_t byte = out[at[TMX_L_BYTE]];
                int32_t v;
                if (at[TMX_L_WIDTH] == 0) {
                    uint32_t q = byte;
                    for (int64_t d = 0; d < at[TMX_L_POSITION]; d++) q = (q * 3) & 255;
                    v = (int32_t)((q * 3) >> 8) - 1;
                } else if (at[TMX_L_WIDTH] == 1) {
                    v = (byte >> at[TMX_L_POSITION]) & 1 ? 1 : -1;
                } else {
                    int32_t zero = fmt == TF_ONNX2 ? 2 : 1;
                    v = ((byte >> at[TMX_L_POSITION]) & 3) - zero;
                }
                assert(v == t[i]);
                /* The scale word that covers the weight: in the block, after the I2_S codes, or apart. */
                if (tf_block_elements(fmt)) {
                    size_t per = tf_block_elements(fmt), bytes = tf_block_bytes(fmt);
                    size_t off = (i / per) * bytes + (fmt == TF_TQ1_0 ? 52 : fmt == TF_TQ2_0 ? 64 : fmt == TF_PTQ1_0 ? 26 : 0);
                    assert(at[TMX_L_SCALE_BYTE] == (int64_t)off);
                    assert((uint32_t)(out[off] | out[off + 1] << 8) == back_words[i / per]);
                } else {
                    assert(at[TMX_L_SCALE_BYTE] == (fmt == TF_I2_S ? N / 4 : -1));
                }
            }
            /* The t27 writer reproduces its own bytes; one flipped byte is found. */
            size_t n_words = tmx_scale_count(ROWS, COLS, tmx_scale_group(fmt, group));
            static uint8_t again[N];
            int64_t first[1];
            assert(tmx_reencode(fmt, ROWS, COLS, group, back, back_words, n_words, out, code_bytes, again, sizeof again, first) == 0);
            assert(first[0] == -1);
            out[17] ^= 1;
            assert(tmx_reencode(fmt, ROWS, COLS, group, back, back_words, n_words, out, code_bytes, again, sizeof again, first) == 1);
            assert(first[0] == 17);
        }
    }
    /* Rejections: unknown format, a scale count that does not fit, NaN. */
    assert(tmx_representable(99, ROWS, COLS, 0, t, s, N / 128, TF_KIND_F16, 128, reasons) == TF_ERR_FORMAT);
    assert(tmx_representable(TF_Q2_0, ROWS, COLS, 0, t, s, N / 128 - 1, TF_KIND_F16, 128, reasons) == TF_ERR_LENGTH);
    uint32_t keep = s[5];
    s[5] = 0x7E00;
    assert(tmx_representable(TF_Q2_0, ROWS, COLS, 0, t, s, N / 128, TF_KIND_F16, 128, reasons) == TF_ERR_SCALE_NONFINITE);
    assert(tmx_round_trip(TF_Q2_0, ROWS, COLS, 0, t, s, N / 128, TF_KIND_F16, 128, words, N, out, sizeof out,
                          back, N, back_words, reasons, result) == TF_ERR_SCALE_NONFINITE);
    s[5] = keep;
    /* Shape: TQ1_0 needs whole 256-weight blocks per row; HF packed rows % 4. */
    assert(tmx_representable(TF_TQ1_0, 16, 128, 0, t, s, 16, TF_KIND_F16, 128, reasons) == TMX_REASON_SHAPE);
    assert(reasons[2 * TMX_REASON_SHAPE] == 1);
    assert(tmx_representable(TF_HF_PACKED, 6, 128, 0, t, s, 6, TF_KIND_F16, 128, reasons) == TMX_REASON_SHAPE);
    /* Codes outside the table: +2 fits 2-bit Q2_0 but not base-3 TQ1_0 or HF packed. */
    reference(t, s, false, true);
    t[300] = 2;
    assert(tmx_representable(TF_Q2_0, ROWS, COLS, 0, t, s, N / 128, TF_KIND_F16, 128, reasons) == TMX_REPRESENTABLE);
    assert(tmx_representable(TF_TQ1_0, ROWS, COLS, 0, t, s, N / 128, TF_KIND_F16, 128, reasons) == TMX_REASON_CODE_OUTSIDE);
    assert(reasons[2 * TMX_REASON_CODE_OUTSIDE] == 1 && reasons[2 * TMX_REASON_CODE_OUTSIDE + 1] == 300);
}

static void test_compare_and_ties(void) {
    enum { R = 16, C = 64, M = R * C };
    static int32_t ref[M], der[M];
    static uint8_t master[2 * M];
    const uint16_t scale = 0x3F9C, tie = 0x3F1C; /* 1.21875 and 0.609375 */
    int64_t result[TMX_R_COUNT], detail[TMX_T_COUNT];
    long pos_nz = 0, pos_z = 0, neg_nz = 0, neg_z = 0, differ = 0, first = -1, der_nz = 0;
    for (size_t i = 0; i < M; i++) {
        uint16_t w;
        uint32_t pick = next_u32() % 5;
        if (pick == 0) w = tie;
        else if (pick == 1) w = tie | 0x8000;
        else w = (uint16_t)(0x3000 + next_u32() % 0x1000) | (uint16_t)((next_u32() & 1) << 15);
        master[2 * i] = (uint8_t)w;
        master[2 * i + 1] = (uint8_t)(w >> 8);
        double v = bf16_value(w);
        der[i] = fabs(v) > 0.609375 ? (v > 0 ? 1 : -1) : 0;
        ref[i] = der[i];
        if ((w & 0x7FFF) == tie) {
            if (next_u32() % 2) ref[i] = v > 0 ? 1 : -1;
            bool neg = (w & 0x8000) != 0;
            if (ref[i]) { if (neg) neg_nz++; else pos_nz++; } else { if (neg) neg_z++; else pos_z++; }
            if (der[i]) der_nz++;
        }
        if (ref[i] != der[i]) { if (!differ) first = (long)i; differ++; }
    }
    uint32_t one[1] = {scale};
    assert(tmx_compare(ref, der, R, C, one, 1, TF_KIND_BF16, 0, one, 1, TF_KIND_BF16, 0, false, result) == 0);
    assert(result[TMX_R_STATUS] == TMX_MISMATCH && result[TMX_R_TRITS_DIFFER] == differ && result[TMX_R_TRITS_FIRST] == first);
    assert(result[TMX_R_TRITS_EXPLAIN] == TMX_EXPLAIN_UNEXPLAINED && result[TMX_R_SCALES_DIFFER] == -1);
    assert(tmx_explain_ties(master, sizeof master, M, scale, ref, der, detail, result) == 0);
    assert(detail[TMX_T_TIE_WORD] == tie && detail[TMX_T_POS_NONZERO] == pos_nz && detail[TMX_T_POS_ZERO] == pos_z);
    assert(detail[TMX_T_NEG_NONZERO] == neg_nz && detail[TMX_T_NEG_ZERO] == neg_z && detail[TMX_T_DERIVED_NONZERO] == der_nz);
    assert(detail[TMX_T_DIFFER_AT_TIES] == differ && detail[TMX_T_DIFFER_ELSEWHERE] == 0);
    assert(result[TMX_R_TRITS_EXPLAIN] == TMX_EXPLAIN_TIE_SPLIT);
    /* One difference away from the tie value leaves it unexplained. */
    size_t off = 0;
    while ((((uint16_t)master[2 * off] | master[2 * off + 1] << 8) & 0x7FFF) == tie) off++;
    ref[off] = ref[off] == 1 ? 0 : 1;
    assert(tmx_compare(ref, der, R, C, one, 1, TF_KIND_BF16, 0, one, 1, TF_KIND_BF16, 0, false, result) == 0);
    assert(tmx_explain_ties(master, sizeof master, M, scale, ref, der, detail, result) == 0);
    assert(detail[TMX_T_DIFFER_ELSEWHERE] == 1 && detail[TMX_T_FIRST_ELSEWHERE] == (int64_t)off);
    assert(result[TMX_R_TRITS_EXPLAIN] == TMX_EXPLAIN_UNEXPLAINED);
    assert(tmx_explain_ties(master, sizeof master - 1, M, scale, ref, der, detail, result) == TF_ERR_LENGTH);
    /* One consistent rule at the tie value is no split: every tie weight +-1 in one tensor and 0 in the
     * other puts every difference at a tie, yet no bf16 value carries both trits in the reference, so a
     * function of the bf16 values gives it and the difference stays unexplained. */
    static int32_t rule[M], rounded[M];
    for (int mode = 0; mode < 2; mode++) {
        long at_ties = 0;
        for (size_t i = 0; i < M; i++) {
            uint16_t w = (uint16_t)(master[2 * i] | master[2 * i + 1] << 8);
            double v = bf16_value(w);
            int32_t sign = v > 0 ? 1 : -1, base = fabs(v) > 0.609375 ? sign : 0;
            bool is_tie = (w & 0x7FFF) == tie;
            rule[i] = is_tie ? (mode == 0 ? sign : 0) : base;
            rounded[i] = is_tie ? (mode == 0 ? 0 : sign) : base;
            if (rule[i] != rounded[i]) at_ties++;
        }
        assert(at_ties > 0);
        assert(tmx_compare(rule, rounded, R, C, one, 1, TF_KIND_BF16, 0, one, 1, TF_KIND_BF16, 0, false, result) == 0);
        assert(result[TMX_R_TRITS_DIFFER] == at_ties);
        assert(tmx_explain_ties(master, sizeof master, M, scale, rule, rounded, detail, result) == 0);
        assert(detail[TMX_T_DIFFER_AT_TIES] == at_ties && detail[TMX_T_DIFFER_ELSEWHERE] == 0);
        if (mode == 0) assert(detail[TMX_T_POS_ZERO] == 0 && detail[TMX_T_NEG_ZERO] == 0);
        else assert(detail[TMX_T_POS_NONZERO] == 0 && detail[TMX_T_NEG_NONZERO] == 0);
        assert(result[TMX_R_TRITS_EXPLAIN] == TMX_EXPLAIN_UNEXPLAINED);
    }
    /* Indices for a reproduction. */
    int64_t idx[4];
    int64_t got = tmx_word_indices(master, sizeof master, M, tie, ref, 1, idx, 4);
    long want = 0;
    for (size_t i = 0; i < M && want < 4; i++)
        if (((uint16_t)master[2 * i] | master[2 * i + 1] << 8) == tie && ref[i] == 1) assert(idx[want++] == (int64_t)i);
    assert(got == want);
    got = tmx_mismatch_indices(ref, der, M, idx, 4);
    assert(got == 4 && idx[0] == (first < (long)off ? first : (long)off));

    /* Scales: bf16 against the f32 it rounds is explained; against another f32 not. */
    float f = 1.2188548f;
    uint32_t f32w;
    memcpy(&f32w, &f, 4);
    uint32_t other[1] = {f32w};
    assert(tmx_compare(ref, ref, R, C, one, 1, TF_KIND_BF16, 0, other, 1, TF_KIND_F32, 0, true, result) == 0);
    assert(result[TMX_R_STATUS] == TMX_MISMATCH && result[TMX_R_TRITS_DIFFER] == 0);
    assert(result[TMX_R_SCALES_DIFFER] == M && result[TMX_R_SCALES_FIRST] == 0);
    assert(result[TMX_R_SCALES_EXPLAIN] == TMX_EXPLAIN_SCALE_BF16_ROUNDING);
    other[0] = f32w + 0x10000;
    assert(tmx_compare(ref, ref, R, C, one, 1, TF_KIND_BF16, 0, other, 1, TF_KIND_F32, 0, true, result) == 0);
    assert(result[TMX_R_SCALES_EXPLAIN] == TMX_EXPLAIN_UNEXPLAINED);
    /* The same value in f16 per 32 weights and bf16 per tensor: a match. */
    uint32_t per_group[R * (C / 32)];
    for (size_t g = 0; g < R * (C / 32); g++) per_group[g] = 0x3CE0; /* 1.21875 in f16 */
    assert(tmx_compare(ref, ref, R, C, one, 1, TF_KIND_BF16, 0, per_group, R * (C / 32), TF_KIND_F16, 32, true, result) == 0);
    assert(result[TMX_R_STATUS] == TMX_MATCH && result[TMX_R_SCALES_DIFFER] == 0);
    per_group[5] = 0x3CE1; /* group 5 = row 2, columns 32..63 */
    assert(tmx_compare(ref, ref, R, C, one, 1, TF_KIND_BF16, 0, per_group, R * (C / 32), TF_KIND_F16, 32, true, result) == 0);
    assert(result[TMX_R_SCALES_DIFFER] == 32 && result[TMX_R_SCALES_FIRST] == 2 * C + 32);
    assert(result[TMX_R_SCALES_EXPLAIN] == TMX_EXPLAIN_UNEXPLAINED);
    /* A zero scale over a group of zero weights: every value is 0 either way. */
    static int32_t zeroed[M];
    memcpy(zeroed, ref, sizeof zeroed);
    for (size_t c = 32; c < 64; c++) zeroed[2 * C + c] = 0;
    per_group[5] = 0;
    assert(tmx_compare(zeroed, zeroed, R, C, one, 1, TF_KIND_BF16, 0, per_group, R * (C / 32), TF_KIND_F16, 32, true, result) == 0);
    assert(result[TMX_R_STATUS] == TMX_MISMATCH && result[TMX_R_SCALES_DIFFER] == 32);
    assert(result[TMX_R_SCALES_EXPLAIN] == TMX_EXPLAIN_SCALE_ZERO_WEIGHTS);
    zeroed[2 * C + 40] = 1;
    assert(tmx_compare(zeroed, zeroed, R, C, one, 1, TF_KIND_BF16, 0, per_group, R * (C / 32), TF_KIND_F16, 32, true, result) == 0);
    assert(result[TMX_R_SCALES_EXPLAIN] == TMX_EXPLAIN_UNEXPLAINED);
    /* The trit must be 0 in both tensors: a zero-scale group whose weight is +1 on one side only is
     * unexplained, whichever side it is on. */
    static int32_t one_side[M];
    memcpy(one_side, zeroed, sizeof one_side);
    zeroed[2 * C + 40] = 0;
    uint32_t whole[R * (C / 32)];
    for (size_t g = 0; g < R * (C / 32); g++) whole[g] = 0x3CE0;
    assert(tmx_compare(zeroed, zeroed, R, C, whole, R * (C / 32), TF_KIND_F16, 32, per_group, R * (C / 32), TF_KIND_F16, 32, true, result) == 0);
    assert(result[TMX_R_SCALES_DIFFER] == 32 && result[TMX_R_SCALES_EXPLAIN] == TMX_EXPLAIN_SCALE_ZERO_WEIGHTS);
    assert(tmx_compare(zeroed, one_side, R, C, whole, R * (C / 32), TF_KIND_F16, 32, per_group, R * (C / 32), TF_KIND_F16, 32, true, result) == 0);
    assert(result[TMX_R_TRITS_DIFFER] == 1 && result[TMX_R_SCALES_DIFFER] == 32);
    assert(result[TMX_R_SCALES_EXPLAIN] == TMX_EXPLAIN_UNEXPLAINED);
    assert(tmx_compare(one_side, zeroed, R, C, per_group, R * (C / 32), TF_KIND_F16, 32, whole, R * (C / 32), TF_KIND_F16, 32, true, result) == 0);
    assert(result[TMX_R_SCALES_EXPLAIN] == TMX_EXPLAIN_UNEXPLAINED);
    per_group[5] = 0x7C00;
    assert(tmx_compare(ref, ref, R, C, one, 1, TF_KIND_BF16, 0, per_group, R * (C / 32), TF_KIND_F16, 32, true, result) == TF_ERR_SCALE_NONFINITE);
}

/* I2_S bytes whose trailer is not zero: the t27 writer differs exactly there. */
static void test_reencode_trailer(void) {
    enum { R = 4, C = 128, M = R * C };
    static int32_t t[M], back[M];
    static uint8_t bytes[M / 4 + 32], again[M / 4 + 32];
    for (size_t i = 0; i < M; i++) t[i] = trit();
    float f = 1.5f;
    uint32_t w;
    memcpy(&w, &f, 4);
    assert(tf_encode_i2s(t, M, w, bytes, sizeof bytes) == (int64_t)sizeof bytes);
    for (size_t k = M / 4 + 4; k < sizeof bytes; k += 3) bytes[k] = 0xA5;
    uint32_t scale[1];
    assert(tf_decode_i2s(bytes, sizeof bytes, M, back, M, scale) == 0);
    int64_t first[1];
    int64_t differ = tmx_reencode(TF_I2_S, R, C, 0, back, scale, 1, bytes, sizeof bytes, again, sizeof again, first);
    assert(differ == tf_i2s_trailer_nonzero(bytes, sizeof bytes, M) && differ == 10 && first[0] == M / 4 + 4);
    assert(tmx_reencode(TF_I2_S, R, C, 0, back, scale, 1, bytes, sizeof bytes - 1, again, sizeof again, first) == TF_ERR_LENGTH);
}

/* Bits per weight and the GGUF metadata share against direct arithmetic: the padding as the
 * distance to the next multiple of the alignment, the quotient of two exact doubles. */
static void test_bits_and_metadata(void) {
    for (int k = 0; k < 20000; k++) {
        uint64_t name = next_u32() % 200, dims = 1 + next_u32() % 4, data = next_u32() % 100000000u;
        uint64_t alignment = (uint64_t)1 << (next_u32() % 12);
        uint64_t padded = (data + alignment - 1) / alignment * alignment;
        assert(tmx_gguf_metadata_bytes(name, dims, data, alignment) == (int64_t)(24 + name + 8 * dims + padded - data));
        uint64_t weights = 1 + next_u32() % 100000000u;
        assert(tmx_bits_per_weight(data, weights) == (double)(8 * data) / (double)weights);
    }
    /* The case of the spec's own test (tfs_llama_gguf_tensor_bits(21, 2, 25067520, 32) == 200540648 in
     * specs/formats/llama_cpp.t27): Bonsai blk.0.ffn_down.weight in Q2_0. */
    assert(8 * (tmx_gguf_metadata_bytes(21, 2, 25067520, 32) + 25067520) == 200540648);
    assert(tmx_gguf_metadata_bytes(21, 2, 1, 48) == TF_ERR_FORMAT && tmx_bits_per_weight(1, 0) == 0.0);
}

int main(void) {
    test_tmx_exact_words(); /* the test blocks of t27/matrix.t27 itself */
    test_tmx_small_cells();
    test_tmx_metadata_share();
    test_bits_and_metadata();
    test_exact_words();
    test_bf16_round();
    test_layouts();
    test_representable_and_round_trips();
    test_compare_and_ties();
    test_reencode_trailer();
    printf("PASS matrix: exact scale words, bf16 rounding, layouts, representability and round trips of 10 formats, "
           "comparisons, tie explanations, code positions, re-encoding, bits per weight and GGUF metadata share\n");
    return 0;
}
