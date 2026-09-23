/* Freestanding stand-in for <assert.h> in the WASM build (tools/build-t27-wasm.sh).
 * Generated headers include <assert.h> for their test blocks; the exported
 * readers never assert. A failed assertion traps, as it aborts natively. */
#ifndef TRINITY_WASM_ASSERT_H
#define TRINITY_WASM_ASSERT_H
#define assert(condition) do { if (!(condition)) __builtin_trap(); } while (0)
#endif
