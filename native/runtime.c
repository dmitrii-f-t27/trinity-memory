/* Allocation, sockets, clocks and worker lifecycle. Protocol and storage
 * algorithms are called through the generated native t27 ABI. */
#define _POSIX_C_SOURCE 200809L
#include "api.h"
#include "runtime.h"
#include "ffi.h"
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <math.h>
#include <netinet/in.h>
#include <poll.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/utsname.h>
#include <time.h>
#include <unistd.h>

struct TMRuntime {
    TMBridgeState state;
    TMTPJSONWorkspace tensor;
    void *allocations[24];
    size_t allocation_count;
    uint8_t *request, *response;
    size_t request_capacity, response_capacity;
    int listener, cancel[2], port;
    bool running;
    double timeout;
    pthread_t worker;
    pthread_mutex_t state_lock;
    bool lock_initialized;
    TMLink link;
    void *link_allocations[16];
    size_t link_allocation_count;
};

static double monotonic(void) {
    struct timespec time;
    if (clock_gettime(CLOCK_MONOTONIC, &time) != 0) return -1;
    return (double)time.tv_sec + (double)time.tv_nsec / 1000000000.0;
}
static int wait_socket(int fd, short events, double deadline, int cancel) {
    for (;;) {
        double now = monotonic();
        if (now < 0 || now >= deadline) return -1;
        double remain = ceil((deadline - now) * 1000.0);
        int milliseconds = remain > INT_MAX ? INT_MAX : (int)remain;
        struct pollfd descriptors[2] = {{fd, events, 0}, {cancel, POLLIN, 0}};
        int result = poll(descriptors, cancel >= 0 ? 2 : 1, milliseconds);
        if (result < 0 && errno == EINTR) continue;
        if (result <= 0 || (cancel >= 0 && descriptors[1].revents)) return -1;
        if (descriptors[0].revents & (events | POLLHUP | POLLERR)) return 0;
        return -1;
    }
}
static void suppress_sigpipe(int fd) {
#if defined(SO_NOSIGPIPE)
    int on = 1;
    (void)setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &on, sizeof on);
#else
    (void)fd;
#endif
}
static int send_all(int fd, const uint8_t *data, size_t size, double deadline, int cancel) {
    size_t done = 0;
    while (done < size) {
        if (wait_socket(fd, POLLOUT, deadline, cancel) != 0) return -1;
#if defined(MSG_NOSIGNAL)
        ssize_t n = send(fd, data + done, size - done, MSG_NOSIGNAL);
#else
        ssize_t n = send(fd, data + done, size - done, 0);
#endif
        if (n < 0 && (errno == EINTR || errno == EAGAIN)) continue;
        if (n <= 0) return -1;
        done += (size_t)n;
    }
    return 0;
}
static ssize_t receive(int fd, uint8_t *data, size_t capacity, double deadline, int cancel) {
    for (;;) {
        if (wait_socket(fd, POLLIN, deadline, cancel) != 0) return -1;
        ssize_t n = recv(fd, data, capacity, 0);
        if (n < 0 && (errno == EINTR || errno == EAGAIN)) continue;
        return n;
    }
}
static void *allocate(TMRuntime *runtime, size_t count, size_t element) {
    if (runtime->allocation_count >= 24 || (element && count > SIZE_MAX / element)) return NULL;
    void *data = calloc(count ? count : 1, element);
    if (data) runtime->allocations[runtime->allocation_count++] = data;
    return data;
}

