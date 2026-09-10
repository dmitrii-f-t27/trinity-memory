/* Platform crypto and entropy primitives. Container/RPC logic is in t27. */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <stddef.h>
#include <stdint.h>
#include <unistd.h>
#if defined(__APPLE__)
#include <CommonCrypto/CommonDigest.h>
#else
#include <openssl/sha.h>
#endif

int32_t tm_os_random(uint8_t *output, size_t size) {
    int fd = open("/dev/urandom", O_RDONLY);
    if (fd < 0) return -1;
    size_t done = 0;
    while (done < size) {
        ssize_t count = read(fd, output + done, size - done);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) { close(fd); return -1; }
        done += (size_t)count;
    }
    return close(fd) == 0 ? 0 : -1;
}

int32_t tm_os_sha256(uint8_t *data, size_t size, uint8_t *output) {
#if defined(__APPLE__)
    if (size > UINT32_MAX) return -1;
    return CC_SHA256(data, (CC_LONG)size, output) ? 0 : -1;
#else
    return SHA256(data, size, output) ? 0 : -1;
#endif
}
