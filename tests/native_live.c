/* Independent acceptance harness for t27/live.t27 (issues #49 and #50). The
 * headers are written here byte by byte from the GGUF v3 definition, and the
 * expected verdicts are the checks of the PrismML fork's loader read from its
 * source (PrismML-Eng/llama.cpp@bdc23b56 src/llama-model.cpp:1196-1335 and
 * :1971-2050, ggml/src/gguf.cpp:781-801), not from the t27 source. Built by tools/test-t27.sh with ASan
 * and UBSan. */
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
double tm_json_strtod(uint8_t *);
#include "json.h"
#include "formats.h"
#include "live.h"

enum { T_U32 = 4, T_I32 = 5, T_BOOL = 7, T_STR = 8, T_ARR = 9, T_U64 = 10 };

typedef struct {
    uint8_t *data;
    size_t size, capacity;
    uint64_t keys, tensors;
} Buf;

static void put(Buf *b, const void *bytes, size_t n) {
    if (b->size + n > b->capacity) {
        b->capacity = (b->size + n) * 2 + 64;
        b->data = realloc(b->data, b->capacity);
        assert(b->data);
    }
    memcpy(b->data + b->size, bytes, n);
    b->size += n;
}
static void u32(Buf *b, uint32_t v) { uint8_t x[4]; for (int i = 0; i < 4; i++) x[i] = (uint8_t)(v >> (8 * i)); put(b, x, 4); }
static void u64(Buf *b, uint64_t v) { uint8_t x[8]; for (int i = 0; i < 8; i++) x[i] = (uint8_t)(v >> (8 * i)); put(b, x, 8); }
static void str(Buf *b, const char *s) { u64(b, strlen(s)); put(b, s, strlen(s)); }

/* Key-value pairs go to `kv`, tensor records to `tr`; finish() joins them. */
typedef struct { Buf kv, tr; } Gguf;

static void kv_u32(Gguf *g, const char *k, uint32_t v) { str(&g->kv, k); u32(&g->kv, T_U32); u32(&g->kv, v); g->kv.keys++; }
static void kv_u64(Gguf *g, const char *k, uint64_t v) { str(&g->kv, k); u32(&g->kv, T_U64); u64(&g->kv, v); g->kv.keys++; }
static void kv_bool(Gguf *g, const char *k, bool v) { uint8_t x = v; str(&g->kv, k); u32(&g->kv, T_BOOL); put(&g->kv, &x, 1); g->kv.keys++; }
static void kv_str(Gguf *g, const char *k, const char *v) { str(&g->kv, k); u32(&g->kv, T_STR); str(&g->kv, v); g->kv.keys++; }
static void kv_strs(Gguf *g, const char *k, const char **v, size_t n) {
    str(&g->kv, k); u32(&g->kv, T_ARR); u32(&g->kv, T_STR); u64(&g->kv, n);
    for (size_t i = 0; i < n; i++) str(&g->kv, v[i]);
    g->kv.keys++;
}
static void kv_ints(Gguf *g, const char *k, uint32_t elem, const int32_t *v, size_t n) {
    str(&g->kv, k); u32(&g->kv, T_ARR); u32(&g->kv, elem); u64(&g->kv, n);
    for (size_t i = 0; i < n; i++) u32(&g->kv, (uint32_t)v[i]);
    g->kv.keys++;
}
static void tensor(Gguf *g, const char *name, uint64_t ne0, uint64_t ne1, uint32_t type, uint64_t offset) {
    str(&g->tr, name); u32(&g->tr, 2); u64(&g->tr, ne0); u64(&g->tr, ne1); u32(&g->tr, type); u64(&g->tr, offset);
    g->tr.tensors++;
}
static Buf finish(Gguf *g) {
    Buf out = {0};
    u32(&out, 0x46554747); u32(&out, 3); u64(&out, g->tr.tensors); u64(&out, g->kv.keys);
    put(&out, g->kv.data, g->kv.size);
    put(&out, g->tr.data, g->tr.size);
    while (out.size % 32) { uint8_t z = 0; put(&out, &z, 1); }
    free(g->kv.data); free(g->tr.data);
    memset(g, 0, sizeof *g);
    return out;
}

