/* Independent acceptance harness for t27/live.t27 and t27/runtimes.t27
 * (issues #48-#51). The headers are written here byte by byte from the GGUF
 * definition, and the expected verdicts are the checks of the pinned readers
 * read from their sources, not from the t27 source: gguf.cpp (ggml-org
 * e6ab7c1a ggml/src/gguf.cpp:230-427 and :459-803; bitnet.cpp's differences
 * in its gguf.cpp:609-616 and :681-687), the loader's file-end rule
 * (src/llama-model-loader.h:40-50), its split rules
 * (src/llama-model-loader.cpp:570-674) and architecture rules
 * (src/llama-model.cpp:43-349, :368-369, src/llama.cpp:352-353), ggml_nbytes
 * (ggml.c:1298-1321, bitnet.cpp's :1300-1331), and the PrismML fork's
 * Hadamard rules (bdc23b56 src/llama-model.cpp:1196-1335, 1971-2095). Built
 * by tools/test-t27.sh with ASan and UBSan. */
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
#include "runtimes.h"
#include "live.h"

enum { T_U16 = 2, T_U32 = 4, T_I32 = 5, T_F32 = 6, T_BOOL = 7, T_STR = 8, T_ARR = 9, T_U64 = 10 };
enum { LLAMA = 1, PRISM = 2, BITNET = 3 };
#define NONE UINT64_MAX

typedef struct {
    uint8_t *data;
    size_t size, capacity;
    uint64_t count;
} Buf;

static void put(Buf *b, const void *bytes, size_t n) {
    if (n == 0) return;                       /* memcpy from a NULL buffer is undefined even for 0 bytes */
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
static void bytes_str(Buf *b, const char *s, size_t n) { u64(b, n); put(b, s, n); }
static void str(Buf *b, const char *s) { bytes_str(b, s, strlen(s)); }

/* Key-value pairs go to `kv`, tensor records to `tr`; finish() joins them. */
typedef struct { Buf kv, tr; uint32_t version; uint64_t data; } Gguf;

static void key(Gguf *g, const char *k) { str(&g->kv, k); g->kv.count++; }
static void kv_u16(Gguf *g, const char *k, uint16_t v) { uint8_t x[2] = {(uint8_t)v, (uint8_t)(v >> 8)}; key(g, k); u32(&g->kv, T_U16); put(&g->kv, x, 2); }
static void kv_u32(Gguf *g, const char *k, uint32_t v) { key(g, k); u32(&g->kv, T_U32); u32(&g->kv, v); }
static void kv_i32(Gguf *g, const char *k, int32_t v) { key(g, k); u32(&g->kv, T_I32); u32(&g->kv, (uint32_t)v); }
static void kv_u64(Gguf *g, const char *k, uint64_t v) { key(g, k); u32(&g->kv, T_U64); u64(&g->kv, v); }
static void kv_bool(Gguf *g, const char *k, bool v) { uint8_t x = v; key(g, k); u32(&g->kv, T_BOOL); put(&g->kv, &x, 1); }
static void kv_str(Gguf *g, const char *k, const char *v) { key(g, k); u32(&g->kv, T_STR); str(&g->kv, v); }
static void kv_strn(Gguf *g, const char *k, const char *v, size_t n) { key(g, k); u32(&g->kv, T_STR); bytes_str(&g->kv, v, n); }
static void kv_strs(Gguf *g, const char *k, const char **v, size_t n) {
    key(g, k); u32(&g->kv, T_ARR); u32(&g->kv, T_STR); u64(&g->kv, n);
    for (size_t i = 0; i < n; i++) str(&g->kv, v[i]);
}
static void kv_ints(Gguf *g, const char *k, uint32_t elem, const int32_t *v, size_t n) {
    key(g, k); u32(&g->kv, T_ARR); u32(&g->kv, elem); u64(&g->kv, n);
    for (size_t i = 0; i < n; i++) u32(&g->kv, (uint32_t)v[i]);
}
/* A tensor record with `dims` dimensions ne[0..dims). */
static void rec(Gguf *g, const char *name, uint32_t dims, const uint64_t *ne, uint32_t type, uint64_t offset) {
    str(&g->tr, name); u32(&g->tr, dims);
    for (uint32_t i = 0; i < dims; i++) u64(&g->tr, ne[i]);
    u32(&g->tr, type); u64(&g->tr, offset);
    g->tr.count++;
}
static void rec2(Gguf *g, const char *name, uint64_t ne0, uint64_t ne1, uint32_t type, uint64_t offset) {
    uint64_t ne[2] = {ne0, ne1};
    rec(g, name, 2, ne, type, offset);
}
static Buf finish(Gguf *g, uint64_t *file_size, uint64_t data_bytes) {
    Buf out = {0};
    u32(&out, 0x46554747); u32(&out, g->version ? g->version : 3); u64(&out, g->tr.count); u64(&out, g->kv.count);
    put(&out, g->kv.data, g->kv.size);
    put(&out, g->tr.data, g->tr.size);
    while (out.size % 32) { uint8_t z = 0; put(&out, &z, 1); }
    if (file_size) *file_size = out.size + data_bytes;
    free(g->kv.data); free(g->tr.data);
    memset(g, 0, sizeof *g);
    return out;
}

static int32_t walk_of(Buf *b, uint64_t file_size, int32_t runtime, TLVWalk *w) {
    int32_t s[64], fit[64]; uint32_t types[64]; uint64_t at[64], size[64];
    return tlv_walk(b->data, 0, b->size, file_size, runtime, s, fit, types, at, size, 64, w);
}

/* A model of one file: a part table with one row. */
static int32_t model_of(Buf *b, uint64_t file_size, int32_t runtime, TLVRun *r) {
    uint64_t row[5] = {0, b->size, file_size, 0, 0};
    return tlv_model(b->data, b->size, row, 1, runtime, r);
}
static int32_t native_of(Buf *b, uint64_t file_size) {
    uint64_t row[5] = {0, b->size, file_size, 0, 0};
    return tlv_native(b->data, b->size, row, 1);
}
static int32_t hadamard_of(Buf *b, uint64_t file_size, TLVHadamard *h) {
    uint64_t row[5] = {0, b->size, file_size, 0, 0};
    return tlv_hadamard(b->data, b->size, row, 1, h);
}

static void test_header_rules(void) {
    TLVWalk w;
    uint64_t fs;
    Gguf g = {0};
    kv_str(&g, "general.architecture", "llama");
    rec2(&g, "a", 32, 2, 0, 0);
    Buf b = finish(&g, &fs, 256);
    assert(walk_of(&b, fs, LLAMA, &w) == 0 && w.tensors == 1 && w.data_start == b.size && w.read == 1);
    /* truncation asks for more */
    assert(tlv_walk(b.data, 0, 3, fs, LLAMA, NULL, NULL, NULL, NULL, NULL, 0, &w) == TF_ERR_TRUNCATED && w.needed == 4);
    assert(tlv_walk(b.data, 0, 30, fs, LLAMA, NULL, NULL, NULL, NULL, NULL, 0, &w) == TF_ERR_TRUNCATED && w.needed > 30);
    assert(tlv_walk(b.data, 0, b.size - 40, fs, LLAMA, NULL, NULL, NULL, NULL, NULL, 0, &w) == TF_ERR_TRUNCATED);
    /* magic and versions (gguf.cpp:476-517) */
    b.data[0] = 0; assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_MAGIC); b.data[0] = 'G';
    b.data[4] = 0; assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_VERSION);
    b.data[4] = 1; assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_VERSION);
    b.data[4] = 4; assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_VERSION);
    b.data[4] = 2; assert(walk_of(&b, fs, LLAMA, &w) == 0);                     /* v2 is read like v3 */
    b.data[4] = 0; b.data[7] = 3; assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_ENDIAN);
    free(b.data);

    /* keys: empty, duplicate (all bytes), NUL-distinct, bad value types */
    g = (Gguf){0}; kv_u32(&g, "", 1); b = finish(&g, &fs, 0);
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_KEY); free(b.data);
    g = (Gguf){0}; kv_u32(&g, "k", 1); kv_u32(&g, "k", 2); b = finish(&g, &fs, 0);
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_KEY); free(b.data);
    g = (Gguf){0}; key(&g, "x"); u32(&g.kv, 13); u32(&g.kv, 0); b = finish(&g, &fs, 0);
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_KEY); free(b.data);
    g = (Gguf){0}; key(&g, "nested"); u32(&g.kv, T_ARR); u32(&g.kv, T_ARR); u64(&g.kv, 0); b = finish(&g, &fs, 0);
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_KEY); free(b.data);
    g = (Gguf){0};
    kv_strn(&g, "general.architecture", "llama", 5);
    key(&g, "a\0x"); g.kv.size -= 1 + 8; bytes_str(&g.kv, "a\0x", 3); u32(&g.kv, T_U32); u32(&g.kv, 1);
    key(&g, "a\0y"); g.kv.size -= 1 + 8; bytes_str(&g.kv, "a\0y", 3); u32(&g.kv, T_U32); u32(&g.kv, 2);
    b = finish(&g, &fs, 0);
    assert(walk_of(&b, fs, LLAMA, &w) == 0); free(b.data);
    /* alignment (gguf.cpp:621-635) */
    g = (Gguf){0}; kv_u64(&g, "general.alignment", 32); b = finish(&g, &fs, 0);
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_ALIGNMENT); free(b.data);
    g = (Gguf){0}; kv_u32(&g, "general.alignment", 24); b = finish(&g, &fs, 0);
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_ALIGNMENT); free(b.data);
    g = (Gguf){0}; kv_u32(&g, "general.alignment", 64); rec2(&g, "a", 32, 1, 0, 0); b = finish(&g, &fs, 0);
    assert(walk_of(&b, b.size + 256, LLAMA, &w) == 0 && w.alignment == 64 && w.data_start % 64 == 0); free(b.data);
}

