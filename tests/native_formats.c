/* Independent acceptance harness for t27/formats.t27. The reference decoders
 * below follow the loop order of the upstream sources pinned in specs/formats/
 * (llama.cpp e6ab7c1a, PrismML fork bdc23b56, microsoft/BitNet 0b341e58,
 * transformers 2c4914fb, onnxruntime 2ecddca3), not the index formulas of the
 * t27 module, so a shared mistake cannot pass both. */
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
double tm_json_strtod(uint8_t *);
#include "json.h"
#include "formats.h"

static uint32_t random_state = 2709;
static uint32_t next_random(void) {
    random_state = random_state * UINT32_C(1664525) + UINT32_C(1013904223);
    return random_state >> 8;
}

/* ---- reference decoders (upstream loop order) ---------------------------- */
static const uint8_t pow3[6] = {1, 3, 9, 27, 81, 243};

/* llama.cpp dequantize_row_tq1_0 / PrismML dequantize_row_ptq1_0: stages of
 * `wide` and `wide/2` bytes with five digits, then a qh tail with four. */
static void ref_b3(const uint8_t *qs, size_t qs_size, const uint8_t *qh, size_t qh_size,
                   size_t wide, int32_t *y) {
    size_t j = 0;
    for (; j + wide <= qs_size - qs_size % wide; j += wide)
        for (size_t n = 0; n < 5; ++n)
            for (size_t m = 0; m < wide; ++m) {
                uint8_t q = (uint8_t)(qs[j + m] * pow3[n]);
                *y++ = (int32_t)(((uint16_t)q * 3) >> 8) - 1;
            }
    for (; j < qs_size; j += wide / 2)
        for (size_t n = 0; n < 5; ++n)
            for (size_t m = 0; m < wide / 2; ++m) {
                uint8_t q = (uint8_t)(qs[j + m] * pow3[n]);
                *y++ = (int32_t)(((uint16_t)q * 3) >> 8) - 1;
            }
    for (size_t n = 0; n < 4; ++n)
        for (size_t k = 0; k < qh_size; ++k) {
            uint8_t q = (uint8_t)(qh[k] * pow3[n]);
            *y++ = (int32_t)(((uint16_t)q * 3) >> 8) - 1;
        }
}

static void ref_tq2(const uint8_t *qs, int32_t *y) {
    for (size_t j = 0; j < 64; j += 32)
        for (size_t l = 0; l < 4; ++l)
            for (size_t m = 0; m < 32; ++m) *y++ = ((qs[j + m] >> (l * 2)) & 3) - 1;
}

static void ref_pairs(const uint8_t *qs, size_t bytes, int32_t *y) {
    for (size_t b = 0; b < bytes; ++b)
        for (size_t l = 0; l < 4; ++l) *y++ = ((qs[b] >> (2 * l)) & 3) - 1;
}

static void ref_bits(const uint8_t *qs, size_t bytes, int32_t *y) {
    for (size_t b = 0; b < bytes; ++b)
        for (size_t l = 0; l < 8; ++l) *y++ = ((qs[b] >> l) & 1) ? 1 : -1;
}

/* One block of any block format; returns the raw fp16 scale word. */
static uint32_t ref_block(int format, const uint8_t *block, int32_t *y) {
    switch (format) {
    case TF_TQ1_0: ref_b3(block, 48, block + 48, 4, 32, y); return (uint32_t)block[52] | (uint32_t)block[53] << 8;
    case TF_PTQ1_0: ref_b3(block, 24, block + 24, 2, 16, y); return (uint32_t)block[26] | (uint32_t)block[27] << 8;
    case TF_TQ2_0: ref_tq2(block, y); return (uint32_t)block[64] | (uint32_t)block[65] << 8;
    case TF_Q2_0: ref_pairs(block + 2, 16, y); break;
    case TF_PQ2_0: ref_pairs(block + 2, 32, y); break;
    case TF_Q1_0: ref_bits(block + 2, 16, y); break;
    default: assert(0);
    }
    return (uint32_t)block[0] | (uint32_t)block[1] << 8;
}

/* bitnet.cpp canonical I2_S (QK_I2_S = 128): byte p of block b holds
 * c[128b+p]<<6 | c[128b+p+32]<<4 | c[128b+p+64]<<2 | c[128b+p+96]. */