/* A header the fork loads: identity or explicit signs, qwen35. The caller
 * may add keys before and tensors after through the hooks. */
typedef struct {
    int version_type, version, block;
    const char *transform, *axis, *mode, *arch;
    const char **names; size_t n_names; bool names_as_ints; bool names_missing;
    const int32_t *widths; size_t n_widths; const int32_t *values; size_t n_values;
    bool gdn_wrong_type;
    const char **inverse; size_t n_inverse;
    uint64_t ne0;
    bool drop_tensor;
} Spec;

static const char *one_name[] = {"blk.0.attn_q.weight"};

static Spec base(void) {
    Spec s = {T_U32, 1, 4, "normalized-sylvester-walsh-hadamard", "input-last-dimension", "identity", "qwen35",
              one_name, 1, false, false, NULL, 0, NULL, 0, false, NULL, 0, 8, false};
    return s;
}

static Buf build(const Spec *s) {
    Gguf g = {0};
    kv_str(&g, "general.architecture", s->arch);
    if (s->version_type == T_U32) kv_u32(&g, "prism.hadamard.version", (uint32_t)s->version);
    if (s->version_type == T_U64) kv_u64(&g, "prism.hadamard.version", (uint64_t)s->version);
    if (s->block >= 0) kv_u32(&g, "prism.hadamard.block_size", (uint32_t)s->block);
    kv_str(&g, "prism.hadamard.transform", s->transform);
    kv_str(&g, "prism.hadamard.axis", s->axis);
    kv_str(&g, "prism.hadamard.sign_mode", s->mode);
    if (!s->names_missing) {
        if (s->names_as_ints) {
            int32_t v[1] = {1};
            kv_ints(&g, "prism.hadamard.weight_names", T_I32, v, 1);
        } else {
            kv_strs(&g, "prism.hadamard.weight_names", s->names, s->n_names);
        }
    }
    if (s->widths) kv_ints(&g, "prism.hadamard.sign_widths", T_I32, s->widths, s->n_widths);
    if (s->values) kv_ints(&g, "prism.hadamard.sign_values", T_I32, s->values, s->n_values);
    if (s->gdn_wrong_type) kv_u32(&g, "prism.hadamard.gdn_v_grouped", 1);
    else kv_bool(&g, "prism.hadamard.gdn_v_grouped", false);
    if (s->inverse) kv_strs(&g, "prism.hadamard.inverse_weight_names", s->inverse, s->n_inverse);
    if (!s->drop_tensor) tensor(&g, "blk.0.attn_q.weight", s->ne0, 2, 142, 0);
    tensor(&g, "token_embd.weight", 8, 4, 142, 64);
    return finish(&g);
}

static int32_t verdict(const Spec *s, TLVHadamard *out) {
    Buf b = build(s);
    int32_t status = tlv_hadamard(b.data, b.size, out);
    free(b.data);
    return status;
}

static void test_keys(void) {
    Gguf g = {0};
    kv_u32(&g, "general.alignment", 32);
    kv_str(&g, "general.architecture", "qwen35");
    const char *names[] = {"a", "bc"};
    kv_strs(&g, "list", names, 2);
    Buf b = finish(&g);
    TLVKey k;
    assert(tlv_key(b.data, b.size, "general.architecture", 20, &k) == 0);
    assert(k.found && k.vtype == T_STR);
    assert(tlv_text_at(b.data, k.at, "qwen35", 6) && !tlv_text_at(b.data, k.at, "qwen3", 5));
    assert(tlv_key(b.data, b.size, "list", 4, &k) == 0);
    assert(k.found && k.vtype == T_ARR && k.elem == T_STR && k.count == 2);
    assert(tlv_key(b.data, b.size, "missing", 7, &k) == 0 && !k.found);
    assert(tlv_key(b.data, b.size, "general.alignmen", 16, &k) == 0 && !k.found);
    b.data[0] = 'X';
    assert(tlv_key(b.data, b.size, "list", 4, &k) == TF_ERR_CONTAINER);
    assert(tlv_records(b.data, b.size) == TF_ERR_CONTAINER);
    b.data[0] = 'G';
    assert(tlv_key(b.data, 30, "list", 4, &k) == TF_ERR_CONTAINER);
    free(b.data);
}