static void test_record_rules(void) {
    TLVWalk w;
    uint64_t fs;
    Gguf g;
    Buf b;
    char long_name[65];
    memset(long_name, 'n', 64); long_name[64] = 0;
    /* names: 64 bytes, duplicates, duplicates up to the first NUL */
    g = (Gguf){0}; rec2(&g, long_name, 32, 1, 0, 0); b = finish(&g, &fs, 128);
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_NAME && w.record == 0); free(b.data);
    g = (Gguf){0}; rec2(&g, "a", 32, 1, 0, 0); rec2(&g, "a", 32, 1, 0, 128); b = finish(&g, &fs, 256);
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_NAME && w.record == 1); free(b.data);
    g = (Gguf){0};
    str(&g.tr, "a"); g.tr.size -= 9; bytes_str(&g.tr, "a\0x", 3); u32(&g.tr, 1); u64(&g.tr, 32); u32(&g.tr, 0); u64(&g.tr, 0); g.tr.count++;
    bytes_str(&g.tr, "a\0y", 3); u32(&g.tr, 1); u64(&g.tr, 32); u32(&g.tr, 0); u64(&g.tr, 128); g.tr.count++;
    b = finish(&g, &fs, 256);
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_NAME && w.record == 1); free(b.data);
    /* dims: 5 is refused, 0 is a scalar (gguf.cpp:675-690) */
    uint64_t five[5] = {1, 1, 1, 1, 1};
    g = (Gguf){0}; rec(&g, "a", 5, five, 0, 0); b = finish(&g, &fs, 32);
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_SHAPE); free(b.data);
    g = (Gguf){0}; rec(&g, "scale", 0, NULL, 0, 0); rec2(&g, "b", 8, 1, 0, 32); b = finish(&g, &fs, 64);
    assert(walk_of(&b, fs, LLAMA, &w) == 0 && w.read == 2); free(b.data);
    /* negative and overflowing shapes */
    g = (Gguf){0}; rec2(&g, "a", (uint64_t)1 << 63, 1, 0, 0); b = finish(&g, &fs, 32);
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_SHAPE); free(b.data);
    /* an element count past 2^63: refused when the int64 product that
     * ggml-org and the fork compute first stays positive; no verdict when it
     * wraps to 0 or below (signed overflow); bitnet.cpp checks without that guard */
    g = (Gguf){0}; rec2(&g, "a", (uint64_t)1 << 40, ((uint64_t)1 << 30) + 1, 0, 0); b = finish(&g, &fs, 32);
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_SHAPE); free(b.data);
    g = (Gguf){0}; rec2(&g, "a", (uint64_t)1 << 32, (uint64_t)1 << 32, 0, 0); b = finish(&g, &fs, 32);
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_LIMIT && w.reader == TLV_ERR_LIMIT && w.record == 0);
    assert(walk_of(&b, fs, PRISM, &w) == TLV_ERR_LIMIT);
    assert(walk_of(&b, fs, BITNET, &w) == TLV_ERR_SHAPE);
    free(b.data);
    /* ... but a refusal after such a record is the reader's answer whichever way it went */
    g = (Gguf){0}; rec2(&g, "a", (uint64_t)1 << 32, (uint64_t)1 << 32, 0, 0); rec2(&g, "b", 8, 1, 200, 0);
    b = finish(&g, &fs, 32);
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_TYPE && w.record == 1);
    free(b.data);
    /* key names longer than 65535 bytes are read like any other */
    {
        char *long_key = malloc(70001);
        memset(long_key, 'k', 70000); long_key[70000] = 0;
        g = (Gguf){0}; kv_u32(&g, long_key, 1); kv_u32(&g, "", 1); b = finish(&g, &fs, 0);
        assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_KEY && w.keys == 2);
        free(b.data); free(long_key);
    }
    /* bitnet.cpp divides by ne[1..3] with no guard for empty tensors */
    g = (Gguf){0}; rec2(&g, "a", 32, 0, 0, 0); b = finish(&g, &fs, 0);
    assert(walk_of(&b, fs, LLAMA, &w) == 0 && walk_of(&b, fs, BITNET, &w) == TLV_ERR_SHAPE); free(b.data);
    g = (Gguf){0}; rec2(&g, "a", 0, 32, 0, 0); b = finish(&g, &fs, 0);
    assert(walk_of(&b, fs, BITNET, &w) == 0); free(b.data);
    /* types per runtime, and whole blocks per row */
    g = (Gguf){0}; rec2(&g, "a", 128, 1, 142, 0); b = finish(&g, &fs, 64);
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_TYPE && w.record == 0);
    assert(walk_of(&b, fs, PRISM, &w) == 0);                                     /* no prism. key needed */
    free(b.data);
    g = (Gguf){0}; rec2(&g, "a", 128, 1, 4, 0); b = finish(&g, &fs, 64);      /* Q4_2, removed */
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_TYPE); free(b.data);
    g = (Gguf){0}; rec2(&g, "blk.0.ffn_down.weight", 1320, 5120, 35, 0); b = finish(&g, &fs, 1 << 20);
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_ROW);                            /* bytes written as ne[0] */
    free(b.data);
    g = (Gguf){0}; kv_str(&g, "general.architecture", "bitnet-b1.58"); rec2(&g, "w", 128, 4, 36, 0);
    b = finish(&g, &fs, 128 * 4 / 4 + 32);
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_TYPE);
    assert(walk_of(&b, fs, BITNET, &w) == 0 && w.bitnet);                        /* I2_S: n / 4 + 32 bytes */
    assert(walk_of(&b, fs - 1, BITNET, &w) == TLV_ERR_BOUNDS);
    free(b.data);
}

