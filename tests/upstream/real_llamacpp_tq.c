/* llama.cpp issue 15193, real tensors: ternary weights of BitNet b1.58 2B4T
 * layer 0 (decoded by t27 from the Hugging Face packed checkpoint) stored
 * with the upstream TQ1_0/TQ2_0 quantizers, read back by the upstream
 * dequantizer and by t27, re-encoded by t27, and multiplied by the generic
 * and the architecture vec_dot kernels. Ternary Bonsai 2 27B layer 0
 * ffn_down (PTQ1_0, group-128 scales) shows what TQ1_0 cannot represent.
 * Usage: real_llamacpp_tq FIXTURE_DIR (see llamacpp_fixtures.py). */
#include "llamacpp_harness.h"

static uint8_t *slurp(const char *dir, const char *name, size_t *size) {
    char path[4096];
    snprintf(path, sizeof path, "%s/%s", dir, name);
    FILE *f = fopen(path, "rb");
    if (!f) { perror(path); exit(2); }
    fseek(f, 0, SEEK_END); *size = (size_t)ftell(f); fseek(f, 0, SEEK_SET);
    uint8_t *p = malloc(*size ? *size : 1);
    if (!p || fread(p, 1, *size, f) != *size) { fprintf(stderr, "%s: read failed\n", path); exit(2); }
    fclose(f);
    return p;
}
static void *xmalloc(size_t n) { void *p = malloc(n ? n : 1); if (!p) { fprintf(stderr, "out of memory\n"); exit(2); } return p; }
static uint64_t rs = 0x15193ull;
static double urand(void) { rs ^= rs << 13; rs ^= rs >> 7; rs ^= rs << 17; return ((double)(rs >> 11) + 0.5) / 9007199254740992.0; }
static double nrand(void) { return sqrt(-2.0 * log(urand())) * cos(6.283185307179586 * urand()); }
static float bf16f(uint16_t b) { uint32_t u = (uint32_t)b << 16; float f; memcpy(&f, &u, 4); return f; }

