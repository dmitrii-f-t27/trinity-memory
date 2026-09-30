/* Issue #98: persistent I/O adapter to the actual pinned GGUF reader.
 * stdin: little-endian u64 header length, u64 sparse file size, header bytes.
 * stdout: one byte A (accepted) or R (refused) per packet. No loader rules
 * are restated here. A reader assertion terminates this worker; the driver
 * records its signal and starts another worker for the next case. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include "ggml.h"
#include "gguf.h"

static uint64_t u64(const unsigned char *p) {
    uint64_t value = 0;
    for (unsigned i = 0; i < 8; ++i) value |= (uint64_t)p[i] << (8 * i);
    return value;
}

int main(void) {
    FILE *file = tmpfile();
    if (!file) return 70;
    unsigned char packet[16], header[65536];
    size_t count;
    while ((count = fread(packet, 1, sizeof packet, stdin)) != 0) {
        if (count != sizeof packet) return 74;
        uint64_t length = u64(packet), size = u64(packet + 8);
        if (length > sizeof header || size > (UINT64_C(1) << 40)) return 64;
        if (fread(header, 1, (size_t)length, stdin) != length) return 74;
        if (ftruncate(fileno(file), 0) || fseek(file, 0, SEEK_SET)) return 74;
        if (fwrite(header, 1, (size_t)length, file) != length || fflush(file)) return 74;
        if (ftruncate(fileno(file), (off_t)size) || fseek(file, 0, SEEK_SET)) return 74;
        struct ggml_context *meta = NULL;
        struct gguf_init_params params = { true, &meta };
        struct gguf_context *ctx = gguf_init_from_file_ptr(file, params);
        int accepted = ctx != NULL;
        if (ctx) gguf_free(ctx);
        if (meta) ggml_free(meta);
        if (putchar(accepted ? 'A' : 'R') == EOF || fflush(stdout)) return 74;
    }
    fclose(file);
    return ferror(stdin) ? 74 : 0;
}
