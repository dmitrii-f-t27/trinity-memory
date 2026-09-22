# Native t27 RTL

The datapaths and state transitions in this directory are native `.t27` code.
The [adapters](../../rtl/t27/) only bind configuration constants, preserve the
external port widths, and connect generated modules. They contain no decoder,
accumulator, memory sequencing, or lane masking implementation. Generated
Verilog is a build artifact in `build/t27/rtl/`.

Run from the repository root using the pinned upstream compiler built by the
main native build workflow:

```sh
python3 scripts/generate_rtl.py --compiler "$T27C"
python3 tests/t27_rtl.py --generated-dir build/t27/rtl
python3 tests/t27_storage.py --compiler "$T27C"
```

`T27C` may instead be provided as an environment variable. The generator checks the compiler pin, rebuilds it, and verifies zero lexer/parser loss.
The simulation runners invoke `iverilog` and `vvp`; missing tools fail this explicit
path. Its machine-readable evidence is `build/t27/rtl-validation.json`.
The existing Python oracle and SystemVerilog fixtures remain migration tests;
none of the legacy RTL implementation is compiled in `tests/t27_rtl.py`.
The frozen originals live in [tests/reference/rtl](../../tests/reference/rtl/),
with original commit and byte hashes recorded in
[the reference manifest](../../tests/reference/rtl-migration-manifest.json).

## Implemented behavior

- Dense5, baseline5 and sparse4:1 decoders: exhaustive input code spaces and
  unknown-code rejection reuse the original decoder scoreboard.
- Dot stream: two pipeline stages, real input/output backpressure, synchronous
  reset, stable stalled results, prefix masks, partial and empty frames,
  canonical padding checks, reserved code rejection, and sticky error drain.
  Int8 values are widened before negation, so `-(-128)` is `128`.
  The accumulator checks signed overflow after each accepted group. An error
  clears the accumulator and the frame drains to `last`, producing result zero
  with `out_error=1`. Cancellation after an overflow cannot repair a frame.
- Storage: synchronous writes while idle, synchronous sequential reads,
  ignored writes/starts while busy, reset abort that preserves RAM contents,
  final lane masking and invalid-code zeroing in native functions.

- Block-RAM trit packing bench (`bram_trit_codec.t27`, `bram_trit_engine.t27`,
  `fpga_bram_bench.t27`, wired by `fpga/ax7203/tms_bram_bench.v`): encoder and
  decoder of three 36-bit word layouts (18, 20 and 22 trits per word), a store
  that writes a 64-bit LFSR trit stream, reads it back one word per clock and
  checks every lane, and the sequencer that reports the results over the UART.
  `tests/test_bram_trit_packing.py` compares the generated C with
  `tools/bram_trit_model.py` on random inputs; `make -C fpga/ax7203 bram-sim`
  runs the whole bench in Icarus. See `docs/hardware.md`, "Block-RAM trit packing".

## Current compiler boundaries

- `trinity_dot_stream_t27` supports `ACC_WIDTH=2..32`; the native accumulator is
  `i32`, its temporary total and bound comparisons use `i64`. The wrapper binds
  the selected signed minimum and maximum. Unsupported widths fail at time
  zero. The migration runner tests the original 32-bit randomized and 12-bit
  overflow scoreboards and adds 5-bit signed endpoints, overflow and unknown
  inputs for both codecs.
- The default storage build supports `TRIT_COUNT=1..320` (`WORDS=1..64`),
  checked at every logical count in that range. Its native array is **64 x u16**
  for both codecs. The adapter exposes 8-bit dense and 10-bit baseline load
  ports and rejects other code widths. No equivalent physical RAM width,
  packing saving or synthesis result is claimed.
- Larger arrays use [explicit source specialization](../../tools/generate-t27-storage.py):
  `python3 tools/generate-t27-storage.py --trits 4096 --output build/t27/storage-4096`.
  With `T27_ROOT` set to the pinned checkout, this generates an **820 x u16**
  array, default logical count 4096, and matching wiring configuration. Compile
  the emitted `stream_storage.v`, `stream_view.v`, and `streams.v` together.
  Physical capacities **1, 65, 820, and 4096 groups** were simulated; selected
  logical counts reach 20480. This is not an exhaustive 1..4096-capacity test.
  [The test runner](../../tests/t27_storage.py) lists every checked case.
- Computed array dimensions in the pinned compiler can silently become scalar
  variables. This implementation uses a literal capacity instead of relying on
  that lowering. Native parameters are not supported by this backend; wrapper
  constants select the supported logical limits. Larger capacities use the explicit source-specialization path above.
- The compiler exposes only primitive data port widths used here (`8/16/32/64`
  bits and `bool`). Wiring adapters pad the original 10/40/5-bit ports and slice
  results. Configuration is bound statically by the adapters.
- Combinational module outputs deliberately use native module-level
  assignments. An `on_comb` function reading globals can have incomplete
  Verilog sensitivity in the current compiler; explicit input-only decoder
  functions do not use that pattern.
- The compiler adds `clk`, `rst_n`, `en`, `ready` ports. Adapters tie its
  `rst_n/en` high and route the original synchronous `rst` to the native
  `reset` argument. Memory data has no reset initializer.
- Arrays become block RAM only with yosys `read_verilog -nomem2reg` (the array
  write sits in a process with an asynchronous reset), a read register assigned
  straight from the array and no reset value on it. Integer types are
  `u8/u16/u32/u64`: an unknown width such as `u36` silently becomes 32 bits, so a
  36-bit word is a masked `u64`.
- `gen-c` emits constants as untyped macros (shift a typed local, not a constant,
  past bit 31) and initializes a module array with `= 0`, which C rejects; the
  C tests of `bram_trit_engine.t27` rewrite that one line to `= {0}`.
- Locals of `on_clock` are declared where they appear in the generated Verilog,
  which plain Verilog rejects after a statement; intermediate values are
  module-level assignments instead.
- `*` lowers to a 64-step shift-add function and a division of a `u64` by a
  constant to a 64-bit divider; constant multiplies are written as shifts and
  adds, digit arithmetic is 16 bits wide. A heavy function called inside a
  branch of another function makes yosys spend minutes in `proc`; such values
  are computed into locals first and selected afterwards.

Evidence from these tests is RTL simulation. Board runs, block RAM mapping and
place-and-route of the FPGA designs built from these modules are recorded in
`docs/hardware.md` and `reports/fpga/`; DDR/HBM throughput and power are unmeasured.
