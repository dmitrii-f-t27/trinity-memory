/* Differential harness: specs/memory/tensorpack.t27 against the executable
 * TensorPack modules (t27/tensorpack.t27, t27/tensorpack_json.t27). Generate
 * the spec headers as specs/types.h and specs/tensorpack.h beside the
 * implementation headers (-I build/t27/specs/impl); link native/float.cpp.
 * No production algorithm is maintained here. */
#include <assert.h>
#include <float.h>
#include <inttypes.h>
#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#if defined(__clang__)
#pragma clang diagnostic ignored "-Wparentheses-equality"
#endif
double tm_json_strtod(uint8_t *);
size_t tm_float_shortest(double, uint8_t *, size_t);
#include "specs/types.h"
#include "specs/tensorpack.h"
#include "codecs.h"
#include "container.h"
#include "tensorpack.h"
#include "json.h"
#include "tensorpack_json.h"

static TMJsonToken tokens[4096];
static uint8_t arena[65536];
static TMTensorDescriptor descriptors[16];
static uint64_t shapes[256];
static double scales[4096];
static TMTPText axes[256];
static TMTPJSONWorkspace workspace(void) {
    return (TMTPJSONWorkspace){tokens, 4096, arena, sizeof arena, descriptors, 16, shapes, 256, scales, 4096, axes, 256};
}

static uint32_t spec_crc32_continue(uint32_t state, const uint8_t *data, size_t count) {
    for (size_t i = 0; i < count; ++i) state = tms_crc32_byte(state, data[i]);
    return state;
}
static uint32_t spec_crc32(const uint8_t *data, size_t count) {
    return spec_crc32_continue(TMS_CRC32_INIT, data, count) ^ TMS_CRC32_XOR_OUT;
}
static uint64_t read_le(const uint8_t *data, size_t offset, size_t bytes) {
    uint64_t value = 0;
    for (size_t i = 0; i < bytes; ++i) value |= (uint64_t)data[offset + i] << (8 * i);
    return value;
}
static void put32(uint8_t *out, uint32_t x) { for (unsigned i = 0; i < 4; ++i) out[i] = (uint8_t)(x >> (8 * i)); }
static void put64(uint8_t *out, uint64_t x) { for (unsigned i = 0; i < 8; ++i) out[i] = (uint8_t)(x >> (8 * i)); }

/* Independent framing with the spec's CRC definition. */
static size_t frame(uint8_t *out, const char *json, const uint8_t *payload, size_t n, uint32_t count) {
    size_t m = strlen(json);
    memset(out, 0, TMS_TTPK_HEADER_BYTES);
    out[0] = TMS_TTPK_MAGIC_0; out[1] = TMS_TTPK_MAGIC_1; out[2] = TMS_TTPK_MAGIC_2; out[3] = TMS_TTPK_MAGIC_3;
    out[TMS_TTPK_VERSION_OFFSET] = TMS_TTPK_VERSION;
    put32(out + TMS_TTPK_METADATA_LENGTH_OFFSET, (uint32_t)m);
    put32(out + TMS_TTPK_TENSOR_COUNT_OFFSET, count);
    put64(out + TMS_TTPK_PAYLOAD_LENGTH_OFFSET, n);
    memcpy(out + TMS_TTPK_METADATA_OFFSET, json, m);
    if (n) memcpy(out + TMS_TTPK_METADATA_OFFSET + m, payload, n);
    uint32_t crc = spec_crc32_continue(TMS_CRC32_INIT, out, TMS_TTPK_CRC_COVERED_HEADER_BYTES);
    put32(out + TMS_TTPK_METADATA_CRC_OFFSET, spec_crc32_continue(crc, out + TMS_TTPK_METADATA_OFFSET, m) ^ TMS_CRC32_XOR_OUT);
    put32(out + TMS_TTPK_PAYLOAD_CRC_OFFSET, spec_crc32(out + TMS_TTPK_METADATA_OFFSET + m, n));
    return TMS_TTPK_METADATA_OFFSET + m + n;
}

