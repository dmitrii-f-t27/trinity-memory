/* WASM ABI for the external format readers and writers; the implementation is
 * unchanged compiler output (formats.h). The exports are listed in
 * tools/build-t27-wasm.sh. Callers place their buffers above __heap_base and
 * grow the memory as needed. */
#include <stddef.h>
#include <stdint.h>
/* json.h needs this adapter only for JSON reals. The one exported reader that
 * parses JSON, tf_safetensors_find, reads headers of strings and integers; a
 * header with a real number stops the module here. */
double tm_json_strtod(uint8_t *text) { (void)text; __builtin_trap(); }
#include "json.h"
#include "formats.h"
/* The Ternary Check contract (t27/ternary_contract.t27): the strict reader and
 * writer behind the CLI contract, class tokens, comparison and verdicts. */
#include "ternary_contract.h"

/* Freestanding wasm32 has no compiler runtime. clang turns the u64 overflow
 * test in tf_times (a > max / b, then a * b) into a 128-bit multiply, which
 * it calls as __multi3; this is that routine, from 64-bit products only. */
typedef unsigned __int128 tf_wasm_u128;

static tf_wasm_u128 tf_wasm_mul64(uint64_t x, uint64_t y) {
    uint64_t x0 = (uint32_t)x, x1 = x >> 32, y0 = (uint32_t)y, y1 = y >> 32;
    uint64_t p00 = x0 * y0, p01 = x0 * y1, p10 = x1 * y0, p11 = x1 * y1;
    uint64_t mid = (p00 >> 32) + (uint32_t)p01 + (uint32_t)p10;
    uint64_t low = (mid << 32) | (uint32_t)p00;
    uint64_t high = p11 + (p01 >> 32) + (p10 >> 32) + (mid >> 32);
    return ((tf_wasm_u128)high << 64) | low;
}

tf_wasm_u128 __multi3(tf_wasm_u128 a, tf_wasm_u128 b) {
    uint64_t al = (uint64_t)a, ah = (uint64_t)(a >> 64), bl = (uint64_t)b, bh = (uint64_t)(b >> 64);
    return tf_wasm_mul64(al, bl) + ((tf_wasm_u128)(ah * bl + al * bh) << 64);
}

/* Field accessors for TFTensorInfo, so callers need not know its layout. */
uint32_t tf_wasm_info_size(void) { return (uint32_t)sizeof(TFTensorInfo); }

uint64_t tf_wasm_info_field(TFTensorInfo *info, uint32_t field) {
    switch (field) {
    case 0: return info->tensor_type;
    case 1: return info->dims;
    case 2: return info->d0;
    case 3: return info->d1;
    case 4: return info->d2;
    case 5: return info->d3;
    case 6: return info->data_start;
    case 7: return info->offset;
    case 8: return info->alignment;
    case 9: return info->needed;
    case 10: return info->prism;
    case 11: return info->bitnet;
    case 12: return info->next_offset;
    case 13: return info->tensors;
    case 14: return info->has_next;
    case 15: return info->prev_end;
    case 16: return info->has_prev;
    default: return UINT64_MAX;
    }
}

/* Sizes and field accessors for tf_safetensors_find's workspace and result. */
uint32_t tf_wasm_json_token_size(void) { return (uint32_t)sizeof(TMJsonToken); }
uint32_t tf_wasm_safe_info_size(void) { return (uint32_t)sizeof(TFSafeInfo); }

uint64_t tf_wasm_safe_info_field(TFSafeInfo *info, uint32_t field) {
    switch (field) {
    case 0: return (uint64_t)(uintptr_t)info->dtype;
    case 1: return info->dtype_size;
    case 2: return info->dims;
    case 3: return info->d0;
    case 4: return info->d1;
    case 5: return info->d2;
    case 6: return info->d3;
    case 7: return info->begin;
    case 8: return info->end;
    case 9: return info->needed;
    case 10: return info->dtype_bits;
    case 11: return info->next_begin;
    case 12: return info->has_next;
    case 13: return info->prev_end;
    case 14: return info->has_prev;
    default: return UINT64_MAX;
    }
}
