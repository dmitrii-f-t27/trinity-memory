/* llama.cpp issue 15193, synthetic part: the upstream TQ1_0/TQ2_0 storage
 * functions and CPU kernels (verbatim ranges of llama.cpp e6ab7c1a, see
 * tests/upstream/llama.cpp.lock.json) against each other and against the t27
 * decoders and encoders of t27/formats.t27. Exits 1 on any mismatch. */
#include "llamacpp_harness.h"

#define NB 10            /* blocks per row: K = 2560, the Qwen3-4B hidden size */
#define K (NB * QK_K)

static uint64_t rs = 0x2709C0FFEEull;
static uint32_t rnd(void) { rs ^= rs << 13; rs ^= rs >> 7; rs ^= rs << 17; return (uint32_t)(rs >> 32); }
static double urand(void) { return ((double)rnd() + 0.5) / 4294967296.0; }
static double nrand(void) { return sqrt(-2.0 * log(urand())) * cos(6.283185307179586 * urand()); }
static double lrand(void) { double u = urand() - 0.5; return (u < 0 ? 1 : -1) * log(1 - 2 * fabs(u)); }
static double trand(int nu) { double z = nrand(), c = 0; for (int i = 0; i < nu; ++i) { double g = nrand(); c += g * g; } return z / sqrt(c / nu); }

static void fill_q8(block_q8_K *y, int lo, int hi) {
    for (int b = 0; b < NB; ++b) {
        y[b].d = 1.0f;
        for (int j = 0; j < QK_K; ++j) y[b].qs[j] = (int8_t)(lo + (int)(rnd() % (uint32_t)(hi - lo + 1)));
        for (int j = 0; j < QK_K / 16; ++j) { int s = 0; for (int i = 0; i < 16; ++i) s += y[b].qs[j * 16 + i]; y[b].bsums[j] = (int16_t)s; }
    }
}

/* T1: every byte value and digit position: the generic digit rule (q*3)>>8
 * against the NEON (vhadd) and AVX2 (avg) formulations. */
static int t1_digits(void) {
    static const uint8_t pow3[6] = {1, 3, 9, 27, 81, 243};
    int bad = 0, noncanon = 0;
    for (int b = 0; b < 256; ++b) {
        for (int n = 0; n < 5; ++n) {
            uint8_t q = (uint8_t)(b * pow3[n]);
            int g = ((uint16_t)q * 3) >> 8;
            int neon = (uint8_t)((q + (q >> 1)) >> 1) >> 6;
            uint8_t t = q ? (uint8_t)(q - 1) : 0, a = (uint8_t)((t + 1) >> 1), r = (uint8_t)((t + a + 1) >> 1);
            int avx = (r >> 6) & 3;
            if (g != neon || g != avx || g > 2) ++bad;
        }
    }
    /* bytes the quantizer can emit: ceil(q*256/243) for q in 0..242 */
    uint8_t canon[256] = {0};
    for (int q = 0; q < 243; ++q) canon[(q * 256 + 242) / 243] = 1;
    for (int b = 0; b < 256; ++b) noncanon += !canon[b];
    printf("T1 digit extraction, 256 bytes x 5 digits: %d mismatches (generic vs NEON formula vs AVX2 formula); "
           "%d of 256 qs byte values are never emitted by quantize_row_tq1_0_ref\n", bad, noncanon);
    return bad != 0 || noncanon != 13;
}

/* The fifth base-3 digit of a TQ1_0 qh byte (the digit rule of T1 at
 * position 4). quantize_row_tq1_0_ref always writes 0 there and the
 * dequantizer never reads it; the t27 reader rejects any other value
 * (TF_ERR_PADDING), while upstream decodes the block silently. */
static int qh_padding_digit(uint8_t b) { uint8_t q = (uint8_t)(b * 81); return ((uint16_t)q * 3) >> 8; }