static void ref_i2s(const uint8_t *data, size_t count, int32_t *y) {
    for (size_t b = 0; b < count / 128; ++b)
        for (size_t p = 0; p < 32; ++p) {
            uint8_t v = data[32 * b + p];
            int32_t c0 = v >> 6, c1 = (v >> 4) & 3, c2 = (v >> 2) & 3, c3 = v & 3;
            y[128 * b + p] = c0 == 3 ? 3 : c0 - 1;
            y[128 * b + p + 32] = c1 == 3 ? 3 : c1 - 1;
            y[128 * b + p + 64] = c2 == 3 ? 3 : c2 - 1;
            y[128 * b + p + 96] = c3 == 3 ? 3 : c3 - 1;
        }
}

/* transformers integrations/bitnet.py unpack_weights. */
static void ref_hf(const uint8_t *packed, size_t rows, size_t cols, int32_t *y) {
    size_t stride = rows / 4;
    for (size_t i = 0; i < 4; ++i)
        for (size_t r = 0; r < stride; ++r)
            for (size_t k = 0; k < cols; ++k)
                y[(i * stride + r) * cols + k] = ((packed[r * cols + k] >> (2 * i)) & 3) - 1;
}

/* llama.cpp quantize_row_tq1_0_ref packing, fed with codes instead of floats. */
static void ref_b3_encode(const int32_t *t, size_t wide, uint8_t *qs, uint8_t *qh) {
    size_t e = 0, out = 0;
    for (size_t m = 0; m < wide; ++m) {
        unsigned q = 0;
        for (size_t n = 0; n < 5; ++n) q = q * 3 + (unsigned)(t[n * wide + m] + 1);
        qs[out++] = (uint8_t)((q * 256 + 242) / 243);
    }
    e = 5 * wide;
    for (size_t m = 0; m < wide / 2; ++m) {
        unsigned q = 0;
        for (size_t n = 0; n < 5; ++n) q = q * 3 + (unsigned)(t[e + n * (wide / 2) + m] + 1);
        qs[out++] = (uint8_t)((q * 256 + 242) / 243);
    }
    e += 5 * (wide / 2);
    for (size_t k = 0; k < wide / 8; ++k) {
        unsigned q = 0;
        for (size_t n = 0; n < 4; ++n) q = q * 3 + (unsigned)(t[e + n * (wide / 8) + k] + 1);
        q *= 3;
        qh[k] = (uint8_t)((q * 256 + 242) / 243);
    }
}

/* ---- tests --------------------------------------------------------------- */
static const int block_formats[] = {TF_TQ1_0, TF_TQ2_0, TF_Q2_0, TF_Q1_0, TF_PQ2_0, TF_PTQ1_0};

static void differential_random_blocks(void) {
    enum { BLOCKS = 7 };
    static uint8_t data[BLOCKS * 66], again[BLOCKS * 66];
    static int32_t got[BLOCKS * 256], want[BLOCKS * 256];
    static uint32_t scales[BLOCKS];
    for (size_t f = 0; f < sizeof block_formats / sizeof block_formats[0]; ++f) {
        int format = block_formats[f];
        size_t per = tf_block_elements(format), bytes = tf_block_bytes(format);
        for (int round = 0; round < 400; ++round) {
            for (size_t i = 0; i < BLOCKS * bytes; ++i) data[i] = (uint8_t)next_random();
            int64_t outside = tf_decode_blocks(format, data, BLOCKS * bytes, BLOCKS * per,
                                               got, BLOCKS * per, scales, BLOCKS);
            assert(outside >= 0);
            int64_t expected_outside = 0;
            for (size_t b = 0; b < BLOCKS; ++b) {
                assert(ref_block(format, data + b * bytes, want + b * per) == scales[b]);
            }
            for (size_t i = 0; i < BLOCKS * per; ++i) {
                assert(got[i] == want[i]);
                if (want[i] > 1) ++expected_outside;
            }
            assert(outside == expected_outside);
            /* Any stored pattern re-encodes exactly when its codes are allowed,
             * except base-3 bytes the ceiling rule never writes. */
            if (outside != 0 || format == TF_TQ1_0 || format == TF_PTQ1_0) continue;
            assert(tf_encode_blocks(format, got, BLOCKS * per, scales, BLOCKS, again, sizeof again)
                   == (int64_t)(BLOCKS * bytes));
            assert(memcmp(data, again, BLOCKS * bytes) == 0);
        }
    }
}