static void test_hadamard_absent_and_valid(void) {
    Gguf g = {0};
    kv_str(&g, "general.architecture", "qwen35");
    kv_u32(&g, "prism.other", 1);
    tensor(&g, "blk.0.attn_q.weight", 128, 2, 142, 0);
    Buf b = finish(&g);
    TLVHadamard h;
    assert(tlv_hadamard(b.data, b.size, &h) == 0 && !h.present);
    free(b.data);

    Spec s = base();
    assert(verdict(&s, &h) == 0);
    assert(h.present && h.block_size == 4 && h.names == 1 && !h.explicit_signs && h.inverse == 0);

    int32_t widths[] = {8, 4};
    int32_t values[] = {1, -1, 1, 1, -1, -1, 1, -1, 1, 1, 1, 1};
    s.mode = "explicit"; s.widths = widths; s.n_widths = 2; s.values = values; s.n_values = 12;
    assert(verdict(&s, &h) == 0);
    assert(h.explicit_signs && h.widths == 2 && h.signs == 12);

    const char *inverse[] = {"token_embd.weight"};
    s.inverse = inverse; s.n_inverse = 1;
    assert(verdict(&s, &h) == 0 && h.inverse == 1);

    const char *moe[] = {"blk.12.ffn_down_exps.weight", "output.weight", "blk.3.ssm_out.weight"};
    Spec m = base();
    m.names = moe; m.n_names = 3; m.drop_tensor = false;
    /* two of the listed names are not tensors of the file */
    assert(verdict(&m, &h) == TLV_ERR_HADAMARD_TENSOR && h.index == 0);
}

