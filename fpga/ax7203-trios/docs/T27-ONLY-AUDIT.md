# t27-only audit of this directory (owner rule of 2026-10-21)

Owner, 2026-10-21: "все пишем только на t27 запомни !" — all authored source is `.t27`; derived files only
through the repository's generators; a missing capability is reported as an exact gap, never replaced silently.
This is the state of `fpga/ax7203-trios/` at the commit that adds this file. Third-party, unmodified code is listed
separately and is not claimed as t27.

## Update: Codex direct implementation, 2026-10-21

The owner explicitly replaced Claude delegation for this task. New source remains `.t27`.
The earlier bare-metal limitation below is superseded by a verified path:
`vsavm.t27` + `vm_firmware.t27` -> `t27c gen` -> generated Zig 0.15.2 -> RV32I object.
Volatile MMIO uses `@Type`; an exported erased code pointer retains the entry, and
llvm-objcopy globalizes it for the unchanged existing startup. Linked image is 3244
bytes; existing ELF checker confirms RV32I, soft-float ABI, reset0, stack0x8000.
Five source tests passed locally. Firmware uses unmodified Zig compiler-runtime
helpers, retained through section GC. There is no new hand-written Rust/C/asm shim.

`vm_vectors.t27` generates independent UART data and checks captured responses.
`serial_runner.t27` is the native host UART executor, with a shared lock, exact
responses, deadlines, no request retries and actual-byte capture. Its no-config
path and parser were tested without opening hardware. `board_gate.t27` now supports
SRAM loading after fresh identity and strict <70 C preflight, keeping the same lock
through loading and the post-temperature read; it still supplies no independent
thermal actuator.

The new VM workflow is emitted by compiling/running `vm_ci.t27`; its YAML and tool
invocations are metadata in the `.t27` source. This is a metadata emitter, not a
new typed CI language. Legacy RTL, XDC, testbench, linker script and Python checks
are reused unchanged and remain explicitly non-t27. The earlier handwritten
workflows and historical compliance gaps are not retrospectively erased.

At source967447a, whole-SoC simulation and FPGA CI are pending. This section makes
no claim of a new board run or full operating-system boot; use the physical receipt.

## Authored as t27 (checked by the pinned t27c, bff21b85, in CI)

| File | What | Check |
|---|---|---|
| `specs/trios/trios_uart_guard.t27` | RX queue bookkeeping, sticky overrun, quiet detection, frame CRC-8 | spec tests; generated to Verilog and used by the SoC; directed testbench `tb/tb_guard.v` (hand-written Verilog, see below) |
| `specs/trios/thermal_policy.t27` | immutable preflight limit (70 C); four-state controller; continuous `want_work` permission (dropping it stops work and cooling never ends in work without it); hysteresis over NEW sample numbers only (a re-read cached result counts once, a gap restarts the count); stale/invalid/timeout/cycle-limit handling; a pure watchdog counter and `run_enable` | 22 spec tests (sequences) after the review of 44cd005a, which found two safety bugs that the earlier 15 tests did not catch: (1) an early return on a repeated sample number ran before the stale/invalid/stop checks; (2) a watchdog kick cleared an already tripped watchdog, so the saved WORK state restarted the run. Fixed: safety conditions act on every call, the trip is latched and is re-armed only by an operator reset with the policy back in OBSERVE. Twenty-two committed `.t27` mutants (`specs/trios/mutants/`, including both reviewed defects re-introduced as mutants 8 and 9) must each fail their own tests on both backends, and each may differ from the policy by only a few lines (CI checks both). Levels: pure policy tested; host supervisor NOT written; independent hardware stop NOT built, so nothing here is autonomous |
| `specs/trios/board_gate.t27` | the board executor, read-only part, in t27 for t27b: board lock, `openFPGALoader` in a pty with a 15 s deadline, then the tested parsers and the thermal policy: preflight (IDCODE, DNA vs a private file, temperature < 70 C) and a temperature series driven by `step`/`wd_step`. Refuses on tool exit status, timeout, empty or filled buffer, wrong IDCODE, wrong DNA, unreadable temperature. No load yet | 3 tests + 5 mutants on t27b; the host workflow builds and links it and runs its no-board refusal paths (status 4). Run against the real board (read-only): preflight ALLOW at 38.9 C, 12-sample idle series |
| `specs/trios/board_parse.t27`, `specs/trios/host_sys.t27` | in progress: strict fail-closed parsers for the openFPGALoader text (temperature, IDCODE, DNA: exactly one key, a received terminator, no truncated or malformed tail; 16 tests on t27b and eighteen committed mutants that must fail, including the reviewed 'number ends the buffer' defect; also the SHA-256 line parser and the expected-hash validator used by `load`) and the libSystem imports for the board executor | `t27b test` in the host workflow |
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