TMRuntime *tm_runtime_new(int32_t port, size_t max_request, size_t max_object,
                         size_t max_storage, size_t max_objects, size_t max_trits,
                         double timeout) {
    if (port < 0 || port > 65535 || !isfinite(timeout) || timeout <= 0 ||
        !max_request || !max_object || !max_storage || !max_objects || !max_trits ||
        max_request > SIZE_MAX - 65537 || max_object > SIZE_MAX / max_objects ||
        max_object > (SIZE_MAX - 16384) / 2 ||
        max_trits > (SIZE_MAX - 16384 - max_object * 2) / 40) return NULL;
    TMRuntime *runtime = calloc(1, sizeof *runtime);
    if (!runtime) return NULL;
    runtime->listener = runtime->cancel[0] = runtime->cancel[1] = -1;
    if (pthread_mutex_init(&runtime->state_lock, NULL) != 0) { free(runtime); return NULL; }
    runtime->lock_initialized = true;
    runtime->port = port;
    runtime->timeout = timeout;
    TMBridgeState *s = &runtime->state;
    s->slot_capacity = max_objects;
    s->object_capacity = max_object;
    s->pool_capacity = max_object * max_objects;
    s->max_request_bytes = max_request;
    s->max_storage_bytes = max_storage;
    s->max_trits = max_trits;
    s->temporary_capacity = max_object;
    s->value_capacity = max_trits;
    s->token_capacity = max_request / 2 + 2;
    s->arena_capacity = max_request + 1;
    s->slots = allocate(runtime, max_objects, sizeof *s->slots);
    s->pool = allocate(runtime, s->pool_capacity, 1);
    s->temporary = allocate(runtime, max_object, 1);
    s->values = allocate(runtime, max_trits, sizeof *s->values);
    s->json_tokens = allocate(runtime, s->token_capacity, sizeof *s->json_tokens);
    s->json_arena = allocate(runtime, s->arena_capacity, 1);
    s->tensor_work = &runtime->tensor;
    TMTPJSONWorkspace *w = s->tensor_work;
    size_t metadata = max_object < 1048576 ? max_object : 1048576;
    w->token_capacity = metadata + 1;
    w->arena_capacity = metadata + 1;
    w->item_capacity = 1024;
    w->shape_capacity = w->axis_capacity = 16384;
    w->scale_capacity = max_trits < metadata + 1 ? max_trits : metadata + 1;
    w->tokens = allocate(runtime, w->token_capacity, sizeof *w->tokens);
    w->arena = allocate(runtime, w->arena_capacity, 1);
    w->items = allocate(runtime, w->item_capacity, sizeof *w->items);
    w->shapes = allocate(runtime, w->shape_capacity, sizeof *w->shapes);
    w->scales = allocate(runtime, w->scale_capacity, sizeof *w->scales);
    w->axes = allocate(runtime, w->axis_capacity, sizeof *w->axes);
    runtime->request_capacity = max_request + 65536;
    runtime->response_capacity = 16384 + max_object * 2 + max_trits * 40;
    runtime->request = allocate(runtime, runtime->request_capacity, 1);
    runtime->response = allocate(runtime, runtime->response_capacity, 1);
    if (runtime->allocation_count != 14 || tm_bridge_init(s) != 0) { tm_runtime_close(runtime); return NULL; }
    return runtime;
}

