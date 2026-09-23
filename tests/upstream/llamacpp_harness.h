/* Shared part of the llama.cpp issue 15193 harness: one translation unit
 * holds the extracted upstream functions (build/upstream/15193/llamacpp_tq.c,
 * written by extract_llamacpp_tq.py) and the generated t27 decoders and
 * encoders (build/t27/formats.h). */
#ifndef LLAMACPP_HARNESS_H
#define LLAMACPP_HARNESS_H
#include "llamacpp_tq.c"
double tm_json_strtod(uint8_t *p);
double tm_json_strtod(uint8_t *p) { return strtod((const char *)p, NULL); }
#include "json.h"
#include "formats.h"

_Static_assert(sizeof(block_tq1_0) == 54, "TQ1_0 block is 54 bytes");
_Static_assert(sizeof(block_tq2_0) == 66, "TQ2_0 block is 66 bytes");

#if !defined(__ARM_NEON) && !defined(__x86_64__)
/* No architecture file applies: llama.cpp would use the generic kernels. */
static void ggml_vec_dot_tq1_0_q8_K(int n, float *s, size_t bs, const void *vx, size_t bx, const void *vy, size_t by, int nrc) {
    ggml_vec_dot_tq1_0_q8_K_generic(n, s, bs, vx, bx, vy, by, nrc);
}
static void ggml_vec_dot_tq2_0_q8_K(int n, float *s, size_t bs, const void *vx, size_t bx, const void *vy, size_t by, int nrc) {
    ggml_vec_dot_tq2_0_q8_K_generic(n, s, bs, vx, bx, vy, by, nrc);
}
#endif

/* run-llamacpp-15193.sh names each variant after the kernel path it tests and
 * passes the expectation; a compiler that picks another path fails the build
 * instead of passing under the wrong name. */
#if defined(HARNESS_EXPECT_DOTPROD)
#if HARNESS_EXPECT_DOTPROD && !(defined(__ARM_NEON) && defined(__ARM_FEATURE_DOTPROD))
#error "variant expects the NEON DOTPROD kernels, but __ARM_FEATURE_DOTPROD is not defined"
#elif !HARNESS_EXPECT_DOTPROD && !(defined(__ARM_NEON) && !defined(__ARM_FEATURE_DOTPROD))
#error "variant expects the NEON int16 kernels, but __ARM_FEATURE_DOTPROD is defined (or no NEON)"
#endif
#endif
#if defined(HARNESS_EXPECT_AVX2)
#if HARNESS_EXPECT_AVX2 && !(defined(__x86_64__) && defined(__AVX2__))
#error "variant expects the x86 AVX2 kernels, but __AVX2__ is not defined"
#elif !HARNESS_EXPECT_AVX2 && !(defined(__x86_64__) && !defined(__AVX2__))
#error "variant expects the x86 generic fallback, but __AVX2__ is defined (or not x86_64)"
#endif
#endif

static inline const char *arch_name(void) {
#if defined(__ARM_NEON) && defined(__ARM_FEATURE_DOTPROD)
    return "arm64 NEON+DOTPROD";
#elif defined(__ARM_NEON)
    return "arm64 NEON int16 path (no DOTPROD)";
#elif defined(__x86_64__) && defined(__AVX2__)
    return "x86_64 AVX2";
#elif defined(__x86_64__)
    return "x86_64 without AVX2 (generic fallback)";
#else
    return "generic";
#endif
}

/* Float results may differ from a double reference only by accumulation
 * order: the AVX2 kernels keep eight lane sums (hsum_float_8). The bound is
 * relative to the sum of the absolute block terms; integer accumulators are
 * compared exactly. */
#define FLOAT_TOLERANCE 1e-5

#define h2f llamacpp_h2f
#define f2h llamacpp_f2h
#endif