## Round 2 (2026-10-21): what changed, and the function → tests → mutant table

Defects fixed in `board_gate.t27` (found by independent review, evidence below; the board was not touched):

1. **Deadline.** The 15 s deadline covered only the pty read loop. After EOF or a full buffer the executor called
   `waitpid(pid, ..., 0)` with no limit. Now `spawn_capture` waits with `WNOHANG` under the same deadline, kills the
   child when it passes (`wait_action` = 2), and a child still not reaped 3 s after the kill ends the executor with status 6
   and leaves the lock in place (a tool that cannot be stopped is a reason not to touch the board). The decision is the pure
   function `wait_action` (+ `deadline_passed`) with a unit test and mutants 06–09.
   Evidence without a board: hashing a FIFO with no writer blocks `shasum` forever; the executor killed it at the deadline, returned
   status 4 after 15 s, left no process and no lock (run locally, repeated in CI).
2. **`series` without identity.** It read only the temperature. Now `identity_check` (IDCODE, then DNA against the private file)
   runs before the first sample in every mode, `TRIOS_EXPECT_DNA_FILE` is required in every mode (`identity_required`, mutants 10–11),
   and the no-board CI run asserts status 4 for `series` without it.
3. **`load` not tied to a verified bitstream.** `absolute_bit_path` only checks syntax. Now `load` requires
   `TRIOS_EXPECT_BITSTREAM_SHA256` (64 hex digits, `expected_hash_valid`), hashes the file with `/usr/bin/shasum -a 256` through the same
   deadline-bound capture, compares with `sha256_line_matches`, and refuses with status 4 before the board lock and before any
   board operation on a missing variable, malformed value, unreadable file or different hash. The file is hashed again after the
   load, which detects a swap during the run (it cannot prevent one: a file can be swapped between the first hash and the loader
   opening it; on a single-user machine this is accepted and recorded, not claimed solved).

Method of the table below: for every mutant the tool (t27b for `board_parse`, `thermal_policy`, `board_gate`; t27c + Zig for `vsavm`, which uses
`i128`, unsupported by t27b) was run; "tests that call it" is a text search of the test bodies (a name inside a string literal counts,
and it is not a coverage measurement); "none directly" marks plumbing that only the no-board CI runs and the board runs exercise.
Result: **72 mutants, 0 survivors** (board_parse 18, thermal_policy 22, board_gate 14, vsavm 18). That is a statement about
these 72 mutants, not a proof that no other mutation survives; functions with "none" in the mutant column have no targeted mutant.
Known unmutated or unit-test-free safety-relevant code: `spawn_capture`/`run_loader`/`bitstream_matches`/`identity_check`/`preflight`/`series`/`main`
(plumbing; covered by the no-board status checks and by the physical runs only), the `pack` field accessors `state_of`, `good_of`, `cycles_of`, `work_allowed`
(exercised by every thermal test, no dedicated mutant), `find`.

### board_parse: 16 tests, 18 mutants