static void handle_connection(TMRuntime *r, int fd) {
    suppress_sigpipe(fd);
    (void)fcntl(fd, F_SETFL, O_NONBLOCK);
    double deadline = monotonic() + r->timeout;
    size_t used = 0;
    TMHttpEnvelope header = {0};
    int parsed = 0;
    int32_t status = 200;
    int64_t output = -1;
    while (parsed == 0) {
        if (used == r->request_capacity) break;
        size_t room = r->request_capacity - used;
        if (room > 65536) room = 65536;
        ssize_t n = receive(fd, r->request + used, room, deadline, r->cancel[0]);
        if (n <= 0) break;
        used += (size_t)n;
        parsed = tm_http_request(r->request, used, r->port, r->state.max_request_bytes, &header);
    }
    if (parsed < 0) {
        status = header.status;
        const char *message = status == 403 ? "only local non-browser RPC requests are accepted" :
            status == 413 ? "request limit exceeded" :
            status == 415 ? "Content-Type must be application/json" : "one Content-Length is required";
        output = tm_bridge_http_error(header.rpc_error, message, r->response, r->response_capacity);
    } else if (parsed == 1) {
        size_t wanted = header.body_offset + header.body_size;
        deadline = monotonic() + r->timeout;
        while (used < wanted) {
            ssize_t n = receive(fd, r->request + used, wanted - used, deadline, r->cancel[0]);
            if (n <= 0) break;
            used += (size_t)n;
        }
        if (used >= wanted) {
            pthread_mutex_lock(&r->state_lock);
            output = tm_bridge_request(&r->state, r->request + header.body_offset,
                        header.body_size, r->response, r->response_capacity, &status);
            pthread_mutex_unlock(&r->state_lock);
        }
    }
    if (output < 0) {
        status = 400;
        output = tm_bridge_http_error(-32700, "invalid JSON", r->response, r->response_capacity);
    }
    if (output >= 0) {
        uint8_t head[512];
        int64_t length = tm_http_response_header(status, (size_t)output, head, sizeof head);
        deadline = monotonic() + r->timeout;
        if (length > 0 && send_all(fd, head, (size_t)length, deadline, r->cancel[0]) == 0)
            (void)send_all(fd, r->response, (size_t)output, deadline, r->cancel[0]);
    }
    /* Finish sending before discarding any body rejected at the header stage.
     * Closing with unread TCP data can reset the connection and lose a 413/403
     * response while the client is still sending its already-framed body. */
    (void)shutdown(fd, SHUT_WR);
    uint8_t discard[4096];
    deadline = monotonic() + r->timeout;
    while (receive(fd, discard, sizeof discard, deadline, r->cancel[0]) > 0) {}
}
static void *serve_worker(void *argument) {
    TMRuntime *runtime = argument;
    for (;;) {
        struct pollfd descriptors[2] = {{runtime->listener, POLLIN, 0}, {runtime->cancel[0], POLLIN, 0}};
        int result = poll(descriptors, 2, -1);
        if (result < 0 && errno == EINTR) continue;
        if (result <= 0 || descriptors[1].revents) break;
        int client = accept(runtime->listener, NULL, NULL);
        if (client < 0) { if (errno == EINTR) continue; break; }
        handle_connection(runtime, client);
        close(client);
    }
    return NULL;
}
int32_t tm_runtime_start(TMRuntime *runtime) {
    if (!runtime || runtime->running || runtime->listener >= 0) return -1;
    int listener = socket(AF_INET, SOCK_STREAM, 0);
    if (listener < 0) return -1;
    runtime->listener = listener;
    int on = 1;
    (void)setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &on, sizeof on);
    struct sockaddr_in address = {0};
    address.sin_family = AF_INET;
    address.sin_port = htons((uint16_t)runtime->port);
    address.sin_addr.s_addr = htonl(UINT32_C(0x7f000001));
    if (bind(listener, (struct sockaddr *)&address, sizeof address) != 0 || listen(listener, 16) != 0 ||
        pipe(runtime->cancel) != 0) return -1;
    socklen_t length = sizeof address;
    if (getsockname(listener, (struct sockaddr *)&address, &length) != 0) return -1;
    runtime->port = ntohs(address.sin_port);
    if (pthread_create(&runtime->worker, NULL, serve_worker, runtime) != 0) return -1;
    runtime->running = true;
    return 0;
}
int32_t tm_runtime_port(TMRuntime *runtime) {
    return runtime && runtime->running ? runtime->port : -1;
}
size_t tm_runtime_stored_bytes(TMRuntime *runtime) {
    if (!runtime) return 0;
    pthread_mutex_lock(&runtime->state_lock);
    size_t result = runtime->state.stored_bytes;
    pthread_mutex_unlock(&runtime->state_lock);
    return result;
}
size_t tm_runtime_object_count(TMRuntime *runtime) {
    if (!runtime) return 0;
    pthread_mutex_lock(&runtime->state_lock);
    size_t result = runtime->state.object_count;
    pthread_mutex_unlock(&runtime->state_lock);
    return result;
}
/* The bytes the device sent during the last fpga call (chip_info or compute.dot: the capture whose
 * sha256 and count that call's evidence reports), for the record of a board run. Returns their
 * count and copies them into out when capacity holds them (a first call with capacity 0 asks
 * for the count); 0 without an fpga link. Taken under the state lock, so between requests. */
