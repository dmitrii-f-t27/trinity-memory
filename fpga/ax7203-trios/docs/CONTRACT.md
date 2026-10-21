# TriOS mesh on the AX7203: board/runtime contract

Status: first measured milestone of "TriOS on the FPGA". It runs one existing TriOS
component, the `trios-mesh` packet/routing core, on a soft CPU. It is **not** the full
TriOS operating system (see "Not provided").

## Stage note (t27 chain) and actual ordering

Recorded as it happened, not as the chain prescribes:

1. The first versions of the RTL (`rtl/`), the firmware (`fw/`), the vectors (`host/mesh_vectors.py`) and this contract were
   written **before any `.t27` file existed**. That is a deviation from "spec before code"; no spec-first evidence is claimed
   for them.
2. An independent review found a receive-path defect (a received byte overwrote an unread one and nothing reported it). The
   fix was specified first: `t27/trios_uart_guard.t27` (queue bookkeeping, sticky overrun, quiet detection, and the frame
   CRC-8 with its check value 0xF4) was written, then the RTL glue and the firmware were changed to use it. So for the
   receive path the order is spec, then code, but only from this correction on.
3. What is generated and what is not: `TriosUartGuardT27` (Verilog) is generated from the `.t27` by the pinned t27c in CI
   (`native/compiler.lock`) and is never committed. Everything else is hand-written glue and is a scoped exception, because
   t27c has no backend for it: the PicoRV32 wrapper, byte slots, UART transmitter/receiver state machines, clocking
   (`rtl/*.v`), the Rust firmware and the host scripts. `third_party/picorv32.v` (ISC, YosysHQ/picorv32 `ef203c2b`) and
   `fw/vendor` (`trios-mesh`, gHashTag/trios `70882158`, hashes in `fw/VENDOR-SHA256.txt`) are unmodified copies.
4. Checks: the spec tests and the generated module are exercised in CI (`spec` job), including a directed testbench
   (`tb/tb_guard.v`); the CRC-8 in the firmware and in `host/mesh_vectors.py` is the spec's function and is checked by
   every frame of the vectors.

## Parts and pins

| Part | Pin/fact | Source |
|---|---|---|
| FPGA | XC7A200T-FBG484-2 (`xc7a200tfbg484-2`) | AX7203; fpga/ax7203/Makefile |
| 200 MHz LVDS clock | R4 / T4, DIFF_SSTL15 | fpga/ax7203/tms_trace_player.xdc (verified on the board) |
| UART | TX N15, RX P20, LVCMOS33, 115200 8N1 | same file |
| LEDs | B13, C13, D14, D15, LVCMOS18 | same file |
| Clock domains | `clk200` 200 MHz (oscillator BUFG, feeds only the 3-bit divider) and `clk25` 25 MHz (divider bit 2 on a second BUFG: CPU, RAM, UART), no PLL | same scheme as fpga/ax7203/tms_trace_player.v; both constrained with `create_clock` by net name in the XDC |

## CPU and ABI

PicoRV32, `ENABLE_MUL=0 ENABLE_DIV=0 COMPRESSED_ISA=0 ENABLE_COUNTERS=0 ENABLE_IRQ=0`: RV32I only. Rust target
`riscv32i-unknown-none-elf` (ilp32, no extensions). `host/check_elf.py` proves the ELF arch attribute is `rv32i...`
and that every instruction is in the RV32I base set.

## Memory map

| Address | Meaning |
|---|---|
| 0x0000_0000 – 0x0000_7FFF | 32 KiB RAM (block RAM, initialised from the firmware image); reset vector 0x0000_0000; stack top 0x8000, at least 6 KiB reserved |
| 0x1000_0000 | UART data: write = transmit byte, read = pop received byte |
| 0x1000_0004 | UART status: bit0 tx busy, bit1 rx valid, bit2 rx overrun |
| 0x1000_0008 | LED[1:0] |
| 0x1000_000C | BUILD_ID |

## Receive path

Bytes go through a 16-byte queue. A byte that arrives when the queue holds 16 is **dropped, never written over a queued
byte**, and sets the **sticky overrun flag** (status bit 2) and a drop counter. Reading the status register clears nothing;
the CPU clears the flag by writing bit 2 (framing error: bit 4) after it has resynchronised. Bit 3 (quiet) says the line
was idle with an empty queue for 64 character times (5.6 ms at 115200 baud).

Requests are framed: `0xA5, len (1..80), payload, CRC-8(len, payload)` (polynomial 0x07, init 0). The firmware executes a
frame only if it was received completely, the CRC matches and **no overrun or line error flag was raised at any point
while it was read**. Otherwise it discards the frame, drains input until the line is quiet, clears the flags and answers
`E 04 reason` (1 overrun, 2 timeout/truncated, 3 bad length, 4 bad CRC, 5 line framing error). Bytes before the sync byte
are skipped. A flag raised while the CPU was busy (for example computing a hash while the host keeps sending) is reported at
the next frame boundary, so lost bytes are never reported as a valid request; buffered frames that arrived before the
loss are refused too. The host must not pause more than about 5 ms inside a frame.

Not covered by a test in simulation: a low stop bit (line framing error, status bit 4) is implemented but the testbench
does not generate one.

## UART protocol (payload of a frame)

Binary, little-endian multi-byte fields, fixed response length per command, request/response one at a time (responses are
not framed). After reset the firmware prints `TRIOS-MESH 70882158 RV32I\n`.

| Request payload | Response |
|---|---|
| `V` | `V` + boot line |
| `N` + 16 B self id | `N 00` (new empty routing table) |
| `P` len raw | `P ok hops ser[34]`: `AnnounceHeader::parse`, then `serialise`; `ok=0` and zeros when parse is `None` |
| `A` len raw quality now(4) | `A status count cost(2)`: parse, `process_announce`; status 0 parse failed, 1 table changed, 2 not changed; cost = `route_cost_to(dest)` or 0xFFFF |
| `Q` dest(16) | `Q found next_hop(16) hops quality last_seen(4) count cost(2)`; `found=0` for self/unknown |
| `T` now(4) ttl(4) | `T count` after `expire` |
| `C` hops quality | `C cost(2)` (`route_cost`) |
| `H` pubkey(32) | `H` + `NodeIdentity::from_pubkey(..).dest_hash` |
| other command | `E 02 cmd`; wrong payload length for a known command: `E 05 cmd`; `P`/`A` raw longer than 64: `E 03 cmd` |

Failure cases covered: short or wrong-type packet, full table (17th route), unknown destination, unknown command, oversize
and mismatched lengths, truncated frame, bad CRC, bad length byte, garbage before sync, overflow burst with recovery.

## Acceptance vectors

`AV1`–`AV4` of `outputs/CLAUDE-FPGA-ACCEPTANCE.md` (packet positive/negative, route positive/negative with the expiry
boundary) plus the crate's own test scenarios, in `host/mesh_vectors.py`. The host never computes a result: it sends
bytes and compares with literals.

## Not provided

Operating-system kernel or scheduler, DDR3, Ethernet, flash persistence (SRAM load only: configuration is lost on
power-off), cryptographic signing/AEAD, physical radio, the Rust/TypeScript orchestrator, the `trios-vm` Zig VM, any UI.
