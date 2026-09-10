/* Independent client envelope/serialization tests. Sanitizer-safe C ABI caller. */
#include <assert.h>
#include <float.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

double tm_json_strtod(uint8_t *text);
size_t tm_float_shortest(double, uint8_t *, size_t);
int32_t tm_os_random(uint8_t *, size_t);
#include "json.h"
#include "json_writer.h"
#include "client.h"

/* Deterministic platform seam tests UUID conversion and entropy failure.
 * Production links native/platform.c instead of this fixture. */
static int random_failure;
int32_t tm_os_random(uint8_t *out, size_t size) {
    assert(size == 16);
    if (random_failure) return -1;
    for (size_t i = 0; i < size; ++i) out[i] = (uint8_t)(i * 17);
    return 0;
}
static uint8_t id[33] = "00112233445546778899aabbccddeeff";
static TMJsonToken tokens[4096];
static uint8_t arena[32768], out[32768];
static TMRPCWorkspace work = {tokens, 4096, arena, sizeof arena, -1, 0};
static uint8_t *u(const char *s) { return (uint8_t *)(uintptr_t)s; }
static void reset(void) {
    work.token_capacity = 4096; work.arena_capacity = sizeof arena;
    work.root = -1; work.token_count = 0;
}
static int validate(const char *body, int wanted) {
    TMRPCReply reply = {77, 88, false};
    int status = tm_rpc_validate(u(body), strlen(body), id, &work, &reply);
    if (status != wanted) fprintf(stderr, "expected %d, got %d: %s\n", wanted, status, body);
    assert(status == wanted);
    if (status < 0) assert(reply.result_index == 77 && reply.error_index == 88 && !reply.remote_error);
    if (status == 0) assert(reply.result_index >= 0 && reply.error_index == -1 && !reply.remote_error);
    if (status == 1) assert(reply.result_index == -1 && reply.error_index >= 0 && reply.remote_error);
    return status == 0 ? (int)reply.result_index : (int)reply.error_index;
}
static void test_request(void) {
    uint8_t generated[34]; memset(generated, 0xa5, sizeof generated);
    assert(tm_rpc_random_id(generated) == 0);
    assert(memcmp(generated, "00112233445546778899aabbccddeeff", 32) == 0);
    assert(generated[32] == 0xa5 && generated[33] == 0xa5);
    random_failure = 1; memset(generated, 0xa5, sizeof generated);
    assert(tm_rpc_random_id(generated) == -32000);
    for (size_t i = 0; i < sizeof generated; ++i) assert(generated[i] == 0xa5);
    random_failure = 0;
    const char *expected = "{\"jsonrpc\":\"2.0\",\"id\":\"00112233445546778899aabbccddeeff\",\"method\":\"memory.info\",\"params\":{}}";
    int64_t n = tm_rpc_prepare(u("memory.info"), 11, NULL, 0, id, out, sizeof out, &work);
    assert(n == (int64_t)strlen(expected)); assert(memcmp(out, expected, (size_t)n) == 0);
    assert(work.root == -1 && work.token_count == 0);
    uint8_t method[] = {'a','"','\\',0, '\n', 0xf0,0x9f,0x8c,0x90};
    const char *params = "[true,false,null,18446744073709551615,-9223372036854775808,1.0,-0.0,5e-324,{\"x\":\"\\u0000\\ud83c\\udf10\"}]";
    n = tm_rpc_prepare(method, sizeof method, u(params), strlen(params), id, out, sizeof out, &work);
    assert(n > 0 && work.tokens[work.root].kind == TM_JSON_ARRAY);
    TMJsonToken parsed[100]; uint8_t decoded[1024]; TMJsonResult result;
    assert(tm_json_parse(out, (size_t)n, parsed, 100, decoded, sizeof decoded, 64, &result) == 0);
    int64_t m = tm_json_field(parsed, result.token_count, result.root, u("method"), 6);
    assert(m >= 0 && parsed[m].text_size == sizeof method && !memcmp(parsed[m].text, method, sizeof method));
    int64_t p = tm_json_field(parsed, result.token_count, result.root, u("params"), 6);
    assert(p >= 0 && parsed[p].kind == TM_JSON_ARRAY);
    int64_t ch = parsed[p].first;
    for (int i = 0; i < 5; ++i) ch = parsed[ch].next;
    assert(parsed[ch].kind == TM_JSON_REAL && parsed[ch].real == 1.0);
    ch = parsed[ch].next;
    assert(parsed[ch].kind == TM_JSON_REAL && parsed[ch].real == 0.0 && signbit(parsed[ch].real));
    ch = parsed[ch].next;
    assert(parsed[ch].kind == TM_JSON_REAL && parsed[ch].real == DBL_TRUE_MIN);
    const char *valid[] = {"null", "false", "1", "1.0", "\"a\"", "[]", "{}"};
    for (size_t i = 0; i < sizeof valid / sizeof valid[0]; ++i)
        assert(tm_rpc_prepare(u(""), 0, u(valid[i]), strlen(valid[i]), id, out, sizeof out, &work) > 0);
    const char *invalid[] = {" ", "01", "NaN", "1e9999", "{\"a\":1,\"\\u0061\":2}", "[1,]", "true false", "18446744073709551616"};
    for (size_t i = 0; i < sizeof invalid / sizeof invalid[0]; ++i)
        assert(tm_rpc_prepare(u("x"), 1, u(invalid[i]), strlen(invalid[i]), id, out, sizeof out, &work) == -32602);
    uint8_t bad_utf8[] = {0xed,0xa0,0x80};
    assert(tm_rpc_prepare(bad_utf8, sizeof bad_utf8, NULL, 0, id, out, sizeof out, &work) == -32602);
    uint8_t bad_id[32]; memcpy(bad_id, id, 32); bad_id[0] = 'A';
    assert(tm_rpc_prepare(u("x"), 1, NULL, 0, bad_id, out, sizeof out, &work) == -32602);
    n = tm_rpc_prepare(u("x"), 1, NULL, 0, id, out, sizeof out, &work);
    for (size_t cap = 0; cap <= (size_t)n; ++cap) {
        memset(out, 0xa5, sizeof out);
        int64_t r = tm_rpc_prepare(u("x"), 1, NULL, 0, id, out, cap, &work);
        assert(r == (cap < (size_t)n ? -32010 : n));
        assert(out[cap] == 0xa5);
    }
    work.token_capacity = 0;
    assert(tm_rpc_prepare(u("x"), 1, u("null"), 4, id, out, sizeof out, &work) == -32010);
    reset(); work.arena_capacity = 0;
    assert(tm_rpc_prepare(u("x"), 1, u("\"a\""), 3, id, out, sizeof out, &work) == -32010); reset();
}
static void test_response(void) {
    int result = validate("{\"jsonrpc\":\"2.0\",\"id\":\"00112233445546778899aabbccddeeff\",\"result\":null}", 0);
    assert(tokens[result].kind == TM_JSON_NULL);
    result = validate("{\"jsonrpc\":\"2.0\",\"id\":\"00112233445546778899aabbccddeeff\",\"result\":{\"a\":[true,2]},\"extra\":0}", 0);
    assert(tokens[result].kind == TM_JSON_OBJECT);
    validate("{\"jsonrpc\":\"2.0\",\"id\":\"00112233445546778899aabbccddeeff\",\"error\":{\"code\":0,\"message\":\"\"}}", 1);
    validate("{\"jsonrpc\":\"2.0\",\"id\":null,\"error\":{\"code\":-32602,\"message\":\"bad\",\"data\":[]}}", 1);
    int error = validate("{\"jsonrpc\":\"2.0\",\"error\":{\"code\":18446744073709551615,\"message\":\"bad\"}}", 1);
    int64_t code = tm_rpc_field(&work, error, "code", 4);
    assert(tokens[code].uinteger == UINT64_MAX && !tokens[code].integer_fits_i64);
    const char *invalid[] = {
        "{}", "[]", "null", "true", "{", "{\"jsonrpc\":2,\"result\":0}",
        "{\"jsonrpc\":\"2.0\",\"result\":0}",
        "{\"jsonrpc\":\"2.0\",\"id\":null,\"result\":0}",
        "{\"jsonrpc\":\"2.0\",\"id\":0,\"result\":0}",
        "{\"jsonrpc\":\"2.0\",\"id\":\"wrong\",\"result\":0}",
        "{\"jsonrpc\":\"2.0\",\"id\":\"00112233445546778899aabbccddeeff\"}",
        "{\"jsonrpc\":\"2.0\",\"result\":0,\"error\":{\"code\":1,\"message\":\"x\"}}",
        "{\"jsonrpc\":\"2.0\",\"error\":null}",
        "{\"jsonrpc\":\"2.0\",\"id\":false,\"error\":{\"code\":1,\"message\":\"x\"}}",
        "{\"jsonrpc\":\"2.0\",\"id\":\"wrong\",\"error\":{\"code\":1,\"message\":\"x\"}}",
        "{\"jsonrpc\":\"2.0\",\"error\":{\"code\":true,\"message\":\"x\"}}",
        "{\"jsonrpc\":\"2.0\",\"error\":{\"code\":1.0,\"message\":\"x\"}}",
        "{\"jsonrpc\":\"2.0\",\"error\":{\"code\":1,\"message\":4}}",
        "{\"jsonrpc\":\"2.0\",\"error\":{\"code\":1}}",
        "{\"jsonrpc\":\"2.0\",\"error\":{\"message\":\"x\"}}",
        "{\"jsonrpc\":\"2.0\",\"jsonrpc\":\"2.0\",\"error\":{\"code\":1,\"message\":\"x\"}}",
        "{\"jsonrpc\":\"2.0\",\"error\":{\"code\":1,\"\\u0063ode\":2,\"message\":\"x\"}}",
        "{\"jsonrpc\":\"2.0\",\"error\":{\"code\":NaN,\"message\":\"x\"}}"
    };
    for (size_t i = 0; i < sizeof invalid / sizeof invalid[0]; ++i) validate(invalid[i], -32000);
    /* Every byte of an otherwise well-formed id must match, including last. */
    char response[200] = "{\"jsonrpc\":\"2.0\",\"id\":\"00112233445546778899aabbccddeeff\",\"result\":1}";
    char *start = strstr(response, "001122");
    for (size_t i = 0; i < 32; ++i) {
        char original = start[i]; start[i] = original == '0' ? '1' : '0';
        validate(response, -32000); start[i] = original;
    }
    work.token_capacity = 1; validate(response, -32010); reset();
    work.arena_capacity = 1; validate(response, -32010); reset();
    uint8_t bad_id[32]; memset(bad_id, 'Z', sizeof bad_id);
    TMRPCReply reply = {77, 88, false};
    assert(tm_rpc_validate(u(response), strlen(response), bad_id, &work, &reply) == -32602);
    assert(reply.result_index == 77);
}
static void test_tree(void) {
    const char *source = "{\"k\":[null,false,true,-9223372036854775808,18446744073709551615,1.0,-0.0,5e-324,1.7976931348623157e308,\"\\u0000\\ud83c\\udf10\"],\"empty\":{}}";
    assert(tm_rpc_parse(u(source), strlen(source), &work) == 0);
    TMJsonWriter writer;
    tm_json_writer_init(out, sizeof out, &writer);
    assert(tm_json_write_tree(&writer, tokens, work.token_count, work.root) == 0);
    size_t expected = writer.used;
    TMJsonToken others[4096]; uint8_t scratch[32768]; TMJsonResult parsed;
    assert(tm_json_parse(out, writer.used, others, 4096, scratch, sizeof scratch, 64, &parsed) == 0);
    assert(parsed.token_count == work.token_count);
    for (size_t i = 0; i < work.token_count; ++i) {
        assert(tokens[i].kind == others[i].kind);
        if (tokens[i].kind == TM_JSON_REAL) assert(memcmp(&tokens[i].real, &others[i].real, sizeof(double)) == 0);
        if (tokens[i].kind == TM_JSON_INTEGER) assert(tokens[i].uinteger == others[i].uinteger && tokens[i].negative == others[i].negative);
        if (tokens[i].kind == TM_JSON_STRING) assert(tokens[i].text_size == others[i].text_size && !memcmp(tokens[i].text, others[i].text, tokens[i].text_size));
    }
    for (size_t cap = 0; cap <= expected; ++cap) {
        memset(out, 0xa5, sizeof out); tm_json_writer_init(out, cap, &writer);
        assert(tm_json_write_tree(&writer, tokens, work.token_count, work.root) == (cap < expected ? TM_JSON_ERR_CAPACITY : 0));
        assert(out[cap] == 0xa5);
    }
    uint64_t seed = UINT64_C(0x123456abcdef);
    for (int i = 0; i < 10000; ++i) {
        seed = seed * UINT64_C(6364136223846793005) + 1;
        double value; memcpy(&value, &seed, sizeof value);
        tm_json_writer_init(out, sizeof out, &writer);
        int status = tm_json_write_f64(&writer, value);
        if (!isfinite(value)) { assert(status == TM_JSON_ERR_NUMBER); continue; }
        assert(status == 0);
        assert(tm_json_parse(out, writer.used, others, 4096, scratch, sizeof scratch, 64, &parsed) == 0);
        assert(others[0].kind == TM_JSON_REAL && !memcmp(&others[0].real, &value, sizeof value));
    }
    uint8_t nested[140]; memset(nested, '[', 64); nested[64] = '0'; memset(nested + 65, ']', 64);
    assert(tm_rpc_parse(nested, 129, &work) == 0);
    tm_json_writer_init(out, sizeof out, &writer);
    assert(tm_json_write_tree(&writer, tokens, work.token_count, work.root) == 0);
    assert(tm_rpc_prepare(u("x"), 1, nested, 129, id, out, sizeof out, &work) == -32010);
    memset(nested, '[', 63); nested[63] = '0'; memset(nested + 64, ']', 63);
    int64_t prepared_size = tm_rpc_prepare(u("x"), 1, nested, 127, id, out, sizeof out, &work);
    assert(prepared_size > 0);
    assert(tm_json_parse(out, (size_t)prepared_size, others, 4096, scratch, sizeof scratch, 64, &parsed) == 0);
    memset(nested, '[', 65); nested[65] = '0'; memset(nested + 66, ']', 65);
    assert(tm_rpc_prepare(u("x"), 1, nested, 131, id, out, sizeof out, &work) == -32010);
    assert(tm_rpc_parse(u("[0]"), 3, &work) == 0);
    tokens[1].next = 1;
    tm_json_writer_init(out, sizeof out, &writer);
    assert(tm_json_write_tree(&writer, tokens, work.token_count, work.root) == TM_JSON_ERR_SYNTAX);
    tokens[1].next = -1; tokens[0].first = INT64_MAX;
    tm_json_writer_init(out, sizeof out, &writer);
    assert(tm_json_write_tree(&writer, tokens, work.token_count, work.root) == TM_JSON_ERR_SYNTAX);
    tokens[0].first = 0;
    tm_json_writer_init(out, sizeof out, &writer);
    assert(tm_json_write_tree(&writer, tokens, work.token_count, work.root) == TM_JSON_ERR_SYNTAX);
    tm_json_writer_init(out, sizeof out, &writer);
    assert(tm_json_write_tree(&writer, tokens, work.token_count, -2) == TM_JSON_ERR_SYNTAX);
}
int main(void) {
    test_request(); test_response(); test_tree();
    puts("native RPC client / JSON tree writer: all checks passed");
    return 0;
}
