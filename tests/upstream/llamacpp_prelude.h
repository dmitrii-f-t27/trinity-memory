/* Minimal stand-ins for the ggml headers that the extracted llama.cpp ranges
 * expect (ggml-impl.h, ggml-cpu-impl.h, simd-mappings.h). Written for this
 * harness; the block structs themselves are extracted from ggml-common.h. */
#ifndef LLAMACPP_PRELUDE_H
#define LLAMACPP_PRELUDE_H
#include <assert.h>
#include <float.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#if defined(__ARM_NEON)
#include <arm_neon.h>
#endif
#if defined(__x86_64__)
#include <immintrin.h>
#endif

#define GGML_RESTRICT restrict
#define QK_K 256
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define MAX(a, b) ((a) > (b) ? (a) : (b))
#define GGML_UNUSED(x) (void)(x)
#define UNUSED GGML_UNUSED
typedef uint16_t ggml_half;

/* IEEE binary16 conversions with round-to-nearest-even, as ggml's F16C, NEON
 * and software paths perform them. */
static inline float llamacpp_h2f(uint16_t h) { _Float16 v; memcpy(&v, &h, 2); return (float)v; }
static inline uint16_t llamacpp_f2h(float f) { _Float16 v = (_Float16)f; uint16_t h; memcpy(&h, &v, 2); return h; }
#define GGML_FP16_TO_FP32(x) llamacpp_h2f(x)
#define GGML_FP32_TO_FP16(x) llamacpp_f2h(x)
#define GGML_CPU_FP16_TO_FP32(x) llamacpp_h2f(x)
#endif
