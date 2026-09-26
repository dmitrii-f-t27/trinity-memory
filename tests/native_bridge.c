/* Independent conformance harness and caller-owned buffer allocation only.
 * All protocol, JSON, base64, metadata and arithmetic logic is generated t27.
 * OS entropy/SHA256 and numeric conversion are separately linked primitives.
 */
#include <assert.h>
#include <math.h>
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
#include "codecs.h"
#include "container.h"
#include "tensorpack.h"
#include "json.h"
#include "json_writer.h"
#include "tensorpack_json.h"
#include "bridge.h"

typedef struct { TMBridgeState state; TMTPJSONWorkspace tensor; } BridgeHarness;

void bridge_test_free(BridgeHarness *h) {
    if (!h) return;
    free(h->state.slots); free(h->state.pool); free(h->state.temporary);
    free(h->state.values); free(h->state.json_tokens); free(h->state.json_arena);
    free(h->state.ev_bitstream); free(h->state.ev_capture);
    free(h->tensor.tokens); free(h->tensor.arena); free(h->tensor.items);
    free(h->tensor.shapes); free(h->tensor.scales); free(h->tensor.axes); free(h);
}
BridgeHarness *bridge_test_new(size_t request, size_t object, size_t storage, size_t objects, size_t trits) {
    if (!objects || objects > 64 || object > 1048576 || request > 2097152 || trits > 4194304) return NULL;
    BridgeHarness *h = calloc(1, sizeof(*h));
    if (!h) return NULL;
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
    if (!s->slots || !s->pool || !s->temporary || !s->values || !s->json_tokens || !s->json_arena ||
        !h->tensor.tokens || !h->tensor.arena || !h->tensor.items || !h->tensor.shapes || !h->tensor.scales || !h->tensor.axes ||
        tm_bridge_init(s) != 0) { bridge_test_free(h); return NULL; }
    return h;
}
int64_t bridge_test_call(BridgeHarness *h, uint8_t *request, size_t length,
                         uint8_t *output, size_t capacity, int32_t *status) {
    return tm_bridge_request(&h->state, request, length, output, capacity, status);
}
/* Device backend of issue #64: backend fpga with the UART transport and the whole
 * evidence block; a zero IDCODE or a missing field is refused by tm_bridge_init. */
BridgeHarness *bridge_test_new_device(size_t request, size_t object, size_t storage, size_t objects, size_t trits,
                                      uint32_t idcode, uint64_t dna, const uint8_t *bitstream, const uint8_t *capture) {
    BridgeHarness *h = bridge_test_new(request, object, storage, objects, trits);
    if (!h) return NULL;
    TMBridgeState *s = &h->state;
    s->backend = 1; s->transport = 1; s->evidence = 15;
    s->ev_idcode = idcode; s->ev_dna = dna;
    s->ev_bitstream = malloc(32); s->ev_capture = malloc(32);
    if (!s->ev_bitstream || !s->ev_capture || tm_bridge_init(s) != 0) { bridge_test_free(h); return NULL; }
    memcpy(s->ev_bitstream, bitstream, 32);
    memcpy(s->ev_capture, capture, 32);
    return h;
}
size_t bridge_test_count(BridgeHarness *h) { return h->state.object_count; }
size_t bridge_test_bytes(BridgeHarness *h) { return h->state.stored_bytes; }