static void test_offsets_and_places(void) {
    TLVWalk w;
    uint64_t fs;
    Gguf g;
    Buf b;
    int32_t s[8], fit[8]; uint32_t types[8]; uint64_t at[8], size[8];
    /* contiguous F32, Q8_0 and Q2_0 records */
    g = (Gguf){0};
    rec2(&g, "a", 8, 1, 0, 0);            /* 32 bytes */
    rec2(&g, "b", 64, 2, 8, 32);          /* 2 x 2 x 34 = 136 -> 160 */
    rec2(&g, "c", 1024, 4, 42, 192);      /* 4 x 16 x 18 = 1152 */
    b = finish(&g, &fs, 192 + 1152);
    assert(tlv_walk(b.data, 0, b.size, fs, LLAMA, s, fit, types, at, size, 8, &w) == 0);
    assert(s[0] == 0 && s[1] == 0 && s[2] == TF_Q2_0 && w.ternary == 1 && w.ternary_ok == 1);
    assert(types[1] == 8 && size[2] == 1 && b.data[at[2]] == 'c');
    free(b.data);
    /* the first record must start at 0; order is record order */
    g = (Gguf){0}; rec2(&g, "a", 8, 1, 0, 32); b = finish(&g, &fs, 64);
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_OFFSETS && w.record == 0 && w.expected == 0 && w.found == 32);
    free(b.data);
    g = (Gguf){0}; rec2(&g, "a", 8, 1, 0, 32); rec2(&g, "b", 8, 1, 0, 0); b = finish(&g, &fs, 64);
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_OFFSETS && w.record == 0); free(b.data);
    /* a gap after a record of known size, attributed to the next record; the first one's place is too big */
    g = (Gguf){0}; rec2(&g, "a", 8, 1, 0, 0); rec2(&g, "b", 8, 1, 0, 64); b = finish(&g, &fs, 128);
    assert(tlv_walk(b.data, 0, b.size, fs, LLAMA, s, fit, types, at, size, 8, &w) == TLV_ERR_OFFSETS);
    assert(w.record == 1 && w.expected == 32 && w.found == 64);
    assert(s[0] == TLV_ERR_OFFSETS && s[1] == 0 && w.misfit_other == 1 && w.misfit_ternary == 0);
    free(b.data);
    /* a zero-weight record takes no bytes, also a ternary one */
    g = (Gguf){0}; rec2(&g, "a", 8, 1, 0, 0); rec2(&g, "empty", 128, 0, 142, 32); rec2(&g, "b", 8, 1, 0, 32);
    b = finish(&g, &fs, 64);
    assert(walk_of(&b, fs, PRISM, &w) == 0); free(b.data);
    g = (Gguf){0}; rec2(&g, "a", 8, 1, 0, 0); rec2(&g, "empty", 8, 0, 0, 4096); rec2(&g, "b", 8, 1, 0, 32);
    b = finish(&g, &fs, 64);
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_OFFSETS && w.record == 1); free(b.data);
    /* the loader: data past the end of the file */
    g = (Gguf){0}; rec2(&g, "a", 8, 1, 0, 0); b = finish(&g, &fs, 31);
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_BOUNDS && w.record == 0 && w.found == fs); free(b.data);
    /* type 42 declared, 128-weight groups stored (PrismML-Eng/llama.cpp#167) */
    g = (Gguf){0};
    for (int i = 0; i < 3; i++) { char name[8]; snprintf(name, sizeof name, "w%d", i); rec2(&g, name, 1024, 4, 42, (uint64_t)i * 1088); }
    b = finish(&g, &fs, 3 * 1088);
    assert(tlv_walk(b.data, 0, b.size, fs, LLAMA, s, fit, types, at, size, 8, &w) == TLV_ERR_OFFSETS);
    assert(w.record == 1 && w.expected == 1152 && w.found == 1088);             /* as gguf.cpp names it */
    assert(s[0] == TF_ERR_EXTENT && s[1] == TF_ERR_EXTENT && s[2] == TF_ERR_EXTENT);  /* the last up to the file end */
    assert(fit[0] == TF_PQ2_0 && fit[1] == TF_PQ2_0 && fit[2] == TF_PQ2_0 && w.ternary == 3 && w.ternary_ok == 0);
    free(b.data);
    /* 64-weight bytes under the fork's 128-weight id: a gap, and the layout that fills it */
    g = (Gguf){0}; rec2(&g, "w0", 1024, 4, 142, 0); rec2(&g, "w1", 1024, 4, 142, 1152); b = finish(&g, &fs, 2304);
    assert(tlv_walk(b.data, 0, b.size, fs, PRISM, s, fit, types, at, size, 8, &w) == TLV_ERR_OFFSETS);
    assert(s[0] == TLV_ERR_OFFSETS && fit[0] == TF_Q2_0 && s[1] == TF_PQ2_0);
    free(b.data);
}

