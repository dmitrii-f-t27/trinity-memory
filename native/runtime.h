#ifndef TRINITY_MEMORY_RUNTIME_H
#define TRINITY_MEMORY_RUNTIME_H
#include <stddef.h>
#include <stdint.h>
typedef struct TMRuntime TMRuntime;
TMRuntime *tm_runtime_new(int32_t port, size_t max_request, size_t max_object,
                         size_t max_storage, size_t max_objects, size_t max_trits,
                         double timeout);
int32_t tm_runtime_start(TMRuntime *runtime);
int32_t tm_runtime_port(TMRuntime *runtime);
void tm_runtime_close(TMRuntime *runtime);
size_t tm_runtime_stored_bytes(TMRuntime *runtime);
size_t tm_runtime_object_count(TMRuntime *runtime);
struct TMTPJSONWorkspace *tm_runtime_tensor_new(size_t json_bytes, size_t max_trits);
void tm_runtime_tensor_free(struct TMTPJSONWorkspace *workspace);
struct TMRPCWorkspace *tm_runtime_rpc_new(size_t json_bytes);
void tm_runtime_rpc_free(struct TMRPCWorkspace *workspace);
void *tm_runtime_alloc(size_t count, size_t element);
void tm_runtime_free(void *pointer);
uint64_t tm_runtime_monotonic_ns(void);
const char *tm_runtime_platform(void);
const char *tm_runtime_utc(void);
void *tm_host_server_new(int32_t port, size_t request, size_t object, size_t storage,
                         size_t objects, size_t trits, double timeout);
int32_t tm_host_server_start(void *server);
int32_t tm_host_server_port(void *server);
void tm_host_server_close(void *server);
/* Raw HTTP body transport. Domain request/response validation is in client.t27. */
int64_t tm_runtime_http(int32_t port, double timeout, uint8_t *request, size_t size,
                        uint8_t *response, size_t capacity);
#endif
