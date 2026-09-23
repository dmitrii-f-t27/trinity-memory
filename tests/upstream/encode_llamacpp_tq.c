/* Upstream-encoded cells of the Ternary Check matrix (issue #32): the ternary
 * weights of BitNet b1.58 2B4T layer 0 (q_proj, down_proj), stored by the
 * pinned llama.cpp reference quantizers quantize_row_tq1_0_ref and
 * quantize_row_tq2_0_ref. The input of each quantizer is the dequantized
 * tensor w = t * weight_scale, with t decoded from the Hugging Face packed
 * checkpoint by t27 (tf_decode_hf_packed) and weight_scale its bf16 value.
 * This program only writes the upstream bytes; trinity_memory.ternary_check
 * reads them with the t27 decoders and decides the cells in t27/matrix.t27.
 * Usage: encode_llamacpp_tq FIXTURE_DIR OUT_DIR (fixtures written by
 * llamacpp_fixtures.py); writes OUT_DIR/bitnet-<tensor>.tq1_0 and .tq2_0.
 * Built by tests/upstream/run-llamacpp-matrix.sh with -ffp-contract=off. */
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

static void spill(const char *dir, const char *name, const void *data, size_t size) {
    char path[4096];
    snprintf(path, sizeof path, "%s/%s", dir, name);
    FILE *f = fopen(path, "wb");
    if (!f || fwrite(data, 1, size, f) != size || fclose(f) != 0) { perror(path); exit(2); }
}

static void *xmalloc(size_t n) { void *p = malloc(n ? n : 1); if (!p) { fprintf(stderr, "out of memory\n"); exit(2); } return p; }

static void bitnet(const char *in, const char *out, const char *stem, size_t rows, size_t cols) {
    char name[256];
    size_t psize, ssize;
    snprintf(name, sizeof name, "%s.packed", stem); uint8_t *packed = slurp(in, name, &psize);
    snprintf(name, sizeof name, "%s.scale", stem); uint8_t *scale = slurp(in, name, &ssize);
    if (psize != rows / 4 * cols || ssize != 2 || cols % QK_K != 0) { fprintf(stderr, "%s: unexpected fixture sizes\n", stem); exit(2); }
    size_t n = rows * cols, blocks = n / QK_K;
    int32_t *t = xmalloc(n * sizeof *t);
    if (tf_decode_hf_packed(packed, psize, rows, cols, t, n) != 0) { fprintf(stderr, "%s: codes outside ternary\n", stem); exit(2); }
    uint32_t bits = (uint32_t)(scale[0] | scale[1] << 8) << 16;
    float c;
    memcpy(&c, &bits, 4);
    float *w = xmalloc(n * sizeof *w);
    for (size_t i = 0; i < n; i++) w[i] = (float)t[i] * c;
    block_tq1_0 *x1 = xmalloc(blocks * sizeof *x1);
    block_tq2_0 *x2 = xmalloc(blocks * sizeof *x2);
    for (size_t r = 0; r < rows; r++) {
        quantize_row_tq1_0_ref(w + r * cols, x1 + r * (cols / QK_K), (int64_t)cols);
        quantize_row_tq2_0_ref(w + r * cols, x2 + r * (cols / QK_K), (int64_t)cols);
    }
    snprintf(name, sizeof name, "%s.tq1_0", stem); spill(out, name, x1, blocks * sizeof *x1);
    snprintf(name, sizeof name, "%s.tq2_0", stem); spill(out, name, x2, blocks * sizeof *x2);
    printf("%s: %zu x %zu, weight_scale %.9g, %zu TQ1_0 bytes and %zu TQ2_0 bytes from llama.cpp's reference quantizers\n",
           stem, rows, cols, c, blocks * sizeof *x1, blocks * sizeof *x2);
    free(packed); free(scale); free(t); free(w); free(x1); free(x2);
}

int main(int argc, char **argv) {
    if (argc != 3) { fprintf(stderr, "usage: %s FIXTURE_DIR OUT_DIR\n", argv[0]); return 2; }
    bitnet(argv[1], argv[2], "bitnet-q_proj", 2560, 2560);
    bitnet(argv[1], argv[2], "bitnet-down_proj", 2560, 6912);
    return 0;
}