static void test_native_and_nbytes(void) {
    uint64_t fs;
    Gguf g;
    Buf b;
    g = (Gguf){0}; kv_str(&g, "general.architecture", "qwen35"); kv_u32(&g, "prism.x", 1); rec2(&g, "a", 8, 1, 0, 0); b = finish(&g, &fs, 32);
    assert(native_of(&b, fs) == PRISM); free(b.data);
    g = (Gguf){0}; kv_str(&g, "general.architecture", "qwen35"); rec2(&g, "a", 128, 1, 143, 0); b = finish(&g, &fs, 32);
    assert(native_of(&b, fs) == PRISM); free(b.data);
    g = (Gguf){0}; kv_str(&g, "general.architecture", "bitnet"); rec2(&g, "a", 128, 1, 36, 0); b = finish(&g, &fs, 64);
    assert(native_of(&b, fs) == BITNET); free(b.data);
    g = (Gguf){0}; kv_str(&g, "general.architecture", "llama"); rec2(&g, "a", 8, 1, 0, 0); b = finish(&g, &fs, 32);
    assert(native_of(&b, fs) == LLAMA); free(b.data);
    /* written for other software: no architecture (unless a later part of a
     * split model), an architecture no mapping knows, a type id nobody defines */
    g = (Gguf){0}; rec2(&g, "a", 8, 1, 0, 0); b = finish(&g, &fs, 32);
    assert(native_of(&b, fs) == TLV_NATIVE_OTHER); free(b.data);
    g = (Gguf){0}; kv_u16(&g, "split.no", 1); kv_u16(&g, "split.count", 2); rec2(&g, "a", 8, 1, 0, 0); b = finish(&g, &fs, 32);
    assert(native_of(&b, fs) == LLAMA); free(b.data);
    g = (Gguf){0}; kv_str(&g, "general.architecture", "k2-horizon"); rec2(&g, "a", 8, 1, 0, 0); b = finish(&g, &fs, 32);
    assert(native_of(&b, fs) == TLV_NATIVE_OTHER); free(b.data);
    g = (Gguf){0}; kv_str(&g, "general.architecture", "gptj"); rec2(&g, "a", 8, 1, 0, 0); b = finish(&g, &fs, 32);
    assert(native_of(&b, fs) == TLV_NATIVE_OTHER); free(b.data);
    g = (Gguf){0}; kv_str(&g, "general.architecture", "llama"); rec2(&g, "a", 256, 1, 66, 0); b = finish(&g, &fs, 256);
    assert(native_of(&b, fs) == TLV_NATIVE_OTHER); free(b.data);
    /* a type id of llama.cpp holding another layout (the vlut fork's I2_V is id
     * 40, NVFP4 in llama.cpp): its records do not fill their places while no
     * ternary record misfits, so the model is written for other software */
    g = (Gguf){0}; kv_str(&g, "general.architecture", "llama");
    rec2(&g, "blk.0.attn_q.weight", 1024, 4, 40, 0); rec2(&g, "blk.0.attn_k.weight", 1024, 4, 40, 1024);
    b = finish(&g, &fs, 2048);
    {
        TLVRun r;
        assert(model_of(&b, fs, LLAMA, &r) == TLV_RUN_REFUSES && r.status == TLV_ERR_OFFSETS);
        assert(native_of(&b, fs) == TLV_NATIVE_OTHER);
    }
    free(b.data);
    /* the legacy Q2_0 files misfit on ternary records: still llama.cpp's */
    g = (Gguf){0}; kv_str(&g, "general.architecture", "qwen35");
    for (int i = 0; i < 2; i++) { char name[8]; snprintf(name, sizeof name, "w%d", i); rec2(&g, name, 1024, 4, 42, (uint64_t)i * 1088); }
    b = finish(&g, &fs, 2 * 1088);
    assert(native_of(&b, fs) == LLAMA); free(b.data);
    /* id 42 with bitnet.cpp's TL2 sizes and a llama architecture: bitnet.cpp
     * accepts it, llama.cpp (Q2_0) does not */
    {
        uint64_t n0 = tlv_nbytes(BITNET, 42, 2560, 640, 1, 1), p0 = (n0 + 31) / 32 * 32;
        g = (Gguf){0}; kv_str(&g, "general.architecture", "llama");
        rec2(&g, "blk.0.ffn_up.weight", 2560, 640, 42, 0); rec2(&g, "blk.0.ffn_down.weight", 2560, 640, 42, p0);
        b = finish(&g, &fs, p0 + n0);
        TLVRun r;
        assert(model_of(&b, fs, BITNET, &r) == TLV_RUN_ACCEPTS && model_of(&b, fs, LLAMA, &r) == TLV_RUN_REFUSES);
        assert(native_of(&b, fs) == BITNET);
        uint64_t row[5] = {0, b.size, fs, 0, 0};
        assert(tlv_ternary_count(b.data, b.size, row, 1, BITNET) == 2);
        int32_t st[4], fit[4]; uint32_t types[4]; uint64_t at[4], size[4];
        TLVWalk w;
        assert(tlv_walk(b.data, 0, b.size, fs, BITNET, st, fit, types, at, size, 4, &w) == 0);
        assert(st[0] == TLV_LAYOUT_TL2 && st[1] == TLV_LAYOUT_TL2 && w.ternary_ok == 2);
        free(b.data);
    }
    /* ggml_nbytes (ggml.c) and bitnet.cpp's special cases (its ggml.c:1300-1330) */
    assert(tlv_nbytes(LLAMA, 0, 8, 2, 1, 1) == 64);
    assert(tlv_nbytes(LLAMA, 42, 1024, 4, 1, 1) == 1152);
    assert(tlv_nbytes(PRISM, 142, 1024, 4, 1, 1) == 1088);
    assert(tlv_nbytes(LLAMA, 142, 1024, 4, 1, 1) == UINT64_MAX);
    assert(tlv_nbytes(LLAMA, 0, 8, 0, 1, 1) == 0);
    assert(tlv_nbytes(BITNET, 36, 128, 4, 1, 1) == 128 * 4 / 4 + 32);
    uint64_t tl2 = (uint64_t)(2560 - 256) * 640 / 3 * 5 / 8 + 256 * 640 / 2 * 4 / 8;
    if (tl2 % 32) tl2 = 32 - tl2 % 32 + tl2;
    assert(tlv_nbytes(BITNET, 42, 2560, 640, 1, 1) == tl2 + 32);
    /* TL2 below 256 columns: int64 arithmetic, division toward zero (the reviewer's 128 x 1 is 96 bytes) */
    assert(tlv_nbytes(BITNET, 42, 128, 1, 1, 1) == 96);
    assert(tlv_tl2_defined(1, (uint64_t)1 << 50) && !tlv_tl2_defined(1, (uint64_t)1 << 60));
    /* ggml_nbytes multiplies before dividing, in size_t: ne0 = 2^62 of q4_0 wraps to 2^58 */
    assert(tlv_nbytes(LLAMA, 2, (uint64_t)1 << 62, 1, 1, 1) == (uint64_t)1 << 58);
    assert(trt_type_count(LLAMA) == 43 && trt_type_count(PRISM) == 144 && trt_type_count(BITNET) == 43);
    assert(trt_blck(LLAMA, 42) == 64 && trt_bytes(LLAMA, 42) == 18 && trt_blck(BITNET, 42) == 1);
    assert(tlv_file_verdict(TLV_RUN_REFUSES, TLV_ERR_TYPE, 5, LLAMA) == TLV_FILE_REFUSED);
    assert(tlv_file_verdict(TF_ERR_TRUNCATED, TF_ERR_TRUNCATED, 5, LLAMA) == TLV_FILE_UNDECIDED);
    assert(tlv_file_verdict(TLV_RUN_ACCEPTS, 0, 0, LLAMA) == TLV_FILE_NO_TERNARY);
    assert(tlv_file_verdict(TLV_RUN_ACCEPTS, 0, 3, PRISM) == TLV_FILE_OK);
    assert(tlv_file_verdict(TLV_RUN_REFUSES, TLV_ERR_TYPE, 3, TLV_NATIVE_OTHER) == TLV_FILE_OTHER);
    assert(tlv_file_verdict(TLV_RUN_REFUSES, TLV_ERR_ENDIAN, 0, LLAMA) == TLV_FILE_OTHER);
}

/* ---- prism.hadamard.* -------------------------------------------------------------- */

typedef struct {
    int version_type, version, block;
    const char *transform, *axis, *mode, *arch;
    const char **names; size_t n_names; bool names_as_ints; bool names_missing;
    const int32_t *widths; size_t n_widths; const int32_t *values; size_t n_values; uint32_t sign_elem;
    int gdn;                       /* 0 absent, 1 false, 2 true, 3 wrong type */
    const char **inverse; size_t n_inverse; int inverse_kind;  /* 0 strings, 1 ints, 2 scalar */
    uint64_t ne0, embd_ne0;
    bool drop_tensor, skip_transform, transform_u32;
    uint32_t rank, groups;
} Spec;

static const char *one_name[] = {"blk.0.attn_q.weight"};

static Spec base(void) {
    Spec s = {0};
    s.version_type = T_U32; s.version = 1; s.block = 4;
    s.transform = "normalized-sylvester-walsh-hadamard"; s.axis = "input-last-dimension"; s.mode = "identity"; s.arch = "qwen35";
    s.names = one_name; s.n_names = 1; s.sign_elem = T_I32; s.ne0 = 8; s.embd_ne0 = 8;
    return s;
}

static Buf build(const Spec *s, uint64_t *fs) {
    Gguf g = {0};
    kv_str(&g, "general.architecture", s->arch);
    if (s->rank) { char k[64]; snprintf(k, sizeof k, "%s.ssm.time_step_rank", s->arch); kv_u32(&g, k, s->rank); }
    if (s->groups) { char k[64]; snprintf(k, sizeof k, "%s.ssm.group_count", s->arch); kv_u32(&g, k, s->groups); }
    if (s->version_type == T_U32) kv_u32(&g, "prism.hadamard.version", (uint32_t)s->version);
    if (s->version_type == T_U64) kv_u64(&g, "prism.hadamard.version", (uint64_t)s->version);
    if (s->block >= 0) kv_u32(&g, "prism.hadamard.block_size", (uint32_t)s->block);
    if (!s->skip_transform) {
        if (s->transform_u32) kv_u32(&g, "prism.hadamard.transform", 1);
        else kv_str(&g, "prism.hadamard.transform", s->transform);
    }
    kv_str(&g, "prism.hadamard.axis", s->axis);
    kv_str(&g, "prism.hadamard.sign_mode", s->mode);
    if (!s->names_missing) {
        if (s->names_as_ints) { int32_t v[1] = {1}; kv_ints(&g, "prism.hadamard.weight_names", T_I32, v, 1); }
        else kv_strs(&g, "prism.hadamard.weight_names", s->names, s->n_names);
    }
    if (s->widths) kv_ints(&g, "prism.hadamard.sign_widths", s->sign_elem, s->widths, s->n_widths);
    if (s->values) kv_ints(&g, "prism.hadamard.sign_values", s->sign_elem, s->values, s->n_values);
    if (s->gdn == 1) kv_bool(&g, "prism.hadamard.gdn_v_grouped", false);
    if (s->gdn == 2) kv_bool(&g, "prism.hadamard.gdn_v_grouped", true);
    if (s->gdn == 3) kv_u32(&g, "prism.hadamard.gdn_v_grouped", 1);
    if (s->inverse) {
        if (s->inverse_kind == 0) kv_strs(&g, "prism.hadamard.inverse_weight_names", s->inverse, s->n_inverse);
        if (s->inverse_kind == 1) { int32_t v[1] = {1}; kv_ints(&g, "prism.hadamard.inverse_weight_names", T_I32, v, 1); }
        if (s->inverse_kind == 2) kv_u32(&g, "prism.hadamard.inverse_weight_names", 1);
    }
    /* F16 records: gguf.cpp accepts any ne[0] for them */
    uint64_t off = 0;
    if (!s->drop_tensor) {
        for (size_t i = 0; i < s->n_names; i++) {
            if (strstr(s->names[i], "attn_norm") || strstr(s->names[i], ".x.")
                || strcmp(s->names[i], "token_embd.weight") == 0) continue;
            bool seen = false;
            for (size_t j = 0; j < i; j++) seen = seen || strcmp(s->names[i], s->names[j]) == 0;
            if (seen) continue;
            rec2(&g, s->names[i], s->ne0, 2, 1, off); off += (s->ne0 * 2 * 2 + 31) / 32 * 32;
        }
    }
    rec2(&g, "token_embd.weight", s->embd_ne0, 4, 1, off); off += (s->embd_ne0 * 4 * 2 + 31) / 32 * 32;
    return finish(&g, fs, off);
}