static void constants(void) {
    assert(TM_TP_ERR_DESCRIPTOR == TMS_TTPK_ERR_DESCRIPTOR && TM_TP_ERR_TEXT == TMS_TTPK_ERR_TEXT);
    assert(TM_TP_ERR_SHAPE == TMS_TTPK_ERR_SHAPE && TM_TP_ERR_SCALE == TMS_TTPK_ERR_SCALE);
    assert(TM_TP_ERR_AXES == TMS_TTPK_ERR_AXES && TM_TP_ERR_OFFSET == TMS_TTPK_ERR_OFFSET);
    assert(TM_TPJ_ERR_SCHEMA == TMS_TTPK_ERR_SCHEMA && TM_TPJ_ERR_JSON == TMS_TTPK_ERR_JSON);
    assert(TMS_TTPK_NESTED_HEADER_BYTES == TMS_TMEM_HEADER_BYTES);
}

static void empty_and_single_tensor_packs(void) {
    uint8_t out[1024], metadata[512], nested[64];
    int32_t values[3] = {1, -1, 0}, restored[4] = {9, 9, 9, 9};
    int64_t size = tm_tp_encode_json(NULL, 0, NULL, 0, metadata, sizeof metadata, out, sizeof out);
    assert(size == TMS_TTPK_EMPTY_PACK_BYTES);
    assert(out[0] == TMS_TTPK_MAGIC_0 && out[1] == TMS_TTPK_MAGIC_1 && out[2] == TMS_TTPK_MAGIC_2 && out[3] == TMS_TTPK_MAGIC_3);
    assert(out[TMS_TTPK_VERSION_OFFSET] == TMS_TTPK_VERSION && out[TMS_TTPK_FLAGS_OFFSET] == TMS_TTPK_FLAGS);
    assert(read_le(out, TMS_TTPK_RESERVED_OFFSET, TMS_TTPK_RESERVED_BYTES) == 0);
    assert(read_le(out, TMS_TTPK_METADATA_LENGTH_OFFSET, TMS_TTPK_METADATA_LENGTH_BYTES) == TMS_TTPK_EMPTY_METADATA_BYTES);
    assert(read_le(out, TMS_TTPK_TENSOR_COUNT_OFFSET, TMS_TTPK_TENSOR_COUNT_BYTES) == 0);
    assert(read_le(out, TMS_TTPK_PAYLOAD_LENGTH_OFFSET, TMS_TTPK_PAYLOAD_LENGTH_BYTES) == 0);
    assert(memcmp(out + TMS_TTPK_METADATA_OFFSET, "{\"order\":\"C\",\"tensors\":[]}", TMS_TTPK_EMPTY_METADATA_BYTES) == 0);
    uint32_t crc = spec_crc32_continue(TMS_CRC32_INIT, out, TMS_TTPK_CRC_COVERED_HEADER_BYTES);
    crc = spec_crc32_continue(crc, out + TMS_TTPK_METADATA_OFFSET, TMS_TTPK_EMPTY_METADATA_BYTES) ^ TMS_CRC32_XOR_OUT;
    assert(read_le(out, TMS_TTPK_METADATA_CRC_OFFSET, TMS_TTPK_CRC_BYTES) == crc);
    assert(read_le(out, TMS_TTPK_PAYLOAD_CRC_OFFSET, TMS_TTPK_CRC_BYTES) == spec_crc32(out, 0));
    assert(tms_ttpk_container_bytes(TMS_TTPK_EMPTY_METADATA_BYTES, 0) == (uint64_t)size);
    TMTPJSONWorkspace w = workspace(); TMTPValidation r;
    assert(tm_tp_decode_json(out, (size_t)size, &w, NULL, 0, 1, &r) == 0 && r.tensor_count == 0 && r.total_trits == 0);
    /* One dense5 tensor: canonical metadata, nested TMEM, exact restore. */
    uint64_t shape[1] = {3};
    double scale = 1.0;
    TMTensorDescriptor d = {.name = (uint8_t *)"w", .name_size = 1, .shape = shape, .rank = 1,
                            .scales = &scale, .scale_count = 1, .scale_axis = -1, .codec = TMS_CODEC_DENSE5};
    size = tm_tp_encode_json(&d, 1, values, 3, metadata, sizeof metadata, out, sizeof out);
    assert(size > 0);
    uint64_t metadata_bytes = read_le(out, TMS_TTPK_METADATA_LENGTH_OFFSET, 4);
    uint64_t payload_bytes = read_le(out, TMS_TTPK_PAYLOAD_LENGTH_OFFSET, 8);
    assert(tms_ttpk_size_consistent((uint64_t)size, metadata_bytes, payload_bytes));
    static const char canonical[] = "{\"order\":\"C\",\"tensors\":[{\"axes\":[],\"codec\":\"dense5\",\"length\":25,\"name\":\"w\",\"offset\":0,\"scale_axis\":null,\"scales\":[1.0],\"shape\":[3]}]}";
    assert(metadata_bytes == strlen(canonical) && memcmp(out + TMS_TTPK_METADATA_OFFSET, canonical, metadata_bytes) == 0);
    assert(payload_bytes == tms_ttpk_nested_length(tms_payload_bytes(TMS_CODEC_DENSE5, 3)));
    assert(tm_pack(TMS_CODEC_DENSE5, values, 3, nested, sizeof nested) == (int64_t)payload_bytes);
    assert(memcmp(out + TMS_TTPK_METADATA_OFFSET + metadata_bytes, nested, (size_t)payload_bytes) == 0);
    assert(tm_tp_decode_json(out, (size_t)size, &w, restored, 4, TMS_TTPK_MAX_TOTAL_TRITS, &r) == 0);
    assert(r.json_binding_valid && r.total_trits == 3 && memcmp(restored, values, sizeof values) == 0 && restored[3] == 9);
    assert(tm_tp_decode_json(out, (size_t)size, &w, restored, 4, 2, &r) == TM_ERR_LIMIT);
    assert(tm_tp_decode_json(out, (size_t)size, &w, restored, 4, 0, &r) == TM_ERR_LIMIT);
    assert(tms_ttpk_total_trit_limit(2) == 2 && tms_ttpk_total_trit_limit(TMS_TTPK_MAX_TOTAL_TRITS + 1) == TMS_TTPK_MAX_TOTAL_TRITS);
}