static void base3_round_trip_and_canonical_bytes(void) {
    static int32_t t[256], back[256];
    static uint8_t mine[54], ref[54];
    uint32_t scale = 0x3c00;
    for (int wide = 16; wide <= 32; wide += 16) {
        int format = wide == 32 ? TF_TQ1_0 : TF_PTQ1_0;
        size_t per = tf_block_elements(format), bytes = tf_block_bytes(format);
        for (int round = 0; round < 2000; ++round) {
            for (size_t i = 0; i < per; ++i) t[i] = (int32_t)(next_random() % 3) - 1;
            assert(tf_encode_blocks(format, t, per, &scale, 1, mine, sizeof mine) == (int64_t)bytes);
            memset(ref, 0, sizeof ref);
            ref_b3_encode(t, (size_t)wide, ref, ref + (wide == 32 ? 48 : 24));
            assert(memcmp(mine, ref, bytes - 2) == 0);
            assert(tf_decode_blocks(format, mine, bytes, per, back, per, &scale, 1) == 0);
            assert(memcmp(t, back, per * sizeof(int32_t)) == 0);
        }
    }
    /* Every one of the 243 five-digit groups round-trips through one byte. */
    int32_t block[256];
    for (unsigned q = 0; q < 243; ++q) {
        memset(block, 0, sizeof block);
        unsigned n = q;
        for (int d = 4; d >= 0; --d) { block[d * 32] = (int32_t)(n % 3) - 1; n /= 3; }
        assert(tf_encode_blocks(TF_TQ1_0, block, 256, &scale, 1, mine, sizeof mine) == 54);
        assert(mine[0] == (uint8_t)((q * 256 + 242) / 243));
        assert(tf_decode_blocks(TF_TQ1_0, mine, 54, 256, back, 256, &scale, 1) == 0);
        for (int d = 0; d < 5; ++d) assert(back[d * 32] == block[d * 32]);
    }
}

static void worked_examples(void) {
    int32_t y[256];
    uint32_t s[1];
    uint8_t q1[18] = {0x00, 0x34, 0xA5, 0x03};
    int32_t want_q1[10] = {1, -1, 1, -1, -1, 1, -1, 1, 1, 1};
    assert(tf_decode_blocks(TF_Q1_0, q1, 18, 128, y, 256, s, 1) == 0 && s[0] == 0x3400);
    assert(memcmp(y, want_q1, sizeof want_q1) == 0);

    uint8_t q2[18] = {0x00, 0x34, 0x1B, 0xE4, 0x06};
    int32_t want_q2[10] = {2, 1, 0, -1, -1, 0, 1, 2, 1, 0};
    assert(tf_decode_blocks(TF_Q2_0, q2, 18, 64, y, 256, s, 1) == 2);
    assert(memcmp(y, want_q2, sizeof want_q2) == 0);

    uint8_t tq2[66] = {0x06, 0x00, 0x01, 0x02, 0x0A, 0x05, 0x09, 0x08, 0x04, 0x02};
    tq2[64] = 0x00; tq2[65] = 0x34;
    int32_t want_tq2[10] = {1, -1, 0, 1, 1, 0, 0, -1, -1, 1};
    assert(tf_decode_blocks(TF_TQ2_0, tq2, 66, 256, y, 256, s, 1) >= 0 && s[0] == 0x3400);
    assert(memcmp(y, want_tq2, sizeof want_tq2) == 0 && y[32] == 0);

    uint8_t tq1[54] = {0xCF, 0x00, 0x56, 0xAB, 0xFF, 0x7A, 0x32, 0xF3, 0x80, 0xA2};
    tq1[52] = 0x00; tq1[53] = 0x34;
    int32_t want_tq1[10] = {1, -1, 0, 1, 1, 0, -1, 1, 0, 0};
    assert(tf_decode_blocks(TF_TQ1_0, tq1, 54, 256, y, 256, s, 1) == 0);
    assert(memcmp(y, want_tq1, sizeof want_tq1) == 0);
    assert(y[32] == 0 && y[64] == -1 && y[96] == 1 && y[128] == 0);

    /* Codes of the upstream quantizer example: x0=1, x1=0.5, x2=-0.5,
     * x3=0.49999997, x64=-1, x96=1 with d = 1.0 (0x3C00). */
    int32_t codes[256] = {0};
    codes[0] = 1; codes[1] = 1; codes[2] = -1; codes[64] = -1; codes[96] = 1;
    uint32_t one[4] = {0x3c00, 0x3c00, 0x3c00, 0x3c00};
    uint8_t out[4 * 18];
    assert(tf_encode_blocks(TF_Q2_0, codes, 256, one, 4, out, sizeof out) == 72);
    assert(out[0] == 0x00 && out[1] == 0x3C && out[2] == 0x4A && out[3] == 0x55);
    assert(tf_encode_blocks(TF_TQ2_0, codes, 256, one, 1, out, sizeof out) == 66);
    assert(out[0] == 0x86 && out[1] == 0x56 && out[2] == 0x54 && out[3] == 0x55);
    uint8_t out1[54];
    assert(tf_encode_blocks(TF_TQ1_0, codes, 256, one, 1, out1, sizeof out1) == 54);
    assert(out1[0] == 0xCF && out1[1] == 0xD5 && out1[2] == 0x2B && out1[3] == 0x80);
    assert(out1[52] == 0x00 && out1[53] == 0x3C);

    /* PrismML PQ2_0 and PTQ1_0 examples. */
    int32_t p[128] = {0};
    p[0] = 1; p[1] = 0; p[2] = -1; p[3] = 1;
    uint32_t half = 0x3800;
    uint8_t pq[34];
    assert(tf_encode_blocks(TF_PQ2_0, p, 128, &half, 1, pq, sizeof pq) == 34);
    assert(pq[0] == 0x00 && pq[1] == 0x38 && pq[2] == 0x86);
    memset(p, 0, sizeof p);
    p[0] = 1; p[16] = 0; p[32] = -1; p[48] = 1; p[64] = 0;
    p[120] = -1; p[122] = 1; p[124] = 0; p[126] = 1;
    uint8_t pt[28];
    assert(tf_encode_blocks(TF_PTQ1_0, p, 128, &half, 1, pt, sizeof pt) == 28);
    assert(pt[0] == 0xCF && pt[24] == 0x49 && pt[26] == 0x00 && pt[27] == 0x38);
}

