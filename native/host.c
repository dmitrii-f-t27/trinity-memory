/* OS and standard formatting primitives only; command logic lives in cli.t27. */
#define _POSIX_C_SOURCE 200809L
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>
#include <unistd.h>
#include <signal.h>
#include <time.h>
#include <sys/stat.h>
#include <errno.h>
#include "api.h"
#include "runtime.h"

#if defined(__clang__)
#pragma clang diagnostic ignored "-Wparentheses-equality"
#endif

static int host_argc;
static char **host_argv;
static uint8_t *input_buffer, *output_buffer;
static int32_t *value_buffer;
static bool allocation_ok = true;
static uint8_t *scratch[4];
static TMTPJSONWorkspace *tensor_workspace;
static TMRPCWorkspace *rpc_workspace;
static volatile sig_atomic_t interrupted;
static struct sigaction old_int, old_term;
static bool signals_prepared;

static const char *tm_os_arg(int32_t index) {
    return index >= 0 && index < host_argc ? host_argv[index] : "";
}
static int32_t tm_os_bytes(uint8_t *data, size_t size) {
    return fwrite(data, 1, size, stdout) == size ? 0 : -1;
}
static int32_t tm_os_flush(void) { return fflush(stdout); }
static int32_t tm_os_error_bytes(uint8_t *data, size_t size) {
    fputs("trinity-memory-t27: ", stderr);
    (void)fwrite(data, 1, size, stderr);
    fputc('\n', stderr);
    return 2;
}
static uint8_t *tm_os_scratch(int32_t slot, size_t size) {
    if (slot < 0 || slot >= 4) { allocation_ok = false; return NULL; }
    free(scratch[slot]);
    scratch[slot] = malloc(size ? size : 1);
    allocation_ok = allocation_ok && scratch[slot] != NULL;
    return scratch[slot];
}
static TMTPJSONWorkspace *tm_os_tensor_workspace(size_t bytes) {
    tm_runtime_tensor_free(tensor_workspace);
    tensor_workspace = tm_runtime_tensor_new(bytes, 4194304);
    allocation_ok = allocation_ok && tensor_workspace != NULL;
    return tensor_workspace;
}
static TMRPCWorkspace *tm_os_rpc_workspace(size_t bytes) {
    tm_runtime_rpc_free(rpc_workspace);
    rpc_workspace = tm_runtime_rpc_new(bytes);
    allocation_ok = allocation_ok && rpc_workspace != NULL;
    return rpc_workspace;
}
static void stop_signal(int signal) { (void)signal; interrupted = 1; }
static int32_t tm_os_prepare_interrupts(void) {
    if (signals_prepared) return 0;
    struct sigaction action = {0};
    action.sa_handler = stop_signal;
    sigemptyset(&action.sa_mask);
    interrupted = 0;
    if (sigaction(SIGINT, &action, &old_int) != 0) return -1;
    if (sigaction(SIGTERM, &action, &old_term) != 0) {
        sigaction(SIGINT, &old_int, NULL);
        return -1;
    }
    signals_prepared = true;
    return 0;
}
static void tm_os_wait_for_interrupt(void) {
    if (tm_os_prepare_interrupts() != 0) return;
    const struct timespec interval = {0, 100000000};
    while (!interrupted) nanosleep(&interval, NULL);
    sigaction(SIGINT, &old_int, NULL);
    sigaction(SIGTERM, &old_term, NULL);
    signals_prepared = false;
}