static void descriptor_rules(void) {
    uint64_t shape[17];
    for (unsigned i = 0; i < 17; ++i) shape[i] = 1;
    double scale = 1.0;
    TMTensorDescriptor d = {.name = (uint8_t *)"w", .name_size = 1, .shape = shape, .rank = 0,
                            .scales = &scale, .scale_count = 1, .scale_axis = -1, .codec = TMS_CODEC_DENSE5};
    struct { size_t rank; uint64_t first, second; } cases[] = {
        {0, 1, 1}, {1, 3, 1}, {2, 2, 3}, {16, 1, 1}, {17, 1, 1}, {1, 0, 1}, {1, 2147483648ULL, 1},
        {1, 2147483647ULL, 1}, {2, 2048, 2048}, {2, 2048, 2049},
    };
    for (size_t i = 0; i < sizeof cases / sizeof cases[0]; ++i) {
        for (unsigned k = 0; k < 17; ++k) shape[k] = 1;
        shape[0] = cases[i].first; shape[1] = cases[i].second;
        d.rank = cases[i].rank;
        int64_t expected = tms_ttpk_shape_count(shape, cases[i].rank);
        int64_t actual = tm_tp_descriptor_count(&d);
        if (expected < 0) assert(actual == TMS_TTPK_ERR_SHAPE);
        else assert(actual == expected);
    }
    d.rank = 1; shape[0] = 3;
    /* Names: length and control characters. */
    static uint8_t name[300];
    memset(name, 'n', sizeof name);
    d.name = name;
    size_t lengths[] = {0, 1, 256, 257};
    for (size_t i = 0; i < 4; ++i) {
        d.name_size = lengths[i];
        assert((tm_tp_descriptor_count(&d) == 3) == tms_ttpk_name_length_valid(lengths[i]));
    }
    d.name_size = 3;
    uint8_t bytes[] = {10, 31, 32, 126, 127};
    for (size_t i = 0; i < sizeof bytes; ++i) {
        name[1] = bytes[i];
        assert((tm_tp_descriptor_count(&d) == 3) == tms_ttpk_text_byte_allowed(bytes[i]));
    }
    name[1] = 'n';
    /* Axes: count, uniqueness, length. */
    static uint8_t long_label[65];
    memset(long_label, 'x', sizeof long_label);
    TMTPText labels[2] = {{(uint8_t *)"x", 1}, {(uint8_t *)"y", 1}};
    d.axes = labels;
    shape[0] = 3; shape[1] = 1; d.rank = 2;
    for (size_t count = 0; count <= 2; ++count) {
        d.axes_count = count;
        assert((tm_tp_descriptor_count(&d) == 3) == tms_ttpk_axes_count_valid(2, (uint32_t)count));
    }
    d.axes_count = 2; labels[1] = labels[0];
    assert(tm_tp_descriptor_count(&d) == TMS_TTPK_ERR_AXES);
    labels[1] = (TMTPText){(uint8_t *)"y", 1};
    labels[0] = (TMTPText){long_label, 64};
    assert(tm_tp_descriptor_count(&d) == 3 && tms_ttpk_axis_length_valid(64));
    labels[0] = (TMTPText){long_label, 65};
    assert(tm_tp_descriptor_count(&d) == TMS_TTPK_ERR_AXES && !tms_ttpk_axis_length_valid(65));
    labels[0] = (TMTPText){(uint8_t *)"", 0};
    assert(tm_tp_descriptor_count(&d) == TMS_TTPK_ERR_AXES && !tms_ttpk_axis_length_valid(0));
    d.axes_count = 0;
    /* Scales: axis validity, count, values. */
    double row_scales[3] = {0.5, 1.0, 2.0};
    d.rank = 2; shape[0] = 2; shape[1] = 3;
    int32_t axis_cases[] = {-1, 0, 1, 2, -2, 16};
    for (size_t i = 0; i < sizeof axis_cases / sizeof axis_cases[0]; ++i) {
        d.scale_axis = axis_cases[i];
        int64_t wanted = tms_ttpk_scale_count(2, axis_cases[i], axis_cases[i] >= 0 && axis_cases[i] < 2 ? shape[axis_cases[i]] : 0);
        d.scales = row_scales; d.scale_count = wanted > 0 ? (size_t)wanted : 1;
        assert((tm_tp_descriptor_count(&d) == 6) == (wanted > 0));
        if (wanted > 0) {
            d.scale_count = (size_t)wanted + 1;
            assert(tm_tp_descriptor_count(&d) == TMS_TTPK_ERR_SCALE);
        }
    }
    d.scale_axis = -1; d.scale_count = 1; d.scales = &scale;
    double scale_values[] = {0.5, DBL_MAX, DBL_TRUE_MIN, 0.0, -1.0, NAN, INFINITY};
    for (size_t i = 0; i < sizeof scale_values / sizeof scale_values[0]; ++i) {
        scale = scale_values[i];
        assert((tm_tp_descriptor_count(&d) == 6) == tms_ttpk_scale_valid(scale_values[i]));
    }
    scale = 1.0;
    d.codec = 6;
    assert(tm_tp_descriptor_count(&d) == TMS_ERR_CODEC);
}