/* T2: integer parity on arbitrary bytes, including TQ1_0 qs bytes the
 * quantizer never emits and TQ2_0 code 3. With x.d = y.d = 1 every float is
 * an exact integer, so the four results must be equal. In 7 of 8 rows the
 * TQ1_0 qh bytes are drawn with a zero padding digit; in every 8th row they
 * are arbitrary, upstream still decodes them, and t27 must return
 * TF_ERR_PADDING exactly when some qh byte has a nonzero padding digit. */
static int t2_kernels(int rows, int ylo, int yhi) {
    static block_tq1_0 x1[NB]; static block_tq2_0 x2[NB]; static block_q8_K y[NB];
    static float deq[K]; static int32_t vals[K]; static uint32_t sc[NB];
    long bad = 0, code3_rows = 0, padding_rows = 0;
    for (int r = 0; r < rows; ++r) {
        int padding = 0;
        for (int b = 0; b < NB; ++b) {
            for (size_t j = 0; j < sizeof x1[b].qs; ++j) x1[b].qs[j] = (uint8_t)rnd();
            for (size_t j = 0; j < sizeof x1[b].qh; ++j) {
                uint8_t v;
                do { v = (uint8_t)rnd(); } while (r % 8 != 7 && qh_padding_digit(v) != 0);
                x1[b].qh[j] = v;
                padding |= qh_padding_digit(v) != 0;
            }
            for (size_t j = 0; j < sizeof x2[b].qs; ++j) x2[b].qs[j] = (uint8_t)rnd();
            x1[b].d = x2[b].d = 0x3C00;
        }
        fill_q8(y, ylo, yhi);
        for (int f = 0; f < 2; ++f) {
            const void *x = f ? (const void *)x2 : (const void *)x1;
            float sg, sa;
            long code3 = 0;
            if (f) {
                dequantize_row_tq2_0(x2, deq, K);
                ggml_vec_dot_tq2_0_q8_K_generic(K, &sg, 0, x2, 0, y, 0, 1);
                ggml_vec_dot_tq2_0_q8_K(K, &sa, 0, x2, 0, y, 0, 1);
                for (int b = 0; b < NB; ++b)
                    for (int j = 0; j < QK_K / 4; ++j)
                        for (int l = 0; l < 4; ++l) code3 += ((x2[b].qs[j] >> (2 * l)) & 3) == 3;
            } else {
                dequantize_row_tq1_0(x1, deq, K);
                ggml_vec_dot_tq1_0_q8_K_generic(K, &sg, 0, x1, 0, y, 0, 1);
                ggml_vec_dot_tq1_0_q8_K(K, &sa, 0, x1, 0, y, 0, 1);
            }
            int64_t out = tf_decode_blocks(f ? TF_TQ2_0 : TF_TQ1_0, (uint8_t *)x, (size_t)NB * (f ? 66 : 54), K, vals, K, sc, NB);
            if (code3) ++code3_rows;
            if (!f && padding) {
                /* rejected by t27, decoded silently upstream: the upstream
                 * functions must still agree with each other */
                long ref = 0;
                for (int i = 0; i < K; ++i) ref += (long)deq[i] * y[i / QK_K].qs[i % QK_K];
                ++padding_rows;
                if (out != TF_ERR_PADDING || (long)sg != ref || (long)sa != ref) {
                    if (bad < 5) printf("  mismatch f=0 row=%d padding digit: t27 status=%lld (want %d) ref=%ld generic=%.1f arch=%.1f\n",
                                        r, (long long)out, TF_ERR_PADDING, ref, sg, sa);
                    ++bad;
                }
                continue;
            }
            long ref = 0, t27 = 0, value_bad = 0;
            for (int i = 0; i < K; ++i) {
                int yv = y[i / QK_K].qs[i % QK_K];
                ref += (long)deq[i] * yv; t27 += (long)vals[i] * yv;
                value_bad += (int)deq[i] != vals[i];
            }
            if (out != code3 || value_bad || ref != t27 || (long)sg != ref || (long)sa != ref) {
                if (bad < 5) printf("  mismatch f=%d row=%d t27 status=%lld code3=%ld values=%ld ref=%ld t27=%ld generic=%.1f arch=%.1f\n",
                                    f, r, (long long)out, code3, value_bad, ref, t27, sg, sa);
                ++bad;
            }
        }
    }
    printf("T2 [%s] %d rows x K=%d, random bytes, y in [%d,%d]: %ld mismatches among dequantize / t27 tf_decode_blocks / "
           "generic vec_dot / arch vec_dot (TQ2_0 rows with a code-3 digit, decoded as +2 and counted by t27: %ld; "
           "TQ1_0 rows with a nonzero qh padding digit, decoded upstream and rejected by t27 as padding: %ld)\n",
           arch_name(), rows, K, ylo, yhi, bad, code3_rows, padding_rows);
    return bad != 0 || padding_rows == 0;
}