| function | tests that call it | mutants aimed at it |
|---|---|---|
| `find` | none directly | none |
| `count` | 1 test(s): one_key_counted_once_and_two_keys_counted_twice | 15 |
| `is_digit` | none directly | 17 |
| `hex_val` | none directly | 13, 18 |
| `ends_number` | none directly | 14 |
| `ends_token` | none directly | none |
| `parse_temp_mc` | 6 test(s): temperature_formats_seen_from_the_installed_tool, a_carriage_return_after_the_number_is_a_terminator ... | 01, 04, 12 |
| `parse_idcode` | 3 test(s): idcode_formats, idcode_truncation_garbage_and_duplicates_are_refused ... | 02, 05 |
| `dna_at` | 3 test(s): dna_formats, dna_truncation_garbage_and_duplicates_are_refused ... | 03 |
| `hex64_value` | 3 test(s): dna_formats, the_expected_dna_file_token ... | 16 |
| `bare_hex64_ok` | 1 test(s): the_expected_dna_file_token | 11 |
| `expected_hash_valid` | 1 test(s): the_expected_hash_must_be_exactly_64_hex_digits | 08 |
| `sha256_line_matches` | 2 test(s): the_shasum_line_must_carry_the_expected_hash, the_shasum_line_refusals | 06, 07, 09, 10 |
| `NOT_FOUND` | 2 test(s): dna_formats, dna_truncation_garbage_and_duplicates_are_refused | none |
| `FOUND` | none directly | none |

| mutant | what is broken | function(s) | first failing test | caught |
|---|---|---|---|---|
| board_parse_mut01 | a temperature that ends the buffer is accepted | parse_temp_mc | a_carriage_return_after_the_number_is_a_terminator, truncated_temperature_is_refused_at_every_point | yes |
| board_parse_mut02 | IDCODE accepted without a terminator | parse_idcode | idcode_truncation_garbage_and_duplicates_are_refused | yes |
| board_parse_mut03 | DNA accepted without the closing quote | dna_at | dna_truncation_garbage_and_duplicates_are_refused | yes |
| board_parse_mut04 | duplicate temperature keys accepted | parse_temp_mc | corrupted_tail_and_duplicate_keys_are_refused | yes |
| board_parse_mut05 | duplicate idcode keys accepted | parse_idcode | idcode_truncation_garbage_and_duplicates_are_refused | yes |
| board_parse_mut06 | only 63 of the 64 hash characters compared | sha256_line_matches | the_shasum_line_must_carry_the_expected_hash | yes |
| board_parse_mut07 | the separator after the hash is not required | sha256_line_matches | the_shasum_line_refusals | yes |
| board_parse_mut08 | an expected hash of any length is valid | expected_hash_valid | the_expected_hash_must_be_exactly_64_hex_digits, the_shasum_line_refusals | yes |
| board_parse_mut09 | the expected hash is not validated before the comparison | sha256_line_matches | the_shasum_line_refusals | yes |
| board_parse_mut10 | hash comparison is case sensitive | sha256_line_matches | the_shasum_line_must_carry_the_expected_hash | yes |
| board_parse_mut11 | trailing garbage accepted after the expected DNA token | bare_hex64_ok | the_expected_dna_file_token | yes |
| board_parse_mut12 | a four-digit temperature accepted | parse_temp_mc | temperature_refusals | yes |
| board_parse_mut13 | lower-case hex digits map to the wrong value | hex_val | dna_formats, the_expected_dna_file_token, the_expected_hash_must_be_exactly_64_hex_digits +2 | yes |
| board_parse_mut14 | a carriage return does not end a number | ends_number | a_carriage_return_after_the_number_is_a_terminator | yes |
| board_parse_mut15 | key occurrences are not counted | count | corrupted_tail_and_duplicate_keys_are_refused, idcode_truncation_garbage_and_duplicates_are_refused, dna_truncation_garbage_and_duplicates_are_refused +1 | yes |
| board_parse_mut16 | a 64-bit hex value is computed with the wrong radix | hex64_value | dna_formats, the_expected_dna_file_token, hex_digits_map_to_their_values | yes |
| board_parse_mut17 | a colon counts as a digit | is_digit | characters_next_to_the_digits_are_not_digits | yes |
| board_parse_mut18 | upper-case hex digits map to the wrong value | hex_val | the_expected_hash_must_be_exactly_64_hex_digits, the_shasum_line_must_carry_the_expected_hash, hex_digits_map_to_their_values | yes |