static void test_hadamard_rejections(void) {
    TLVHadamard h;
    Spec s;
    s = base(); s.version_type = T_U64; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_TYPE && h.present);
    s = base(); s.version = 2; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_VERSION);
    s = base(); s.block = -1; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_MISSING);
    s = base(); s.block = 0; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_BLOCK);
    s = base(); s.block = 12; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_BLOCK);
    s = base(); s.transform = "sylvester"; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_TRANSFORM);
    s = base(); s.axis = "output"; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_TRANSFORM);
    s = base(); s.mode = "random"; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_TRANSFORM);
    s = base(); s.names_missing = true; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_MISSING);
    s = base(); s.names_as_ints = true; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_TYPE);
    s = base(); s.n_names = 0; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_NAME);

    int32_t w8[] = {8}, w6[] = {6}, w0[] = {0}, wneg[] = {-8};
    int32_t v8[] = {1, -1, 1, 1, -1, -1, 1, -1};
    int32_t bad[] = {1, -1, 1, 0, -1, -1, 1, -1};
    s = base(); s.mode = "explicit"; s.widths = w8; s.n_widths = 0; s.values = v8; s.n_values = 8;
    assert(verdict(&s, &h) == TLV_ERR_HADAMARD_SIGNS);
    s.widths = w6; s.n_widths = 1; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_SIGNS);  /* not a multiple of 4 */
    s.widths = w0; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_SIGNS);
    s.widths = wneg; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_SIGNS);
    s.widths = w8; s.values = bad; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_SIGNS && h.index == 3);
    s.values = v8; s.n_values = 7; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_SIGNS);    /* width past the values */
    int32_t v12[] = {1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1};
    s.values = v12; s.n_values = 12; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_SIGNS);   /* values left over */
    s = base(); s.mode = "explicit"; s.values = v8; s.n_values = 8;
    assert(verdict(&s, &h) == TLV_ERR_HADAMARD_MISSING);                                /* no widths array */

    s = base(); s.gdn_wrong_type = true; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_TYPE);
    s = base(); s.arch = "gemma3"; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_ARCH);
    s = base(); s.arch = "qwen35moe"; assert(verdict(&s, &h) == 0);

    const char *embd[] = {"token_embd.weight"};
    s = base(); s.names = embd; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_NAME && h.index == 0);
    const char *twice[] = {"blk.0.attn_q.weight", "blk.0.attn_q.weight"};
    s = base(); s.names = twice; s.n_names = 2; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_NAME && h.index == 1);
    const char *nodigit[] = {"blk.x.attn_q.weight"};
    s = base(); s.names = nodigit; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_NAME);
    const char *kind[] = {"blk.0.attn_norm.weight"};
    s = base(); s.names = kind; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_NAME);
    s = base(); s.arch = "dspark"; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_NAME);

    const char *out_inverse[] = {"output.weight"};
    s = base(); s.inverse = out_inverse; s.n_inverse = 1;
    assert(verdict(&s, &h) == TLV_ERR_HADAMARD_NAME && h.index == 1);
    const char *inv_twice[] = {"token_embd.weight", "token_embd.weight"};
    s = base(); s.inverse = inv_twice; s.n_inverse = 2; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_NAME && h.index == 2);

    s = base(); s.drop_tensor = true; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_TENSOR && h.index == 0);
    s = base(); s.ne0 = 10; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_TENSOR);             /* 4 does not divide 10 */
    int32_t w4[] = {4};
    int32_t v4[] = {1, 1, -1, 1};
    s = base(); s.mode = "explicit"; s.widths = w4; s.n_widths = 1; s.values = v4; s.n_values = 4;
    assert(verdict(&s, &h) == TLV_ERR_HADAMARD_TENSOR);                                  /* no vector of width 8 */
}

/* Tensors of 1024 x 4 weights stored back to back in blocks of `block_bytes`
 * per `group` weights, declared as `type`; returns the file size. */
static Buf layout_file(uint32_t type, uint64_t group, uint64_t block_bytes, int count, bool prism, uint64_t *file_size) {
    Gguf g = {0};
    kv_u32(&g, "general.alignment", 32);
    if (prism) kv_u32(&g, "prism.hadamard.version", 1);
    uint64_t offset = 0;
    char name[32];
    for (int i = 0; i < count; i++) {
        snprintf(name, sizeof name, "blk.%d.w", i);
        tensor(&g, name, 1024, 4, type, offset);
        offset += 4 * (1024 / group) * block_bytes;
        offset = (offset + 31) / 32 * 32;
    }
    Buf b = finish(&g);
    *file_size = b.size + offset;
    return b;
}

