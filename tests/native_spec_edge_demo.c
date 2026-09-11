/* Differential harness: specs/memory/edge_demo.t27 scoring rules against the
 * executable scoring in t27/compute.t27 (tm_scale_scores, tm_argmax_scores).
 * Generate specs/types.h, specs/edge_demo.h and the implementation headers into
 * one include directory; link native/float.cpp. No production algorithm here. */
#include <assert.h>
#include <float.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#if defined(__clang__)
#pragma clang diagnostic ignored "-Wparentheses-equality"
#endif
double tm_json_strtod(uint8_t *);
size_t tm_float_shortest(double, uint8_t *, size_t);
uint64_t tm_float_bits(double);
#include "specs/types.h"
#include "specs/edge_demo.h"
#include "codecs.h"
#include "container.h"
#include "compute.h"

static uint32_t random_state = 2709;
static uint32_t next_random(void) {
    random_state = random_state * UINT32_C(1664525) + UINT32_C(1013904223);
    return random_state;
}

static void argmax_parity(void) {
    static const double cases[][3] = {
        {0.0, 0.0, 0.0}, {5.0, 5.0, 1.0}, {1.0, 2.0, 2.0}, {1.0, 3.0, 2.0}, {-1.0, -2.0, -3.0}, {480.0, -480.0, 0.0},
        {-480.0, 480.0, 0.0}, {0.0, 0.0, 360.0}, {249.0, -249.0, 3.0}, {3.0, 6.0, -1.5}, {2.0, 2.0, 2.0}, {-1.0, -1.0, 0.0},
    };
    for (size_t i = 0; i < sizeof cases / sizeof cases[0]; ++i) {
        double scores[3] = {cases[i][0], cases[i][1], cases[i][2]};
        int64_t best = -7, tied = -7;
        assert(tm_argmax_scores(scores, 3, &best, &tied) == 0);
        int32_t spec = tms_edge_argmax(scores[0], scores[1], scores[2]);
        if (spec < 0) assert(tied != 0);
        else assert(tied == 0 && best == spec);
    }
    for (unsigned trial = 0; trial < 20000; ++trial) {
        double scores[3];
        for (unsigned k = 0; k < 3; ++k) scores[k] = (double)((int32_t)(next_random() % 7) - 3) * ((next_random() & 1) ? 0.5 : 1.0);
        int64_t best = -7, tied = -7;
        assert(tm_argmax_scores(scores, 3, &best, &tied) == 0);
        int32_t spec = tms_edge_argmax(scores[0], scores[1], scores[2]);
        if (spec < 0) assert(tied != 0);
        else assert(tied == 0 && best == spec);
    }
    double bad[3] = {NAN, 0.0, 0.0};
    int64_t best, tied;
    assert(tm_argmax_scores(bad, 3, &best, &tied) != 0);
    assert(!tms_edge_score_finite(bad[0]));
}

static void scale_parity(void) {
    int64_t raw[3] = {480, -480, 0};
    double one[1] = {1.0}, rows[3] = {1.0, 2.0, 0.5}, output[3];
    assert(tm_scale_scores(raw, 3, one, 1, output, 3) == 0);
    for (unsigned k = 0; k < 3; ++k) assert(output[k] == tms_edge_score(raw[k], one[0]));
    assert(tm_scale_scores(raw, 3, rows, 3, output, 3) == 0);
    for (unsigned k = 0; k < 3; ++k) assert(output[k] == tms_edge_score(raw[k], rows[k]));
    int64_t tie[3] = {3, 3, -3};
    assert(tm_scale_scores(tie, 3, rows, 3, output, 3) == 0);
    int64_t best = -7, tied = -7;
    assert(tm_argmax_scores(output, 3, &best, &tied) == 0 && tied == 0 && best == 1);
    assert(tms_edge_argmax(output[0], output[1], output[2]) == 1);
    double zero[1] = {0.0}, negative[1] = {-1.0}, nan_scale[1] = {NAN}, inf_scale[1] = {INFINITY};
    assert(tm_scale_scores(raw, 3, zero, 1, output, 3) == -1);
    assert(tm_scale_scores(raw, 3, negative, 1, output, 3) == -1);
    assert(tm_scale_scores(raw, 3, nan_scale, 1, output, 3) == -1);
    assert(tm_scale_scores(raw, 3, inf_scale, 1, output, 3) == -1);
    int64_t large[3] = {3, 3, -3};
    double overflow[3] = {1e308, 1e308, 1.0};
    assert(tm_scale_scores(large, 3, overflow, 3, output, 3) == -3);
    assert(!tms_edge_score_finite(tms_edge_score(3, 1e308) * 10.0));
    assert(tm_scale_scores(raw, 3, rows, 2, output, 3) == -1);
    assert(tm_scale_scores(raw, 3, one, 1, output, 2) == -2);
}

static void templates_and_fixtures(void) {
    int32_t samples[12];
    for (uint32_t fixture = 0; fixture < 3; ++fixture) {
        for (uint32_t column = 0; column < 12; ++column) samples[column] = tms_edge_synthetic_sample(fixture, column);
        int64_t raw[3];
        for (uint32_t row = 0; row < 3; ++row) raw[row] = tms_edge_accumulator(row, samples);
        double one[1] = {1.0}, scores[3];
        assert(tm_scale_scores(raw, 3, one, 1, scores, 3) == 0);
        int64_t best = -7, tied = -7;
        assert(tm_argmax_scores(scores, 3, &best, &tied) == 0 && tied == 0);
        assert(best == (int64_t)tms_edge_expected_label(fixture));
    }
    int32_t constant[12];
    for (uint32_t column = 0; column < 12; ++column) constant[column] = 17;
    double scores[3];
    for (uint32_t row = 0; row < 3; ++row) scores[row] = (double)tms_edge_accumulator(row, constant);
    int64_t best = -7, tied = -7;
    assert(tm_argmax_scores(scores, 3, &best, &tied) == 0 && tied != 0);
    assert(tms_edge_argmax(scores[0], scores[1], scores[2]) == -1);
    assert(tms_edge_raw_payload_bytes(TMS_CODEC_DENSE5) == tms_payload_bytes(TMS_CODEC_DENSE5, 36));
    assert(tms_edge_raw_payload_bytes(TMS_CODEC_BASELINE2) == tms_payload_bytes(TMS_CODEC_BASELINE2, 36));
}

int main(void) {
    argmax_parity();
    scale_parity();
    templates_and_fixtures();
    printf("PASS spec/memory/edge_demo differential harness: argmax and tie rules on fixed and 20000 random score triples, "
           "scalar and per-row scales, rejected scales, synthetic fixtures, payload bytes\n");
    return 0;
}
