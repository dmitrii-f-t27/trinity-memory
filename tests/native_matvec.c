/* Independent acceptance harness for t27/matvec.t27 (issue #33). The oracles
 * below are written from the definitions, not from the t27 source: the
 * activation stream is checked against values printed by CPython 3.14.3
 * (random.Random(seed).randint(-128, 127)), the product against a column-order
 * loop, the scale words against a decoding through memcpy and ldexp, and the
 * exact integer form against 128-bit sums. Built by tools/test-t27.sh with
 * ASan and UBSan. */
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
#include "compute.h"
#include "random.h"
#include "matvec.h"

static uint64_t state = 33;
static uint32_t next_u32(void) {
    state ^= state << 13;
    state ^= state >> 7;
    state ^= state << 17;
    return (uint32_t)state;
}

static uint64_t fnv1a(const int8_t *x, size_t n) {
    uint64_t h = UINT64_C(14695981039346656037);
    for (size_t i = 0; i < n; i++) { h ^= (uint8_t)x[i]; h *= UINT64_C(1099511628211); }
    return h;
}

/* CPython 3.14.3: [random.Random(s).randint(-128, 127) for _ in range(17408)]. */
static void test_activations_match_cpython(void) {
    static int8_t x[17408];
    const int64_t seeds[4] = {27, 0, -5, INT64_C(1099511627776)};
    const int8_t first[4][8] = {{117, 13, 18, -28, -91, -95, 2, 42}, {69, 87, -108, 4, 120, 79, 27, 116},
                                {2, 55, -114, 110, -1, -102, -48, -71}, {-75, 52, -95, 90, -104, 70, 76, -89}};
    const uint64_t hashes[4] = {UINT64_C(15192625832262307961), UINT64_C(10500266053884108338),
                                UINT64_C(7192277429968235816), UINT64_C(15973983664564197553)};
    for (size_t s = 0; s < 4; s++) {
        assert(tmv_activations(seeds[s], x, 17408) == 0);
        assert(memcmp(x, first[s], 8) == 0);
        assert(fnv1a(x, 17408) == hashes[s]);
    }
    assert(tmv_activations(27, x, 17408) == 0);
    int64_t sum = 0;
    for (size_t i = 0; i < 17408; i++) sum += x[i];
    assert(sum == 1058 && x[2559] == -117 && x[6911] == 14 && x[17407] == 50);
    /* A shorter vector is the prefix of the same stream. */
    int8_t shorter[2560];
    assert(tmv_activations(27, shorter, 2560) == 0);
    assert(memcmp(shorter, x, 2560) == 0);
}

/* Oracle: accumulate column by column across all rows, then cut groups. */
static void oracle_matvec(const int32_t *w, size_t rows, size_t cols, const int8_t *x, size_t group,
                          int64_t *partials, int64_t *y) {
    size_t groups = cols / group;
    memset(partials, 0, rows * groups * sizeof *partials);
    for (size_t k = 0; k < cols; k++)
        for (size_t r = 0; r < rows; r++) partials[r * groups + k / group] += (int64_t)w[r * cols + k] * x[k];
    for (size_t r = 0; r < rows; r++) {
        y[r] = 0;
        for (size_t j = 0; j < groups; j++) y[r] += partials[r * groups + j];
    }
}

static void test_matvec_random_shapes(void) {
    size_t checked = 0;
    for (int round = 0; round < 300; round++) {
        size_t group_choices[5] = {1, 3, 64, 128, 0};
        size_t group = group_choices[next_u32() % 5];
        size_t groups = 1 + next_u32() % 6;
        if (group == 0) { group = 1 + next_u32() % 200; groups = 1; }
        size_t rows = 1 + next_u32() % 23, cols = group * groups;
        int32_t *w = malloc(rows * cols * sizeof *w);
        int8_t *x = malloc(cols);
        int64_t *row64 = malloc(cols * 8), *x64 = malloc(cols * 8);
        int64_t *p = malloc(rows * groups * 8), *y = malloc(rows * 8);
        int64_t *op = malloc(rows * groups * 8), *oy = malloc(rows * 8);
        for (size_t i = 0; i < rows * cols; i++) w[i] = (int32_t)(next_u32() % 3) - 1;
        for (size_t i = 0; i < cols; i++) {
            uint32_t pick = next_u32() % 8;
            x[i] = pick == 0 ? -128 : pick == 1 ? 127 : (int8_t)(next_u32() & 255);
        }
        assert(tmv_matvec(w, rows, cols, x, group, row64, x64, cols, p, rows * groups, y, rows) == 0);
        oracle_matvec(w, rows, cols, x, group, op, oy);
        assert(memcmp(p, op, rows * groups * 8) == 0);
        assert(memcmp(y, oy, rows * 8) == 0);
        checked += rows;
        free(w); free(x); free(row64); free(x64); free(p); free(y); free(op); free(oy);
    }
    printf("matvec: %zu random rows equal the column-order oracle\n", checked);
}