static void i2s(void) {
    enum { N = 128 * 5 };
    static int32_t t[N], back[N], ref[N];
    static uint8_t data[N / 4 + 32];
    uint32_t scale;
    memset(t, 0, sizeof t);
    t[0] = 1; t[32] = 0; t[64] = -1; t[96] = 1;
    assert(tf_encode_i2s(t, 128, 0x3f800000, data, sizeof data) == 128 / 4 + 32);
    assert(data[0] == 0x92);
    assert(data[32] == 0x00 && data[33] == 0x00 && data[34] == 0x80 && data[35] == 0x3f);
    for (int round = 0; round < 300; ++round) {
        for (size_t i = 0; i < N; ++i) t[i] = (int32_t)(next_random() % 3) - 1;
        assert(tf_encode_i2s(t, N, 0x3d4ccccd, data, sizeof data) == N / 4 + 32);
        assert(tf_decode_i2s(data, sizeof data, N, back, N, &scale) == 0 && scale == 0x3d4ccccd);
        ref_i2s(data, N, ref);
        assert(memcmp(back, t, sizeof t) == 0 && memcmp(ref, t, sizeof t) == 0);
        /* Random payload bytes, including code 3, against the reference. */
        for (size_t i = 0; i < N / 4; ++i) data[i] = (uint8_t)next_random();
        int64_t outside = tf_decode_i2s(data, sizeof data, N, back, N, &scale);
        ref_i2s(data, N, ref);
        int64_t threes = 0;
        for (size_t i = 0; i < N; ++i) threes += ref[i] == 3;
        assert(outside == threes && memcmp(back, ref, sizeof ref) == 0);
    }
    assert(tf_i2s_trailer_nonzero(data, sizeof data, N) == 0);
    data[N / 4 + 31] = 1;
    data[N / 4 + 4] = 7;
    assert(tf_decode_i2s(data, sizeof data, N, back, N, &scale) >= 0);
    assert(tf_i2s_trailer_nonzero(data, sizeof data, N) == 2);
    assert(tf_i2s_trailer_nonzero(data, sizeof data - 1, N) == TF_ERR_LENGTH);
    assert(tf_decode_i2s(data, sizeof data - 1, N, back, N, &scale) == TF_ERR_LENGTH);
    assert(tf_decode_i2s(data, sizeof data, 100, back, N, &scale) == TF_ERR_LENGTH);
}