static int32_t verdict(const Spec *s, TLVHadamard *out) {
    uint64_t fs;
    Buf b = build(s, &fs);
    TLVWalk w;
    int32_t st[64], fit[64]; uint32_t types[64]; uint64_t at[64], size[64];
    assert(tlv_walk(b.data, 0, b.size, fs, PRISM, st, fit, types, at, size, 64, &w) == 0);  /* a header gguf.cpp reads */
    int32_t status = hadamard_of(&b, fs, out);
    free(b.data);
    return status;
}

static void test_hadamard(void) {
    TLVHadamard h;
    Spec s;
    uint64_t fs;
    /* absent: not a verdict */
    Gguf g = {0}; kv_str(&g, "general.architecture", "qwen35"); kv_u32(&g, "prism.other", 1); rec2(&g, "a", 8, 1, 1, 0);
    Buf b = finish(&g, &fs, 32);
    assert(hadamard_of(&b, fs, &h) == 0 && !h.present); free(b.data);

    s = base(); assert(verdict(&s, &h) == 0 && h.present && h.block_size == 4 && h.names == 1 && !h.explicit_signs);
    int32_t widths[] = {8, 4};
    int32_t values[] = {1, -1, 1, 1, -1, -1, 1, -1, 1, 1, 1, 1};
    s.mode = "explicit"; s.widths = widths; s.n_widths = 2; s.values = values; s.n_values = 12;
    assert(verdict(&s, &h) == 0 && h.explicit_signs && h.widths == 2 && h.signs == 12);
    s.sign_elem = T_U32; assert(verdict(&s, &h) == 0);                          /* uint32 arrays: 0xffffffff is -1 */
    const char *inverse[] = {"token_embd.weight"};
    s = base(); s.inverse = inverse; s.n_inverse = 1; assert(verdict(&s, &h) == 0 && h.inverse == 1);
    s.embd_ne0 = 10; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_TENSOR && h.index == 1);  /* inverse table rules */
    s = base(); s.inverse = inverse; s.inverse_kind = 2; assert(verdict(&s, &h) == 0 && h.inverse == 0);  /* not an array: ignored */
    s = base(); s.inverse = inverse; s.inverse_kind = 1; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_TYPE);
    /* identity mode never reads sign keys, however malformed */
    int32_t bad_w[] = {3};
    s = base(); s.widths = bad_w; s.n_widths = 1; s.sign_elem = T_F32; assert(verdict(&s, &h) == 0);

    /* rejections, in the fork's order */
    s = base(); s.version_type = T_U64; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_TYPE && h.present);
    s = base(); s.version = 2; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_VERSION);
    s = base(); s.block = -1; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_MISSING);
    s = base(); s.skip_transform = true; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_MISSING);
    s = base(); s.transform_u32 = true; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_TYPE);
    s = base(); s.block = 0; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_BLOCK);
    s = base(); s.block = 12; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_BLOCK);
    s = base(); s.transform = "sylvester"; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_TRANSFORM);
    s = base(); s.axis = "output"; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_TRANSFORM);
    s = base(); s.mode = "random"; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_TRANSFORM);
    s = base(); s.names_missing = true; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_MISSING);
    s = base(); s.names_as_ints = true; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_TYPE);
    s = base(); s.n_names = 0; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_NAME);
    int32_t w8[] = {8}, w6[] = {6}, w0[] = {0}, wneg[] = {-8}, w4[] = {4};
    int32_t v8[] = {1, -1, 1, 1, -1, -1, 1, -1};
    int32_t bad[] = {1, -1, 1, 0, -1, -1, 1, -1};
    int32_t v12[] = {1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1};
    int32_t v4[] = {1, 1, -1, 1};
    s = base(); s.mode = "explicit"; s.widths = w8; s.n_widths = 0; s.values = v8; s.n_values = 8;
    assert(verdict(&s, &h) == TLV_ERR_HADAMARD_SIGNS);
    s.n_widths = 1; s.widths = w6; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_SIGNS);
    s.widths = w0; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_SIGNS);
    s.widths = wneg; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_SIGNS);
    s.widths = w8; s.values = bad; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_SIGNS && h.index == 3);
    s.values = v8; s.n_values = 7; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_SIGNS);
    s.values = v12; s.n_values = 12; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_SIGNS);
    s = base(); s.mode = "explicit"; s.values = v8; s.n_values = 8; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_MISSING);
    s = base(); s.gdn = 3; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_TYPE);
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
    s = base(); s.inverse = out_inverse; s.n_inverse = 1; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_NAME && h.index == 1);
    const char *inv_twice[] = {"token_embd.weight", "token_embd.weight"};
    s = base(); s.inverse = inv_twice; s.n_inverse = 2; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_NAME && h.index == 2);
    s = base(); s.drop_tensor = true; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_TENSOR && h.index == 0);
    s = base(); s.ne0 = 10; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_TENSOR);
    s = base(); s.mode = "explicit"; s.widths = w4; s.n_widths = 1; s.values = v4; s.n_values = 4;
    assert(verdict(&s, &h) == TLV_ERR_HADAMARD_TENSOR);                        /* no vector of width 8 */
    /* grouped GDN heads (src/llama-model.cpp:2090-2095) */
    const char *ssm[] = {"blk.3.ssm_out.weight"};
    s = base(); s.names = ssm; s.gdn = 2; s.ne0 = 1024; s.rank = 48; s.groups = 16;
    assert(verdict(&s, &h) == TLV_ERR_HADAMARD_TENSOR && h.gdn_v_grouped);      /* 1024 % 48 != 0 */
    s.ne0 = 1536; assert(verdict(&s, &h) == 0);
    s.groups = 0; assert(verdict(&s, &h) == TLV_ERR_HADAMARD_TENSOR);           /* no group count */
    s.groups = 16; s.gdn = 1; s.ne0 = 1024; assert(verdict(&s, &h) == 0);       /* false: not checked */
}

static void test_c_strings(void) {
    TLVHadamard h;
    uint64_t fs;
    /* the loaders read values and names up to the first NUL */
    Gguf g = {0};
    kv_strn(&g, "general.architecture", "qwen35\0x", 8);
    kv_u32(&g, "prism.hadamard.version", 1); kv_u32(&g, "prism.hadamard.block_size", 4);
    kv_str(&g, "prism.hadamard.transform", "normalized-sylvester-walsh-hadamard");
    kv_str(&g, "prism.hadamard.axis", "input-last-dimension"); kv_str(&g, "prism.hadamard.sign_mode", "identity");
    key(&g, "prism.hadamard.weight_names"); u32(&g.kv, T_ARR); u32(&g.kv, T_STR); u64(&g.kv, 1);
    bytes_str(&g.kv, "blk.0.attn_q.weight\0junk", 24);
    rec2(&g, "blk.0.attn_q.weight", 8, 2, 1, 0);
    Buf b = finish(&g, &fs, 32);
    assert(hadamard_of(&b, fs, &h) == 0);
    TLVRun r;
    assert(tlv_runtime(b.data, b.size, fs, PRISM, &r) == TLV_RUN_ACCEPTS);
    assert(tlv_runtime(b.data, b.size, fs, LLAMA, &r) == TLV_RUN_IGNORES_ROTATION);
    free(b.data);
}