/* T3: real scales: generic vs arch float results relative to the sum of the
 * block terms. NEON sums blocks in the generic order; AVX2 keeps 8 lanes. */
static int t3_float(int rows) {
    static float w[K], a[K]; static block_tq1_0 x1[NB]; static block_tq2_0 x2[NB]; static block_q8_K y[NB]; static float deq[K];
    double worst = 0; int exact = 0;
    for (int r = 0; r < rows; ++r) {
        for (int i = 0; i < K; ++i) { w[i] = (float)(0.02 * nrand()); a[i] = (float)nrand(); }
        quantize_row_tq1_0_ref(w, x1, K); quantize_row_tq2_0_ref(w, x2, K); quantize_row_q8_K_ref(a, y, K);
        for (int f = 0; f < 2; ++f) {
            float sg, sa;
            if (f) { ggml_vec_dot_tq2_0_q8_K_generic(K, &sg, 0, x2, 0, y, 0, 1); ggml_vec_dot_tq2_0_q8_K(K, &sa, 0, x2, 0, y, 0, 1); dequantize_row_tq2_0(x2, deq, K); }
            else   { ggml_vec_dot_tq1_0_q8_K_generic(K, &sg, 0, x1, 0, y, 0, 1); ggml_vec_dot_tq1_0_q8_K(K, &sa, 0, x1, 0, y, 0, 1); dequantize_row_tq1_0(x1, deq, K); }
            double mag = 0;
            for (int b = 0; b < NB; ++b) {
                double term = 0;
                for (int j = 0; j < QK_K; ++j) term += (double)deq[b * QK_K + j] * y[b].qs[j];
                mag += fabs(term * y[b].d);
            }
            exact += (sg == sa);
            if (fabs((double)sg - sa) / fmax(mag, 1e-30) > worst) worst = fabs((double)sg - sa) / fmax(mag, 1e-30);
        }
    }
    printf("T3 [%s] %d rows x 2 formats, Gaussian weights, q8_K activations: generic == arch bit for bit in %d/%d; "
           "max |generic - arch| / sum|block terms| = %.3g\n", arch_name(), rows, exact, 2 * rows, worst);
    return worst > FLOAT_TOLERANCE;
}

/* T4: storage round trip for already-ternary weights w = t*s: upstream
 * quantizer against the t27 encoder, t27 decoder on upstream bytes. */