### thermal_policy: 22 tests, 22 mutants

| function | tests that call it | mutants aimed at it |
|---|---|---|
| `plausible` | 1 test(s): plausibility_edges | 02, 19, 20 |
| `preflight_allows` | 2 test(s): preflight_limit_is_strict, preflight_refuses_bad_readings | 16 |
| `pack` | 17 test(s): dropping_want_work_stops_work_at_once, cooling_enters_at_the_threshold_and_stops_work ... | 22 |
| `state_of` | 16 test(s): observe_waits_for_the_operator_and_a_cool_die, dropping_want_work_stops_work_at_once ... | none |
| `good_of` | 4 test(s): one_cached_result_read_again_counts_once, a_gap_in_the_sample_numbers_restarts_the_count ... | none |
| `cycles_of` | 2 test(s): cooling_enters_at_the_threshold_and_stops_work, the_cycle_limit_ends_in_an_error | none |
| `seq_of` | 1 test(s): sample_numbers_wrap_at_256_and_still_count_as_consecutive | none |
| `work_allowed` | 9 test(s): observe_waits_for_the_operator_and_a_cool_die, dropping_want_work_stops_work_at_once ... | none |
| `step` | 17 test(s): observe_waits_for_the_operator_and_a_cool_die, dropping_want_work_stops_work_at_once ... | 03, 04, 05, 06, 08, 13, 14, 15, 18, 21 |
| `wd_step` | 4 test(s): the_watchdog_cuts_the_run_enable_when_nobody_calls_step, a_kick_restarts_a_running_watchdog_only ... | 09, 10 |
| `wd_expired` | 4 test(s): the_watchdog_cuts_the_run_enable_when_nobody_calls_step, a_kick_restarts_a_running_watchdog_only ... | 07 |
| `run_enable` | 3 test(s): the_watchdog_cuts_the_run_enable_when_nobody_calls_step, a_late_kick_does_not_clear_a_trip_and_work_stays_forbidden ... | 11, 12 |
| `hot_run` | 1 test(s): cooling_timeout_is_an_error | none |
| `silent_ticks` | 3 test(s): the_watchdog_cuts_the_run_enable_when_nobody_calls_step, a_kick_restarts_a_running_watchdog_only ... | none |
| `LOAD_MAX_MC` | none directly | 01 |
| `COOL_ENTER_MC` | none directly | none |
| `COOL_EXIT_MC` | none directly | none |
| `TEMP_MAX_PLAUSIBLE_MC` | none directly | none |
| `EXIT_GOOD_SAMPLES` | none directly | 17 |
| `STALE_MAX_AGE` | none directly | none |
| `COOL_TIMEOUT` | none directly | none |
| `MAX_COOL_CYCLES` | none directly | none |
| `WATCHDOG_LIMIT_TICKS` | 4 test(s): the_watchdog_cuts_the_run_enable_when_nobody_calls_step, a_kick_restarts_a_running_watchdog_only ... | none |