static void test_layout_fit(void) {
    uint64_t size;
    TFTensorInfo info;
    /* type 42 declared, 128-weight groups of 34 bytes stored (PrismML-Eng/llama.cpp#167) */
    Buf b = layout_file(42, 128, 34, 3, false, &size);
    for (uint64_t i = 0; i < 3; i++) {
        assert(tf_gguf_nth(b.data, b.size, i, &info) == 0);
        assert(tf_gguf_check(&info, size) == TF_ERR_EXTENT);
        assert(tlv_layout_fit(&info, size) == TF_PQ2_0);
        assert(!tlv_prism_unmarked(&info));
    }
    free(b.data);
    /* the declared layout itself fits */
    b = layout_file(42, 64, 18, 2, false, &size);
    assert(tf_gguf_nth(b.data, b.size, 0, &info) == 0 && tf_gguf_check(&info, size) == TF_Q2_0);
    assert(tlv_layout_fit(&info, size) == TF_Q2_0);
    assert(tf_gguf_nth(b.data, b.size, 1, &info) == 0 && tlv_layout_fit(&info, size) == TF_Q2_0);
    free(b.data);
    /* TQ2_0 bytes (66 per 256) under type 42 */
    b = layout_file(42, 256, 66, 2, false, &size);
    assert(tf_gguf_nth(b.data, b.size, 0, &info) == 0 && tlv_layout_fit(&info, size) == TF_TQ2_0);
    free(b.data);
    /* a gap no layout fills */
    b = layout_file(42, 128, 40, 2, false, &size);
    assert(tf_gguf_nth(b.data, b.size, 0, &info) == 0 && tlv_layout_fit(&info, size) == 0);
    free(b.data);
    /* fork ids with and without the fork's marker */
    b = layout_file(142, 128, 34, 2, false, &size);
    for (uint64_t i = 0; i < 2; i++) {
        assert(tf_gguf_nth(b.data, b.size, i, &info) == 0 && tlv_prism_unmarked(&info));
        assert(tf_gguf_check(&info, size) == 0);            /* not a ternary layout without the marker */
        assert(tlv_record_check(&info, size) == TF_PQ2_0 && !info.prism);
    }
    assert(tlv_record_check(&info, size - 1) == TF_ERR_EXTENT);
    free(b.data);
    /* group-64 bytes under the group-128 id: nothing overlaps, but the first
     * record leaves a gap that gguf.cpp refuses; the last one ends inside the file */
    b = layout_file(142, 64, 18, 2, false, &size);
    assert(tf_gguf_nth(b.data, b.size, 0, &info) == 0 && tlv_record_check(&info, size) == TLV_ERR_OFFSETS);
    assert(tlv_layout_fit(&info, size) == TF_Q2_0);
    assert(tf_gguf_nth(b.data, b.size, 1, &info) == 0 && tlv_record_check(&info, size) == TF_PQ2_0);
    free(b.data);
    /* Q2_0 declared, 40-byte blocks of 128 stored: a gap larger than declared */
    b = layout_file(42, 128, 40, 2, false, &size);
    assert(tf_gguf_nth(b.data, b.size, 0, &info) == 0 && tf_gguf_check(&info, size) == TF_Q2_0);
    assert(tlv_record_check(&info, size) == TLV_ERR_OFFSETS);
    free(b.data);
    /* legacy layout (#167): the extent rule fires first */
    b = layout_file(42, 128, 34, 2, false, &size);
    assert(tf_gguf_nth(b.data, b.size, 0, &info) == 0 && tlv_record_check(&info, size) == TF_ERR_EXTENT);
    free(b.data);
    b = layout_file(42, 64, 18, 2, false, &size);
    assert(tf_gguf_nth(b.data, b.size, 0, &info) == 0 && tlv_record_check(&info, size) == TF_Q2_0);
    free(b.data);
    b = layout_file(143, 128, 28, 1, true, &size);
    assert(tf_gguf_nth(b.data, b.size, 0, &info) == 0 && !tlv_prism_unmarked(&info));
    assert(tf_gguf_check(&info, size) == TF_PTQ1_0 && tlv_layout_fit(&info, size) == TF_PTQ1_0);
    assert(tlv_record_check(&info, size) == TF_PTQ1_0 && info.prism);
    free(b.data);
    /* a record that is not a ternary layout */
    b = layout_file(8, 32, 34, 1, false, &size);
    assert(tf_gguf_nth(b.data, b.size, 0, &info) == 0 && tlv_record_check(&info, size) == 0);
    free(b.data);
}

int main(void) {
    test_keys();
    test_hadamard_absent_and_valid();
    test_hadamard_rejections();
    test_layout_fit();
    printf("PASS live: GGUF keys, prism.hadamard rules of the PrismML loader (types, values, signs, "
           "architectures, weight paths, tensors), layout of rejected extents, unmarked fork ids, contiguous offsets\n");
    return 0;
}