static int t4_storage(int rows) {
    static float w[K], deq[K]; static int32_t t[K], back[K]; static uint32_t sc[NB], sc2[NB];
    static block_tq1_0 x1[NB]; static block_tq2_0 x2[NB]; static uint8_t e1[NB * 54], e2[NB * 66];
    long lossy_vals[3] = {0, 0, 0}, trit_bad = 0, byte_bad = 0, scale_rounded[3] = {0, 0, 0};
    for (int r = 0; r < rows; ++r) {
        int kind = r % 3;  /* 0: fp16-exact scale, 1: bf16-exact scale (BitNet style), 2: arbitrary f32 scale */
        float s = kind == 0 ? h2f((uint16_t)(0x2000 + rnd() % 0x2400)) : (float)(0.01 + 3 * urand());
        if (kind == 1) { uint32_t bits; memcpy(&bits, &s, 4); bits &= 0xFFFF0000u; memcpy(&s, &bits, 4); }
        for (int i = 0; i < K; ++i) { uint32_t u = rnd() % 10; t[i] = u < 4 ? 0 : (u < 7 ? 1 : -1); }
        if (r % 7 == 0) for (int i = 0; i < QK_K; ++i) t[i] = 0;   /* an all-zero block */
        for (int i = 0; i < K; ++i) w[i] = (float)t[i] * s;
        for (int f = 0; f < 2; ++f) {
            if (f) { quantize_row_tq2_0_ref(w, x2, K); dequantize_row_tq2_0(x2, deq, K); }
            else   { quantize_row_tq1_0_ref(w, x1, K); dequantize_row_tq1_0(x1, deq, K); }
            for (int i = 0; i < K; ++i) lossy_vals[kind] += deq[i] != w[i];
            for (int b = 0; b < NB; ++b) { uint16_t d = f ? x2[b].d : x1[b].d; sc[b] = d; if (d && h2f(d) != s) ++scale_rounded[kind]; }
            int64_t rc = tf_decode_blocks(f ? TF_TQ2_0 : TF_TQ1_0, f ? (uint8_t *)x2 : (uint8_t *)x1, (size_t)NB * (f ? 66 : 54), K, back, K, sc2, NB);
            for (int i = 0; i < K; ++i) trit_bad += back[i] != t[i];
            for (int b = 0; b < NB; ++b) trit_bad += sc2[b] != sc[b];
            int64_t n = tf_encode_blocks(f ? TF_TQ2_0 : TF_TQ1_0, t, K, sc, NB, f ? e2 : e1, (size_t)NB * (f ? 66 : 54));
            if (rc != 0 || n != NB * (f ? 66 : 54) || memcmp(f ? e2 : e1, f ? (void *)x2 : (void *)x1, (size_t)n)) ++byte_bad;
        }
    }
    printf("T4 %d rows x 2 formats of ternary weights t*s: t27 decode of upstream bytes, wrong trits or scales: %ld; "
           "t27 encode != upstream quantize bytes: %ld rows; dequantized values != t*s by scale kind "
           "[fp16-exact s, bf16 s, arbitrary f32 s]: [%ld, %ld, %ld]; block scales changed by fp16 rounding: [%ld, %ld, %ld]\n",
           rows, trit_bad, byte_bad, lossy_vals[0], lossy_vals[1], lossy_vals[2], scale_rounded[0], scale_rounded[1], scale_rounded[2]);
    return trit_bad != 0 || byte_bad != 0 || lossy_vals[0] != 0 || lossy_vals[1] != 0;
}

/* T5 (informational): TQ1_0 absmax ternarization of float-trained rows, the
 * situation of the issue (Qwen3-4B-Instruct-2507 quantized to TQ1_0). */
