/* Independent host conformance harness for generated t27 numeric kernels.
 * Compile with generated compute.h/sparsity.h on the include path and -lm.
 * The application algorithms live in t27; C supplies independent test oracles.
 */
#include <assert.h>
#include <float.h>
#include <inttypes.h>
#include <limits.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ffi.h"
#include "compute.h"
#include "sparsity.h"

static uint64_t random_state = 27;
static uint32_t next_u32(void) {
    random_state ^= random_state << 13;
    random_state ^= random_state >> 7;
    random_state ^= random_state << 17;
    return (uint32_t)random_state;
}

static void test_dot_boundaries(void) {
    int64_t out[2] = {91, 92};
    int64_t weights[6] = {1, -1, 0, -1, 1, 1};
    int64_t activations[3] = {-128, 127, -128};
    assert(tm_dot_i64(weights, 6, activations, 3, 2, 3, out, 2) == 0);
    assert(out[0] == -255 && out[1] == 127);
    assert(tm_dot_i64(NULL, 0, NULL, 0, 1, 0, out, 2) == 0 && out[0] == 0);
    assert(tm_dot_i64(NULL, 0, NULL, 0, 2, 0, out, 2) == -1);
    out[0] = 91;
    out[1] = 92;
    assert(tm_dot_i64(weights, 6, activations, 3, 2, 3, out, 1) == -2);
    assert(tm_dot_i64(weights, 5, activations, 3, 2, 3, out, 2) == -1);
    assert(tm_dot_i64(weights, 6, activations, 2, 2, 3, out, 2) == -1);
    assert(tm_dot_i64(NULL, 6, activations, 3, 2, 3, out, 2) == -1);
    assert(tm_dot_i64(weights, 6, NULL, 3, 2, 3, out, 2) == -1);
    assert(tm_dot_i64(weights, 6, activations, 3, 2, 3, NULL, 2) == -1);
    weights[5] = 2;
    assert(tm_dot_i64(weights, 6, activations, 3, 2, 3, out, 2) == -1);
    weights[5] = 1;
    activations[2] = -129;
    assert(tm_dot_i64(weights, 6, activations, 3, 2, 3, out, 2) == -1);
    activations[2] = 128;
    assert(tm_dot_i64(weights, 6, activations, 3, 2, 3, out, 2) == -1);
    assert(out[0] == 91 && out[1] == 92);
    /* Length arithmetic is rejected before dereferencing these small arrays. */
    uint64_t huge = UINT64_C(72057594037927936);
    assert(tm_dot_i64(weights, huge, activations, huge, 1, huge, out, 2) == -3);
    assert(tm_dot_i64(weights, UINT64_MAX, activations, 3, UINT64_MAX, 3, out, 2) == -1);

    int64_t sum = 41;
    assert(tm_add_i64_checked(INT64_MAX, 1, &sum) == -3 && sum == 41);
    assert(tm_add_i64_checked(INT64_MIN, -1, &sum) == -3 && sum == 41);
    assert(tm_add_i64_checked(INT64_MAX - 1, 1, &sum) == 0 && sum == INT64_MAX);
    assert(tm_add_i64_checked(INT64_MIN + 1, -1, &sum) == 0 && sum == INT64_MIN);
    assert(tm_add_i64_checked(0, INT64_MIN, &sum) == 0 && sum == INT64_MIN);
    assert(tm_add_i64_checked(INT64_MAX, INT64_MIN, &sum) == 0 && sum == -1);
    assert(tm_add_i64_checked(0, 0, NULL) == -1);
}