static bool tm_os_arg_equal(int32_t index, const char *text) {
    return index >= 0 && index < host_argc && strcmp(host_argv[index], text) == 0;
}
static int32_t tm_os_error(const char *text) {
    fprintf(stderr, "trinity-memory-t27: %s\n", text);
    return 2;
}
static int32_t tm_os_usage(void) {
    fputs("Native .t27 Trinity Memory commands:\n"
          "  trinity-memory-t27 pack INPUT.json OUTPUT.tmem [--codec NAME]\n"
          "  trinity-memory-t27 unpack INPUT.tmem OUTPUT.json\n"
          "  trinity-memory-t27 inspect INPUT.tmem\n"
          "  trinity-memory-t27 export-rtl INPUT.json OUTPUT.mem [--codec NAME]\n"
          "  trinity-memory-t27 tensor-pack INPUT.json OUTPUT.ttpk\n"
          "  trinity-memory-t27 tensor-unpack INPUT.ttpk OUTPUT.json\n"
          "  trinity-memory-t27 tensor-inspect INPUT.ttpk\n"
          "  trinity-memory-t27 serve [--port PORT]\n"
          "  trinity-memory-t27 upload INPUT [--url URL]\n"
          "  trinity-memory-t27 download HANDLE OUTPUT [--url URL]\n"
          "  trinity-memory-t27 dot HANDLE TENSOR INPUT.json [--url URL]\n"
          "  trinity-memory-t27 benchmark [--count N] [--repeats N] [--seed N]\n"
          "  trinity-memory-t27 conformance [--vectors PATH] [--rtl] [--seed N]\n"
          "  trinity-memory-t27 edge-demo [--rtl] [--seed N] [--output PATH] [--html PATH]\n", stderr);
    return 2;
}
static int64_t tm_os_read_path(const char *path, size_t limit) {
    if (!path || limit == SIZE_MAX || limit > INT64_MAX) return -1;
    FILE *file = fopen(path, "rb");
    if (!file) return -1;
    free(input_buffer);
    input_buffer = malloc(limit + 1);
    if (!input_buffer) { allocation_ok = false; fclose(file); return -1; }
    size_t size = fread(input_buffer, 1, limit + 1, file);
    bool failed = ferror(file) || size > limit;
    if (fclose(file) != 0) failed = true;
    return failed ? -1 : (int64_t)size;
}
static int64_t tm_os_read(int32_t argument, size_t limit) {
    if (argument < 0 || argument >= host_argc) return -1;
    return tm_os_read_path(host_argv[argument], limit);
}
static uint8_t *tm_os_input(void) { return input_buffer; }
static int32_t *tm_os_values(size_t capacity) {
    if (capacity > SIZE_MAX / sizeof(*value_buffer)) {
        allocation_ok = false;
        return NULL;
    }
    value_buffer = calloc(capacity ? capacity : 1, sizeof(*value_buffer));
    allocation_ok = allocation_ok && value_buffer != NULL;
    return value_buffer;
}
static uint8_t *tm_os_output(size_t capacity) {
    output_buffer = malloc(capacity ? capacity : 1);
    allocation_ok = allocation_ok && output_buffer != NULL;
    return output_buffer;
}
static bool tm_os_allocated(void) { return allocation_ok; }
static int32_t tm_os_write_path(const char *target, uint8_t *data, size_t size, bool create_parents) {
    if (!target || !*target || (!data && size)) return -1;
    if (create_parents) {
        char *parent = strdup(target);
        if (!parent) return -1;
        for (char *p = parent + 1; *p; p++) {
            if (*p != '/') continue;
            *p = 0;
            if (mkdir(parent, 0777) != 0 && errno != EEXIST) { free(parent); return -1; }
            *p = '/';
        }
        free(parent);
    }
    size_t len = strlen(target);
    if (len > SIZE_MAX - 12) return -1;
    char *temporary = malloc(len + 12);
    if (!temporary) return -1;
    snprintf(temporary, len + 12, "%s.tmp.XXXXXX", target);
    int fd = mkstemp(temporary);
    if (fd < 0) { free(temporary); return -1; }
    FILE *file = fdopen(fd, "wb");
    bool failed = file == NULL;
    if (file) {
        failed = fwrite(data, 1, size, file) != size;
        if (fclose(file) != 0) failed = true;
    } else { close(fd); }
    if (!failed && rename(temporary, target) != 0) failed = true;
    if (failed) unlink(temporary);
    free(temporary);
    return failed ? -1 : 0;
}
static int32_t tm_os_write(int32_t argument, uint8_t *data, size_t size) {
    if (argument < 0 || argument >= host_argc) return -1;
    return tm_os_write_path(host_argv[argument], data, size, false);
}
static void tm_os_text(const char *text) { fputs(text, stdout); }
static void tm_os_integer(int64_t value) { printf("%" PRId64, value); }

#include "cli.h"

int main(int argc, char **argv) {
    host_argc = argc;
    host_argv = argv;
    int result = tm_cli_main(argc);
    if (fflush(stdout) != 0 || ferror(stdout)) result = 2;
    free(input_buffer);
    free(output_buffer);
    free(value_buffer);
    for (unsigned i = 0; i < 4; i++) free(scratch[i]);
    tm_runtime_tensor_free(tensor_workspace);
    tm_runtime_rpc_free(rpc_workspace);
    return result;
}
