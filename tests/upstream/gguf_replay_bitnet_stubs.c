/* bitnet.cpp's ggml-base refers to two functions of its CPU kernels
 * (src/ggml-bitnet-mad.cpp, outside its llama.cpp submodule), which the replay
 * does not build: the I2_S row converters. gguf_init_from_file never calls
 * them; these stand-ins let the library resolve (the replay is linked with
 * -rdynamic) and stop the program if anything does call them. */
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>

void dequantize_row_i2_s(const void *x, float *y, int64_t k);
size_t quantize_i2_s(const float *src, void *dst, int64_t nrows, int64_t n_per_row, const float *imatrix);

void dequantize_row_i2_s(const void *x, float *y, int64_t k) {
    (void) x; (void) y; (void) k;
    abort();
}

size_t quantize_i2_s(const float *src, void *dst, int64_t nrows, int64_t n_per_row, const float *imatrix) {
    (void) src; (void) dst; (void) nrows; (void) n_per_row; (void) imatrix;
    abort();
}