static void header_rules(void) {
    uint8_t out[128], copy[128], metadata[64];
    int64_t size = tm_tp_encode_json(NULL, 0, NULL, 0, metadata, sizeof metadata, out, sizeof out);
    assert(size == TMS_TTPK_EMPTY_PACK_BYTES);
    TMTPHeader header;
    assert(tm_tp_validate_header(out, (size_t)size, &header) == 0 && header.container_bytes == (size_t)size);
    memcpy(copy, out, (size_t)size); copy[3] = 'X';
    assert(tm_tp_validate_header(copy, (size_t)size, &header) == TM_ERR_HEADER);
    memcpy(copy, out, (size_t)size); copy[TMS_TTPK_VERSION_OFFSET] = 2;
    assert(tm_tp_validate_header(copy, (size_t)size, &header) == TM_ERR_HEADER);
    memcpy(copy, out, (size_t)size); copy[TMS_TTPK_FLAGS_OFFSET] = 1;
    assert(tm_tp_validate_header(copy, (size_t)size, &header) == TM_ERR_HEADER);
    memcpy(copy, out, (size_t)size); copy[TMS_TTPK_RESERVED_OFFSET + 1] = 1;
    assert(tm_tp_validate_header(copy, (size_t)size, &header) == TM_ERR_HEADER);
    assert(tm_tp_validate_header(out, TMS_TTPK_HEADER_BYTES - 1, &header) == TM_ERR_LENGTH);
    assert(tm_tp_validate_header(out, (size_t)size - 1, &header) == TM_ERR_LENGTH);
    assert(!tms_ttpk_size_consistent((uint64_t)size - 1, TMS_TTPK_EMPTY_METADATA_BYTES, 0));
    memcpy(copy, out, (size_t)size); copy[TMS_TTPK_METADATA_CRC_OFFSET] ^= 1;
    assert(tm_tp_validate_header(copy, (size_t)size, &header) == TM_ERR_CHECKSUM);
    /* Limits are checked from the header fields before anything else is read. */
    struct { uint32_t metadata, tensors; uint64_t payload; } limits[] = {
        {TMS_TTPK_MAX_METADATA_BYTES + 1, 0, 0}, {0, TMS_TTPK_MAX_TENSORS + 1, 0}, {0, 0, TMS_TTPK_MAX_PAYLOAD_BYTES + 1},
    };
    for (size_t i = 0; i < 3; ++i) {
        memset(copy, 0, TMS_TTPK_HEADER_BYTES);
        memcpy(copy, out, 8);
        put32(copy + TMS_TTPK_METADATA_LENGTH_OFFSET, limits[i].metadata);
        put32(copy + TMS_TTPK_TENSOR_COUNT_OFFSET, limits[i].tensors);
        put64(copy + TMS_TTPK_PAYLOAD_LENGTH_OFFSET, limits[i].payload);
        assert(!tms_ttpk_header_limits_valid(limits[i].metadata, limits[i].tensors, limits[i].payload));
        assert(tm_tp_validate_header(copy, TMS_TTPK_HEADER_BYTES, &header) == TM_ERR_LIMIT);
    }
    assert(tms_ttpk_header_limits_valid(TMS_TTPK_MAX_METADATA_BYTES, TMS_TTPK_MAX_TENSORS, TMS_TTPK_MAX_PAYLOAD_BYTES));
    static uint8_t framed[256];
    size_t n = frame(framed, "\xef\xbb\xbf{}", NULL, 0, 0);
    assert(tm_tp_validate_header(framed, n, &header) == TMS_TTPK_ERR_TEXT);
    n = frame(framed, "\xff", NULL, 0, 0);
    assert(tm_tp_validate_header(framed, n, &header) == TMS_TTPK_ERR_TEXT);
    n = frame(framed, "{\"order\":\"C\",\"tensors\":[]}", NULL, 0, 0);
    assert(tm_tp_validate_header(framed, n, &header) == 0 && n == TMS_TTPK_EMPTY_PACK_BYTES);
}

