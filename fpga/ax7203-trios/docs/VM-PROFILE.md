# TriOS VSAVM P1 on AX7203

Source contract: `gHashTag/trios@70882158c9ff07c1157ff8a1a50ce39ee172fb3c`,
`crates/trios-vm/src/vm.zig` and `crates/trios-hybrid/src/hybrid_impl.zig`.
New implementation is authored in `specs/trios/vsavm.t27` and `vm_firmware.t27`.
Generated Zig, object files and bitstreams are compiler outputs, never edited.

P1 is a bounded VM runtime, **not a complete TriOS operating system**. It has four
vector registers and scalar s0. Signed binary representation preserves balanced
ternary integer arithmetic within 39 trits, ±2026277576509488133. Programs hold
at most 64 instructions. Supported original opcodes: 0 load, 1 store to s0,
2 constant, 11 add, 12 negate, 13 multiply, 14 move, 24 nop, 25 halt.
Other opcodes fail with error 1; overflow fails with error 2 before committing
the destination or advancing PC/cycles. No JIT, vector similarity, floating
operations, scheduler, filesystem, GUI or persistent boot is claimed.

`beginProgram` preserves registers and resets PC, cycles, halt and error. Invalid
register indices alias v0 as in the original source. Halt increments PC/cycles;
program exhaustion does not set the halt flag. There are no branches in P1, so
run executes at most 64 instructions and returns to the UART monitor.

## Wire protocol

115200 baud, 8N1. One request at a time: byte A5, one-byte payload length, payload,
CRC-8 over length and payload (polynomial 07, initial value zero).
Firmware validates the complete frame before mutation. Existing UART queue,
framing-error and quiet-timeout guards remain in use.

| Payload | Response | Bytes |
|---|---|---:|
| ASCII V | `VTRIOS-VSAVM 70882158 T27 RV32I P1\n` | 35 |
| Z | Z, 0; cold VM reset | 2 |
| B | B, 0; begin empty program, preserving registers | 2 |
| A, op, dst, src1, src2, signed imm64 LE | A, status (0 accepted; 3 full) | 2 |
| R | R, VM error; execute loaded program | 2 |
| S | S, VM error; one step | 2 |
| Q | Q, error, halted, program length, pc64, cycles64, s0, v0, v1, v2, v3 | 60 |

All seven Q words are little endian; values are signed two's complement where
applicable. PC is widened from u32. Boot banner is the V response without its
leading V (34 bytes). Invalid command/length returns E,1,command. Bad frames
return E,4,reason after draining to quiet; reason 1 line/queue fault, 2 interrupted
payload, 3 bad length, 4 CRC/missing CRC. A failed frame never executes a command.

Responses have fixed lengths and no CRC. Read exactly the documented length and
compare all bytes; binary Q data can contain arbitrary delimiter bytes. Timeout,
extra bytes or uncertain delivery invalidate the session. Never blindly retry A:
it appends non-idempotently. Recover with B and a complete reupload, then check Q
program length and state. Tests use independent expected states, not the VM as
its own oracle.

## Build and acceptance

The existing PicoRV32 RV32I SoC, 32 KiB RAM, UART, board pins and 25 MHz clock are
reused unchanged. Existing startup assembly is mechanically extracted unchanged
from `fw/src/main.rs`; it initializes SP and BSS and calls trios_main. Generated
Zig keeps the entry through an exported erased pointer; llvm-objcopy globalizes
the emitted zero-argument entry for the existing startup. No new foreign-language
shim is authored. Zig's compiler runtime supplies software multiplication,
shifts and memory routines; section GC retains only the required routines.

Required evidence: source tests, RV32I/ABI/memory checks and no GP/TP dependency,
whole-SoC UART simulation, timing-qualified bitstream at exact SHA, fresh board
identity and temperature strictly below 70 C, then real UART execution of multiple
uploaded programs. A banner or host-only test is not board acceptance. SRAM only;
flash remains unchanged. Autonomous cooling is not supplied by this VM port.