static int bitnet(const char *dir, const char *label, const char *stem, size_t rows, size_t cols) {
    char name[256];
    size_t psize, ssize;
    snprintf(name, sizeof name, "%s.packed", stem); uint8_t *packed = slurp(dir, name, &psize);
    snprintf(name, sizeof name, "%s.scale", stem); uint8_t *sb = slurp(dir, name, &ssize);
    if (psize != rows / 4 * cols || ssize != 2) { fprintf(stderr, "%s: unexpected fixture sizes\n", stem); exit(2); }
    int32_t *t = xmalloc(rows * cols * sizeof *t);
    int64_t outside = tf_decode_hf_packed(packed, psize, rows, cols, t, rows * cols);
    float s = bf16f((uint16_t)(sb[0] | sb[1] << 8));
    size_t nb = cols / QK_K;
    block_tq1_0 *x1 = xmalloc(nb * sizeof *x1); block_tq2_0 *x2 = xmalloc(nb * sizeof *x2); block_q8_K *y = xmalloc(nb * sizeof *y);
    float *w = xmalloc(cols * 4), *deq = xmalloc(cols * 4), *a = xmalloc(cols * 4), *yd = xmalloc(nb * 4);
    int32_t *back = xmalloc(cols * 4); uint32_t *sc = xmalloc(nb * 4); uint8_t *enc = xmalloc(nb * 66);
    uint16_t *xd = xmalloc(nb * 2);
    for (size_t i = 0; i < cols; ++i) a[i] = (float)nrand();
    quantize_row_q8_K_ref(a, y, (int64_t)cols);
    for (size_t b = 0; b < nb; ++b) yd[b] = y[b].d;
    printf("%s: HF packed %zu x %zu, t27 tf_decode_hf_packed status %lld (codes outside ternary), weight_scale bf16 = %.9g (fp16-exact: %s)\n",
           label, rows, cols, (long long)outside, s, h2f(f2h(s)) == s ? "yes" : "no");
    int fail = outside != 0;
    const float cs[2] = {s, 1.0f / s};
    const char *cn[2] = {"c = weight_scale (bf16)", "c = 1/weight_scale (f32)"};
    for (int k = 0; k < 2; ++k) {
        float c = cs[k];
        long trit_bad[2] = {0, 0}, byte_bad[2] = {0, 0}, val_bad[2] = {0, 0}, int_bad[2] = {0, 0};
        double worst[2] = {0, 0};
        for (size_t r = 0; r < rows; ++r) {
            const int32_t *tr = t + r * cols;
            for (size_t i = 0; i < cols; ++i) w[i] = (float)tr[i] * c;
            for (int f = 0; f < 2; ++f) {
                uint8_t *bytes = f ? (uint8_t *)x2 : (uint8_t *)x1;
                size_t bb = f ? 66 : 54;
                if (f) { quantize_row_tq2_0_ref(w, x2, (int64_t)cols); dequantize_row_tq2_0(x2, deq, (int64_t)cols); }
                else   { quantize_row_tq1_0_ref(w, x1, (int64_t)cols); dequantize_row_tq1_0(x1, deq, (int64_t)cols); }
                /* storage: t27 reads the upstream bytes and writes them back */
                int64_t rc = tf_decode_blocks(f ? TF_TQ2_0 : TF_TQ1_0, bytes, nb * bb, cols, back, cols, sc, nb);
                trit_bad[f] += rc != 0;
                for (size_t i = 0; i < cols; ++i) { trit_bad[f] += back[i] != tr[i]; val_bad[f] += deq[i] != (float)tr[i] * c; }
                int64_t n = tf_encode_blocks(f ? TF_TQ2_0 : TF_TQ1_0, back, cols, sc, nb, enc, nb * bb);
                byte_bad[f] += (n != (int64_t)(nb * bb)) || memcmp(enc, bytes, nb * bb);
                /* float matvec row through the arch kernel vs a double reference */
                float got;
                if (f) ggml_vec_dot_tq2_0_q8_K((int)cols, &got, 0, x2, 0, y, 0, 1);
                else   ggml_vec_dot_tq1_0_q8_K((int)cols, &got, 0, x1, 0, y, 0, 1);
                double ref = 0, mag = 0;
                for (size_t b = 0; b < nb; ++b) {
                    long si = 0;
                    for (int j = 0; j < QK_K; ++j) si += (long)tr[b * QK_K + j] * y[b].qs[j];
                    double term = (double)si * h2f(f ? x2[b].d : x1[b].d) * y[b].d;
                    ref += term; mag += fabs(term);
                }
                if (fabs(got - ref) / fmax(mag, 1e-30) > worst[f]) worst[f] = fabs(got - ref) / fmax(mag, 1e-30);
                /* integer accumulators: unit scales make both kernels return the exact integer */
                for (size_t b = 0; b < nb; ++b) {
                    xd[b] = f ? x2[b].d : x1[b].d;
                    if (f) x2[b].d = 0x3C00; else x1[b].d = 0x3C00;
                    y[b].d = 1.0f;
                }
                float gi, gg;
                if (f) { ggml_vec_dot_tq2_0_q8_K((int)cols, &gi, 0, x2, 0, y, 0, 1); ggml_vec_dot_tq2_0_q8_K_generic((int)cols, &gg, 0, x2, 0, y, 0, 1); }
                else   { ggml_vec_dot_tq1_0_q8_K((int)cols, &gi, 0, x1, 0, y, 0, 1); ggml_vec_dot_tq1_0_q8_K_generic((int)cols, &gg, 0, x1, 0, y, 0, 1); }
                long exact = 0;
                for (size_t i = 0; i < cols; ++i) exact += (long)tr[i] * y[i / QK_K].qs[i % QK_K];
                int_bad[f] += (long)gi != exact || (long)gg != exact;
                for (size_t b = 0; b < nb; ++b) {
                    y[b].d = yd[b];
                    if (f) x2[b].d = xd[b]; else x1[b].d = xd[b];
                }
            }
        }
        for (int f = 0; f < 2; ++f) {
            printf("  %s, %s: trits changed %ld / %zu; t27 encode != upstream bytes: %ld rows; dequantized != t*c: %ld; "
                   "integer accumulators (generic and arch) != exact: %ld / %zu rows; float matvec max err / sum|terms| = %.3g\n",
                   f ? "TQ2_0" : "TQ1_0", cn[k], trit_bad[f], rows * cols, byte_bad[f], val_bad[f], int_bad[f], rows, worst[f]);
            /* c = 1/weight_scale is not fp16-exact: only the dequantized values may differ */
            fail |= trit_bad[f] != 0 || byte_bad[f] != 0 || int_bad[f] != 0 || worst[f] > FLOAT_TOLERANCE || (k == 0 && val_bad[f] != 0);
        }
    }
    free(packed); free(sb); free(t); free(x1); free(x2); free(y); free(w); free(deq); free(a); free(yd); free(back); free(sc); free(enc); free(xd);
    return fail;
}