static void test_matvec_extremes_and_rejections(void) {
    enum { COLS = 17408, GROUP = 64 };
    static int32_t w[2 * COLS];
    static int8_t x[COLS];
    static int64_t row64[COLS], x64[COLS], p[2 * (COLS / GROUP)];
    int64_t y[2] = {0, 0};
    for (size_t k = 0; k < COLS; k++) { w[k] = -1; w[COLS + k] = 1; x[k] = -128; }
    assert(tmv_matvec(w, 2, COLS, x, GROUP, row64, x64, COLS, p, 2 * (COLS / GROUP), y, 2) == 0);
    assert(y[0] == 128 * COLS && y[1] == -128 * COLS && p[0] == 128 * GROUP);
    assert(tmv_max_abs(y, 2) == 128 * COLS);
    int64_t extreme[2] = {5, INT64_MIN};
    assert(tmv_max_abs(extreme, 2) == TF_ERR_LENGTH);
    extreme[1] = -INT64_MAX;
    assert(tmv_max_abs(extreme, 2) == INT64_MAX);

    /* Every rejection leaves partials and y untouched. */
    int64_t sentinel_p[2 * (COLS / GROUP)];
    for (size_t i = 0; i < 2 * (COLS / GROUP); i++) sentinel_p[i] = p[i] = 77;
    y[0] = y[1] = 91;
    w[COLS + 12345] = 2; /* 2-bit code 3 read as +2 */
    assert(tmv_outside_ternary(w, 2 * COLS) == 1);
    assert(tmv_matvec(w, 2, COLS, x, GROUP, row64, x64, COLS, p, 2 * (COLS / GROUP), y, 2) == TF_ERR_CODE);
    w[COLS + 12345] = -2;
    assert(tmv_matvec(w, 2, COLS, x, GROUP, row64, x64, COLS, p, 2 * (COLS / GROUP), y, 2) == TF_ERR_CODE);
    w[COLS + 12345] = 3;
    assert(tmv_matvec(w, 2, COLS, x, GROUP, row64, x64, COLS, p, 2 * (COLS / GROUP), y, 2) == TF_ERR_CODE);
    w[COLS + 12345] = 1;
    assert(tmv_matvec(w, 2, COLS, x, 100, row64, x64, COLS, p, 2 * (COLS / GROUP), y, 2) == TF_ERR_LENGTH);
    assert(tmv_matvec(w, 0, COLS, x, GROUP, row64, x64, COLS, p, 2 * (COLS / GROUP), y, 2) == TF_ERR_LENGTH);
    assert(tmv_matvec(w, 2, 0, x, GROUP, row64, x64, COLS, p, 2 * (COLS / GROUP), y, 2) == TF_ERR_LENGTH);
    assert(tmv_matvec(w, 2, COLS, x, 0, row64, x64, COLS, p, 2 * (COLS / GROUP), y, 2) == TF_ERR_LENGTH);
    assert(tmv_matvec(w, 2, COLS, x, GROUP, row64, x64, COLS - 1, p, 2 * (COLS / GROUP), y, 2) == TF_ERR_CAPACITY);
    assert(tmv_matvec(w, 2, COLS, x, GROUP, row64, x64, COLS, p, 2 * (COLS / GROUP) - 1, y, 2) == TF_ERR_CAPACITY);
    assert(tmv_matvec(w, 2, COLS, x, GROUP, row64, x64, COLS, p, 2 * (COLS / GROUP), y, 1) == TF_ERR_CAPACITY);
    assert(memcmp(p, sentinel_p, sizeof sentinel_p) == 0 && y[0] == 91 && y[1] == 91);
}