static void test_runtime(void) {
    TLVRun r;
    TLVWalk w;
    uint64_t fs;
    /* the architecture comes before the Hadamard rules (src/llama-model.cpp:368-369), even with a bad version */
    Spec s = base(); s.arch = "foo"; s.version = 2;
    Buf b = build(&s, &fs);
    for (int32_t rt = LLAMA; rt <= BITNET; rt++) {
        assert(tlv_runtime(b.data, b.size, fs, rt, &r) == TLV_RUN_REFUSES && r.status == TLV_ERR_ARCH);
    }
    free(b.data);
    s = base(); b = build(&s, &fs);
    assert(tlv_runtime(b.data, b.size, fs, PRISM, &r) == TLV_RUN_ACCEPTS && r.verdict == TLV_RUN_ACCEPTS);
    assert(tlv_runtime(b.data, b.size, fs, LLAMA, &r) == TLV_RUN_IGNORES_ROTATION);
    assert(tlv_runtime(b.data, b.size, fs, BITNET, &r) == TLV_RUN_IGNORES_ROTATION);
    free(b.data);
    s = base(); s.version = 2; b = build(&s, &fs);
    assert(tlv_runtime(b.data, b.size, fs, PRISM, &r) == TLV_RUN_REFUSES && r.status == TLV_ERR_HADAMARD_VERSION);
    assert(tlv_runtime(b.data, b.size, fs, LLAMA, &r) == TLV_RUN_IGNORES_ROTATION);
    free(b.data);
    /* the fork's ids without any prism. key */
    Gguf g = {0}; kv_str(&g, "general.architecture", "qwen35"); rec2(&g, "a", 128, 4, 142, 0);
    b = finish(&g, &fs, 4 * 34);
    assert(tlv_runtime(b.data, b.size, fs, LLAMA, &r) == TLV_RUN_REFUSES && r.status == TLV_ERR_TYPE && r.record == 0);
    assert(tlv_runtime(b.data, b.size, fs, PRISM, &r) == TLV_RUN_ACCEPTS);
    free(b.data);
    /* bitnet.cpp's architecture and I2_S */
    g = (Gguf){0}; kv_str(&g, "general.architecture", "bitnet-b1.58"); rec2(&g, "w", 128, 4, 36, 0);
    b = finish(&g, &fs, 128 * 4 / 4 + 32);
    assert(tlv_runtime(b.data, b.size, fs, LLAMA, &r) == TLV_RUN_REFUSES && r.status == TLV_ERR_TYPE);
    assert(tlv_runtime(b.data, b.size, fs, BITNET, &r) == TLV_RUN_ACCEPTS);
    free(b.data);
    g = (Gguf){0}; rec2(&g, "w", 8, 1, 0, 0); b = finish(&g, &fs, 32);            /* no architecture key */
    assert(tlv_runtime(b.data, b.size, fs, LLAMA, &r) == TLV_RUN_REFUSES && r.status == TLV_ERR_ARCH);
    assert(tlv_runtime(b.data, 20, fs, LLAMA, &r) == TF_ERR_TRUNCATED && r.verdict == TF_ERR_TRUNCATED);
    free(b.data);
    /* names the mapping refuses: unmapped (gptj), the unknown entry, clip as a main model */
    const char *refused[] = {"gptj", "(unknown)", "clip"};
    for (int i = 0; i < 3; i++) {
        g = (Gguf){0}; kv_str(&g, "general.architecture", refused[i]); rec2(&g, "w", 8, 1, 0, 0); b = finish(&g, &fs, 32);
        for (int32_t rt = LLAMA; rt <= BITNET; rt++) {
            assert(tlv_runtime(b.data, b.size, fs, rt, &r) == TLV_RUN_REFUSES && r.status == TLV_ERR_ARCH);
        }
        free(b.data);
    }
    /* the loader reads general.architecture as a string before any file-end check */
    g = (Gguf){0}; kv_u32(&g, "general.architecture", 7); rec2(&g, "w", 8, 1, 0, 0); b = finish(&g, &fs, 16);
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_BOUNDS);
    assert(tlv_runtime(b.data, b.size, fs, LLAMA, &r) == TLV_RUN_REFUSES && r.status == TLV_ERR_ARCH);
    free(b.data);
}

/* ---- split models (src/llama-model-loader.cpp:570-674) ----------------------------- */

/* Part `no` (1-based) of `count`: part 1 carries the architecture and the
 * split keys, the others split.no/split.count/split.tensors.count only (as
 * gguf-split writes them). One f32 tensor of 8 x 1 per part. */
static Buf split_part(int no, int count, int32_t declared, const char *tensor, int split_no_kind, uint64_t *fs,
                      uint64_t data) {
    Gguf g = {0};
    if (no == 1) kv_str(&g, "general.architecture", "llama");
    if (split_no_kind == 0) kv_u16(&g, "split.no", (uint16_t)(no - 1));
    if (split_no_kind == 1) {                                     /* an array of one u16 */
        key(&g, "split.no"); u32(&g.kv, T_ARR); u32(&g.kv, T_U16); u64(&g.kv, 1);
        uint8_t x[2] = {(uint8_t)(no - 1), 0}; put(&g.kv, x, 2);
    }
    if (split_no_kind == 2) kv_u32(&g, "split.no", (uint32_t)(no - 1));
    kv_u16(&g, "split.count", (uint16_t)count);
    kv_i32(&g, "split.tensors.count", declared);
    rec2(&g, tensor, 8, 1, 0, 0);
    return finish(&g, fs, data);
}

typedef struct { Buf all; uint64_t rows[5 * 4]; size_t count; } Parts;

static void add_part(Parts *m, Buf part, uint64_t fs, uint64_t no, uint64_t count) {
    uint64_t *row = m->rows + 5 * m->count;
    row[0] = m->all.size; row[1] = part.size; row[2] = fs; row[3] = no; row[4] = count;
    put(&m->all, part.data, part.size);
    free(part.data);
    m->count++;
}

