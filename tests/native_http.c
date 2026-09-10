#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "ffi.h"
#include "json.h"
#include "json_writer.h"
#include "http.h"

static int parse(const char *text, TMHttpEnvelope *result) {
    return tm_http_request((uint8_t *)text, strlen(text), 8787, 2097152, result);
}

int main(void) {
    TMHttpEnvelope result = {0};
    const char *valid = "POST / HTTP/1.1\r\nHost: 127.0.0.1:8787\r\n"
        "Content-Length: 2\r\nContent-Type: application/json\r\n\r\n{}";
    assert(parse(valid, &result) == 1 && result.body_size == 2 && result.status == 200);
    size_t header = result.body_offset;
    for (size_t i = 0; i < header; i++)
        assert(tm_http_request((uint8_t *)valid, i, 8787, 2097152, &result) == 0);
    assert(tm_http_request((uint8_t *)valid, header, 8787, 2097152, &result) == 1);
    assert(parse("POST / HTTP/1.0\nHOST: localhost:8787\nContent-Length: 0000\n"
                 "Content-Type: APPLICATION/JSON ; charset=utf-8\n\n", &result) == 1);
    assert(result.body_size == 0);
    const char *invalid_hosts[] = {
        "evil.test:8787", "127.0.0.1:8788", "127.0.0.1:08787", "LOCALHOST:8787", "localhost", "[::1]:8787"
    };
    char request[1024];
    for (unsigned i = 0; i < sizeof invalid_hosts / sizeof *invalid_hosts; i++) {
        snprintf(request, sizeof request, "POST / HTTP/1.1\r\nHost: %s\r\n"
                 "Content-Length: 0\r\nContent-Type: application/json\r\n\r\n", invalid_hosts[i]);
        assert(parse(request, &result) == -1 && result.status == 403);
    }
    const char *bad_lengths[] = {"", "+2", "-1", "2x", "1.0", "00000000000"};
    for (unsigned i = 0; i < sizeof bad_lengths / sizeof *bad_lengths; i++) {
        snprintf(request, sizeof request, "POST / HTTP/1.1\r\nHost: localhost:8787\r\n"
                 "Content-Length: %s\r\nContent-Type: application/json\r\n\r\n", bad_lengths[i]);
        assert(parse(request, &result) == -1 && result.status == 400);
    }
    assert(parse("POST / HTTP/1.1\r\nHost: localhost:8787\r\nContent-Length: 9999999999\r\n"
                 "Content-Type: application/json\r\n\r\n", &result) == -1 && result.status == 413 && result.rpc_error == -32010);
    const char *forbidden[] = {
        "Host: localhost:8787\r\n", "Origin: null\r\n", "Origin: \r\n",
        "Transfer-Encoding: chunked\r\n", "Content-Length: 0\r\n"
    };
    for (unsigned i = 0; i < sizeof forbidden / sizeof *forbidden; i++) {
        snprintf(request, sizeof request, "POST / HTTP/1.1\r\nHost: localhost:8787\r\n%s"
                 "Content-Length: 0\r\nContent-Type: application/json\r\n\r\n", forbidden[i]);
        assert(parse(request, &result) == -1);
        assert(result.status == (i < 3 ? 403 : 400));
    }
    assert(parse("POST /other HTTP/1.1\r\n\r\n", &result) == -1 && result.status == 403);
    assert(parse("GET / HTTP/1.1\r\n\r\n", &result) == -1 && result.status == 405);
    assert(parse("POST / HTTP/1.1\r\nHost: localhost:8787\r\nContent-Length: 0\r\n\r\n", &result) == -1 && result.status == 415);
    const char *urls[] = {"http://localhost:1", "HTTP://LOCALHOST:65535/", "http://127.0.0.1:0008787/"};
    int ports[] = {1, 65535, 8787};
    for (unsigned i = 0; i < 3; i++) assert(tm_http_url((uint8_t *)urls[i], strlen(urls[i])) == ports[i]);
    const char *bad_urls[] = {"http://localhost", "https://localhost:1", "http://x:1", "http://localhost:0",
        "http://localhost:65536", "http://u@localhost:1", "http://localhost:1/?x", "http://localhost:1#x", "http://localhost:1/a"};
    for (unsigned i = 0; i < sizeof bad_urls / sizeof *bad_urls; i++) assert(tm_http_url((uint8_t *)bad_urls[i], strlen(bad_urls[i])) < 0);
    uint8_t output[1024];
    int64_t n = tm_http_request_header(8787, 2, output, sizeof output);
    assert(n > 0 && tm_http_request(output, (size_t)n, 8787, 2, &result) == 1);
    assert(result.body_offset == (size_t)n && result.body_size == 2);
    n = tm_http_response_header(400, 2, output, sizeof output);
    TMHttpResponse response = {0};
    assert(n > 0 && tm_http_response(output, (size_t)n, 2, &response) == 1);
    assert(response.status == 400 && response.has_length && !response.chunked && response.body_size == 2);
    assert(tm_http_response(output, (size_t)n, 1, &response) == -2);
    const char *redirect = "HTTP/1.1 302 Found\r\nLocation: http://localhost:1/\r\n\r\n";
    assert(tm_http_response((uint8_t *)redirect, strlen(redirect), 100, &response) < 0);
    const char *chunked = "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n";
    assert(tm_http_response((uint8_t *)chunked, strlen(chunked), 100, &response) == 1 && response.chunked);
    const char *chunks = "1\r\n{\r\n1;extension=yes\r\n}\r\n0\r\nX-Trailer: ok\r\n\r\n";
    size_t written = 0;
    for (size_t i = 0; i < strlen(chunks); i++) assert(tm_http_chunks((uint8_t *)chunks, i, output, sizeof output, &written) == 0);
    assert(tm_http_chunks((uint8_t *)chunks, strlen(chunks), output, sizeof output, &written) == 1);
    assert(written == 2 && output[0] == '{' && output[1] == '}');
    assert(tm_http_chunks((uint8_t *)chunks, strlen(chunks), output, 1, &written) == -2);
    assert(tm_http_chunks((uint8_t *)"x\r\n", 3, output, sizeof output, &written) == -1);
    assert(tm_http_chunks((uint8_t *)"1\r\nx!\r", 6, output, sizeof output, &written) == -1);
    puts("PASS native HTTP headers: incremental reads, Host/Origin/content limits, duplicate framing, loopback URL validation");
    return 0;
}