static void hf_packed(void) {
    enum { ROWS = 12, COLS = 7 };
    static int32_t t[ROWS * COLS], back[ROWS * COLS], ref[ROWS * COLS];
    static uint8_t packed[ROWS / 4 * COLS];
    memset(t, 0, sizeof t);
    t[0 * COLS] = 1; t[3 * COLS] = 0; t[6 * COLS] = -1; t[9 * COLS] = 1;
    assert(tf_encode_hf_packed(t, ROWS, COLS, packed, sizeof packed) == ROWS / 4 * COLS);
    assert(packed[0] == 0x86);
    for (int round = 0; round < 300; ++round) {
        for (size_t i = 0; i < ROWS * COLS; ++i) t[i] = (int32_t)(next_random() % 3) - 1;
        assert(tf_encode_hf_packed(t, ROWS, COLS, packed, sizeof packed) == ROWS / 4 * COLS);
        assert(tf_decode_hf_packed(packed, sizeof packed, ROWS, COLS, back, ROWS * COLS) == 0);
        ref_hf(packed, ROWS, COLS, ref);
        assert(memcmp(back, t, sizeof t) == 0 && memcmp(ref, t, sizeof t) == 0);
    }
    assert(tf_decode_hf_packed(packed, sizeof packed, 10, COLS, back, ROWS * COLS) == TF_ERR_LENGTH);
}

static void linear2(void) {
    int32_t t[4] = {1, 0, -1, 1}, back[4];
    uint8_t row[4];
    assert(tf_encode_linear2(t, 1, 4, 4, 2, row, sizeof row) == 4 && row[0] == 0xDB);
    assert(tf_encode_linear2(t, 1, 4, 4, 1, row, sizeof row) == 4 && row[0] == 0x86);
    assert(tf_decode_linear2(row, 4, 1, 4, 4, 1, back, 4) == 0 && memcmp(back, t, sizeof t) == 0);
    /* The same bytes read with ONNX's default zero point shift every value. */
    assert(tf_decode_linear2(row, 4, 1, 4, 4, 2, back, 4) == 1 && back[0] == 0 && back[2] == -2);
    enum { ROWS = 5, COLS = 70, ROW_BYTES = 32 };  /* bs = 64 pads 70 -> 128 weights */
    static int32_t w[ROWS * COLS], w2[ROWS * COLS];
    static uint8_t data[ROWS * ROW_BYTES];
    for (int round = 0; round < 200; ++round) {
        for (size_t i = 0; i < ROWS * COLS; ++i) w[i] = (int32_t)(next_random() % 3) - 1;
        assert(tf_encode_linear2(w, ROWS, COLS, ROW_BYTES, 2, data, sizeof data) == sizeof data);
        assert(tf_decode_linear2(data, sizeof data, ROWS, COLS, ROW_BYTES, 2, w2, ROWS * COLS) == 0);
        assert(memcmp(w, w2, sizeof w) == 0);
        for (size_t n = 0; n < ROWS; ++n)
            for (size_t k = 0; k < COLS; ++k)
                assert((((data[n * ROW_BYTES + k / 4] >> (2 * (k % 4))) & 3) - 2) == w[n * COLS + k]);
    }
}

static void scale_words(void) {
    assert(tf_f16_value(0x3c00) == 1.0 && tf_f16_value(0x3400) == 0.25 && tf_f16_value(0xc000) == -2.0);
    assert(tf_f16_value(0x0001) == 5.9604644775390625e-08 && tf_f16_value(0x7bff) == 65504.0);
    assert(!tf_f16_finite(0x7c00) && tf_f16_finite(0x7bff));
    assert(tf_f32_value(0x3f800000) == 1.0 && tf_f32_value(0x3dcccccd) == (double)0.1f && tf_f32_value(0x3d4ccccd) == (double)0.05f);
    assert(tf_f32_value(0x00000001) == 1.401298464324817e-45);
    assert(tf_bf16_value(0x3f80) == 1.0 && tf_bf16_value(0xbe00) == -0.125);
}