static void t5_quality(void) {
    enum { N = 256 };
    static float W[N][K], D1[K], D4[K], a[K]; static block_tq1_0 x1[NB]; static block_q4_0 x4[K / 32]; static block_q8_K y[NB];
    const char *names[3] = {"Gaussian", "Laplace", "Student-t nu=4"};
    for (int dist = 0; dist < 3; ++dist) {
        double e1 = 0, e4 = 0, eb = 0, ww = 0, zeros = 0, dot1 = 0, n1 = 0, yy = 0, ey1 = 0, ey4 = 0, eyb = 0;
        for (int i = 0; i < K; ++i) a[i] = (float)nrand();
        quantize_row_q8_K_ref(a, y, K);
        for (int r = 0; r < N; ++r) {
            for (int i = 0; i < K; ++i) W[r][i] = (float)(0.02 * (dist == 0 ? nrand() : dist == 1 ? lrand() / sqrt(2.0) : trand(4) / sqrt(2.0)));
            quantize_row_tq1_0_ref(W[r], x1, K); dequantize_row_tq1_0(x1, D1, K);
            quantize_row_q4_0_ref(W[r], x4, K); dequantize_row_q4_0(x4, D4, K);
            double m = 0; for (int i = 0; i < K; ++i) m += fabs(W[r][i]); m /= K;   /* BitNet absmean, per row */
            double yr = 0, y4 = 0, yb = 0; float yk;
            ggml_vec_dot_tq1_0_q8_K(K, &yk, 0, x1, 0, y, 0, 1);
            for (int i = 0; i < K; ++i) {
                double wv = W[r][i], q = fmax(-1, fmin(1, nearbyint(wv / m))) * m;
                e1 += (wv - D1[i]) * (wv - D1[i]); e4 += (wv - D4[i]) * (wv - D4[i]); eb += (wv - q) * (wv - q); ww += wv * wv;
                zeros += D1[i] == 0; dot1 += wv * D1[i]; n1 += (double)D1[i] * D1[i];
                yr += wv * a[i]; y4 += D4[i] * a[i]; yb += q * a[i];
            }
            yy += yr * yr; ey1 += (yk - yr) * (yk - yr); ey4 += (y4 - yr) * (y4 - yr); eyb += (yb - yr) * (yb - yr);
        }
        printf("T5 %-15s TQ1_0: zero trits %.1f%%, weight rel.RMSE %.3f, cos(w,w^) %.3f, matvec rel.err %.3f | "
               "Q4_0: weight rel.RMSE %.3f, matvec rel.err %.3f | absmean ternary (BitNet rule, not upstream): rel.RMSE %.3f, matvec %.3f\n",
               names[dist], 100.0 * zeros / ((double)N * K), sqrt(e1 / ww), dot1 / sqrt(ww * n1), sqrt(ey1 / yy),
               sqrt(e4 / ww), sqrt(ey4 / yy), sqrt(eb / ww), sqrt(eyb / yy));
    }
}

/* T6: ternary rows (w = t*s): the arch kernel equals the exact integer
 * result times the scales, up to float accumulation. */
static int t6_ternary_matvec(int rows) {
    static float w[K], a[K]; static block_tq1_0 x1[NB]; static block_tq2_0 x2[NB]; static block_q8_K y[NB];
    double worst = 0;
    for (int r = 0; r < rows; ++r) {
        const float s = 1.21875f;   /* BitNet 2B4T layer-0 q_proj weight_scale (bf16) */
        double mag = 0;
        for (int i = 0; i < K; ++i) { uint32_t u = rnd() % 3; w[i] = (float)((int)u - 1) * s; a[i] = (float)nrand(); }
        quantize_row_q8_K_ref(a, y, K);
        double ref = 0;
        for (int b = 0; b < NB; ++b) {
            long si = 0;
            for (int j = 0; j < QK_K; ++j) si += (long)((int)(w[b * QK_K + j] / s)) * y[b].qs[j];
            ref += (double)si * s * y[b].d; mag += fabs((double)si * s * y[b].d);
        }
        quantize_row_tq1_0_ref(w, x1, K); quantize_row_tq2_0_ref(w, x2, K);
        float s1, s2; ggml_vec_dot_tq1_0_q8_K(K, &s1, 0, x1, 0, y, 0, 1); ggml_vec_dot_tq2_0_q8_K(K, &s2, 0, x2, 0, y, 0, 1);
        double e = fmax(fabs(s1 - ref), fabs(s2 - ref)) / fmax(mag, 1e-30);
        if (e > worst) worst = e;
    }
    printf("T6 [%s] %d ternary rows (s=1.21875): max |vec_dot - exact integer*scale| / sum|block terms| = %.3g\n", arch_name(), rows, worst);
    return worst > FLOAT_TOLERANCE;
}

int main(void) {
    int fail = 0;
    fail |= t1_digits();
    fail |= t2_kernels(4000, -127, 127);
    fail |= t2_kernels(1000, -128, 127);
    fail |= t3_float(2000);
    fail |= t4_storage(3000);
    fail |= t6_ternary_matvec(2000);
    t5_quality();
    printf("%s [%s]\n", fail ? "FAIL: storage or kernel mismatch" : "PASS: no storage or kernel mismatch", arch_name());
    return fail != 0;
}