static void test_split_models(void) {
    TLVRun r;
    uint64_t fs1, fs2;
    Parts m;
    const char *prism_names[] = {"blk.1.attn_q.weight"};
    /* a whole split model */
    m = (Parts){0};
    add_part(&m, split_part(1, 2, 2, "blk.0.attn_q.weight", 0, &fs1, 32), fs1, 1, 2);
    add_part(&m, split_part(2, 2, 2, "blk.1.attn_q.weight", 0, &fs2, 32), fs2, 2, 2);
    for (int32_t rt = LLAMA; rt <= BITNET; rt++) {
        assert(tlv_model(m.all.data, m.all.size, m.rows, m.count, rt, &r) == TLV_RUN_ACCEPTS);
    }
    assert(tlv_loaded(m.all.data, m.all.size, m.rows, m.count) == 2 && tlv_native(m.all.data, m.all.size, m.rows, m.count) == LLAMA);
    /* the first file alone: part 2 is missing */
    assert(tlv_model(m.all.data, m.all.size, m.rows, 1, LLAMA, &r) == TLV_RUN_REFUSES && r.status == TLV_ERR_SPLIT
           && r.part == 1 && r.expected == 2);
    /* a later part as the model: split.no must be 0 in the file loaded first */
    {
        Parts later = {0};
        uint64_t fs;
        add_part(&later, split_part(2, 2, 2, "blk.1.attn_q.weight", 0, &fs, 32), fs, 2, 2);
        assert(tlv_model(later.all.data, later.all.size, later.rows, 1, LLAMA, &r) == TLV_RUN_REFUSES
               && r.status == TLV_ERR_SPLIT && r.part == 0);
        assert(tlv_native(later.all.data, later.all.size, later.rows, 1) == LLAMA);  /* not other software */
        free(later.all.data);
    }
    /* the first file's name must end in -00001-of-00002.gguf */
    m.rows[3] = 0; m.rows[4] = 0;
    assert(tlv_model(m.all.data, m.all.size, m.rows, m.count, LLAMA, &r) == TLV_RUN_REFUSES && r.status == TLV_ERR_SPLIT);
    m.rows[3] = 1; m.rows[4] = 3;
    assert(tlv_model(m.all.data, m.all.size, m.rows, m.count, LLAMA, &r) == TLV_RUN_REFUSES && r.status == TLV_ERR_SPLIT);
    free(m.all.data);
    /* split.tensors.count must equal the tensors loaded */
    m = (Parts){0};
    add_part(&m, split_part(1, 2, 3, "a", 0, &fs1, 32), fs1, 1, 2);
    add_part(&m, split_part(2, 2, 3, "b", 0, &fs2, 32), fs2, 2, 2);
    assert(tlv_model(m.all.data, m.all.size, m.rows, m.count, LLAMA, &r) == TLV_RUN_REFUSES && r.status == TLV_ERR_SPLIT
           && r.expected == 3 && r.found == 2);
    free(m.all.data);
    /* a name loaded from an earlier part */
    m = (Parts){0};
    add_part(&m, split_part(1, 2, 2, "same", 0, &fs1, 32), fs1, 1, 2);
    add_part(&m, split_part(2, 2, 2, "same", 0, &fs2, 32), fs2, 2, 2);
    assert(tlv_model(m.all.data, m.all.size, m.rows, m.count, LLAMA, &r) == TLV_RUN_REFUSES && r.status == TLV_ERR_NAME
           && r.part == 1 && r.record == 0);
    free(m.all.data);
    /* split.no in a later part: a u16 or an array of one u16 (gguf_get_val_u16); a u32 aborts */
    m = (Parts){0};
    add_part(&m, split_part(1, 2, 2, "a", 0, &fs1, 32), fs1, 1, 2);
    add_part(&m, split_part(2, 2, 2, "b", 1, &fs2, 32), fs2, 2, 2);
    assert(tlv_model(m.all.data, m.all.size, m.rows, m.count, LLAMA, &r) == TLV_RUN_ACCEPTS);
    free(m.all.data);
    m = (Parts){0};
    add_part(&m, split_part(1, 2, 2, "a", 0, &fs1, 32), fs1, 1, 2);
    add_part(&m, split_part(2, 2, 2, "b", 2, &fs2, 32), fs2, 2, 2);
    assert(tlv_model(m.all.data, m.all.size, m.rows, m.count, LLAMA, &r) == TLV_RUN_REFUSES && r.status == TLV_ERR_SPLIT
           && r.part == 1);
    free(m.all.data);
    /* a later part's data past the end of its own file */
    m = (Parts){0};
    add_part(&m, split_part(1, 2, 2, "a", 0, &fs1, 32), fs1, 1, 2);
    add_part(&m, split_part(2, 2, 2, "b", 0, &fs2, 16), fs2, 2, 2);
    assert(tlv_model(m.all.data, m.all.size, m.rows, m.count, LLAMA, &r) == TLV_RUN_REFUSES && r.status == TLV_ERR_BOUNDS
           && r.part == 1 && r.record == 0);
    free(m.all.data);
    /* split.count of the wrong type in the first file */
    {
        Gguf g = {0}; uint64_t fs;
        kv_str(&g, "general.architecture", "llama"); kv_u32(&g, "split.count", 2); rec2(&g, "a", 8, 1, 0, 0);
        Buf b = finish(&g, &fs, 32);
        assert(model_of(&b, fs, LLAMA, &r) == TLV_RUN_REFUSES && r.status == TLV_ERR_SPLIT);
        free(b.data);
    }
    /* the fork's Hadamard rules find listed weights in any part */
    {
        Gguf g = {0}; uint64_t fs;
        kv_str(&g, "general.architecture", "qwen35");
        kv_u16(&g, "split.no", 0); kv_u16(&g, "split.count", 2); kv_i32(&g, "split.tensors.count", 2);
        kv_u32(&g, "prism.hadamard.version", 1); kv_u32(&g, "prism.hadamard.block_size", 4);
        kv_str(&g, "prism.hadamard.transform", "normalized-sylvester-walsh-hadamard");
        kv_str(&g, "prism.hadamard.axis", "input-last-dimension"); kv_str(&g, "prism.hadamard.sign_mode", "identity");
        kv_strs(&g, "prism.hadamard.weight_names", prism_names, 1);
        rec2(&g, "blk.0.attn_q.weight", 8, 1, 0, 0);
        Buf first = finish(&g, &fs, 32);
        m = (Parts){0};
        add_part(&m, first, fs, 1, 2);
        add_part(&m, split_part(2, 2, 2, "blk.1.attn_q.weight", 0, &fs2, 32), fs2, 2, 2);
        assert(tlv_model(m.all.data, m.all.size, m.rows, m.count, PRISM, &r) == TLV_RUN_ACCEPTS);
        assert(tlv_model(m.all.data, m.all.size, m.rows, m.count, LLAMA, &r) == TLV_RUN_IGNORES_ROTATION);
        assert(tlv_model(m.all.data, m.all.size, m.rows, 1, PRISM, &r) == TLV_RUN_REFUSES && r.status == TLV_ERR_SPLIT);
        TLVHadamard h;
        assert(tlv_hadamard(m.all.data, m.all.size, m.rows, 1, &h) == TLV_ERR_HADAMARD_TENSOR);  /* the first file alone */
        free(m.all.data);
    }
    /* a bad part table is no verdict */
    uint64_t bad[5] = {8, 0, 0, 0, 0};
    uint8_t byte = 0;
    assert(tlv_model(&byte, 1, bad, 1, LLAMA, &r) == TF_ERR_CONTAINER && tlv_native(&byte, 1, bad, 1) == TLV_NATIVE_OTHER);
}

/* ---- what the readers refuse whatever the scanner's limits ------------------------- */

