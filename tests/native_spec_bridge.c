/* Differential harness: specs/memory/bridge.t27 against the executable Bridge
 * (t27/bridge.t27, t27/client.t27). Requests pass through tm_bridge_request in
 * process; HTTP framing is exercised by the TCP replay in tests/test_spec_bridge.py.
 * Generate the spec headers as specs/types.h and specs/bridge.h beside the
 * implementation headers (-I build/t27); link native/float.cpp and native/platform.c.
 * No production algorithm is maintained here. */
#include <assert.h>
#include <inttypes.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#if defined(__clang__)
#pragma clang diagnostic ignored "-Wparentheses-equality"
#endif
double tm_json_strtod(uint8_t *);
size_t tm_float_shortest(double, uint8_t *, size_t);
int32_t tm_os_random(uint8_t *, size_t);
int32_t tm_os_sha256(uint8_t *, size_t, uint8_t *);
uint64_t tm_os_monotonic_ns(void);
int32_t tm_os_serial_open(uint8_t *, size_t, uint32_t);
int64_t tm_os_serial_read(int32_t, uint8_t *, size_t, uint32_t);
int64_t tm_os_serial_write(int32_t, uint8_t *, size_t, uint32_t);
int32_t tm_os_serial_drain(int32_t);
int32_t tm_os_serial_close(int32_t);
#include "specs/types.h"
#include "specs/bridge.h"
#include "codecs.h"
#include "container.h"
#include "tensorpack.h"
#include "json.h"
#include "json_writer.h"
#include "tensorpack_json.h"
#include "fpga_link.h"
#include "bridge.h"
#include "client.h"

typedef struct { TMBridgeState state; TMTPJSONWorkspace tensor; } Harness;

static void harness_free(Harness *h) {
    free(h->state.slots); free(h->state.pool); free(h->state.temporary);
    free(h->state.values); free(h->state.json_tokens); free(h->state.json_arena);
    free(h->tensor.tokens); free(h->tensor.arena); free(h->tensor.items);
    free(h->tensor.shapes); free(h->tensor.scales); free(h->tensor.axes); free(h);
}

static Harness *harness_new(size_t request, size_t object, size_t storage, size_t objects, size_t trits) {
    Harness *h = calloc(1, sizeof(*h));
    assert(h);
    TMBridgeState *s = &h->state;
    s->slot_capacity = objects; s->pool_capacity = object * objects; s->object_capacity = object;
    s->max_request_bytes = request; s->max_storage_bytes = storage; s->max_trits = trits;
    s->temporary_capacity = object; s->value_capacity = trits;
    s->token_capacity = request / 2 + 8; s->arena_capacity = request + 1;
    s->slots = calloc(objects, sizeof(*s->slots)); s->pool = malloc(s->pool_capacity);
    s->temporary = malloc(object); s->values = calloc(trits, sizeof(*s->values));
    s->json_tokens = calloc(s->token_capacity, sizeof(*s->json_tokens)); s->json_arena = malloc(s->arena_capacity);
    h->tensor.token_capacity = object / 2 + 8; h->tensor.arena_capacity = object + 1;
    h->tensor.item_capacity = 1024; h->tensor.shape_capacity = 16384;
    h->tensor.scale_capacity = object / 2 + 1; h->tensor.axis_capacity = 16384;
    h->tensor.tokens = calloc(h->tensor.token_capacity, sizeof(*h->tensor.tokens));
    h->tensor.arena = malloc(h->tensor.arena_capacity);
    h->tensor.items = calloc(h->tensor.item_capacity, sizeof(*h->tensor.items));
    h->tensor.shapes = calloc(h->tensor.shape_capacity, sizeof(*h->tensor.shapes));
    h->tensor.scales = calloc(h->tensor.scale_capacity, sizeof(*h->tensor.scales));
    h->tensor.axes = calloc(h->tensor.axis_capacity, sizeof(*h->tensor.axes));
    s->tensor_work = &h->tensor;
    assert(s->slots && s->pool && s->temporary && s->values && s->json_tokens && s->json_arena);
    assert(h->tensor.tokens && h->tensor.arena && h->tensor.items && h->tensor.shapes && h->tensor.scales && h->tensor.axes);
    assert(tm_bridge_init(s) == 0);
    return h;
}