size_t tm_runtime_fpga_capture(TMRuntime *runtime, uint8_t *out, size_t capacity) {
    if (!runtime || !runtime->link.configured) return 0;
    pthread_mutex_lock(&runtime->state_lock);
    size_t used = runtime->link.capture_used;
    if (out && used <= capacity) memcpy(out, runtime->link.capture, used);
    pthread_mutex_unlock(&runtime->state_lock);
    return used;
}
void *tm_host_server_new(int32_t port, size_t request, size_t object, size_t storage,
                         size_t objects, size_t trits, double timeout) {
    return tm_runtime_new(port, request, object, storage, objects, trits, timeout);
}
int32_t tm_host_server_start(void *server) { return tm_runtime_start(server); }
int32_t tm_host_server_port(void *server) { return tm_runtime_port(server); }
void tm_host_server_close(void *server) { tm_runtime_close(server); }

void *tm_runtime_alloc(size_t count, size_t element) {
    if (!element || count > SIZE_MAX / element) return NULL;
    return calloc(count ? count : 1, element);
}
void tm_runtime_free(void *pointer) { free(pointer); }

TMTPJSONWorkspace *tm_runtime_tensor_new(size_t bytes, size_t max_trits) {
    if (bytes > SIZE_MAX - 2 || !max_trits) return NULL;
    TMTPJSONWorkspace *w = calloc(1, sizeof *w);
    if (!w) return NULL;
    w->token_capacity = bytes / 2 + 2;
    w->arena_capacity = bytes + 1;
    w->item_capacity = 1024;
    w->shape_capacity = w->axis_capacity = 16384;
    w->scale_capacity = max_trits < bytes / 2 + 1 ? max_trits : bytes / 2 + 1;
    w->tokens = tm_runtime_alloc(w->token_capacity, sizeof *w->tokens);
    w->arena = tm_runtime_alloc(w->arena_capacity, 1);
    w->items = tm_runtime_alloc(w->item_capacity, sizeof *w->items);
    w->shapes = tm_runtime_alloc(w->shape_capacity, sizeof *w->shapes);
    w->scales = tm_runtime_alloc(w->scale_capacity, sizeof *w->scales);
    w->axes = tm_runtime_alloc(w->axis_capacity, sizeof *w->axes);
    if (!w->tokens || !w->arena || !w->items || !w->shapes || !w->scales || !w->axes) {
        tm_runtime_tensor_free(w); return NULL;
    }
    return w;
}
void tm_runtime_tensor_free(TMTPJSONWorkspace *w) {
    if (!w) return;
    free(w->tokens); free(w->arena); free(w->items);
    free(w->shapes); free(w->scales); free(w->axes); free(w);
}
TMRPCWorkspace *tm_runtime_rpc_new(size_t bytes) {
    if (bytes > SIZE_MAX - 2) return NULL;
    TMRPCWorkspace *w = calloc(1, sizeof *w);
    if (!w) return NULL;
    w->token_capacity = bytes / 2 + 2;
    w->arena_capacity = bytes + 1;
    w->tokens = tm_runtime_alloc(w->token_capacity, sizeof *w->tokens);
    w->arena = tm_runtime_alloc(w->arena_capacity, 1);
    if (!w->tokens || !w->arena) { tm_runtime_rpc_free(w); return NULL; }
    return w;
}
void tm_runtime_rpc_free(TMRPCWorkspace *w) {
    if (!w) return;
    free(w->tokens); free(w->arena); free(w);
}
uint64_t tm_runtime_monotonic_ns(void) {
    struct timespec time;
    if (clock_gettime(CLOCK_MONOTONIC, &time) != 0) return 0;
    return (uint64_t)time.tv_sec * UINT64_C(1000000000) + (uint64_t)time.tv_nsec;
}
const char *tm_runtime_platform(void) {
    struct utsname name;
    static _Thread_local char description[1024];
    if (uname(&name) != 0) return "unknown";
    int size = snprintf(description, sizeof(description), "%s %s %s", name.sysname, name.release, name.machine);
    return size > 0 && (size_t)size < sizeof(description) ? description : "unknown";
}
const char *tm_runtime_utc(void) {
    static _Thread_local char result[32];
    time_t now = time(NULL);
    struct tm value;
    if (!gmtime_r(&now, &value) || !strftime(result, sizeof result, "%Y-%m-%dT%H:%M:%SZ", &value)) return "unknown";
    return result;
}
static void *link_allocate(TMRuntime *runtime, size_t count, size_t element) {
    if (runtime->link_allocation_count >= 16 || (element && count > SIZE_MAX / element)) return NULL;
    void *data = calloc(count ? count : 1, element);
    if (data) runtime->link_allocations[runtime->link_allocation_count++] = data;
    return data;
}
int32_t tm_runtime_fpga(TMRuntime *runtime, uint8_t *path, size_t path_size, uint32_t baud,
                        uint32_t reply_ms, uint32_t quiet_ms, uint32_t attempts, uint32_t min_protocol,
                        uint32_t region, uint8_t *bitstream_sha256, uint32_t idcode, uint64_t dna,
                        uint32_t build_id) {
    if (!runtime || runtime->running || runtime->link.configured || !path || path_size == 0 || path_size > 1023 ||
        !bitstream_sha256 || !attempts || attempts > 64 || region % 16 != 0) return -1;
    /* The device must speak the matvec extension, and the host must be able to set the rate. */
    if (min_protocol < TL_PROTO_MATVEC || !tm_os_serial_rate_ok(baud)) return -1;
    TMBridgeState *s = &runtime->state;
    TMLink *l = &runtime->link;
    size_t trits = s->max_trits;
    if (trits > (SIZE_MAX - 65536) / 4) return -1;
    /* Row-padded images: 2 bits per trit (baseline2) plus padding; the capture holds a whole
     * read-back of the image, its framing and the response lines. */
    l->image_capacity = trits / 2 + 65536;
    l->capture_capacity = l->image_capacity * 2 + 1048576;
    l->rx_capacity = 16384;
    l->frame_capacity = 8192;
    l->x_capacity = trits;
    l->act_capacity = 1024 * 80;
    l->acc_capacity = trits;
    l->path = link_allocate(runtime, path_size, 1);
    l->bitstream = link_allocate(runtime, 32, 1);
    l->rx = link_allocate(runtime, l->rx_capacity, 1);
    l->frame = link_allocate(runtime, l->frame_capacity, 1);
    l->capture = link_allocate(runtime, l->capture_capacity, 1);
    l->image = link_allocate(runtime, l->image_capacity, 1);
    l->x = link_allocate(runtime, l->x_capacity, 1);
    l->act = link_allocate(runtime, l->act_capacity, 1);
    l->acc = link_allocate(runtime, l->acc_capacity, sizeof *l->acc);
    l->seen = link_allocate(runtime, 1024, 1);
    l->status = link_allocate(runtime, 37, sizeof *l->status);   /* 23 or 37 status lines */
    l->z = link_allocate(runtime, 11, sizeof *l->z);
    l->loaded_digest = link_allocate(runtime, 32, 1);
    l->image_digest = link_allocate(runtime, 32, 1);
    if (runtime->link_allocation_count != 14) return -1;
    memcpy(l->path, path, path_size);
    memcpy(l->bitstream, bitstream_sha256, 32);
    l->path_size = path_size;
    l->baud = baud;
    l->reply_ms = reply_ms;
    l->quiet_ms = quiet_ms;
    l->max_attempts = attempts;
    l->min_protocol = min_protocol;
    l->region = region;
    l->idcode = idcode;
    l->dna = dna;
    l->build_id = build_id;
    l->fd = -1;
    l->configured = true;
    s->backend = 1;
    s->transport = 1;
    s->link = l;
    return 0;
}
void tm_runtime_close(TMRuntime *runtime) {
    if (!runtime) return;
    if (runtime->running) {
        uint8_t byte = 1;
        (void)write(runtime->cancel[1], &byte, 1);
        pthread_join(runtime->worker, NULL);
    }
    if (runtime->listener >= 0) close(runtime->listener);
    if (runtime->cancel[0] >= 0) close(runtime->cancel[0]);
    if (runtime->cancel[1] >= 0) close(runtime->cancel[1]);
    for (size_t i = 0; i < runtime->allocation_count; i++) free(runtime->allocations[i]);
    if (runtime->link.configured) tl_close(&runtime->link);
    for (size_t i = 0; i < runtime->link_allocation_count; i++) free(runtime->link_allocations[i]);
    if (runtime->lock_initialized) pthread_mutex_destroy(&runtime->state_lock);
    free(runtime);
}