static unsigned test_dot_random(void) {
    unsigned checks = 0;
    int64_t weights[4 * 257], activations[257], actual[4];
    for (uint64_t columns = 1; columns <= 257; columns++) {
        uint64_t rows = 1 + next_u32() % 4;
        for (uint64_t i = 0; i < rows * columns; i++)
            weights[i] = (int64_t)(next_u32() % 3) - 1;
        for (uint64_t i = 0; i < columns; i++)
            activations[i] = (int64_t)(next_u32() % 256) - 128;
        assert(tm_dot_i64(weights, rows * columns, activations, columns,
                          rows, columns, actual, 4) == 0);
        for (uint64_t row = 0; row < rows; row++) {
            int64_t expected = 0;
            for (uint64_t col = 0; col < columns; col++)
                expected += weights[row * columns + col] * activations[col];
            assert(actual[row] == expected);
            checks++;
        }
    }
    for (uint64_t i = 0; i < 257; i++) { weights[i] = -1; activations[i] = -128; }
    assert(tm_dot_i64(weights, 257, activations, 257, 1, 257, actual, 4) == 0);
    assert(actual[0] == 32896);
    return checks + 1;
}

static void test_scale_and_classification(void) {
    int64_t raw[3] = {2, 3, 1};
    double scales[3] = {2.0, 1.0, 1.0}, scores[3] = {-7.0, -8.0, -9.0};
    int64_t best = -1, tied = -1;
    assert(tm_scale_scores(raw, 3, scales, 3, scores, 3) == 0);
    assert(scores[0] == 4.0 && scores[1] == 3.0 && scores[2] == 1.0);
    assert(tm_argmax_scores(scores, 3, &best, &tied) == 0 && best == 0 && tied == 0);
    assert(tm_scale_scores(raw, 3, scales, 1, scores, 3) == 0);
    assert(scores[0] == 4.0 && scores[1] == 6.0 && scores[2] == 2.0);
    assert(tm_argmax_scores(scores, 3, &best, &tied) == 0 && best == 1 && tied == 0);
    double reset_tie[3] = {4.0, 4.0, 5.0}, retain_tie[3] = {5.0, 4.0, 5.0};
    assert(tm_argmax_scores(reset_tie, 3, &best, &tied) == 0 && best == 2 && tied == 0);
    assert(tm_argmax_scores(retain_tie, 3, &best, &tied) == 0 && best == 0 && tied == 1);
    double zero_tie[2] = {-0.0, 0.0};
    assert(tm_argmax_scores(zero_tie, 2, &best, &tied) == 0 && best == 0 && tied == 1);
    double negative[3] = {-3.0, -1.0, -2.0};
    assert(tm_argmax_scores(negative, 3, &best, &tied) == 0 && best == 1 && tied == 0);
    assert(tm_argmax_scores(negative, 0, &best, &tied) == -1);
    assert(tm_argmax_scores(NULL, 3, &best, &tied) == -1);
    assert(tm_argmax_scores(negative, 3, NULL, &tied) == -1);
    assert(tm_argmax_scores(negative, 3, &best, NULL) == -1);
    assert(tm_scale_scores(raw, 3, scales, 2, scores, 3) == -1);
    assert(tm_scale_scores(raw, 3, scales, 3, scores, 2) == -2);
    assert(tm_scale_scores(NULL, 3, scales, 3, scores, 3) == -1);
    assert(tm_scale_scores(raw, 3, NULL, 3, scores, 3) == -1);
    assert(tm_scale_scores(raw, 3, scales, 3, NULL, 3) == -1);
    for (unsigned which = 0; which < 5; which++) {
        double invalid[] = {NAN, INFINITY, -INFINITY, 0.0, -1.0};
        scales[2] = invalid[which];
        scores[0] = -7.0; scores[1] = -8.0; scores[2] = -9.0;
        assert(tm_scale_scores(raw, 3, scales, 3, scores, 3) == -1);
        assert(scores[0] == -7.0 && scores[1] == -8.0 && scores[2] == -9.0);
    }
    scales[2] = DBL_MAX;
    raw[2] = 2;
    assert(tm_scale_scores(raw, 3, scales, 3, scores, 3) == -3);
    assert(scores[0] == -7.0 && scores[1] == -8.0 && scores[2] == -9.0);
    raw[2] = 0;
    assert(tm_scale_scores(raw, 3, scales, 3, scores, 3) == 0 && scores[2] == 0.0);
    for (unsigned which = 0; which < 3; which++) {
        double invalid[] = {NAN, INFINITY, -INFINITY};
        scores[2] = invalid[which]; best = 11; tied = 12;
        assert(tm_argmax_scores(scores, 3, &best, &tied) == -1);
        assert(best == 11 && tied == 12);
    }
}