static uint8_t response[131072];
static int32_t status;

/* Send one request body; return the NUL-terminated response text. */
static const char *call(Harness *h, const char *request, size_t length) {
    int64_t written = tm_bridge_request(&h->state, (uint8_t *)request, length, response, sizeof response - 1, &status);
    assert(written > 0);
    response[written] = 0;
    return (const char *)response;
}

static const char *call_text(Harness *h, const char *request) { return call(h, request, strlen(request)); }

static int has_code(const char *text, int32_t code) {
    char needle[32];
    snprintf(needle, sizeof needle, "\"code\":%" PRId32, code);
    return strstr(text, "\"error\"") != NULL && strstr(text, needle) != NULL;
}

static void expect_error(Harness *h, const char *request, int32_t expected_status, int32_t code) {
    const char *text = call_text(h, request);
    assert(status == expected_status);
    assert(has_code(text, code));
}

static void constants_and_base64(void) {
    assert(TM_RPC_ERR_TRANSPORT == TMS_RPC_ERR_TRANSPORT);
    assert(TM_RPC_ERR_LIMIT == TMS_RPC_ERR_LIMIT);
    assert(TM_RPC_ERR_PARAMS == TMS_RPC_ERR_INVALID_PARAMS);
    uint8_t input[300], encoded[512], decoded[300];
    for (size_t i = 0; i < sizeof input; ++i) input[i] = (uint8_t)(i * 37 + 11);
    for (size_t size = 0; size <= sizeof input; ++size) {
        TMJsonWriter writer; tm_json_writer_init(encoded, sizeof encoded, &writer);
        tm_bridge_b64_encode(&writer, input, size);
        assert(writer.error == 0 && writer.used == tms_bridge_b64_encoded_size(size));
        assert(tm_bridge_b64_decode(encoded, writer.used, decoded, sizeof decoded) == (int64_t)size);
    }
    assert(tm_bridge_b64_decode((uint8_t *)"!!!!", 4, decoded, sizeof decoded) == TMS_BRIDGE_B64_INVALID);
    assert(tm_bridge_b64_decode((uint8_t *)"AB==", 4, decoded, sizeof decoded) == TMS_BRIDGE_B64_NONCANONICAL);
    assert(tm_bridge_b64_decode((uint8_t *)"AAAA", 4, decoded, 2) == TMS_BRIDGE_B64_SHORT_OUTPUT);
}

static void capabilities_and_identity(void) {
    Harness *h = harness_new(TMS_BRIDGE_DEFAULT_MAX_REQUEST_BYTES, TMS_BRIDGE_DEFAULT_MAX_OBJECT_BYTES,
                             TMS_BRIDGE_DEFAULT_MAX_STORAGE_BYTES, TMS_BRIDGE_DEFAULT_MAX_OBJECTS,
                             TMS_BRIDGE_DEFAULT_MAX_TRITS);
    const char *text = call_text(h, "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"trinity.capabilities\"}");
    assert(status == TMS_HTTP_OK && strstr(text, "\"result\":{"));
    static const char *expected[] = {
        "\"protocol\":\"trinity-memory-bridge\"", "\"version\":1", "\"backend\":\"emulator\"", "\"hardware\":false",
        "\"persistence\":\"process-memory\"", "\"authentication\":\"none-loopback-only\"",
        "\"formats\":[\"TMEM/1\",\"TensorPack/1\"]",
        "\"codecs\":[\"baseline2\",\"dense5\",\"dense17\",\"dense22\",\"sparse41\",\"sparse82\"]",
        "\"methods\":[\"trinity.capabilities\",\"memory.upload\",\"memory.read\",\"memory.info\",\"memory.delete\",\"compute.dot\",\"chip_info\",\"trinity_chipInfo\"]",
        "\"max_request_bytes\":2097152", "\"max_object_bytes\":524288", "\"max_storage_bytes\":8388608",
        "\"max_objects\":16", "\"max_trits\":1000000",
    };
    for (size_t i = 0; i < sizeof expected / sizeof expected[0]; ++i) assert(strstr(text, expected[i]));
    text = call_text(h, "{\"jsonrpc\":\"2.0\",\"id\":\"abc\",\"method\":\"trinity_chipInfo\",\"params\":{}}");
    char anchor[32];
    snprintf(anchor, sizeof anchor, "\"anchor\":%" PRIu32, (uint32_t)TMS_BRIDGE_IDENTITY_ANCHOR);
    assert(status == TMS_HTTP_OK && strstr(text, "\"id\":\"abc\"") && strstr(text, anchor));
    assert(strstr(text, "\"hardware\":false") && strstr(text, "\"identity_kind\":\"synthetic-public-16-byte\""));
    const char *phi = strstr(text, "\"phi_id\":\"");
    assert(phi && phi[10 + 2 * TMS_BRIDGE_IDENTITY_BYTES] == '"');
    for (unsigned i = 0; i < 2 * TMS_BRIDGE_IDENTITY_BYTES; ++i) assert(tms_bridge_handle_char_valid((uint32_t)(uint8_t)phi[10 + i]));
    text = call_text(h, "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"chip_info\"}");
    assert(status == TMS_HTTP_OK && strstr(text, "\"anchor\":\"0x47C0\"") && strstr(text, "\"phi\":\""));
    harness_free(h);
}

