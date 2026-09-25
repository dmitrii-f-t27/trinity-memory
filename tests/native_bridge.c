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
uint64_t tm_os_monotonic_ns(void);
int32_t tm_os_serial_rate_ok(uint32_t);
int32_t tm_os_serial_open(uint8_t *, size_t, uint32_t);
int64_t tm_os_serial_read(int32_t, uint8_t *, size_t, uint32_t);
int64_t tm_os_serial_write(int32_t, uint8_t *, size_t, uint32_t);
int32_t tm_os_serial_drain(int32_t);
int32_t tm_os_serial_close(int32_t);
#include "codecs.h"
#include "container.h"
#include "tensorpack.h"
#include "json.h"
#include "json_writer.h"
#include "tensorpack_json.h"
#include "fpga_link.h"
#include "bridge.h"

typedef struct { TMBridgeState state; TMTPJSONWorkspace tensor; TMLink link; } BridgeHarness;

static void link_free(TMLink *l) {
    if (l->configured) tl_close(l);
    free(l->path); free(l->bitstream); free(l->rx); free(l->frame); free(l->capture); free(l->image);
    free(l->x); free(l->act); free(l->acc); free(l->seen); free(l->status); free(l->z);
    free(l->loaded_digest); free(l->image_digest);
}
void bridge_test_free(BridgeHarness *h) {
    if (!h) return;
    link_free(&h->link);
    free(h->state.slots); free(h->state.pool); free(h->state.temporary);
    free(h->state.values); free(h->state.json_tokens); free(h->state.json_arena);
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
/* The fpga backend on the harness: the link's buffers, as native/runtime.c's tm_runtime_fpga. */
int32_t bridge_test_fpga(BridgeHarness *h, uint8_t *path, size_t path_size, uint32_t baud, uint32_t reply_ms,
                         uint32_t quiet_ms, uint32_t attempts, uint32_t min_protocol, uint32_t region,
                         uint8_t *bitstream, uint32_t idcode, uint64_t dna, uint32_t build_id) {
    TMLink *l = &h->link;
    if (l->configured || !path || !path_size || path_size > 1023 || !attempts) return -1;
    if (min_protocol < TL_PROTO_MATVEC || !tm_os_serial_rate_ok(baud)) return -1;
    size_t trits = h->state.max_trits;
    l->image_capacity = trits / 2 + 65536; l->capture_capacity = l->image_capacity * 2 + 1048576;
    l->rx_capacity = 16384; l->frame_capacity = 8192; l->x_capacity = trits; l->act_capacity = 1024 * 80;
    l->acc_capacity = trits;
    l->path = malloc(path_size); l->bitstream = malloc(32); l->rx = malloc(l->rx_capacity);
    l->frame = malloc(l->frame_capacity); l->capture = malloc(l->capture_capacity); l->image = malloc(l->image_capacity);
    l->x = malloc(l->x_capacity); l->act = calloc(l->act_capacity, 1); l->acc = calloc(trits, sizeof(*l->acc));
    l->seen = calloc(1024, 1); l->status = calloc(37, sizeof(*l->status)); l->z = calloc(11, sizeof(*l->z));
    l->loaded_digest = calloc(32, 1); l->image_digest = calloc(32, 1);
    if (!l->path || !l->bitstream || !l->rx || !l->frame || !l->capture || !l->image || !l->x || !l->act || !l->acc ||
        !l->seen || !l->status || !l->z || !l->loaded_digest || !l->image_digest) return -1;
    memcpy(l->path, path, path_size); memcpy(l->bitstream, bitstream, 32);
    l->path_size = path_size; l->baud = baud; l->reply_ms = reply_ms; l->quiet_ms = quiet_ms;
    l->max_attempts = attempts; l->min_protocol = min_protocol; l->region = region;
    l->idcode = idcode; l->dna = dna; l->build_id = build_id; l->fd = -1; l->configured = true;
    h->state.backend = 1; h->state.transport = 1; h->state.link = l;
    return 0;
}
int64_t bridge_test_call(BridgeHarness *h, uint8_t *request, size_t length,
                         uint8_t *output, size_t capacity, int32_t *status) {
    return tm_bridge_request(&h->state, request, length, output, capacity, status);
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
int main(void) {
    unsigned encodings = test_base64(); test_protocol(); test_transactions();
    printf("PASS native Bridge: %u base64 lengths, strict padding, protocol, signed dot, UUIDv4, storage limits and atomic short-output transactions\n", encodings);
    return 0;
}
