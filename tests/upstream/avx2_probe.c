/* Exits 0 when the host (or Rosetta 2) executes the AVX2, FMA and F16C
 * instructions the x86 TQ kernels use: vpmaddubsw, vpshufb, vcvtph2ps and
 * vfmadd. Built by run-llamacpp-15193.sh with the x86_64-avx2 flags. */
#include <immintrin.h>
#include <stdio.h>

int main(void) {
    volatile int seed = 3;
    __m256i a = _mm256_set1_epi8((char)seed);
    __m256i b = _mm256_set1_epi8((char)(seed - 1));
    __m256i p = _mm256_maddubs_epi16(a, b);                 /* 3*2 + 3*2 = 12 */
    p = _mm256_shuffle_epi8(p, _mm256_setzero_si256());     /* low byte: 12 */
    __m256 h = _mm256_cvtph_ps(_mm_set1_epi16(0x3C00));     /* 1.0f */
    __m256 f = _mm256_fmadd_ps(h, _mm256_set1_ps((float)seed), h);   /* 4.0f */
    int ok = _mm256_extract_epi8(p, 0) == 12 && _mm256_cvtss_f32(f) == 4.0f;
    printf("avx2 probe: %s\n", ok ? "ok" : "wrong result");
    return ok ? 0 : 1;
}