static void envelope_ids_and_errors(void) {
    Harness *h = harness_new(65536, 4096, 8192, 2, 4096);
    static char oversized[65537 + 1];
    memset(oversized, '{', 65537); oversized[65537] = 0;
    const char *text = call(h, oversized, 65537);
    assert(status == tms_bridge_request_status(65537, 65536, false) && status == TMS_HTTP_PAYLOAD_TOO_LARGE);
    assert(has_code(text, tms_bridge_request_error(65537, 65536, false)) && strstr(text, "\"id\":null"));
    text = call_text(h, "{broken");
    assert(status == tms_bridge_request_status(7, 65536, false) && has_code(text, TMS_RPC_ERR_PARSE));
    expect_error(h, "{\"jsonrpc\":\"2.0\",\"id\":1,\"id\":2,\"method\":\"chip_info\"}", TMS_HTTP_BAD_REQUEST, TMS_RPC_ERR_PARSE);
    expect_error(h, "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"x\",\"params\":{\"n\":NaN}}", TMS_HTTP_BAD_REQUEST, TMS_RPC_ERR_PARSE);
    text = call_text(h, "[]");
    assert(status == TMS_HTTP_OK && has_code(text, TMS_RPC_ERR_INVALID_REQUEST) && strstr(text, "\"id\":null"));
    expect_error(h, "{}", TMS_HTTP_OK, TMS_RPC_ERR_INVALID_REQUEST);
    expect_error(h, "{\"jsonrpc\":\"2.0\",\"method\":\"trinity.capabilities\"}", TMS_HTTP_OK, TMS_RPC_ERR_INVALID_REQUEST);
    expect_error(h, "{\"jsonrpc\":\"2.0\",\"id\":true,\"method\":\"trinity.capabilities\"}", TMS_HTTP_OK, TMS_RPC_ERR_INVALID_REQUEST);
    expect_error(h, "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"trinity.capabilities\",\"extra\":2}", TMS_HTTP_OK, TMS_RPC_ERR_INVALID_REQUEST);
    expect_error(h, "{\"jsonrpc\":\"1.0\",\"id\":1,\"method\":\"trinity.capabilities\"}", TMS_HTTP_OK, TMS_RPC_ERR_INVALID_REQUEST);
    expect_error(h, "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":[]}", TMS_HTTP_OK, TMS_RPC_ERR_INVALID_REQUEST);
    /* Integer ids: the boundary is the spec's double-precision integer range. */
    char request[512];
    int64_t ids[] = {TMS_RPC_ID_MAX, TMS_RPC_ID_MAX + 1, TMS_RPC_ID_MIN, TMS_RPC_ID_MIN - 1, 0, -1};
    for (size_t i = 0; i < sizeof ids / sizeof ids[0]; ++i) {
        snprintf(request, sizeof request, "{\"jsonrpc\":\"2.0\",\"id\":%" PRId64 ",\"method\":\"trinity.capabilities\"}", ids[i]);
        text = call_text(h, request);
        assert(status == TMS_HTTP_OK);
        if (tms_rpc_id_integer_valid(ids[i])) {
            char echoed[64];
            snprintf(echoed, sizeof echoed, "\"id\":%" PRId64 ",\"result\"", ids[i]);
            assert(strstr(text, echoed));
        } else {
            assert(has_code(text, TMS_RPC_ERR_INVALID_REQUEST) && strstr(text, "\"id\":null"));
        }
    }
    /* String ids: at most 128 code points. */
    for (unsigned length = 128; length <= 129; ++length) {
        char id[130];
        memset(id, 'x', length); id[length] = 0;
        snprintf(request, sizeof request, "{\"jsonrpc\":\"2.0\",\"id\":\"%s\",\"method\":\"trinity.capabilities\"}", id);
        text = call_text(h, request);
        assert(status == TMS_HTTP_OK);
        assert((strstr(text, "\"result\"") != NULL) == tms_rpc_id_string_valid(length));
    }
    expect_error(h, "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"trinity_proveInference\"}", TMS_HTTP_OK, TMS_RPC_ERR_METHOD_NOT_FOUND);
    expect_error(h, "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"trinity.capabilities\",\"params\":[]}", TMS_HTTP_OK, TMS_RPC_ERR_INVALID_PARAMS);
    expect_error(h, "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"trinity.capabilities\",\"params\":{\"extra\":1}}", TMS_HTTP_OK, TMS_RPC_ERR_INVALID_PARAMS);
    expect_error(h, "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"memory.read\",\"params\":{\"handle\":\"../../etc/passwd\"}}", TMS_HTTP_OK, TMS_RPC_ERR_INVALID_PARAMS);
    expect_error(h, "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"memory.read\",\"params\":{\"handle\":\"0123456789ABCDEF0123456789ABCDEF\"}}", TMS_HTTP_OK, TMS_RPC_ERR_INVALID_PARAMS);
    expect_error(h, "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"memory.read\",\"params\":{\"handle\":\"0123456789abcdef0123456789abcdef\"}}", TMS_HTTP_OK, TMS_RPC_ERR_NOT_FOUND);
    expect_error(h, "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"memory.read\",\"params\":{}}", TMS_HTTP_OK, TMS_RPC_ERR_INVALID_PARAMS);
    expect_error(h, "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"memory.upload\"}", TMS_HTTP_OK, TMS_RPC_ERR_INVALID_PARAMS);
    expect_error(h, "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"memory.upload\",\"params\":{\"data\":\"!\"}}", TMS_HTTP_OK, TMS_RPC_ERR_INVALID_PARAMS);
    expect_error(h, "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"memory.upload\",\"params\":{\"data\":\"AB==\"}}", TMS_HTTP_OK, TMS_RPC_ERR_INVALID_PARAMS);
    expect_error(h, "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"memory.upload\",\"params\":{\"data\":\"AAAA\"}}", TMS_HTTP_OK, TMS_RPC_ERR_INVALID_PARAMS);
    expect_error(h, "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"memory.upload\",\"params\":{\"data\":true}}", TMS_HTTP_OK, TMS_RPC_ERR_INVALID_PARAMS);
    assert(h->state.object_count == 0 && h->state.stored_bytes == 0);
    harness_free(h);
}