struct ranked { double value; size_t index; };
static int rank_order(const void *left, const void *right) {
    const struct ranked *a = left, *b = right;
    if (fabs(a->value) > fabs(b->value)) return -1;
    if (fabs(a->value) < fabs(b->value)) return 1;
    return (a->index > b->index) - (a->index < b->index);
}

static unsigned test_projection(void) {
    double values[73];
    int64_t actual[73], expected[73];
    unsigned checks = 0;
    for (size_t count = 0; count <= 73; count++) {
        for (size_t i = 0; i < count; i++) values[i] = ((int)(next_u32() % 11) - 5) * 0.25;
        for (size_t block = 1; block <= 9; block++) {
            for (size_t k = 0; k <= block; k++) {
                memset(expected, 0, sizeof expected);
                for (size_t start = 0; start < count; start += block) {
                    size_t width = count - start < block ? count - start : block;
                    struct ranked ordered[9];
                    for (size_t i = 0; i < width; i++)
                        ordered[i] = (struct ranked){values[start + i], start + i};
                    qsort(ordered, width, sizeof ordered[0], rank_order);
                    for (size_t i = 0; i < k && i < width; i++)
                        expected[ordered[i].index] = (ordered[i].value > 0) - (ordered[i].value < 0);
                }
                assert(tm_project_topk(values, count, block, k, actual, 73) == 0);
                assert(memcmp(actual, expected, count * sizeof actual[0]) == 0);
                checks++;
            }
        }
    }
    assert(tm_project_topk(NULL, 0, 4, 1, NULL, 0) == 0);
    assert(tm_project_topk(values, 3, 0, 0, actual, 73) == -1);
    assert(tm_project_topk(values, 3, 2, 3, actual, 73) == -1);
    assert(tm_project_topk(values, 3, 4, 1, actual, 2) == -2);
    assert(tm_project_topk(NULL, 3, 4, 1, actual, 73) == -1);
    assert(tm_project_topk(values, 3, 4, 1, NULL, 73) == -1);
    for (unsigned i = 0; i < 3; i++) {
        double invalid[] = {NAN, INFINITY, -INFINITY};
        values[0] = 1.0; values[1] = invalid[i];
        actual[0] = 91; actual[1] = 92;
        assert(tm_project_topk(values, 2, 4, 1, actual, 73) == -1);
        assert(actual[0] == 91 && actual[1] == 92);
    }
    return checks;
}

static void test_entropy(void) {
    double result = 8.0;
    int64_t uniform[6] = {-1, 0, 1, 1, 0, -1};
    int64_t one_symbol[3] = {1, 1, 1};
    assert(tm_entropy_trits(uniform, 6, &result) == 0);
    assert(fabs(result - log2(3.0)) < 2e-15);
    assert(tm_entropy_trits(one_symbol, 3, &result) == 0 && result == 0.0);
    assert(tm_entropy_trits(NULL, 0, &result) == 0 && result == 0.0);
    one_symbol[2] = 2; result = 9.0;
    assert(tm_entropy_trits(one_symbol, 3, &result) == -1 && result == 9.0);
    assert(tm_entropy_trits(NULL, 3, &result) == -1);
    assert(tm_entropy_trits(uniform, 6, NULL) == -1);
    for (unsigned i = 0; i <= 1000; i++) {
        double p = i / 1000.0, other = (1.0 - p) / 2.0;
        double expected = (p ? -p * log2(p) : 0) + (other ? -2 * other * log2(other) : 0);
        assert(tm_symmetric_entropy(p, &result) == 0);
        assert(fabs(result - expected) < 2e-15);
    }
    double invalid[] = {-0.01, 1.01, NAN, INFINITY, -INFINITY};
    for (unsigned i = 0; i < sizeof invalid / sizeof invalid[0]; i++) {
        result = 9.0;
        assert(tm_symmetric_entropy(invalid[i], &result) == -1 && result == 9.0);
    }
    assert(tm_symmetric_entropy(0.5, NULL) == -1);
}

