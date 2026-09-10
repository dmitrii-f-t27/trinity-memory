/* Browser ABI buffers; codec implementation is unchanged compiler output. */
#include "codecs.h"
static int32_t browser_values[5];
static uint8_t browser_output[8];
int32_t *tm_wasm_values(void) { return browser_values; }
uint8_t *tm_wasm_output(void) { return browser_output; }