static size_t upload_request(char *out, size_t capacity, const uint8_t *container, size_t size, int id) {
    TMJsonWriter writer; tm_json_writer_init((uint8_t *)out, capacity, &writer);
    char prefix[96];
    snprintf(prefix, sizeof prefix, "{\"jsonrpc\":\"2.0\",\"id\":%d,\"method\":\"memory.upload\",\"params\":{\"data\":\"", id);
    tm_bridge_text(&writer, prefix);
    tm_bridge_b64_encode(&writer, (uint8_t *)container, size);
    tm_bridge_text(&writer, "\"}}");
    assert(writer.error == 0);
    return writer.used;
}

static void lifecycle_reads_dot_and_limits(void) {
    Harness *h = harness_new(65536, 4096, 8192, 2, 4096);
    int32_t values[3] = {1, -1, 0};
    uint8_t container[64];
    int64_t bytes = tm_pack(TMS_CODEC_DENSE5, values, 3, container, sizeof container);
    assert(bytes == 25);
    static char request[16384];
    size_t length = upload_request(request, sizeof request, container, (size_t)bytes, 1);
    const char *text = call(h, request, length);
    assert(status == TMS_HTTP_OK && strstr(text, "\"bytes\":25") && strstr(text, "\"backend\":\"emulator\""));
    const char *start = strstr(text, "\"handle\":\"");
    assert(start);
    char handle[33];
    memcpy(handle, start + 10, 32); handle[32] = 0;
    assert(start[10 + TMS_BRIDGE_HANDLE_HEX_CHARS] == '"');
    for (unsigned i = 0; i < TMS_BRIDGE_HANDLE_HEX_CHARS; ++i) assert(tms_bridge_handle_char_valid((uint32_t)(uint8_t)handle[i]));
    assert(tms_bridge_handle_layout_valid((uint32_t)(uint8_t)handle[TMS_BRIDGE_HANDLE_VERSION_CHAR_INDEX],
                                          (uint32_t)(uint8_t)handle[TMS_BRIDGE_HANDLE_VARIANT_CHAR_INDEX]));
    assert(h->state.object_count == 1 && h->state.stored_bytes == 25);
    /* Read ranges agree with the spec's range rule. */
    struct { uint64_t offset, length; } ranges[] = {{0, 25}, {3, 5}, {25, 0}, {26, 0}, {1, 25}, {0, 26}, {24, 1}};
    for (size_t i = 0; i < sizeof ranges / sizeof ranges[0]; ++i) {
        snprintf(request, sizeof request,
                 "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"memory.read\",\"params\":{\"handle\":\"%s\",\"offset\":%" PRIu64 ",\"length\":%" PRIu64 "}}",
                 handle, ranges[i].offset, ranges[i].length);
        text = call_text(h, request);
        assert(status == TMS_HTTP_OK);
        if (tms_bridge_read_range_valid(25, ranges[i].offset, ranges[i].length)) {
            char fields[96];
            snprintf(fields, sizeof fields, "\"offset\":%" PRIu64 ",\"length\":%" PRIu64 ",\"total_bytes\":25", ranges[i].offset, ranges[i].length);
            assert(strstr(text, fields));
        } else {
            assert(has_code(text, TMS_RPC_ERR_INVALID_PARAMS));
        }
    }
    /* Dot: exact row product equals the spec reference; rejections follow the spec rules. */
    int64_t activations[3] = {-128, 127, -128};
    snprintf(request, sizeof request,
             "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"compute.dot\",\"params\":{\"handle\":\"%s\",\"tensor_name\":\"weights\",\"activations\":[-128,127,-128]}}", handle);
    text = call_text(h, request);
    char accumulator[64];
    snprintf(accumulator, sizeof accumulator, "\"accumulators\":[%" PRId64 "]", tms_bridge_dot_row(values, activations, 3));
    assert(status == TMS_HTTP_OK && strstr(text, accumulator) && strstr(text, "\"scales\":[1.0]"));
    assert(strstr(text, "\"arithmetic\":\"exact-integer\"") && strstr(text, "\"input_shape\":[3]") && strstr(text, "\"output_shape\":[1]"));
    assert(strstr(text, "\"scale_applied\":false"));
    snprintf(request, sizeof request,
             "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"compute.dot\",\"params\":{\"handle\":\"%s\",\"tensor_name\":\"other\",\"activations\":[0,0,0]}}", handle);
    expect_error(h, request, TMS_HTTP_OK, TMS_RPC_ERR_NOT_FOUND);
    snprintf(request, sizeof request,
             "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"compute.dot\",\"params\":{\"handle\":\"%s\",\"tensor_name\":\"weights\",\"activations\":[0,0]}}", handle);
    expect_error(h, request, TMS_HTTP_OK, TMS_RPC_ERR_INVALID_PARAMS);
    assert(!tms_bridge_dot_layout_valid(1, TMS_BRIDGE_DOT_NO_SCALE_AXIS, 3, 2));
    int64_t bad[] = {TMS_BRIDGE_ACTIVATION_MAX + 1, TMS_BRIDGE_ACTIVATION_MIN - 1};
    for (size_t i = 0; i < 2; ++i) {
        assert(!tms_bridge_activation_valid(bad[i]));
        snprintf(request, sizeof request,
                 "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"compute.dot\",\"params\":{\"handle\":\"%s\",\"tensor_name\":\"weights\",\"activations\":[0,0,%" PRId64 "]}}", handle, bad[i]);
        expect_error(h, request, TMS_HTTP_OK, TMS_RPC_ERR_INVALID_PARAMS);
    }
    snprintf(request, sizeof request,
             "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"compute.dot\",\"params\":{\"handle\":\"%s\",\"tensor_name\":\"weights\",\"activations\":[0,0,1.5]}}", handle);
    expect_error(h, request, TMS_HTTP_OK, TMS_RPC_ERR_INVALID_PARAMS);
    /* Storage accounting: the second slot fills, the third upload is refused, delete reclaims. */
    length = upload_request(request, sizeof request, container, (size_t)bytes, 4);
    text = call(h, request, length);
    assert(status == TMS_HTTP_OK && strstr(text, "\"result\""));
    assert(tms_bridge_storage_accepts(1, 2, 25, 25, 8192));
    assert(!tms_bridge_storage_accepts(2, 2, 50, 25, 8192));
    text = call(h, request, length);
    assert(status == TMS_HTTP_OK && has_code(text, TMS_RPC_ERR_LIMIT));
    assert(h->state.object_count == 2 && h->state.stored_bytes == 50);
    snprintf(request, sizeof request, "{\"jsonrpc\":\"2.0\",\"id\":5,\"method\":\"memory.delete\",\"params\":{\"handle\":\"%s\"}}", handle);
    text = call_text(h, request);
    assert(status == TMS_HTTP_OK && strstr(text, "\"deleted\":true"));
    assert(h->state.object_count == 1 && h->state.stored_bytes == 25);
    snprintf(request, sizeof request, "{\"jsonrpc\":\"2.0\",\"id\":6,\"method\":\"memory.read\",\"params\":{\"handle\":\"%s\"}}", handle);
    expect_error(h, request, TMS_HTTP_OK, TMS_RPC_ERR_NOT_FOUND);
    expect_error(h, request, TMS_HTTP_OK, TMS_RPC_ERR_NOT_FOUND);
    /* Trit limit: a TMEM count above max_trits is refused before decoding. */
    static int32_t many[4097];
    static uint8_t big[8192];
    bytes = tm_pack(TMS_CODEC_DENSE5, many, 4097, big, sizeof big);
    assert(bytes > 0 && tms_bridge_tmem_trit_limit_exceeded(4097, 4096));
    length = upload_request(request, sizeof request, big, (size_t)bytes, 7);
    text = call(h, request, length);
    assert(status == TMS_HTTP_OK && has_code(text, TMS_RPC_ERR_LIMIT));
    /* Object limit: an encoded upload longer than the object capacity allows is refused. */
    static int32_t wide[20480];
    bytes = tm_pack(TMS_CODEC_DENSE5, wide, 20480, big, sizeof big);
    assert(bytes == 4120 && tms_bridge_b64_encoded_size((uint64_t)bytes) > tms_bridge_b64_encoded_size(4096));
    length = upload_request(request, sizeof request, big, (size_t)bytes, 8);
    text = call(h, request, length);
    assert(status == TMS_HTTP_OK && has_code(text, TMS_RPC_ERR_LIMIT));
    assert(h->state.object_count == 1 && h->state.stored_bytes == 25);
    harness_free(h);
}

