"""Build-time byte asset encoding only; no codec or report algorithms."""
import base64
from pathlib import Path
import sys
payload = base64.b64encode(Path(sys.argv[1]).read_bytes()).decode("ascii")
Path(sys.argv[2]).write_text('#include <stdint.h>\n#include <stddef.h>\nstatic uint8_t wasm[] =\n' +
    '\n'.join('"' + payload[i:i + 100] + '"' for i in range(0, len(payload), 100)) +
    ';\nuint8_t *tm_report_wasm_base64(void) { return wasm; }\n'
    'size_t tm_report_wasm_base64_size(void) { return sizeof(wasm) - 1; }\n', encoding="ascii")