int64_t tm_runtime_http(int32_t port, double timeout, uint8_t *request, size_t size,
                        uint8_t *response, size_t capacity) {
    if (port < 1 || port > 65535 || !isfinite(timeout) || timeout <= 0 ||
        !capacity || capacity > (SIZE_MAX - 131072) / 8) return -32000;
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return -32000;
    suppress_sigpipe(fd);
    (void)fcntl(fd, F_SETFL, O_NONBLOCK);
    struct sockaddr_in address = {0};
    address.sin_family = AF_INET;
    address.sin_port = htons((uint16_t)port);
    address.sin_addr.s_addr = htonl(UINT32_C(0x7f000001));
    double deadline = monotonic() + timeout;
    int connected = connect(fd, (struct sockaddr *)&address, sizeof address);
    int64_t answer = -32000;
    uint8_t *incoming = NULL;
    if (connected != 0) {
        if (errno != EINPROGRESS || wait_socket(fd, POLLOUT, deadline, -1) != 0) goto done;
        int error = 0;
        socklen_t length = sizeof error;
        if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &length) != 0 || error != 0) goto done;
    }
    uint8_t head[512];
    int64_t header_size = tm_http_request_header(port, size, head, sizeof head);
    if (header_size < 0 || send_all(fd, head, (size_t)header_size, deadline, -1) != 0 ||
        send_all(fd, request, size, deadline, -1) != 0) goto done;
    /* Wire capacity also permits one-byte chunk framing and bounded trailers. */
    size_t incoming_capacity = capacity * 8 + 131072;
    incoming = malloc(incoming_capacity);
    if (!incoming) goto done;
    size_t used = 0;
    int parsed = 0;
    TMHttpResponse envelope = {0};
    for (;;) {
        if (used == incoming_capacity) { answer = -32010; break; }
        size_t room = incoming_capacity - used;
        if (room > 65536) room = 65536;
        ssize_t n = receive(fd, incoming + used, room, deadline, -1);
        if (n < 0) break;
        used += (size_t)n;
        if (!parsed) {
            parsed = tm_http_response(incoming, used, capacity, &envelope);
            if (parsed < 0) { answer = parsed == -2 ? -32010 : -32000; break; }
        }
        if (parsed == 1) {
            size_t body = used - envelope.body_offset;
            if (envelope.chunked) {
                size_t written = 0;
                int chunks = tm_http_chunks(incoming + envelope.body_offset, body, response, capacity, &written);
                if (chunks == 1) { answer = (int64_t)written; break; }
                if (chunks < 0) { answer = chunks == -2 ? -32010 : -32000; break; }
            } else {
                if (body > capacity) { answer = -32010; break; }
                if ((envelope.has_length && body >= envelope.body_size) || (!envelope.has_length && n == 0)) {
                    size_t length = envelope.has_length ? envelope.body_size : body;
                    memcpy(response, incoming + envelope.body_offset, length);
                    answer = (int64_t)length;
                    break;
                }
            }
        }
        if (n == 0) break;
    }
done:
    free(incoming);
    close(fd);
    return answer;
}