static void test_reader_limits(void) {
    TLVWalk w;
    uint64_t fs;
    Gguf g;
    Buf b;
    /* negative counts (gguf.cpp:519-541) */
    g = (Gguf){0}; rec2(&g, "a", 8, 1, 0, 0); b = finish(&g, &fs, 32);
    b.data[15] = 0x80; assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_COUNT); b.data[15] = 0;
    b.data[23] = 0x80; assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_COUNT); b.data[23] = 0;
    /* more records than the file holds: the reader runs out of bytes after the first */
    {
        uint64_t end_of_first = 24 + 8 + 1 + 4 + 16 + 4 + 8;
        b.data[8] = 2;
        assert(tlv_walk(b.data, 0, b.size, end_of_first, LLAMA, NULL, NULL, NULL, NULL, NULL, 0, &w) == TLV_ERR_SHORT);
        assert(w.record == 1 && w.read == 1);
        b.data[8] = 0xff; b.data[9] = 0xff; b.data[10] = 0xff; b.data[11] = 0xff; b.data[12] = 0xff;
        assert(tlv_walk(b.data, 0, b.size, end_of_first, LLAMA, NULL, NULL, NULL, NULL, NULL, 0, &w) == TLV_ERR_SHORT);
    }
    free(b.data);
    /* a string longer than 2^30 (gguf.cpp:19, :347-350) is refused, not waited for */
    g = (Gguf){0}; key(&g, "k"); u32(&g.kv, T_STR); u64(&g.kv, ((uint64_t)1 << 30) + 1); b = finish(&g, &fs, 0);
    assert(walk_of(&b, (uint64_t)1 << 40, LLAMA, &w) == TLV_ERR_KEY);
    free(b.data);
    /* an array of more than 2^30 elements (gguf.cpp:20, :279) */
    g = (Gguf){0}; key(&g, "k"); u32(&g.kv, T_ARR); u32(&g.kv, T_U32); u64(&g.kv, ((uint64_t)1 << 30) + 1); b = finish(&g, &fs, 0);
    assert(walk_of(&b, (uint64_t)1 << 40, LLAMA, &w) == TLV_ERR_KEY);
    free(b.data);
    /* a string array whose length prefixes alone outrun the file */
    g = (Gguf){0}; key(&g, "k"); u32(&g.kv, T_ARR); u32(&g.kv, T_STR); u64(&g.kv, 1000); b = finish(&g, &fs, 0);
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_SHORT);
    free(b.data);
    /* a header that runs past the end of the file is refused; past the bytes given, more are asked for */
    g = (Gguf){0}; kv_str(&g, "general.architecture", "llama"); rec2(&g, "a", 8, 1, 0, 0); b = finish(&g, &fs, 32);
    assert(tlv_walk(b.data, 0, 40, 40, LLAMA, NULL, NULL, NULL, NULL, NULL, 0, &w) == TLV_ERR_SHORT && w.reader == TLV_ERR_SHORT);
    assert(tlv_walk(b.data, 0, 40, fs, LLAMA, NULL, NULL, NULL, NULL, NULL, 0, &w) == TF_ERR_TRUNCATED && w.needed > 40);
    free(b.data);
    /* a name of 64 bytes or more is refused without its bytes */
    g = (Gguf){0}; str(&g.tr, "x"); g.tr.size -= 9; u64(&g.tr, 1000); g.tr.count++; b = finish(&g, &fs, 0);
    assert(tlv_walk(b.data, 0, b.size, 1 << 20, LLAMA, NULL, NULL, NULL, NULL, NULL, 0, &w) == TLV_ERR_NAME && w.record == 0);
    free(b.data);
    /* the data section must start inside the file (gguf.cpp:771-776): f32 [32] in a file that ends at the records */
    g = (Gguf){0}; rec2(&g, "a", 32, 1, 0, 0);
    b = finish(&g, &fs, 0);
    {
        uint64_t unpadded = 24 + 8 + 1 + 4 + 16 + 4 + 8;
        assert(tlv_walk(b.data, 0, unpadded, unpadded, LLAMA, NULL, NULL, NULL, NULL, NULL, 0, &w) == TLV_ERR_SHORT);
        assert(w.reader == TLV_ERR_SHORT);
    }
    free(b.data);
    /* general.alignment as an array of one u32: bitnet.cpp's gguf_get_val_u32 takes it, the others refuse it */
    g = (Gguf){0}; key(&g, "general.alignment"); u32(&g.kv, T_ARR); u32(&g.kv, T_U32); u64(&g.kv, 1); u32(&g.kv, 32);
    rec2(&g, "a", 8, 1, 0, 0); b = finish(&g, &fs, 32);
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_ALIGNMENT && walk_of(&b, fs, PRISM, &w) == TLV_ERR_ALIGNMENT);
    assert(walk_of(&b, fs, BITNET, &w) == 0);
    free(b.data);
    g = (Gguf){0}; key(&g, "general.alignment"); u32(&g.kv, T_ARR); u32(&g.kv, T_U32); u64(&g.kv, 2); u32(&g.kv, 32); u32(&g.kv, 32);
    b = finish(&g, &fs, 0);
    assert(walk_of(&b, fs, BITNET, &w) == TLV_ERR_ALIGNMENT);
    free(b.data);
    /* TL2's signed size overflows: no verdict */
    g = (Gguf){0}; kv_str(&g, "general.architecture", "bitnet-b1.58"); rec2(&g, "w", 1, (uint64_t)1 << 60, 42, 0);
    b = finish(&g, &fs, 4096);
    assert(walk_of(&b, fs, BITNET, &w) == TLV_ERR_LIMIT && w.reader == TLV_ERR_LIMIT);
    free(b.data);
}

/* ---- the scanner's own limits ------------------------------------------------------- */

static void test_scanner_limits(void) {
    TLVWalk w;
    TLVRun r;
    uint64_t fs;
    Gguf g;
    Buf b;
    /* skipping values again for each later key is bounded: 300 keys after a million strings */
    g = (Gguf){0};
    for (int i = 0; i < 256; i++) { char k[16]; snprintf(k, sizeof k, "k%d", i); kv_u32(&g, k, 1); }
    key(&g, "strings"); u32(&g.kv, T_ARR); u32(&g.kv, T_STR); u64(&g.kv, 1000000);
    for (int i = 0; i < 1000000; i++) u64(&g.kv, 0);
    for (int i = 0; i < 300; i++) { char k[16]; snprintf(k, sizeof k, "z%d", i); kv_u32(&g, k, 1); }
    b = finish(&g, &fs, 0);
    assert(walk_of(&b, fs, LLAMA, &w) == TLV_ERR_LIMIT && w.reader == TLV_ERR_LIMIT);
    free(b.data);
    /* the fork's Hadamard limit is no verdict, not a refusal */
    g = (Gguf){0};
    kv_str(&g, "general.architecture", "qwen35");
    kv_u32(&g, "prism.hadamard.version", 1); kv_u32(&g, "prism.hadamard.block_size", 4);
    kv_str(&g, "prism.hadamard.transform", "normalized-sylvester-walsh-hadamard");
    kv_str(&g, "prism.hadamard.axis", "input-last-dimension"); kv_str(&g, "prism.hadamard.sign_mode", "identity");
    key(&g, "prism.hadamard.weight_names"); u32(&g.kv, T_ARR); u32(&g.kv, T_STR); u64(&g.kv, 32769);
    for (int i = 0; i < 32769; i++) str(&g.kv, "blk.0.attn_q.weight");
    rec2(&g, "blk.0.attn_q.weight", 8, 1, 0, 0);
    b = finish(&g, &fs, 32);
    assert(model_of(&b, fs, PRISM, &r) == TLV_ERR_LIMIT && r.verdict == TLV_ERR_LIMIT);
    assert(model_of(&b, fs, LLAMA, &r) == TLV_RUN_IGNORES_ROTATION);
    free(b.data);
}

/* ---- per-record answers ------------------------------------------------------------- */

static void test_record_answers(void) {
    TLVWalk w;
    uint64_t fs;
    Gguf g;
    Buf b;
    int32_t s[4], fit[4]; uint32_t types[4]; uint64_t at[4], size[4];
    /* the ternary count does not depend on the room given for per-record answers */
    g = (Gguf){0}; rec2(&g, "a", 320, 4, 35, 0); rec2(&g, "b", 256, 4, 35, 0); b = finish(&g, &fs, 4096);
    assert(tlv_walk(b.data, 0, b.size, fs, LLAMA, NULL, NULL, NULL, NULL, NULL, 0, &w) == TLV_ERR_ROW && w.ternary == 1);
    assert(tlv_walk(b.data, 0, b.size, fs, LLAMA, s, fit, types, at, size, 4, &w) == TLV_ERR_ROW && w.ternary == 1);
    free(b.data);
    /* offsets out of record order: the record before the lower offset has no place */
    g = (Gguf){0}; rec2(&g, "a", 1024, 4, 42, 1152); rec2(&g, "b", 1024, 4, 42, 0); b = finish(&g, &fs, 2304);
    assert(tlv_walk(b.data, 0, b.size, fs, LLAMA, s, fit, types, at, size, 4, &w) == TLV_ERR_OFFSETS);
    assert(s[0] == TLV_ERR_OFFSETS && fit[0] == 0 && w.ternary_ok == 0);
    free(b.data);
}

int main(void) {
    test_header_rules();
    test_record_rules();
    test_offsets_and_places();
    test_native_and_nbytes();
    test_hadamard();
    test_c_strings();
    test_runtime();
    test_split_models();
    test_reader_limits();
    test_scanner_limits();
    test_record_answers();
    printf("PASS live: gguf.cpp header, key and record rules, offsets in record order, file end, per-record places "
           "and fitting layouts, runtime tables, the PrismML Hadamard rules with GDN heads, C-string names, "
           "runtime verdicts, split models, the readers' own limits and bitnet.cpp's differences, the scanner's "
           "limits\n");
    return 0;
}