/* ---- scale words, decoded independently ------------------------------------ */
static double ref_f16(uint32_t w) {
    int e = (int)((w >> 10) & 31);
    double m = (double)(w & 1023);
    double v = e == 0 ? ldexp(m, -24) : ldexp(m + 1024.0, e - 25);
    return (w & 0x8000) ? -v : v;
}
static double ref_f32(uint32_t w) { float f; memcpy(&f, &w, 4); return (double)f; }
static double ref_bf16(uint32_t w) { return ref_f32((w & 0xffff) << 16); }

static void test_scale_tensor(void) {
    int64_t y[5] = {0, 1, -2561, 536870911, -536870911};
    double out[5];
    const uint32_t bf16_words[3] = {0x3f9c, 0x400a, 0x0001};           /* 1.21875, 2.15625, a subnormal */
    for (size_t i = 0; i < 3; i++) {
        assert(tmv_scale_tensor(y, 5, bf16_words[i], TF_KIND_BF16, out, 5) == 0);
        for (size_t r = 0; r < 5; r++) assert(out[r] == (double)y[r] * ref_bf16(bf16_words[i]));
    }
    const uint32_t f32_word = 0x3f9c02a3;                                /* about 1.2188548 */
    assert(tmv_scale_tensor(y, 5, f32_word, TF_KIND_F32, out, 5) == 0);
    for (size_t r = 0; r < 5; r++) {
        /* The product is exact: the double result times 1/scale gives y back. */
        assert(out[r] == (double)y[r] * ref_f32(f32_word));
        long double exact = (long double)y[r] * (long double)ref_f32(f32_word);
        assert((long double)out[r] == exact || sizeof(long double) == sizeof(double));
    }
    assert(tmv_scale_tensor(y, 5, 0x3c00, TF_KIND_F16, out, 5) == 0 && out[3] == 536870911.0);
    for (size_t r = 0; r < 5; r++) out[r] = 42.0;
    y[3] = 536870912;
    assert(tmv_scale_tensor(y, 5, 0x3f9c, TF_KIND_BF16, out, 5) == TF_ERR_LENGTH);
    y[3] = 0;
    assert(tmv_scale_tensor(y, 5, 0x7fc0, TF_KIND_BF16, out, 5) == TF_ERR_SCALE_NONFINITE);
    assert(tmv_scale_tensor(y, 5, 0x7f800000, TF_KIND_F32, out, 5) == TF_ERR_SCALE_NONFINITE);
    assert(tmv_scale_tensor(y, 5, 0x3f9c, 9, out, 5) == TF_ERR_FORMAT);
    assert(tmv_scale_tensor(y, 5, 0x3f9c, TF_KIND_BF16, out, 4) == TF_ERR_CAPACITY);
    assert(tmv_scale_tensor(y, 0, 0x3f9c, TF_KIND_BF16, out, 5) == TF_ERR_LENGTH);
    for (size_t r = 0; r < 5; r++) assert(out[r] == 42.0);
}

static int64_t ref_f16_scaled(uint32_t w) {
    double v = ldexp(ref_f16(w), 24);
    return (int64_t)v;
}

