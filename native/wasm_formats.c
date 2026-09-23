/* WASM ABI for the external format readers and writers; the implementation is
 * unchanged compiler output (formats.h). The exports are listed in
 * tools/build-t27-wasm.sh. Callers place their buffers above __heap_base and
 * grow the memory as needed. */
#include <stddef.h>
#include <stdint.h>
/* json.h needs this adapter only for JSON reals; the exported readers parse no
 * JSON (safetensors lookup is not exported). */
double tm_json_strtod(uint8_t *text) { (void)text; __builtin_trap(); }
#include "json.h"
#include "formats.h"

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
    default: return UINT64_MAX;
    }
}
