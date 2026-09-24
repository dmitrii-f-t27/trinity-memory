/* Platform crypto, entropy, clock and serial primitives. Container/RPC logic
 * and the device link protocol (t27/fpga_link.t27) are in t27. */
#if defined(__wasm__)
/* WebAssembly has no serial port or clock host here: the serial hooks fail
 * cleanly (-1) and the clock reads 0. The crypto hooks are not built for wasm. */
#include <stddef.h>
#include <stdint.h>
int32_t tm_os_serial_open(uint8_t *path, size_t size, uint32_t baud) { (void)path; (void)size; (void)baud; return -1; }
int64_t tm_os_serial_read(int32_t fd, uint8_t *output, size_t capacity, uint32_t timeout_ms) {
    (void)fd; (void)output; (void)capacity; (void)timeout_ms; return -1;
}
int64_t tm_os_serial_write(int32_t fd, uint8_t *data, size_t size, uint32_t timeout_ms) {
    (void)fd; (void)data; (void)size; (void)timeout_ms; return -1;
}
int32_t tm_os_serial_drain(int32_t fd) { (void)fd; return -1; }
int32_t tm_os_serial_close(int32_t fd) { (void)fd; return -1; }
uint64_t tm_os_monotonic_ns(void) { return 0; }
#else
#define _POSIX_C_SOURCE 200809L
/* The termios rates above 38400 (B57600 .. B921600) are extensions: visible with these. */
#if defined(__APPLE__)
#define _DARWIN_C_SOURCE
#else
#define _DEFAULT_SOURCE
#endif
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <termios.h>
#include <time.h>
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

uint64_t tm_os_monotonic_ns(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) return 0;
    return (uint64_t)now.tv_sec * UINT64_C(1000000000) + (uint64_t)now.tv_nsec;
}

/* Remaining milliseconds to a monotonic deadline, 0 when it has passed. */
static int remaining_ms(uint64_t deadline) {
    uint64_t now = tm_os_monotonic_ns();
    if (now >= deadline) return 0;
    uint64_t left = (deadline - now + 999999) / 1000000;
    return left > INT_MAX ? INT_MAX : (int)left;
}

static int baud_constant(uint32_t baud, speed_t *speed) {
    switch (baud) {
    case 9600: *speed = B9600; return 0;
    case 19200: *speed = B19200; return 0;
    case 38400: *speed = B38400; return 0;
#if defined(B57600)
    case 57600: *speed = B57600; return 0;
#endif
#if defined(B115200)
    case 115200: *speed = B115200; return 0;
#endif
#if defined(B230400)
    case 230400: *speed = B230400; return 0;
#endif
#if defined(B460800)
    case 460800: *speed = B460800; return 0;
#endif
#if defined(B921600)
    case 921600: *speed = B921600; return 0;
#endif
    default: return -1;
    }
}

/* Open a serial device (path: size bytes, no NUL inside) at `baud`, 8N1, raw, no flow
 * control, non-blocking; pending input is discarded. Returns the descriptor or -1 (no such
 * path, not a terminal, unsupported rate, or a driver that refuses the settings). */