/* ---- containers ---------------------------------------------------------- */
static size_t put(uint8_t *out, size_t at, const void *src, size_t n) { memcpy(out + at, src, n); return at + n; }
static size_t put_u32(uint8_t *out, size_t at, uint32_t v) {
    for (int i = 0; i < 4; ++i) out[at + i] = (uint8_t)(v >> (8 * i));
    return at + 4;
}
static size_t put_u64(uint8_t *out, size_t at, uint64_t v) {
    for (int i = 0; i < 8; ++i) out[at + i] = (uint8_t)(v >> (8 * i));
    return at + 8;
}
static size_t put_str(uint8_t *out, size_t at, const char *s) {
    at = put_u64(out, at, strlen(s));
    return put(out, at, s, strlen(s));
}

static size_t build_gguf(uint8_t *g, uint32_t alignment, bool prism, uint64_t *second_offset) {
    size_t at = 0;
    at = put_u32(g, at, 0x46554747);
    at = put_u32(g, at, 3);
    at = put_u64(g, at, 2);
    at = put_u64(g, at, prism ? 4 : 3);
    at = put_str(g, at, "general.architecture");
    at = put_u32(g, at, 8);
    at = put_str(g, at, "bitnet-b1.58");
    at = put_str(g, at, "tokenizer.ggml.tokens");
    at = put_u32(g, at, 9);
    at = put_u32(g, at, 8);
    at = put_u64(g, at, 3);
    at = put_str(g, at, "<s>");
    at = put_str(g, at, "hello");
    at = put_str(g, at, "");
    at = put_str(g, at, "general.alignment");
    at = put_u32(g, at, 4);
    at = put_u32(g, at, alignment);
    if (prism) {
        at = put_str(g, at, "prism.hadamard.block_size");
        at = put_u32(g, at, 10);
        at = put_u64(g, at, 1024);
    }
    at = put_str(g, at, "blk.0.attn_q.weight");
    at = put_u32(g, at, 2);
    at = put_u64(g, at, 512);
    at = put_u64(g, at, 3);
    at = put_u32(g, at, 35);
    at = put_u64(g, at, 0);
    uint64_t tq2_bytes = 512 / 256 * 66 * 3;
    *second_offset = (tq2_bytes + alignment - 1) / alignment * alignment;
    at = put_str(g, at, "blk.0.ffn_down.weight");
    at = put_u32(g, at, 2);
    at = put_u64(g, at, 256);
    at = put_u64(g, at, 4);
    at = put_u32(g, at, prism ? 143 : 36);
    at = put_u64(g, at, *second_offset);
    return at;
}

static void gguf(void) {
    static uint8_t g[4096];
    uint64_t second_offset = 0;
    size_t end = build_gguf(g, 64, false, &second_offset);
    uint64_t start = (end + 63) / 64 * 64;
    TFTensorInfo info;
    uint8_t name_q[] = "blk.0.attn_q.weight", name_d[] = "blk.0.ffn_down.weight", name_x[] = "blk.9.nope";
    assert(tf_gguf_find(g, end, name_q, sizeof name_q - 1, &info) == 0);
    assert(info.tensor_type == 35 && info.dims == 2 && info.d0 == 512 && info.d1 == 3);
    assert(info.alignment == 64 && info.data_start == start && info.offset == 0 && !info.prism);
    assert(tf_gguf_tensor_bytes(&info) == 2 * 66 * 3);
    assert(tf_gguf_find(g, end, name_d, sizeof name_d - 1, &info) == 0);
    assert(info.tensor_type == 36 && info.offset == second_offset && info.d0 == 256 && info.d1 == 4);
    assert(tf_gguf_tensor_bytes(&info) == 1024 / 4 + 32);
    assert(tf_gguf_find(g, end, name_x, sizeof name_x - 1, &info) == TF_ERR_NOT_FOUND);
    assert(tf_gguf_nth(g, end, 1, &info) == 0 && info.tensors == 2 && info.tensor_type == 36);
    assert(info.name_size == sizeof name_d - 1 && memcmp(g + info.name_at, name_d, info.name_size) == 0);
    assert(tf_gguf_nth(g, end, 2, &info) == TF_ERR_NOT_FOUND);
    /* Every shorter prefix asks for more bytes, and never for more than the header. */
    for (size_t size = 0; size < end; ++size) {
        assert(tf_gguf_find(g, size, name_d, sizeof name_d - 1, &info) == TF_ERR_TRUNCATED);
        assert(info.needed > size && info.needed <= end);
    }
    end = build_gguf(g, 32, true, &second_offset);
    assert(tf_gguf_find(g, end, name_d, sizeof name_d - 1, &info) == 0);
    assert(info.prism && info.tensor_type == 143 && tf_format_of_ggml(143, true) == TF_PTQ1_0);
    assert(tf_format_of_ggml(143, false) == 0);
    assert(tf_gguf_tensor_bytes(&info) == 1024 / 128 * 28);
    g[4] = 2;
    assert(tf_gguf_find(g, end, name_d, sizeof name_d - 1, &info) == TF_ERR_CONTAINER);
}