/* Section 10 against t27/fpga_link.t27 (the host link) and t27/bridge.t27 (the backend field). */
static void device_backend(void) {
    assert(TL_Z_COUNT == TMS_DEVICE_Z_COUNT && TL_MAX_ROWS == TMS_DEVICE_MAX_ROWS_PER_RUN);
    assert(TL_MAX_WPR == TMS_DEVICE_MAX_WORDS_PER_ROW && TL_WORD_BYTES == TMS_DEVICE_WORD_BYTES);
    assert(TL_ACT_BLOCK_BYTES == TMS_DEVICE_ACT_BLOCK_BYTES && TL_ACT_FRAME_BLOCKS == TMS_DEVICE_ACT_FRAME_BLOCKS);
    assert(TL_MATVEC_PAYLOAD == TMS_DEVICE_MATVEC_PAYLOAD_BYTES && TL_MAX_LEN == TMS_DEVICE_CHUNK_BYTES);
    assert(TL_CMD_ACT == TMS_WIRE_CMD_ACTIVATIONS && TL_CMD_MATVEC == TMS_WIRE_CMD_MATVEC);
    assert(TL_IX_ACT == TMS_WIRE_INDEX_ACTIVATIONS && TL_IX_MATVEC == TMS_WIRE_INDEX_MATVEC);
    assert(TL_PROTO_MATVEC == TMS_DEVICE_PROTOCOL_MATVEC);
    assert(TL_RUN_REJECTED == TMS_DEVICE_RUN_REJECTED && TL_RUN_SHORT == TMS_DEVICE_RUN_SHORT);
    assert(tl_lanes(0) == tms_device_lanes(0) && tl_lanes(1) == tms_device_lanes(1));
    static int32_t trits[3 * 6912];
    static uint8_t image[8192];
    for (size_t i = 0; i < sizeof trits / sizeof trits[0]; i++) trits[i] = (int32_t)(i % 3) - 1;
    const uint64_t shapes[][2] = {{1, 1}, {3, 5}, {2, 64}, {2, 65}, {3, 80}, {2, 81}, {1, 2560}, {3, 6912}};
    for (size_t k = 0; k < sizeof shapes / sizeof shapes[0]; k++) {
        for (uint32_t codec = 0; codec < 2; codec++) {
            uint64_t rows = shapes[k][0], cols = shapes[k][1];
            assert(tl_words_per_row(cols, codec) == tms_device_words_per_row(cols, codec));
            int64_t size = tl_image(trits, 0, (size_t)rows, (size_t)cols, codec, image, sizeof image);
            if (tms_device_image_bytes(rows, cols, codec) <= sizeof image) {
                assert(size >= 0 && (uint64_t)size == tms_device_image_bytes(rows, cols, codec));
            } else {
                assert(size == TL_ERR_LIMIT);
            }
        }
    }
    int64_t ys[] = {-884736, 884736, 0, -1, 12345};
    uint32_t c = 0;
    for (size_t i = 0; i < 5; i++) c = tms_device_checksum_step(c, (uint32_t)ys[i]);
    assert(c == tl_result_checksum(ys, 5));
    assert(tl_signed32(tms_device_y_word(0, 0) | 0xFFFFFFFFu) == -1);
    /* A zeroed state is the emulator: backend 0, transport 0, hardware false. */
    Harness *h = harness_new(65536, 4096, 8192, 2, 4096);
    assert(h->state.backend == 0 && tms_bridge_backend_transport(h->state.backend) == h->state.transport);
    assert(!tms_bridge_backend_hardware(h->state.backend));
    const char *text = call_text(h, "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"trinity.capabilities\"}");
    assert(strstr(text, "\"backend\":\"emulator\",\"hardware\":false") && !strstr(text, "\"transport\""));
    harness_free(h);
}

int main(void) {
    constants_and_base64();
    capabilities_and_identity();
    envelope_ids_and_errors();
    lifecycle_reads_dot_and_limits();
    device_backend();
    printf("PASS spec/memory/bridge differential harness: client constants, base64 sizes, "
           "capabilities, identity, envelope and id bounds, error codes, handles, read ranges, "
           "dot, storage and trit limits, device backend constants, row images and checksum\n");
    return 0;
}