int32_t tm_os_serial_open(uint8_t *path, size_t size, uint32_t baud) {
    char name[1024];
    speed_t speed;
    if (!path || size == 0 || size >= sizeof name || memchr(path, 0, size) || baud_constant(baud, &speed) != 0) return -1;
    memcpy(name, path, size);
    name[size] = 0;
    int fd;
    do { fd = open(name, O_RDWR | O_NOCTTY | O_NONBLOCK | O_CLOEXEC); } while (fd < 0 && errno == EINTR);
    if (fd < 0) return -1;
    struct termios t;
    if (tcgetattr(fd, &t) != 0) { close(fd); return -1; }
    t.c_iflag &= ~(tcflag_t)(IGNBRK | BRKINT | PARMRK | ISTRIP | INLCR | IGNCR | ICRNL | IXON | IXOFF | IXANY | INPCK);
    t.c_oflag &= ~(tcflag_t)OPOST;
    t.c_lflag &= ~(tcflag_t)(ECHO | ECHONL | ICANON | ISIG | IEXTEN);
    t.c_cflag &= ~(tcflag_t)(CSIZE | PARENB | CSTOPB);
#if defined(CRTSCTS)
    t.c_cflag &= ~(tcflag_t)CRTSCTS;
#endif
    t.c_cflag |= CS8 | CLOCAL | CREAD;
    t.c_cc[VMIN] = 0;
    t.c_cc[VTIME] = 0;
    if (cfsetispeed(&t, speed) != 0 || cfsetospeed(&t, speed) != 0 || tcsetattr(fd, TCSANOW, &t) != 0) {
        close(fd);
        return -1;
    }
    (void)tcflush(fd, TCIFLUSH);
    return fd;
}

/* Read at most `capacity` bytes, waiting up to timeout_ms for the first one. Returns the
 * count (a partial read is normal), 0 when nothing arrived in time, -1 on an error or a
 * hang-up. A signal does not end the wait early. */
int64_t tm_os_serial_read(int32_t fd, uint8_t *output, size_t capacity, uint32_t timeout_ms) {
    if (fd < 0 || !output || capacity == 0) return -1;
    uint64_t deadline = tm_os_monotonic_ns() + (uint64_t)timeout_ms * UINT64_C(1000000);
    for (;;) {
        ssize_t n = read(fd, output, capacity);
        if (n > 0) return (int64_t)n;
        if (n == 0) {
            /* No data (VMIN = 0) or, after POLLIN, end of file: poll tells which below. */
        } else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
            return -1;
        }
        int wait = remaining_ms(deadline);
        if (wait == 0) return 0;
        struct pollfd p = {fd, POLLIN, 0};
        int ready = poll(&p, 1, wait);
        if (ready < 0 && errno == EINTR) continue;
        if (ready < 0) return -1;
        if (ready == 0) continue;
        if (p.revents & (POLLERR | POLLNVAL)) return -1;
        if (p.revents & POLLIN) {
            n = read(fd, output, capacity);
            if (n > 0) return (int64_t)n;
            if (n == 0) return -1;
            if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) return -1;
            continue;
        }
        if (p.revents & POLLHUP) return -1;
    }
}

/* Write all `size` bytes, waiting up to timeout_ms in total. Partial writes and signals are
 * resumed. Returns size, or -1 on an error or when the time ran out. */
int64_t tm_os_serial_write(int32_t fd, uint8_t *data, size_t size, uint32_t timeout_ms) {
    if (fd < 0 || (!data && size)) return -1;
    uint64_t deadline = tm_os_monotonic_ns() + (uint64_t)timeout_ms * UINT64_C(1000000);
    size_t done = 0;
    while (done < size) {
        ssize_t n = write(fd, data + done, size - done);
        if (n > 0) { done += (size_t)n; continue; }
        if (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) return -1;
        int wait = remaining_ms(deadline);
        if (wait == 0) return -1;
        struct pollfd p = {fd, POLLOUT, 0};
        int ready = poll(&p, 1, wait);
        if (ready < 0 && errno != EINTR) return -1;
        if (ready > 0 && (p.revents & (POLLERR | POLLNVAL | POLLHUP))) return -1;
    }
    return (int64_t)size;
}

/* Wait until the OS has sent everything written (tcdrain). */
int32_t tm_os_serial_drain(int32_t fd) {
    if (fd < 0) return -1;
    for (;;) {
        if (tcdrain(fd) == 0) return 0;
        if (errno != EINTR) return -1;
    }
}

int32_t tm_os_serial_close(int32_t fd) {
    if (fd < 0) return -1;
    return close(fd) == 0 ? 0 : -1;
}
#endif