static void safetensors(void) {
    static TMJsonToken tokens[256];
    static uint8_t arena[4096], file[1024];
    const char *json =
        "{\"__metadata__\":{\"format\":\"pt\"},"
        "\"model.layers.0.mlp.down_proj.weight\":{\"dtype\":\"U8\",\"shape\":[4,8],\"data_offsets\":[0,32]},"
        "\"model.layers.0.mlp.down_proj.weight_scale\":{\"dtype\":\"BF16\",\"shape\":[1],\"data_offsets\":[32,34]}}";
    size_t n = strlen(json);
    put_u64(file, 0, n);
    memcpy(file + 8, json, n);
    TFSafeInfo info;
    uint8_t weight[] = "model.layers.0.mlp.down_proj.weight";
    uint8_t scale[] = "model.layers.0.mlp.down_proj.weight_scale";
    uint8_t missing[] = "lm_head.weight";
    assert(tf_safetensors_find(file, 8 + n, weight, sizeof weight - 1, tokens, 256, arena, sizeof arena, &info) == 0);
    assert(info.dtype_size == 2 && memcmp(info.dtype, "U8", 2) == 0);
    assert(info.dims == 2 && info.d0 == 4 && info.d1 == 8 && info.begin == 8 + n && info.end == 8 + n + 32);
    assert(tf_safetensors_find(file, 8 + n, scale, sizeof scale - 1, tokens, 256, arena, sizeof arena, &info) == 0);
    assert(info.dtype_size == 4 && info.dims == 1 && info.d0 == 1 && info.begin == 8 + n + 32);
    assert(tf_safetensors_find(file, 8 + n, missing, sizeof missing - 1, tokens, 256, arena, sizeof arena, &info)
           == TF_ERR_NOT_FOUND);
    assert(tf_safetensors_find(file, 8 + n - 1, weight, sizeof weight - 1, tokens, 256, arena, sizeof arena, &info)
           == TF_ERR_TRUNCATED && info.needed == 8 + n);
    file[8] = '[';
    assert(tf_safetensors_find(file, 8 + n, weight, sizeof weight - 1, tokens, 256, arena, sizeof arena, &info)
           == TF_ERR_CONTAINER);
}

static void absmean(void) {
    /* bf16 words: 1.0, -0.5, 0.25, 0.0 -> mean 0.4375, s = 1/0.4375;
     * w*s = 2.2857, -1.1428, 0.5714, 0 -> +1, -1, +1, 0. */
    uint8_t w[8] = {0x80, 0x3f, 0x00, 0xbf, 0x80, 0x3e, 0x00, 0x00};
    int32_t t[4];
    double r[2];
    assert(tf_absmean_bf16(w, 4, 1e-5, t, 4, r) == 0);
    assert(r[0] == 0.4375 && t[0] == 1 && t[1] == -1 && t[2] == 1 && t[3] == 0);
    /* Exactly half after scaling rounds to zero (half to even): 1.0 and 0.5 and
     * 0.5 and 0.0 -> mean 0.5, s = 2: 2, 1, 1, 0 are all away from 0.5 ... use
     * 1.0, 0.25, 0.25, 0.5: mean 0.5, s = 2, 0.25*2 = 0.5 exactly -> 0. */
    uint8_t h[8] = {0x80, 0x3f, 0x80, 0x3e, 0x80, 0x3e, 0x00, 0x3f};
    assert(tf_absmean_bf16(h, 4, 1e-9, t, 4, r) == 2);
    assert(r[1] == 2.0 && t[0] == 1 && t[1] == 0 && t[2] == 0 && t[3] == 1);
    int32_t a[5] = {1, 0, -1, 1, 0}, b[5] = {1, 0, 1, 1, -1};
    int64_t first;
    assert(tf_mismatches(a, b, 5, &first) == 2 && first == 2);
    assert(tf_mismatches(a, a, 5, &first) == 0 && first == -1);
}