static void test_group_scales(void) {
    enum { ROWS = 7, GROUPS = 272 };
    static int64_t p[ROWS * GROUPS], exact[ROWS];
    static uint32_t s128[ROWS * GROUPS / 2], s64[ROWS * GROUPS];
    static double f128[ROWS], f64v[ROWS];
    int64_t first = 0;
    for (int round = 0; round < 50; round++) {
        for (size_t i = 0; i < ROWS * GROUPS; i++) p[i] = (int64_t)(next_u32() % 16385) - 8192;
        for (size_t i = 0; i < ROWS * GROUPS / 2; i++) {
            uint32_t word = next_u32() & 0xffff;
            if (((word >> 10) & 31) == 31) word &= 0xfbff; /* keep it finite */
            s128[i] = word;
            s64[2 * i] = s64[2 * i + 1] = word;
        }
        assert(tmv_scale_groups(p, ROWS, GROUPS, s128, ROWS * GROUPS / 2, TF_KIND_F16, 2, f128, ROWS) == 0);
        assert(tmv_scale_groups(p, ROWS, GROUPS, s64, ROWS * GROUPS, TF_KIND_F16, 1, f64v, ROWS) == 0);
        assert(tmv_mismatches_f64(f128, f64v, ROWS, &first) == 0 && first == -1);
        assert(tmv_exact_groups_f16(p, ROWS, GROUPS, s128, ROWS * GROUPS / 2, 2, exact, ROWS) == 0);
        for (size_t r = 0; r < ROWS; r++) {
            double sum = 0.0;
            __int128 wide = 0;
            for (size_t j = 0; j < GROUPS; j++) {
                sum += ref_f16(s128[(r * GROUPS + j) / 2]) * (double)p[r * GROUPS + j];
                wide += (__int128)ref_f16_scaled(s128[(r * GROUPS + j) / 2]) * p[r * GROUPS + j];
            }
            assert(f128[r] == sum);
            assert((__int128)exact[r] == wide);
        }
        /* The f64 sum equals the exact value where the oracle says so. */
        int64_t expect = 0;
        for (size_t r = 0; r < ROWS; r++)
            if (ldexp(f128[r], 24) != (double)exact[r] || (int64_t)ldexp(f128[r], 24) != exact[r]) expect++;
        assert(tmv_exact_mismatches(exact, 24, f128, ROWS, &first) == expect);
    }
    /* Constructed rows: exact equality, then one ulp away. */
    int64_t e2[2] = {3, -((INT64_C(1) << 53) + 1)};
    double v2[2] = {ldexp(3.0, -24), ldexp(-(double)(INT64_C(1) << 53), -24)};
    assert(tmv_exact_mismatches(e2, 24, v2, 2, &first) == 1 && first == 1);
    e2[1] = -(INT64_C(1) << 53);
    assert(tmv_exact_mismatches(e2, 24, v2, 2, &first) == 0 && first == -1);

    /* Rejections leave outputs untouched. */
    for (size_t r = 0; r < ROWS; r++) { f128[r] = 7.0; exact[r] = 7; }
    assert(tmv_scale_groups(p, ROWS, GROUPS, s128, ROWS * GROUPS / 2 - 1, TF_KIND_F16, 2, f128, ROWS) == TF_ERR_LENGTH);
    assert(tmv_scale_groups(p, ROWS, GROUPS, s128, ROWS * GROUPS / 2, TF_KIND_F16, 3, f128, ROWS) == TF_ERR_LENGTH);
    assert(tmv_scale_groups(p, ROWS, GROUPS, s128, ROWS * GROUPS / 2, 0, 2, f128, ROWS) == TF_ERR_FORMAT);
    assert(tmv_scale_groups(p, ROWS, GROUPS, s128, ROWS * GROUPS / 2, TF_KIND_F16, 2, f128, ROWS - 1) == TF_ERR_CAPACITY);
    uint32_t keep = s128[100];
    s128[100] = 0x7c00; /* +inf */
    assert(tmv_scale_groups(p, ROWS, GROUPS, s128, ROWS * GROUPS / 2, TF_KIND_F16, 2, f128, ROWS) == TF_ERR_SCALE_NONFINITE);
    assert(tmv_exact_groups_f16(p, ROWS, GROUPS, s128, ROWS * GROUPS / 2, 2, exact, ROWS) == TF_ERR_SCALE_NONFINITE);
    s128[100] = keep;
    int64_t keep_p = p[5];
    p[5] = (INT64_C(1) << 22) + 1;
    assert(tmv_exact_groups_f16(p, ROWS, GROUPS, s128, ROWS * GROUPS / 2, 2, exact, ROWS) == TF_ERR_LENGTH);
    p[5] = INT64_C(1) << 29;
    assert(tmv_scale_groups(p, ROWS, GROUPS, s128, ROWS * GROUPS / 2, TF_KIND_F16, 2, f128, ROWS) == TF_ERR_LENGTH);
    p[5] = keep_p;
    /* A row sum that leaves i64: three terms of 2047 * 2^29 * 2^22. */
    int64_t big[4] = {INT64_C(1) << 22, INT64_C(1) << 22, INT64_C(1) << 22, 0};
    uint32_t largest[4] = {0x7bff, 0x7bff, 0x7bff, 0x7bff};
    int64_t two[2] = {7, 7};
    assert(tmv_exact_groups_f16(big, 1, 4, largest, 4, 1, two, 1) == TF_ERR_LENGTH);
    big[2] = 0;
    assert(tmv_exact_groups_f16(big, 1, 4, largest, 4, 1, two, 1) == 0);
    assert(two[0] == 2 * (INT64_C(2047) << 51) && two[1] == 7);
    for (size_t r = 0; r < ROWS; r++) assert(f128[r] == 7.0 && exact[r] == 7);
    for (uint32_t w = 0; w < 0x10000; w++)
        if (((w >> 10) & 31) != 31) assert(tmv_f16_scaled(w) == ref_f16_scaled(w));
}

