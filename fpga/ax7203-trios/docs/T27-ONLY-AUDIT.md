# t27-only audit of this directory (owner rule of 2026-10-21)

Owner, 2026-10-21: "все пишем только на t27 запомни !" — all authored source is `.t27`; derived files only
through the repository's generators; a missing capability is reported as an exact gap, never replaced silently.
This is the state of `fpga/ax7203-trios/` at the commit that adds this file. Third-party, unmodified code is listed
separately and is not claimed as t27.

## Authored as t27 (checked by the pinned t27c, bff21b85, in CI)

| File | What | Check |
|---|---|---|
| `specs/trios/trios_uart_guard.t27` | RX queue bookkeeping, sticky overrun, quiet detection, frame CRC-8 | spec tests; generated to Verilog and used by the SoC; directed testbench `tb/tb_guard.v` (hand-written Verilog, see below) |
| `specs/trios/thermal_policy.t27` | immutable preflight limit (70 C); four-state controller; continuous `want_work` permission (dropping it stops work and cooling never ends in work without it); hysteresis over NEW sample numbers only (a re-read cached result counts once, a gap restarts the count); stale/invalid/timeout/cycle-limit handling; a pure watchdog counter and `run_enable` | 20 spec tests (sequences) after the review of 44cd005a, which found two safety bugs that the earlier 15 tests did not catch: (1) an early return on a repeated sample number ran before the stale/invalid/stop checks; (2) a watchdog kick cleared an already tripped watchdog, so the saved WORK state restarted the run. Fixed: safety conditions act on every call, the trip is latched and is re-armed only by an operator reset with the policy back in OBSERVE. Ten committed `.t27` mutants (`specs/trios/mutants/`, including both reviewed defects re-introduced as mutants 8 and 9) must each fail their own tests, and each may differ from the policy by only a few lines (CI checks both). Levels: pure policy tested; host supervisor NOT written; independent hardware stop NOT built, so nothing here is autonomous |
| `specs/trios/board_parse.t27`, `specs/trios/host_sys.t27` | in progress: strict parsers for the openFPGALoader text (temperature, IDCODE, DNA; 4 tests on t27b) and the libSystem imports for the board executor | `t27b test` in the host workflow |
| `specs/trios/host_smoke.t27` | the t27 host path: t27b from gHashTag/t27 master (extern boundary, PR 11324), Mach-O executable calling libSystem; exit code 0 only if write returned 3 | workflow `t27-host-ax7203-trios`. First two attempts failed: the emitted `_main` was a prologue plus `brk #10` (t27b TrapKind::Stub: `undefined;` or an ignored `expr;` in a function no test reaches), no call to write; the smoke now uses every call result and the job checks the disassembly for the call and for the absence of brk. Not yet a passing smoke until that run is green |

## Not t27, authored before the rule, kept as legacy and NOT extended

| File(s) | Language | Why still there | Exact gap |
|---|---|---|---|
| `rtl/trios_soc_core.v`, `rtl/trios_soc_ax7203.v` | Verilog | the SoC glue: PicoRV32 wrapper, bus, RAM, UART TX/RX, clock | pinned t27c cannot instantiate third-party Verilog (PicoRV32) or Xilinx primitives (IBUFDS, BUFG). The UART TX/RX can be replaced by the existing hardware-verified t27 cores `TrinityFpgaUartTxT27`/`RxT27` (t27/rtl) — next migration step, needs a new bitstream and board run. gHashTag/t27 has newer primitive support (specs/tri/t27b/verilog_plan.t27) not usable with the pinned compiler |
| `fw/src/main.rs`, startup assembly, `fw/link.ld` | Rust, RISC-V asm, linker script | the RV32I payload entry, MMIO, calls into the real trios-mesh crate | t27c `gen-rust` emits pure functions only (declarations, no tests); no t27 backend targets RV32 bare metal (t27b emits aarch64). Pure decisions (CRC-8, frame checks, thermal policy) can be generated to Rust and included; entry/startup/MMIO cannot |
| `host/mesh_run.py`, `mesh_vectors.py`, `make_mem.py`, `check_elf.py`, `check_timing.py`, `build_report.py`, `board_parse.py`, `test_board_parsers.py` | Python | CI checks and the UART vector runner | a t27 host runner needs t27b @extern (available on gHashTag/t27 master since PR 11324; the locally installed t27b binaries predate it: `unsupported construct ExprCall(@extern)`). The host workflow builds a current t27b (green in run 39645174840: smoke executable prints `ok`, rc 0, disassembly calls `write`); the board executor and the vector runner are the next t27 work |
| `tb/tb_guard.v`, `tb/tb_trios_soc.v` | Verilog testbenches | stimulus for the guard and the SoC | no stimulus generator in the pinned t27c beyond `test` blocks |
| `../../.github/workflows/fpga-ax7203-trios.yml`, `../../.github/workflows/t27-host-ax7203-trios.yml`, `trios_soc_ax7203.xdc` | YAML with embedded shell, XDC | CI orchestration, pin and clock constraints | CORRECTION: commit 12ba4994 extended the workflow by 68 lines of new shell (thermal checks, mutant generation with sed, t27b steps) and a second workflow was added; that is new hand-written shell, not legacy. There is no t27 generator of CI workflows or of mutants; the shell is listed here as a gap, not hidden, and holds no product logic: all pass/fail decisions are in `.t27` tests whose verdicts the shell only reads. XDC: t27b's Verilog path has pin attributes (`<port>_LOC`, `_IOSTANDARD`) and clock declarations (`<inst>_<out>_PERIOD_PS`) that replace it |
| `host/board_run.py` | Python | REMOVED in this commit | it had a pre-load limit I raised from 70 C to 100 C (a flaw: the board was loaded at 93.96 C). The gate now lives in `thermal_policy.t27`; no board load is made until a t27 executor applies it |