static void scale_layouts(void) {
    /* One scale per 128 weights (f16) against one per 64 written twice. */
    uint32_t g128[3] = {0x3c00, 0x3400, 0xb800}, g64[6] = {0x3c00, 0x3c00, 0x3400, 0x3400, 0xb800, 0xb800};
    int64_t first;
    assert(tf_compare_scales(g128, 3, TF_KIND_F16, 128, g64, 6, TF_KIND_F16, 64, 384, &first) == 0 && first == -1);
    g64[3] = 0x3401;
    assert(tf_compare_scales(g128, 3, TF_KIND_F16, 128, g64, 6, TF_KIND_F16, 64, 384, &first) == 64 && first == 192);
    /* bf16 0x3f80 == f16 0x3c00 == 1.0; a per-tensor f32 scale covers all. */
    uint32_t bf[1] = {0x3f80}, f32[1] = {0x3f800000}, h[2] = {0x3c00, 0x3c00};
    assert(tf_compare_scales(bf, 1, TF_KIND_BF16, 256, h, 2, TF_KIND_F16, 128, 256, &first) == 0);
    assert(tf_compare_scales(f32, 1, TF_KIND_F32, 1000, bf, 1, TF_KIND_BF16, 1000, 1000, &first) == 0);
    assert(tf_compare_scales(g128, 2, TF_KIND_F16, 128, g64, 6, TF_KIND_F16, 64, 384, &first) == TF_ERR_LENGTH);
    uint32_t scales[3] = {0x3f80, 0x3e80, 0x3f80}, biases[3] = {0xbf80, 0xbe80, 0x3f80};
    assert(tf_affine_not_ternary(scales, biases, 3, TF_KIND_BF16) == 1);
    uint8_t words[6] = {0x00, 0x3c, 0x01, 0x00, 0xff, 0xff};
    uint32_t out[3];
    assert(tf_words(words, 6, 2, out, 3) == 3 && out[0] == 0x3c00 && out[1] == 1 && out[2] == 0xffff);
    assert(tf_words(words, 6, 4, out, 1) == TF_ERR_LENGTH);
}

static void rejections(void) {
    int32_t codes[256] = {0};
    uint32_t scale = 0x3c00;
    uint8_t out[66];
    codes[5] = 2;
    assert(tf_encode_blocks(TF_TQ1_0, codes, 256, &scale, 1, out, sizeof out) == TF_ERR_CODE);
    assert(tf_encode_blocks(TF_TQ2_0, codes, 256, &scale, 1, out, sizeof out) == 66);
    codes[5] = 0;
    assert(tf_encode_blocks(TF_Q1_0, codes, 128, &scale, 1, out, sizeof out) == TF_ERR_CODE);
    assert(tf_encode_blocks(TF_TQ2_0, codes, 200, &scale, 1, out, sizeof out) == TF_ERR_LENGTH);
    assert(tf_encode_blocks(TF_TQ2_0, codes, 256, &scale, 1, out, 65) == TF_ERR_CAPACITY);
    assert(tf_encode_blocks(99, codes, 256, &scale, 1, out, sizeof out) == TF_ERR_FORMAT);
    int32_t y[256];
    assert(tf_decode_blocks(TF_TQ2_0, out, 65, 256, y, 256, &scale, 1) == TF_ERR_LENGTH);
    assert(tf_decode_blocks(TF_TQ2_0, out, 66, 256, y, 255, &scale, 1) == TF_ERR_CAPACITY);
}

int main(void) {
    differential_random_blocks();
    base3_round_trip_and_canonical_bytes();
    worked_examples();
    i2s();
    hf_packed();
    linear2();
    scale_words();
    gguf();
    safetensors();
    absmean();
    scale_layouts();
    rejections();
    puts("PASS formats: TQ1_0, TQ2_0, Q2_0, Q1_0, PQ2_0, PTQ1_0, I2_S, HF packed, linear 2-bit, GGUF, safetensors");
    return 0;
}