static void chain_and_schema_rules(void) {
    static uint8_t framed[4096];
    uint8_t first[32], second[32], payload[80];
    int32_t a[3] = {1, 0, -1}, b[3] = {0, 1, 1}, restored[8];
    assert(tm_pack(TMS_CODEC_DENSE5, a, 3, first, sizeof first) == 25);
    assert(tm_pack(TMS_CODEC_DENSE5, b, 3, second, sizeof second) == 25);
    memcpy(payload, first, 25); memcpy(payload + 25, second, 25); payload[50] = 0;
    TMTPJSONWorkspace w = workspace(); TMTPValidation r;
    struct { const char *json; uint64_t off0, len0, off1, len1; size_t payload; int32_t expected; } chains[] = {
        {"{\"order\":\"C\",\"tensors\":[{\"axes\":[],\"codec\":\"dense5\",\"length\":25,\"name\":\"a\",\"offset\":0,\"scale_axis\":null,\"scales\":[1.0],\"shape\":[3]},"
         "{\"axes\":[],\"codec\":\"dense5\",\"length\":25,\"name\":\"b\",\"offset\":25,\"scale_axis\":null,\"scales\":[1.0],\"shape\":[3]}]}", 0, 25, 25, 25, 50, 0},
        {"{\"order\":\"C\",\"tensors\":[{\"axes\":[],\"codec\":\"dense5\",\"length\":25,\"name\":\"a\",\"offset\":0,\"scale_axis\":null,\"scales\":[1.0],\"shape\":[3]},"
         "{\"axes\":[],\"codec\":\"dense5\",\"length\":25,\"name\":\"b\",\"offset\":26,\"scale_axis\":null,\"scales\":[1.0],\"shape\":[3]}]}", 0, 25, 26, 25, 51, TMS_TTPK_ERR_OFFSET},
        {"{\"order\":\"C\",\"tensors\":[{\"axes\":[],\"codec\":\"dense5\",\"length\":25,\"name\":\"a\",\"offset\":0,\"scale_axis\":null,\"scales\":[1.0],\"shape\":[3]},"
         "{\"axes\":[],\"codec\":\"dense5\",\"length\":25,\"name\":\"b\",\"offset\":0,\"scale_axis\":null,\"scales\":[1.0],\"shape\":[3]}]}", 0, 25, 0, 25, 50, TMS_TTPK_ERR_OFFSET},
        {"{\"order\":\"C\",\"tensors\":[{\"axes\":[],\"codec\":\"dense5\",\"length\":25,\"name\":\"a\",\"offset\":0,\"scale_axis\":null,\"scales\":[1.0],\"shape\":[3]},"
         "{\"axes\":[],\"codec\":\"dense5\",\"length\":25,\"name\":\"b\",\"offset\":25,\"scale_axis\":null,\"scales\":[1.0],\"shape\":[3]}]}", 0, 25, 25, 25, 51, TMS_TTPK_ERR_OFFSET},
    };
    for (size_t i = 0; i < sizeof chains / sizeof chains[0]; ++i) {
        uint64_t offsets[2] = {chains[i].off0, chains[i].off1}, lengths[2] = {chains[i].len0, chains[i].len1};
        size_t n = frame(framed, chains[i].json, payload, chains[i].payload, 2);
        int32_t status = tm_tp_decode_json(framed, n, &w, restored, 8, TMS_TTPK_MAX_TOTAL_TRITS, &r);
        assert(status == chains[i].expected);
        assert((status == 0) == tms_ttpk_chain_valid(offsets, lengths, 2, chains[i].payload));
    }
    /* Schema and JSON rules on a single-tensor pack. */
    struct { const char *json; int32_t expected; } cases[] = {
        {"{\"order\":\"F\",\"tensors\":[{\"axes\":[],\"codec\":\"dense5\",\"length\":25,\"name\":\"a\",\"offset\":0,\"scale_axis\":null,\"scales\":[1.0],\"shape\":[3]}]}", TMS_TTPK_ERR_SCHEMA},
        {"{\"order\":\"C\",\"tensors\":[{\"axes\":[],\"codec\":\"dense5\",\"length\":25,\"name\":\"a\",\"offset\":0,\"scale_axis\":null,\"scales\":[1.0],\"shape\":[3]}],\"x\":1}", TMS_TTPK_ERR_SCHEMA},
        {"{\"order\":\"C\",\"tensors\":[{\"codec\":\"dense5\",\"length\":25,\"name\":\"a\",\"offset\":0,\"scale_axis\":null,\"scales\":[1.0],\"shape\":[3]}]}", TMS_TTPK_ERR_SCHEMA},
        {"{\"order\":\"C\",\"tensors\":[{\"axes\":[],\"codec\":\"dense5\",\"length\":25,\"name\":\"a\",\"offset\":0,\"scale_axis\":null,\"scales\":[1.0],\"shape\":[3],\"x\":1}]}", TMS_TTPK_ERR_SCHEMA},
        {"{\"order\":\"C\",\"tensors\":[{\"axes\":[],\"codec\":\"dense5\",\"length\":25,\"name\":\"a\",\"offset\":0,\"scale_axis\":null,\"scales\":[1],\"shape\":[3]}]}", TMS_TTPK_ERR_SCALE},
        {"{\"order\":\"C\",\"tensors\":[{\"axes\":[],\"codec\":\"dense5\",\"length\":25,\"name\":\"a\",\"offset\":0,\"scale_axis\":16,\"scales\":[1.0],\"shape\":[3]}]}", TMS_TTPK_ERR_SCALE},
        {"{\"order\":\"C\",\"tensors\":[{\"axes\":[],\"codec\":\"dense5\",\"length\":25,\"name\":\"a\",\"offset\":0,\"scale_axis\":null,\"scales\":[NaN],\"shape\":[3]}]}", TMS_TTPK_ERR_JSON},
        {"{\"order\":\"C\",\"tensors\":[{\"axes\":[],\"codec\":\"dense5\",\"length\":25,\"name\":\"a\",\"offset\":000000000000000000000,\"scale_axis\":null,\"scales\":[1.0],\"shape\":[3]}]}", TMS_TTPK_ERR_JSON},
        {"{\"order\":\"C\",\"tensors\":[[[[[[[[[]]]]]]]]]}", TMS_TTPK_ERR_JSON},
        {"{\"order\":\"C\",\"tensors\":[{\"axes\":[],\"codec\":\"unknown\",\"length\":25,\"name\":\"a\",\"offset\":0,\"scale_axis\":null,\"scales\":[1.0],\"shape\":[3]}]}", TM_ERR_CODEC},
        {"{\"order\":\"C\",\"tensors\":[{\"axes\":[],\"codec\":\"dense5\",\"length\":25,\"name\":\"a\",\"offset\":0,\"scale_axis\":null,\"scales\":[1.0],\"shape\":[3]}]}", 0},
    };
    for (size_t i = 0; i < sizeof cases / sizeof cases[0]; ++i) {
        size_t n = frame(framed, cases[i].json, first, 25, 1);
        assert(tm_tp_decode_json(framed, n, &w, restored, 8, TMS_TTPK_MAX_TOTAL_TRITS, &r) == cases[i].expected);
    }
    assert(!tms_ttpk_scale_axis_valid(1, TMS_TTPK_MAX_SCALE_AXIS + 1));
}