## The primitive backend that does exist (read in gHashTag/t27 d9737e1, to be verified at the pinned master)

`t27b verilog` (cli/t27b, specs/tri/t27b/verilog_plan.t27, verilog_emit.t27) instantiates device primitives and other modules,
takes pin attributes and clock periods from the spec, and declares inputs/inouts; `specs/physics/xc7_primitives.t27` declares
STARTUPE2, BUFG, BSCANE2, IBUFDS, PLLE2_ADV, ODDR (no XADC); `specs/trinet/eth_top_ax7203.t27` is an AX7203 board top in `.t27`
with no hand Verilog or XDC (gHashTag/t27 PR 8890). None of this exists in the pinned t27c bff21b85 (`gen-verilog-hir` leaves
instances empty). Whether PicoRV32 can be declared as an extern module, and whether XADC can be declared, is not verified. The CI probe
`t27b verilog` on those two specs runs in `t27-host-ax7203-trios`. The SoC top, the UART (existing t27 cores) and the bus/RAM
move to `.t27` on this path; that is the main remaining migration and needs a new bitstream and a board run.

## Third-party, unmodified (not claimed as t27)

`third_party/picorv32.v` (ISC, YosysHQ/picorv32 `ef203c2b…`), `fw/vendor` (`trios-mesh` from gHashTag/trios `70882158…`,
hashes in `fw/VENDOR-SHA256.txt`), the toolchain image `regymm/openxc7@sha256:35c91073…`, openFPGALoader.

## Quiescing the SoC safely (design constraints, nothing built)

- t27b's primitive list (`specs/physics/xc7_primitives.t27`) declares no XADC and no BUFGCE; nextpnr has a BUFGCE packer, but an XADC
  INIT writer was not found in the trees read. An XADC cell in Yosys does not prove end-to-end support: it must be checked on the
  pinned toolchain before it is relied on. Until then sensing is the host (`--read-xadc`) plus an independent hardware watchdog.
- PicoRV32 has `clk`/`resetn` and no clock enable; reset is synchronous. Gating only the CPU clock can leave a bus transaction
  (`mem_valid` without `mem_ready`) active and repeat side effects on resume. A stop therefore needs a protocol: refuse new
  requests at a safe boundary, complete or abort the transaction, keep the watchdog, the sensor path and the control UART alive, then
  hold the CPU in reset or gate a domain that has no outstanding side effects. Partial MMIO and half-received UART frames must be
  covered by simulation before any bitstream is built. Not implemented.
- If on-die sensing is unavailable the supervisor stays on the host; loss of the host must stop computation (the latched
  watchdog, specified in `thermal_policy.t27`), and a design with host sensing must not be described as on-die thermal protection.

## Cooling: what exists and what does not

- Policy (t27, tested): `thermal_policy.t27`. Limits are our test policy, not a datasheet rating.
- Fabric-side thermal control without the CPU needs the XADC primitive in the design; the pinned t27c cannot instantiate
  primitives and nextpnr-xilinx support for XADC is unverified. Not implemented.
- Fan control by GPIO: not confirmed for this board; no pin invented.
- JTAG shutdown/quiesce (JSHUTDOWN/JPROGRAM, UG470 v1.17): the AMD page returned only its title; the PDF text was not read.
  Not used, not claimed. openFPGALoader 1.1.1 on this Mac has `--read-xadc`, `--read-dna`, `--detect`, `--reset`; no shutdown option.
- The clock-enable throttle inside the SoC (stop the CPU on command) is a design change: `.t27` first, then generation, simulation, CI,
  and only then the board.