| mutant | what is broken | function(s) | first failing test | caught |
|---|---|---|---|---|
| thermal_mut01 | the load limit raised to the 100 C once used | LOAD_MAX_MC | preflight_limit_is_strict, observe_waits_for_the_operator_and_a_cool_die | yes |
| thermal_mut02 | sensor validity ignored | plausible | preflight_refuses_bad_readings, sensor_loss_is_an_error_and_never_resumes_by_itself, a_repeated_sample_number_still_raises_stale_and_invalid +1 | yes |
| thermal_mut03 | ERROR not sticky | step | sensor_loss_is_an_error_and_never_resumes_by_itself | yes |
| thermal_mut04 | want_work ignored | step | observe_waits_for_the_operator_and_a_cool_die, dropping_want_work_stops_work_at_once, cooling_never_ends_in_work_without_the_operators_permission +1 | yes |
| thermal_mut05 | a repeated sample counted in COOL | step | one_cached_result_read_again_counts_once, a_repeated_sample_number_still_reacts_to_heat_but_never_starts_work_or_counts | yes |
| thermal_mut06 | a gap in the sample numbers ignored | step | a_gap_in_the_sample_numbers_restarts_the_count | yes |
| thermal_mut07 | watchdog never expires | wd_expired | the_watchdog_cuts_the_run_enable_when_nobody_calls_step, a_late_kick_does_not_clear_a_trip_and_work_stays_forbidden, a_trip_is_rearmed_only_by_an_operator_reset_with_the_policy_back_in_observe | yes |
| thermal_mut08 | review defect 1 (44cd005a): early return on a repeated sample number before the safety checks | step | a_repeated_sample_number_still_honours_stop, a_repeated_sample_number_still_raises_stale_and_invalid, a_repeated_sample_number_still_reacts_to_heat_but_never_starts_work_or_counts | yes |
| thermal_mut09 | review defect 2 (44cd005a): a late kick clears a tripped watchdog | wd_step | a_late_kick_does_not_clear_a_trip_and_work_stays_forbidden | yes |
| thermal_mut10 | re-arm without the policy back in OBSERVE | wd_step | a_trip_is_rearmed_only_by_an_operator_reset_with_the_policy_back_in_observe | yes |
| thermal_mut11 | run_enable ignores the watchdog | run_enable | the_watchdog_cuts_the_run_enable_when_nobody_calls_step, a_late_kick_does_not_clear_a_trip_and_work_stays_forbidden | yes |
| thermal_mut12 | run_enable ignores the policy state | run_enable | a_trip_is_rearmed_only_by_an_operator_reset_with_the_policy_back_in_observe | yes |
| thermal_mut13 | cooling never times out | step | cooling_timeout_is_an_error | yes |
| thermal_mut14 | unlimited cooling cycles | step | the_cycle_limit_ends_in_an_error | yes |
| thermal_mut15 | cooling entered one step too late | step | cooling_enters_at_the_threshold_and_stops_work | yes |
| thermal_mut16 | preflight accepts exactly the limit | preflight_allows | preflight_limit_is_strict | yes |
| thermal_mut17 | hysteresis needs two samples instead of three | EXIT_GOOD_SAMPLES | cooling_leaves_only_after_three_new_good_samples_without_chatter, a_gap_in_the_sample_numbers_restarts_the_count, sample_numbers_wrap_at_256_and_still_count_as_consecutive | yes |
| thermal_mut18 | cooling exit one step too strict | step | cooling_leaves_only_after_three_new_good_samples_without_chatter | yes |
| thermal_mut19 | a reading of the maximum allowed age is refused | plausible | preflight_refuses_bad_readings, plausibility_edges | yes |
| thermal_mut20 | the plausible maximum is off by one | plausible | plausibility_edges | yes |
| thermal_mut21 | sample numbers wrap at 128 instead of 256 | step | sample_numbers_wrap_at_256_and_still_count_as_consecutive | yes |
| thermal_mut22 | the packed sample number loses its top bit | pack | sample_numbers_wrap_at_256_and_still_count_as_consecutive | yes |

### board_gate: 7 tests, 14 mutants

