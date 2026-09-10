/* Generic subprocess/filesystem ABI. RTL commands and verification are .t27. */
#define _GNU_SOURCE
#define _DARWIN_C_SOURCE
#define _POSIX_C_SOURCE 200809L
#include "process.h"
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <poll.h>
#include <spawn.h>
#include <signal.h>
#include <sys/wait.h>
#include <sys/stat.h>
#include <time.h>
#include <dlfcn.h>
#include <limits.h>
extern char **environ;
#define TM_PROCESS_SLOTS 128
struct process {
    char directory[PATH_MAX];
    char *args[TM_PROCESS_SLOTS];
    size_t argc;
    char *files[TM_PROCESS_SLOTS];
    size_t filec;
    void *buffers[TM_PROCESS_SLOTS];
    size_t bufferc;
};
static bool basename_ok(const char *name) {
    return name && *name && strcmp(name, ".") && strcmp(name, "..") &&
           strlen(name) < 256 && !strchr(name, '/');
}
static uint64_t now_ms(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) return 0;
    return (uint64_t)now.tv_sec * UINT64_C(1000) + (uint64_t)now.tv_nsec / UINT64_C(1000000);
}
void *tm_process_new(void) {
    struct process *p = calloc(1, sizeof(*p));
    if (!p) return NULL;
    strcpy(p->directory, "/tmp/trinity-t27-rtl-XXXXXX");
    if (!mkdtemp(p->directory)) { free(p); return NULL; }
    return p;
}
void *tm_process_buffer(void *pointer, size_t size) {
    struct process *p = pointer;
    if (!p || p->bufferc == TM_PROCESS_SLOTS) return NULL;
    void *data = calloc(size ? size : 1, 1);
    if (data) p->buffers[p->bufferc++] = data;
    return data;
}
int32_t tm_process_output(void *pointer, const char *name) {
    struct process *p = pointer;
    if (!p || !basename_ok(name)) return -1;
    for (size_t i = 0; i < p->filec; i++) if (!strcmp(name, p->files[i])) return 0;
    if (p->filec == TM_PROCESS_SLOTS) return -1;
    char *copy = strdup(name);
    if (!copy) return -1;
    p->files[p->filec++] = copy;
    return 0;
}
int32_t tm_process_write(void *pointer, const char *name, uint8_t *data, size_t size) {
    struct process *p = pointer;
    if ((!data && size) || tm_process_output(p, name)) return -1;
    char path[PATH_MAX];
    int n = snprintf(path, sizeof(path), "%s/%s", p->directory, name);
    if (n < 0 || (size_t)n >= sizeof(path)) return -1;
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW, 0600);
    if (fd < 0) return -1;
    size_t offset = 0;
    while (offset < size) {
        ssize_t wrote = write(fd, data + offset, size - offset);
        if (wrote < 0 && errno == EINTR) continue;
        if (wrote <= 0) { close(fd); return -1; }
        offset += (size_t)wrote;
    }
    return close(fd) ? -1 : 0;
}
void tm_process_reset(void *pointer) {
    struct process *p = pointer;
    if (!p) return;
    for (size_t i = 0; i < p->argc; i++) free(p->args[i]);
    p->argc = 0;
}
int32_t tm_process_arg(void *pointer, uint8_t *value, size_t size) {
    struct process *p = pointer;
    if (!p || !value || size == SIZE_MAX || p->argc == TM_PROCESS_SLOTS || memchr(value, 0, size)) return -1;
    char *arg = malloc(size + 1);
    if (!arg) return -1;
    memcpy(arg, value, size); arg[size] = 0;
    p->args[p->argc++] = arg;
    return 0;
}
int64_t tm_process_run(void *pointer, const char *program, uint8_t *output,
                      size_t capacity, int32_t timeout_ms) {
    struct process *p = pointer;
    if (!p || !program || !*program || (!output && capacity) || capacity > INT64_MAX ||
        timeout_ms <= 0 || timeout_ms > 3600000) return -1;
    int pipefd[2];
    if (pipe(pipefd)) return -1;
    posix_spawn_file_actions_t actions;
    posix_spawnattr_t attributes;
    if (posix_spawn_file_actions_init(&actions)) { close(pipefd[0]); close(pipefd[1]); return -1; }
    if (posix_spawnattr_init(&attributes)) {
        posix_spawn_file_actions_destroy(&actions); close(pipefd[0]); close(pipefd[1]); return -1;
    }
    #if defined(__APPLE__)
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
#endif
    int setup = posix_spawn_file_actions_addchdir_np(&actions, p->directory);
#if defined(__APPLE__)
#pragma clang diagnostic pop
#endif
    setup |= posix_spawn_file_actions_adddup2(&actions, pipefd[1], STDOUT_FILENO);
    setup |= posix_spawn_file_actions_adddup2(&actions, pipefd[1], STDERR_FILENO);
    setup |= posix_spawn_file_actions_addclose(&actions, pipefd[0]);
    setup |= posix_spawn_file_actions_addclose(&actions, pipefd[1]);
    setup |= posix_spawnattr_setflags(&attributes, POSIX_SPAWN_SETPGROUP);
    setup |= posix_spawnattr_setpgroup(&attributes, 0);
    char *argv[TM_PROCESS_SLOTS + 2];
    argv[0] = (char *)program;
    for (size_t i = 0; i < p->argc; i++) argv[i + 1] = p->args[i];
    argv[p->argc + 1] = NULL;
    pid_t pid = -1;
    int spawned = setup ? setup : posix_spawnp(&pid, program, &actions, &attributes, argv, environ);
    posix_spawnattr_destroy(&attributes);
    posix_spawn_file_actions_destroy(&actions);
    close(pipefd[1]);
    if (spawned) { close(pipefd[0]); return -1; }
    int flags = fcntl(pipefd[0], F_GETFL, 0);
    if (flags < 0 || fcntl(pipefd[0], F_SETFL, flags | O_NONBLOCK)) {
        kill(-pid, SIGKILL); close(pipefd[0]); while (waitpid(pid, NULL, 0) < 0 && errno == EINTR) {} return -1;
    }
    uint64_t deadline = now_ms() + timeout_ms;
    size_t used = 0;
    bool eof = false, reaped = false, failed = false;
    int status = 0;
    while (!eof || !reaped) {
        if (!reaped) {
            pid_t result = waitpid(pid, &status, WNOHANG);
            if (result == pid) reaped = true;
            else if (result < 0 && errno != EINTR) { failed = true; break; }
        }
        uint64_t now = now_ms();
        if (now >= deadline) { failed = true; break; }
        if (!eof) {
            uint8_t extra;
            ssize_t got = read(pipefd[0], used < capacity ? output + used : &extra,
                               used < capacity ? capacity - used : 1);
            if (got > 0) {
                if (used == capacity) { failed = true; break; }
                used += (size_t)got;
                continue;
            }
            if (!got) eof = true;
            else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) { failed = true; break; }
        }
        if (!eof || !reaped) {
            struct pollfd fd = {pipefd[0], POLLIN, 0};
            int delay = (int)(deadline - now > 20 ? 20 : deadline - now);
            if (poll(eof ? NULL : &fd, eof ? 0 : 1, delay) < 0 && errno != EINTR) { failed = true; break; }
        }
    }
    close(pipefd[0]);
    if (failed) kill(-pid, SIGKILL);
    if (!reaped) while (waitpid(pid, &status, 0) < 0) { if (errno != EINTR) { failed = true; break; } }
    return failed || !WIFEXITED(status) || WEXITSTATUS(status) ? -1 : (int64_t)used;
}
int64_t tm_process_resource(const char *name, uint8_t *output, size_t capacity) {
    if (!name || !*name || !output || strchr(name, '\\') || name[0] == '/' ||
        !strcmp(name, "..") || strstr(name, "../")) return -1;
    const char *override = getenv("TRINITY_T27_RTL_ROOT");
    if (!override) override = getenv("TRINITY_MEMORY_RTL_ROOT");
    char base[PATH_MAX], path[PATH_MAX];
    if (override && *override) {
        if (!realpath(override, base)) return -1;
    } else {
        Dl_info info;
        /* Use a data address to avoid a function/object pointer cast. */
        static const char anchor = 0;
        if (!dladdr(&anchor, &info) || !info.dli_fname || !realpath(info.dli_fname, base)) return -1;
        char *slash = strrchr(base, '/');
        if (!slash) return -1;
        *slash = 0;
        size_t len = strlen(base);
        if (len + sizeof("/rtl/resources") > sizeof(base)) return -1;
        memcpy(base + len, "/rtl/resources", sizeof("/rtl/resources"));
    }
    int n = snprintf(path, sizeof(path), "%s/%s", base, name);
    if (n < 0 || (size_t)n >= sizeof(path) || (size_t)n >= capacity) return -1;
    struct stat st;
    if (stat(path, &st) || !S_ISREG(st.st_mode)) return -1;
    memcpy(output, path, (size_t)n + 1);
    return n;
}
void tm_process_free(void *pointer) {
    struct process *p = pointer;
    if (!p) return;
    tm_process_reset(p);
    for (size_t i = 0; i < p->filec; i++) {
        char path[PATH_MAX];
        int n = snprintf(path, sizeof(path), "%s/%s", p->directory, p->files[i]);
        if (n >= 0 && (size_t)n < sizeof(path)) unlink(path);
        free(p->files[i]);
    }
    for (size_t i = 0; i < p->bufferc; i++) free(p->buffers[i]);
    rmdir(p->directory);
    free(p);
}