static void test_difference_and_residual(void) {
    enum { ROWS = 9, COLS = 256 };
    static int32_t a[ROWS * COLS], b[ROWS * COLS], d[ROWS * COLS];
    static int8_t x[COLS];
    static int64_t row64[COLS], x64[COLS], p[ROWS * 4], ya[ROWS], yb[ROWS], yd[ROWS];
    int64_t nonzero = 0, touched = 0, first = 0;
    for (size_t i = 0; i < ROWS * COLS; i++) {
        a[i] = (int32_t)(next_u32() % 3) - 1;
        b[i] = a[i];
        if (i / COLS != 4 && next_u32() % 50 == 0) b[i] = a[i] == 0 ? (next_u32() & 1 ? 1 : -1) : 0;
        nonzero += a[i] != b[i];
    }
    for (size_t r = 0; r < ROWS; r++) {
        bool any = false;
        for (size_t k = 0; k < COLS; k++) any |= a[r * COLS + k] != b[r * COLS + k];
        touched += any;
    }
    for (size_t k = 0; k < COLS; k++) x[k] = (int8_t)(next_u32() & 255);
    assert(tmv_difference(a, b, ROWS * COLS, d, ROWS * COLS) == nonzero);
    assert(tmv_rows_nonzero(d, ROWS, COLS) == touched && touched < ROWS);
    assert(tmv_matvec(a, ROWS, COLS, x, 64, row64, x64, COLS, p, ROWS * 4, ya, ROWS) == 0);
    assert(tmv_matvec(b, ROWS, COLS, x, 64, row64, x64, COLS, p, ROWS * 4, yb, ROWS) == 0);
    assert(tmv_matvec(d, ROWS, COLS, x, 64, row64, x64, COLS, p, ROWS * 4, yd, ROWS) == 0);
    assert(tmv_residual(ya, yb, yd, ROWS, &first) == 0 && first == -1);
    yd[3] += 1;
    assert(tmv_residual(ya, yb, yd, ROWS, &first) == 1 && first == 3);
    int64_t differ = 0;
    for (size_t r = 0; r < ROWS; r++) differ += ya[r] != yb[r];
    assert(tmv_mismatches_i64(ya, yb, ROWS, &first) == differ);
    assert(ya[4] == yb[4]);
    /* A +1/-1 pair is not a trit difference; out stays untouched. */
    a[0] = 1; b[0] = -1; d[0] = 5;
    assert(tmv_difference(a, b, ROWS * COLS, d, ROWS * COLS) == TF_ERR_CODE && d[0] == 5);
    assert(tmv_difference(a, b, ROWS * COLS, d, ROWS * COLS - 1) == TF_ERR_CAPACITY);
}

int main(void) {
    test_tmv_cpython_prefix(); /* the test blocks of t27/matvec.t27 itself */
    test_tmv_small_product();
    test_tmv_f16_scaled_words();
    test_activations_match_cpython();
    test_matvec_random_shapes();
    test_matvec_extremes_and_rejections();
    test_scale_tensor();
    test_group_scales();
    test_difference_and_residual();
    puts("native matvec: CPython activation stream (4 seeds x 17408), random products, rejections, "
         "tensor and group float steps, exact f16 integer form, difference residuals passed");
    return 0;
}