static void float_presentation(void) {
    struct { double value; int32_t exponent; const char *text; } cases[] = {
        {0.0001, -4, "0.0001"}, {0.00001, -5, "1e-05"}, {1e15, 15, "1000000000000000.0"}, {1e16, 16, "1e+16"},
        {123456789.0, 8, "123456789.0"}, {1.0000000000000002, 0, "1.0000000000000002"}, {0.5, -1, "0.5"}, {1e20, 20, "1e+20"},
    };
    for (size_t i = 0; i < sizeof cases / sizeof cases[0]; ++i) {
        uint8_t text[64];
        int64_t n = tm_tpj_format_f64(cases[i].value, text, sizeof text);
        assert(n == (int64_t)strlen(cases[i].text) && memcmp(text, cases[i].text, (size_t)n) == 0);
        const char *e = strchr(cases[i].text, 'e');
        assert((e == NULL) == tms_ttpk_float_uses_fixed_notation(cases[i].exponent));
        if (e) assert(strlen(e + 2) >= TMS_TTPK_FLOAT_EXPONENT_MIN_DIGITS && (e[1] == '+' || e[1] == '-'));
    }
}

int main(void) {
    constants();
    empty_and_single_tensor_packs();
    descriptor_rules();
    header_rules();
    chain_and_schema_rules();
    float_presentation();
    printf("PASS spec/memory/tensorpack differential harness: status codes, empty and single-tensor packs, "
           "descriptor rules, header rules and limits, offset chain, schema and JSON rules, float presentation\n");
    return 0;
}