static int bonsai(const char *dir, size_t ne0, size_t ne1) {
    size_t size;
    uint8_t *data = slurp(dir, "bonsai-ffn_down.ptq1_0", &size);
    size_t groups = ne0 / 128, rb = groups * 28, nb = ne0 / QK_K;
    if (size != rb * ne1) { fprintf(stderr, "bonsai: unexpected fixture size %zu\n", size); exit(2); }
    int32_t *t = xmalloc(ne0 * 4), *back = xmalloc(ne0 * 4);
    uint32_t *sc = xmalloc(groups * 4), *sc1 = xmalloc(nb * 4);
    float *w = xmalloc(ne0 * 4), *deq = xmalloc(ne0 * 4);
    block_tq1_0 *x1 = xmalloc(nb * sizeof *x1); uint8_t *enc = xmalloc(nb * 54);
    long pairs = 0, unequal = 0, trit_changed = 0, val_changed = 0, nz = 0, outside = 0, byte_bad = 0;
    double max_ratio = 1, sum_rel = 0;
    for (size_t r = 0; r < ne1; ++r) {
        int64_t o = tf_decode_blocks(TF_PTQ1_0, data + r * rb, rb, ne0, t, ne0, sc, groups);
        if (o < 0) { printf("t27 PTQ1_0 decode status %lld\n", (long long)o); return 1; }
        outside += o;
        for (size_t i = 0; i < ne0; ++i) w[i] = (float)t[i] * h2f((uint16_t)sc[i / 128]);
        quantize_row_tq1_0_ref(w, x1, (int64_t)ne0);
        tf_decode_blocks(TF_TQ1_0, (uint8_t *)x1, nb * 54, ne0, back, ne0, sc1, nb);
        int64_t n = tf_encode_blocks(TF_TQ1_0, back, ne0, sc1, nb, enc, nb * 54);
        byte_bad += n != (int64_t)(nb * 54) || memcmp(enc, x1, nb * 54);
        dequantize_row_tq1_0(x1, deq, (int64_t)ne0);
        for (size_t i = 0; i < ne0; ++i) {
            trit_changed += back[i] != t[i];
            if (t[i]) { ++nz; val_changed += deq[i] != w[i]; sum_rel += fabs(deq[i] - w[i]) / fabs(w[i]); }
        }
        for (size_t p = 0; p < nb; ++p) {
            double a = h2f((uint16_t)sc[2 * p]), b = h2f((uint16_t)sc[2 * p + 1]), q = a > b ? a / b : b / a;
            if (q > max_ratio) max_ratio = q;
            ++pairs; unequal += sc[2 * p] != sc[2 * p + 1];
        }
    }
    printf("Ternary Bonsai 2 27B blk.0.ffn_down PTQ1_0 (%zu x %zu, group-128 fp16 scales, %ld codes outside ternary) "
           "-> upstream quantize_row_tq1_0_ref (one fp16 scale per 256):\n"
           "  256-weight blocks whose two group-128 scales differ: %ld / %ld; largest scale ratio inside one block: %.4f\n"
           "  trits changed by TQ1_0 requantization: %ld; t27 encode != upstream bytes: %ld rows; "
           "nonzero weights whose dequantized value changed: %ld / %ld (mean relative change %.4f)\n",
           ne0, ne1, outside, unequal, pairs, max_ratio, trit_changed, byte_bad, val_changed, nz, sum_rel / (double)nz);
    free(data); free(t); free(back); free(sc); free(sc1); free(w); free(deq); free(x1); free(enc);
    /* The value changes are the expected cost of one scale per 256 weights
     * and are reported, not failed; the trits must survive (every scale ratio
     * inside a block is below 2) and t27 must re-encode the upstream bytes. */
    return outside != 0 || trit_changed != 0 || byte_bad != 0;
}

int main(int argc, char **argv) {
    if (argc != 2) { fprintf(stderr, "usage: %s FIXTURE_DIR\n", argv[0]); return 2; }
    int fail = 0;
    fail |= bitnet(argv[1], "BitNet b1.58 2B4T layer 0 q_proj", "bitnet-q_proj", 2560, 2560);
    fail |= bitnet(argv[1], "BitNet b1.58 2B4T layer 0 down_proj", "bitnet-down_proj", 2560, 6912);
    fail |= bonsai(argv[1], 17408, 5120);
    printf("%s [%s]\n", fail ? "FAIL: storage or kernel mismatch on real tensors" : "PASS: real tensors, no storage or kernel mismatch", arch_name());
    return fail != 0;
}