static void test_exact_projection(void) {
    uint8_t lo[]={0x20,0,0,0,0,0,0},hi[]={0x20,0,0,0,0,0,1};
    uint8_t huge[128]={0x80};
    TMExactWeight weights[]={{lo,sizeof lo,0x1p53,true},{hi,sizeof hi,0x1p53,true},
                            {NULL,0,-0x1p53,false},{huge,sizeof huge,0x1p1023,true}};
    int64_t out[4]={7,8,9,10};
    assert(tm_project_topk_exact(weights,2,2,1,out,4)==0&&out[0]==0&&out[1]==1);
    assert(tm_project_topk_exact(weights,3,3,2,out,4)==0&&out[0]==1&&out[1]==1&&out[2]==0);
    assert(tm_project_topk_exact(weights,4,4,1,out,4)==0&&out[3]==1);
    assert(tm_exact_magnitude_compare(&weights[1],&weights[2])==1);
    weights[2].real=nextafter(0x1p53,INFINITY);
    assert(tm_exact_magnitude_compare(&weights[1],&weights[2])==-1);
    uint8_t one=1;weights[0]=(TMExactWeight){&one,1,1.0,true};
    weights[1]=(TMExactWeight){NULL,0,1.5,false};
    assert(tm_exact_magnitude_compare(&weights[0],&weights[1])==-1);
    weights[1].real=nextafter(1.0,0.0);
    assert(tm_exact_magnitude_compare(&weights[0],&weights[1])==1);
    weights[0]=(TMExactWeight){NULL,0,0.0,true};weights[1].real=0x1p-1074;
    assert(tm_exact_magnitude_compare(&weights[0],&weights[1])==-1);
    weights[1].real=-0.0;assert(tm_exact_magnitude_compare(&weights[0],&weights[1])==0);
    uint8_t boundary[128];memset(boundary,0xff,sizeof boundary);boundary[6]=0xfb;
    TMExactWeight maximum={boundary,128,DBL_MAX,true};
    assert(tm_exact_weight_valid(&maximum));
    boundary[6]=0xfc;memset(boundary+7,0,121);assert(!tm_exact_weight_valid(&maximum));
    uint8_t leading[]={0,1};TMExactWeight malformed={leading,2,1.0,true};
    out[0]=77;assert(tm_project_topk_exact(&malformed,1,1,1,out,1)==-1&&out[0]==77);
    malformed=(TMExactWeight){NULL,1,1.0,true};assert(!tm_exact_weight_valid(&malformed));
    malformed=(TMExactWeight){&one,1,0.0,true};assert(!tm_exact_weight_valid(&malformed));
    malformed=(TMExactWeight){NULL,0,INFINITY,false};assert(!tm_exact_weight_valid(&malformed));
    assert(tm_project_topk_exact(NULL,0,4,1,NULL,0)==0);
    assert(tm_project_topk_exact(weights,2,0,0,out,4)==-1);
    assert(tm_project_topk_exact(weights,2,2,3,out,4)==-1);
    assert(tm_project_topk_exact(weights,2,2,1,out,1)==-2);
    assert(tm_project_topk_exact(NULL,2,2,1,out,4)==-1);
    assert(tm_project_topk_exact(weights,2,2,1,NULL,4)==-1);
}

int main(void) {
    test_tm_compute_finite_contract();
    test_tm_dot_signed_extremes();
    test_tm_scaled_argmax_contract();
    test_tm_topk_lower_index_ties();
    test_tm_entropy_limits();
    test_dot_boundaries();
    unsigned dots = test_dot_random();
    test_scale_and_classification();
    unsigned projections = test_projection();
    test_entropy();
    test_exact_projection();
    printf("PASS: 5 generated inline tests, %u exact dot rows, %u top-k comparisons, "
           "1001 entropy fractions, and invalid-input/overflow boundaries.\n", dots, projections);
    return 0;
}
