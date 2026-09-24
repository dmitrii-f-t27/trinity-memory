/* Independent replay for Ternary Check Live (issues #48, #55): feed a GGUF
 * header cached by trinity_memory.live to a pinned upstream gguf.cpp and
 * report whether that reader accepts the file. The header is written at the
 * start of a sparse file of the real file size, so every offset gguf.cpp
 * computes is the one it would compute on the real file; tensor data are
 * never read (no_alloc), so the loader's file-end rule is not part of the
 * replay. Built against each runtime pinned in specs/runtimes/ by
 * tools/live-replay.sh. Usage: gguf_replay HEADER FILE_SIZE */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include "ggml.h"
#include "gguf.h"

int main(int argc, char **argv) {
    if (argc != 3) { fprintf(stderr, "usage: %s HEADER FILE_SIZE\n", argv[0]); return 64; }
    FILE *in = fopen(argv[1], "rb");
    if (!in) { perror(argv[1]); return 66; }
    const char *dir = getenv("TMPDIR");
    char path[4096];
    snprintf(path, sizeof path, "%s/gguf-replay-XXXXXX", dir && *dir ? dir : "/tmp");
    int fd = mkstemp(path);
    if (fd < 0) { perror("mkstemp"); return 70; }
    char buf[1 << 16];
    size_t n;
    while ((n = fread(buf, 1, sizeof buf, in)) > 0) {
        if (write(fd, buf, n) != (ssize_t) n) { perror("write"); close(fd); unlink(path); return 74; }
    }
    fclose(in);
    long long size = atoll(argv[2]);
    if (ftruncate(fd, size) != 0) { perror("ftruncate"); close(fd); unlink(path); return 74; }
    close(fd);
    struct ggml_context *meta = NULL;
    struct gguf_init_params params = { /*.no_alloc =*/ true, /*.ctx =*/ &meta };
    struct gguf_context *ctx = gguf_init_from_file(path, params);
    unlink(path);
    if (!ctx) { printf("REFUSED\n"); return 2; }
    printf("ACCEPTED tensors=%lld\n", (long long) gguf_get_n_tensors(ctx));
    gguf_free(ctx);
    if (meta) ggml_free(meta);
    return 0;
}