| function | tests that call it | mutants aimed at it |
|---|---|---|
| `exited_ok` | 1 test(s): exit_status_decoding | 14 |
| `capture_usable` | 1 test(s): capture_must_be_a_complete_clean_run | 01, 02, 03, 05 |
| `deadline_passed` | 1 test(s): the_deadline_boundary | 06 |
| `wait_action` | 1 test(s): the_wait_for_a_child_always_ends | 07, 08, 09 |
| `identity_required` | 1 test(s): every_mode_checks_the_boards_identity | 10, 11 |
| `idcode_allowed` | 1 test(s): only_the_xc7a200t_idcodes_are_accepted | 04 |
| `b2u` | none directly | none |
| `put_str` | none directly | none |
| `put_dec` | none directly | none |
| `emit` | none directly | none |
| `env_text` | none directly | none |
| `add_arg` | none directly | none |
| `spawn_capture` | none directly | none |
| `run_loader` | none directly | none |
| `bitstream_matches` | none directly | none |
| `captured` | none directly | none |
| `usable` | none directly | none |
| `read_temp` | none directly | none |
| `diag` | none directly | none |
| `refuse` | none directly | none |
| `identity_check` | none directly | none |
| `preflight` | none directly | none |
| `series` | 1 test(s): exact_mode_and_bitstream_argument | none |
| `dec_env` | none directly | none |
| `text_equal` | 1 test(s): exact_mode_and_bitstream_argument | 13 |
| `absolute_bit_path` | 1 test(s): exact_mode_and_bitstream_argument | 12 |
| `main` | none directly | none |
| `O_OUT` | none directly | none |
| `O_CAP` | none directly | none |
| `CAP_SIZE` | none directly | none |
| `O_ARG` | none directly | none |
| `O_ENV` | none directly | none |
| `O_FILE` | none directly | none |
| `O_POLL` | none directly | none |
| `MEM_SIZE` | none directly | none |
| `TIMEOUT_MS` | none directly | none |
| `KILL_GRACE_MS` | none directly | none |
| `LOCK_PATH` | none directly | none |
| `LOADER` | none directly | none |
| `S_LEN` | none directly | none |
| `S_STATUS` | none directly | none |
| `S_TIMEOUT` | none directly | none |
| `S_SPAWN` | none directly | none |

| mutant | what is broken | function(s) | first failing test | caught |
|---|---|---|---|---|
| board_gate_mut01 | a timed-out capture accepted | capture_usable | capture_must_be_a_complete_clean_run | yes |
| board_gate_mut02 | a non-zero tool exit status accepted | capture_usable | capture_must_be_a_complete_clean_run | yes |
| board_gate_mut03 | a completely filled capture buffer accepted | capture_usable | capture_must_be_a_complete_clean_run | yes |
| board_gate_mut04 | any found IDCODE accepted | idcode_allowed | only_the_xc7a200t_idcodes_are_accepted | yes |
| board_gate_mut05 | a capture of a tool that never started accepted | capture_usable | capture_must_be_a_complete_clean_run | yes |
| board_gate_mut06 | the deadline never passes | deadline_passed | the_wait_for_a_child_always_ends, the_deadline_boundary | yes |
| board_gate_mut07 | an overdue child is never killed | wait_action | the_wait_for_a_child_always_ends | yes |
| board_gate_mut08 | an unkillable child is waited for without limit | wait_action | the_wait_for_a_child_always_ends | yes |
| board_gate_mut09 | a reaped child is not recognised | wait_action | the_wait_for_a_child_always_ends | yes |
| board_gate_mut10 | series skips the identity check | identity_required | every_mode_checks_the_boards_identity | yes |
| board_gate_mut11 | only load checks the identity | identity_required | every_mode_checks_the_boards_identity | yes |
| board_gate_mut12 | any absolute path accepted as a bitstream | absolute_bit_path | exact_mode_and_bitstream_argument | yes |
| board_gate_mut13 | a mode matches on a prefix | text_equal | exact_mode_and_bitstream_argument | yes |
| board_gate_mut14 | death by a signal accepted as a clean exit | exited_ok | capture_must_be_a_complete_clean_run, exit_status_decoding | yes |

