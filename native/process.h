#ifndef TRINITY_MEMORY_PROCESS_H
#define TRINITY_MEMORY_PROCESS_H
#include <stddef.h>
#include <stdint.h>
void *tm_process_new(void);
void *tm_process_buffer(void *process, size_t size);
int32_t tm_process_write(void *process, const char *name, uint8_t *data, size_t size);
int32_t tm_process_output(void *process, const char *name);
int32_t tm_process_arg(void *process, uint8_t *value, size_t size);
void tm_process_reset(void *process);
int64_t tm_process_run(void *process, const char *program, uint8_t *output,
                       size_t capacity, int32_t timeout_ms);
int64_t tm_process_resource(const char *name, uint8_t *output, size_t capacity);
void tm_process_free(void *process);
#endif
