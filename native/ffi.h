#ifndef TRINITY_MEMORY_FFI_H
#define TRINITY_MEMORY_FFI_H
#include <stddef.h>
#include <stdint.h>
uint64_t tm_float_bits(double value);
double tm_json_strtod(uint8_t *text);
size_t tm_float_shortest(double value, uint8_t *output, size_t capacity);
int32_t tm_os_random(uint8_t *output, size_t size);
int32_t tm_os_sha256(uint8_t *data, size_t size, uint8_t *output);
#endif