static unsigned test_base64(void) {
    uint8_t input[513], encoded[1024], output[513];
    for (size_t i = 0; i < sizeof input; i++) input[i] = (uint8_t)(i * 151 + 29);
    for (size_t size = 0; size <= sizeof input; size++) {
        TMJsonWriter writer; tm_json_writer_init(encoded, sizeof encoded, &writer);
        tm_bridge_b64_encode(&writer, input, size);
        assert(writer.error == 0 && writer.used == 4 * ((size + 2) / 3));
        assert(tm_bridge_b64_decode(encoded, writer.used, output, sizeof output) == (int64_t)size);
        assert(memcmp(input, output, size) == 0);
        if (size) assert(tm_bridge_b64_decode(encoded, writer.used, output, size - 1) == -3);
    }
    static const char *invalid[] = {"A", "A===", "AA=A", "AA==AA==", "AA =", "AAAA=", "====", "-AAA", "_AAA"};
    for (size_t i = 0; i < sizeof invalid / sizeof invalid[0]; i++)
        assert(tm_bridge_b64_decode((uint8_t *)invalid[i], strlen(invalid[i]), output, sizeof output) == -1);
    assert(tm_bridge_b64_decode((uint8_t *)"AB==", 4, output, sizeof output) == -2);
    assert(tm_bridge_b64_decode((uint8_t *)"AAB=", 4, output, sizeof output) == -2);
    return 514;
}
static void test_protocol(void) {
    BridgeHarness *h = bridge_test_new(65536, 4096, 8192, 2, 4096); assert(h);
    uint8_t response[65536]; int32_t status;
    char request[] = "{\"jsonrpc\":\"2.0\",\"id\":17,\"method\":\"trinity.capabilities\"}";
    int64_t length = bridge_test_call(h, (uint8_t *)request, strlen(request), response, sizeof response - 1, &status);
    assert(length > 0 && status == 200); response[length] = 0;
    assert(strstr((char *)response, "\"hardware\":false"));
    assert(strstr((char *)response, "\"max_objects\":2"));
    char malformed[] = "{\"jsonrpc\":\"2.0\",\"id\":1,\"id\":2,\"method\":\"chip_info\"}";
    length = bridge_test_call(h, (uint8_t *)malformed, strlen(malformed), response, sizeof response - 1, &status);
    assert(length > 0 && status == 400); response[length] = 0;
    assert(strstr((char *)response, "\"code\":-32700"));
    assert(bridge_test_count(h) == 0 && bridge_test_bytes(h) == 0);
    assert(tm_bridge_http_error(-1, "quoted \"error\"", response, sizeof response) > 0);
    assert(tm_bridge_http_error(-1, "message", response, 4) < 0);
    bridge_test_free(h);
}
static void test_transactions(void) {
    BridgeHarness *h = bridge_test_new(65536, 4096, 8192, 2, 4096); assert(h);
    int32_t values[] = {1, -1, 0}; uint8_t tmem[64], request[1024], response[65536];
    int64_t bytes = tm_pack(1, values, 3, tmem, sizeof tmem); assert(bytes == 25);
    TMJsonWriter writer; tm_json_writer_init(request, sizeof request, &writer);
    tm_bridge_text(&writer, "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"memory.upload\",\"params\":{\"data\":\"");
    tm_bridge_b64_encode(&writer, tmem, (size_t)bytes); tm_bridge_text(&writer, "\"}}");
    assert(writer.error == 0);
    int32_t status;
    assert(bridge_test_call(h, request, writer.used, response, 16, &status) < 0);
    assert(bridge_test_count(h) == 0 && bridge_test_bytes(h) == 0);
    int64_t length = bridge_test_call(h, request, writer.used, response, sizeof response - 1, &status);
    assert(length > 0 && status == 200); response[length] = 0;
    char *start = strstr((char *)response, "\"handle\":\""); assert(start);
    char handle[33]; memcpy(handle, start + 10, 32); handle[32] = 0;
    assert(handle[12] == '4' && strchr("89ab", handle[16]));
    assert(bridge_test_count(h) == 1 && bridge_test_bytes(h) == 25);
    char dot[256];
    int written = snprintf(dot, sizeof dot, "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"compute.dot\",\"params\":{\"handle\":\"%s\",\"tensor_name\":\"weights\",\"activations\":[-128,127,-128]}}", handle);
    assert(written > 0 && (size_t)written < sizeof dot);
    length = bridge_test_call(h, (uint8_t *)dot, (size_t)written, response, sizeof response - 1, &status);
    assert(length > 0 && status == 200); response[length] = 0;
    assert(strstr((char *)response, "\"accumulators\":[-255]"));
    assert(strstr((char *)response, "\"scales\":[1.0]"));
    char remove[192];
    written = snprintf(remove, sizeof remove, "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"memory.delete\",\"params\":{\"handle\":\"%s\"}}", handle);
    assert(written > 0 && (size_t)written < sizeof remove);
    assert(bridge_test_call(h, (uint8_t *)remove, (size_t)written, response, 16, &status) < 0);
    assert(bridge_test_count(h) == 1 && bridge_test_bytes(h) == 25);
    length = bridge_test_call(h, (uint8_t *)remove, (size_t)written, response, sizeof response - 1, &status);
    assert(length > 0); response[length] = 0;
    assert(strstr((char *)response, "\"deleted\":true"));
    assert(bridge_test_count(h) == 0 && bridge_test_bytes(h) == 0);
    for (int i = 0; i < 3; i++) {
        length = bridge_test_call(h, request, writer.used, response, sizeof response - 1, &status);
        assert(length > 0); response[length] = 0;
        if (i == 2) assert(strstr((char *)response, "\"code\":-32010"));
    }
    assert(bridge_test_count(h) == 2 && bridge_test_bytes(h) == 50);
    assert(tm_bridge_init(&h->state) == 0);
    assert(bridge_test_count(h) == 0 && bridge_test_bytes(h) == 0);
    bridge_test_free(h);
}
static void test_device_backend(void) {
    uint8_t bitstream[32], capture[32], response[65536]; int32_t status;
    for (size_t i = 0; i < 32; i++) { bitstream[i] = (uint8_t)(i * 8 + 1); capture[i] = (uint8_t)(255 - i * 7); }
    BridgeHarness *h = bridge_test_new_device(65536, 4096, 8192, 2, 4096, 56762515, 0x00389c0c2d85e85cULL,
                                              bitstream, capture); assert(h);
    char request[] = "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"trinity.capabilities\"}";
    int64_t length = bridge_test_call(h, (uint8_t *)request, strlen(request), response, sizeof response - 1, &status);
    assert(length > 0 && status == 200); response[length] = 0;
    assert(strstr((char *)response, "\"backend\":\"fpga\",\"hardware\":true,\"transport\":\"uart\""));
    assert(strstr((char *)response, "\"idcode\":56762515"));
    assert(strstr((char *)response, "\"dna\":\"00389c0c2d85e85c\""));
    char identity[] = "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"trinity_chipInfo\"}";
    length = bridge_test_call(h, (uint8_t *)identity, strlen(identity), response, sizeof response - 1, &status);
    assert(length > 0 && status == 200); response[length] = 0;
    assert(strstr((char *)response, "\"identity_kind\":\"device-evidence\",\"status\":\"memory device\""));
    char dot[] = "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"compute.dot\",\"params\":{\"handle\":\"00112233445566778899aabbccddeeff\",\"tensor_name\":\"weights\",\"activations\":[1,2,3]}}";
    length = bridge_test_call(h, (uint8_t *)dot, strlen(dot), response, sizeof response - 1, &status);
    assert(length > 0 && status == 200); response[length] = 0;
    assert(strstr((char *)response, "\"code\":-32000"));
    assert(strstr((char *)response, "device transport not linked"));
    bridge_test_free(h);
    /* A device server without the evidence block is refused at construction. */
    h = bridge_test_new(65536, 4096, 8192, 2, 4096); assert(h);
    h->state.backend = 1; h->state.transport = 1; h->state.evidence = 15;
    assert(tm_bridge_init(&h->state) != 0);
    h->state.ev_idcode = 56762515;
    assert(tm_bridge_init(&h->state) == 0);
    h->state.transport = 0;
    assert(tm_bridge_init(&h->state) != 0);
    bridge_test_free(h);
}
int main(void) {
    unsigned encodings = test_base64(); test_protocol(); test_transactions(); test_device_backend();
    printf("PASS native Bridge: %u base64 lengths, strict padding, protocol, signed dot, UUIDv4, storage limits, atomic short-output transactions and the device backend seam\n", encodings);
    return 0;
}