### vsavm: 11 tests, 18 mutants

| function | tests that call it | mutants aimed at it |
|---|---|---|
| `init_vm` | 9 test(s): original_trios_arithmetic_program, independent_signed_program_and_aliases ... | none |
| `reg_index` | 1 test(s): register_indexes_0_to_3_are_valid_and_4_maps_to_v0 | 13 |
| `supported` | 1 test(s): every_supported_opcode_is_listed_and_the_neighbours_are_not | 01, 02 |
| `begin_program` | 4 test(s): reload_preserves_registers_and_empty_program_does_nothing, explicit_unsupported_and_overflow_errors_do_not_commit ... | 15 |
| `append` | 9 test(s): original_trios_arithmetic_program, independent_signed_program_and_aliases ... | 06 |
| `step` | 4 test(s): independent_signed_program_and_aliases, reload_preserves_registers_and_empty_program_does_nothing ... | 03, 04, 05, 07, 08, 09, 10, 11, 12, 16, 17, 18 |
| `run` | 9 test(s): original_trios_arithmetic_program, independent_signed_program_and_aliases ... | none |
| `instruction` | 9 test(s): original_trios_arithmetic_program, independent_signed_program_and_aliases ... | none |
| `MAX_PROGRAM` | 1 test(s): program_capacity_is_checked_before_write | none |
| `MAX_VALUE` | 4 test(s): explicit_unsupported_and_overflow_errors_do_not_commit, the_value_range_is_exactly_39_trits ... | 14 |

| mutant | what is broken | function(s) | first failing test | caught |
|---|---|---|---|---|
| vsavm_mut01 | HALT (25) unsupported | supported | original_trios_arithmetic_program | yes |
| vsavm_mut02 | opcode 17 (cosine comparison) accepted as supported | supported | explicit_unsupported_and_overflow_errors_do_not_commit | yes |
| vsavm_mut03 | overflow never reported | step | explicit_unsupported_and_overflow_errors_do_not_commit | yes |
| vsavm_mut04 | negative overflow not reported | step | negative_overflow_and_the_exact_range_edges | yes |
| vsavm_mut05 | positive overflow not reported | step | explicit_unsupported_and_overflow_errors_do_not_commit | yes |
| vsavm_mut06 | program capacity not checked | append | program_capacity_is_checked_before_write | yes |
| vsavm_mut07 | a halted machine steps again | step | a_halted_or_failed_machine_does_not_step_again | yes |
| vsavm_mut08 | a failed machine steps again | step | a_halted_or_failed_machine_does_not_step_again | yes |
| vsavm_mut09 | stepping past the end of the program | step | independent_signed_program_and_aliases | yes |
| vsavm_mut10 | ADD computes a subtraction | step | original_trios_arithmetic_program | yes |
| vsavm_mut11 | negation drops the sign | step | independent_signed_program_and_aliases | yes |
| vsavm_mut12 | MUL computes an addition | step | independent_signed_program_and_aliases | yes |
| vsavm_mut13 | register index 4 reaches past the registers | reg_index | register_indexes_0_to_3_are_valid_and_4_maps_to_v0 | yes |
| vsavm_mut14 | range limit off by one | MAX_VALUE | the_value_range_is_exactly_39_trits | yes |
| vsavm_mut15 | begin_program keeps the error | begin_program | explicit_unsupported_and_overflow_errors_do_not_commit | yes |
| vsavm_mut16 | STORE takes the wrong operand | step | original_trios_arithmetic_program | yes |
| vsavm_mut17 | cycles are not counted | step | original_trios_arithmetic_program | yes |
| vsavm_mut18 | an unsupported opcode is skipped without an error | step | explicit_unsupported_and_overflow_errors_do_not_commit | yes |
