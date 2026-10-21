# LOOP-STATE — where the AX7203 TriOS work stands, for the next cycle

Written 2026-10-21 (round 2). Read this first, then `T27-ONLY-AUDIT.md` (what is `.t27`, what is not, the
function → tests → mutant table) and `CONTRACT.md` (UART protocol, memory map). Facts below were read from files, jobs
or runs; anything not verified says so. Branch `fpga/ax7203-trios-mesh` of `dmitrii-f-t27/trinity-memory`; issue #140; draft PR #141.
Nothing here says the full TriOS operating system runs. It does not.

## Safety invariants (do not weaken, do not add a bypass)

1. **Load limit 70 C, strict.** `LOAD_MAX_MC = 70000` in `specs/trios/thermal_policy.t27`; `preflight_allows` requires a valid, fresh,
   plausible reading `< 70000`. No option, environment variable or argument changes it. Cooling thresholds (75 C enter, 65 C exit,
   3 consecutive new samples, 120-sample timeout, 3 cycles) are our test policy, not datasheet ratings. History: a first runner
   raised the bound to 100 C and loaded the board at 93.96 C; that run is a component result, not a pass of the 70 C rule.
2. **Fresh preflight every time.** IDCODE (`0x3636093` / `0x13636093`), DNA equal to the private expected value, temperature, all in the
   same executor run that loads or samples. `series` also checks identity now. A stale or earlier preflight never counts.
3. **One board lock.** `/private/tmp/claude-501/ax7203-board.lock` (a directory made with `mkdir`). Held from identity through the load and the
   post-load reading. Another holder → status 3. A tool that cannot be stopped → status 6 and the lock is left in place on purpose.
4. **No load without an explicit owner decision.** `TRIOS_MODE=load` additionally needs the exact SHA-256 of the verified bitstream
   (`TRIOS_EXPECT_BITSTREAM_SHA256`); the owner decides when to run it. SRAM only: flash, eFUSE, BBRAM keys and power are never touched.
5. **Board identifiers stay private.** The DNA and adapter serial live in `~/Brain/private/` and the environment, never in this repo, a
   comment, a log or a commit. (The DNA was already public on `master` in `docs/hardware.md` and `conformance/memory_bridge.json` before this
   work; that is a separate owner question.)
6. **Authored source is `.t27`.** Generated files (Verilog, Zig, the VM workflow YAML) come from generators; do not hand-edit them.
   Hand-written exceptions are listed in the audit with the exact compiler gap.

## What exists, with the evidence

| Item | State | Evidence |
|---|---|---|
| Component run on the board (trios-mesh on PicoRV32, SRAM) | done once, 56/56 UART steps, historical | bitstream SHA256 `5d7ad37ae5fe6c4a8593d3003d5ea557008f11934a99681853e1dbd84cd0bc33`, source `c76c6914`, CI run 39640280821; loaded at 93.96 C (see invariant 1) |
| VSAVM profile + firmware + vectors (all `.t27`) | built in CI, not on the board | workflow `fpga-ax7203-trios-vm` run 39661772012 on `967447a`: `firmware-sim` and `bitstream` success; bitstream SHA256 `d9da5655d52889ab3448d12d5c42ecbd9f762481a96ef6761d32fd28277c66e7`, firmware.bin `9584c322…`, timing PASS clk25 96.98 MHz at 25 / clk200 1223.99 MHz at 200; toolchain image `regymm/openxc7@sha256:35c91073…` with its chip database (nextpnr-xilinx `1743d0f4…`, chipdb sha256 `81dc7e2c…`) |
| Board executor (preflight, series, load) | built with t27b, board runs read-only earlier | `specs/trios/board_gate.t27`; no-board paths run in `t27-host-ax7203-trios` |
| Serial acceptance runner | written, not run on a board in this round | `specs/trios/serial_runner.t27` |
| Mutation checks | 72 mutants (parser 18, thermal 22, gate 14, VSAVM 18), 0 survivors | table in `T27-ONLY-AUDIT.md`; thermal and board mutants on t27b (thermal also on the pinned t27c), VSAVM mutants on t27c + Zig in the VM workflow |
| Cooling | policy in `.t27` only | no independent hardware stop, no fabric XADC, no fan control: exact gaps in the audit |

## How to check the state without touching the board

- `t27b test specs/trios/<module>.t27` for `board_parse`, `thermal_policy`, `board_gate`, `host_smoke`; every file in `specs/trios/mutants/` must
  make its module fail. The t27b binary is the CI artifact of workflow `t27-host-ax7203-trios` (built from gHashTag/t27 `aeb772f4`).
- `vsavm.t27`: `t27c gen` (gHashTag/t27 `d9737e1f`) then `zig test` (0.15.2; on this Mac use the shim sysroot, see the Brain memory note on the Xcode 26 SDK).
- The executor's no-board paths: any configuration problem must exit with status 4 before the lock; the CI job lists the cases
  (series without the DNA file, load without a path / hash / file, malformed or wrong hash, hung child killed at the deadline).
- Regenerate the VM workflow with the compiled `vm_ci.t27` (t27b build, link, run; output to `.github/workflows/fpga-ax7203-trios-vm.yml`).
  Regenerating an unchanged `vm_ci.t27` reproduced the committed YAML byte for byte (checked 2026-10-21).

## Do not repeat

- Do not rebuild the FPGA bitstream for a change that touches only `specs/trios/` policy, parser, gate or mutants: workflows `t27-policy-*`, `t27-host-*` and
  the VM workflow have their own path filters. The PR re-triggers the original `fpga-ax7203` trace-player workflow because its own file is in the PR diff;
  cancel those runs by hand.
- `BUILD_ID` is the commit prefix baked into the netlist, so two builds of identical RTL differ in FASM and `.bit`; bitstream hashes from different commits
  are not evidence of (non-)reproducibility.
- t27b traps (compiles but emits `brk #10`): `undefined;` and an ignored non-void call (`f();`) in a function no test reaches; bind every result
  (`const r : i32 = f();`). Arrays passed to internal functions are copied: pass slices (`a[0 .. n]`) when the callee writes. `bool` to integer needs
  a helper. Mutant files must be regenerated from the current spec after every edit of the spec (a stale mutant also "fails").
- In zsh an unmatched glob aborts the command (`rm -f dir/x*` with no match): use `find` or `setopt nonomatch`.
- Do not cancel runs of other branches or other tasks; cancel only this branch's superseded runs.

## What is not verified

- No new board run in round 2; the board was not touched (no `openFPGALoader`, no preflight, no XADC read).
- `serial_runner.t27` and the VSAVM bitstream have never been run on the board.
- CI results of round 2 are recorded in the PR #141 body and the report that accompanies the push, with run IDs; do not assume green.
- Unknown: the thermal behaviour of the VSAVM bitstream on the board; the independent hardware stop (not built); JTAG shutdown (UG470 not read).

## Next step (one)

When the owner decides to use the board: run the executor in `preflight` mode only, read the temperature, and stop; the load of a verified bitstream
(by SHA-256) is a separate owner decision. Until then the work is source-only: the remaining migration is the SoC glue (`rtl/*.v`, `tb/*.v`, `fw/`) to `.t27`
via `t27b verilog` (needs a newer pin; PicoRV32 as an extern module and an XADC declaration are unverified).
