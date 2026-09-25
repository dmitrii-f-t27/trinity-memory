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

- DDR3 calibration status (`fpga_ddr3_status.t27`, wired by
  `fpga/ax7203/ddr3/tms_ddr3_ax7203.v` to UberDDR3, the line emitter and the
  UART): a header line, then status lines. A line goes out whenever the line
  emitter is free and the controller's calibration state or `o_calib_complete`
  differs from the last line sent, and periodically. Changes are coalesced: a
  line takes about 1.7 ms at 115200 baud, calibration passes through its states
  much faster, so a line carries the latest state and most transitions never get
  a line of their own. The highest state reached and the number of returns to
  IDLE (a wrong self-test read or a failed alignment step), kept every clock,
  are exact.
  `tests/test_ddr3_flow.py` runs it with the emitter and the transmitter in
  Icarus. See `docs/hardware.md`, "DDR3 in the open flow".

- DDR3 pattern test (`fpga_ddr3_pattern.t27`, wired by
  `fpga/ax7203/ddr3/tms_ddr3_ax7203.v` with `PATTERN_TEST` 1, issue #61): a
  Wishbone master on UberDDR3's user port that, after calibration, writes every
  burst address of the region with address-unique data (per 64-bit word
  `key ^ lin((word << 32) | address)`, `lin` a bijection of 64 bits), reads it all
  back and compares, then repeats with the complement; per pass it reports the
  wrong bursts, 64-bit words and bits, the DQ bits ever wrong, the first failing
  burst and the controller clocks of each phase, and it arbitrates the report
  line with the status reporter. `tests/test_ddr3_pattern.py` checks its functions
  in C against `tools/ddr3_pattern_model.py` and runs the whole top in Icarus
  against a behavioural Wishbone memory with injected faults. See
  `docs/hardware.md`, "Pattern test (#61)".

- DDR3 read path (`fpga_ddr3_reader.t27`, wired by `fpga/ax7203/ddr3/tms_ddr3_reader.v`
  into `fpga/ax7203/ddr3/tms_ddr3_ax7203.v` when that top is read with `define DDR3_READER`
  (`make ... DDR3_APP=reader`), issue #62, x16): after calibration it fills a region of
  UberDDR3's memory with the baseline2 or dense5 device-code bytes of a trit stream the board
  generates (no UART), reads it back through a Wishbone burst reader that keeps at most `cap`
  requests outstanding, and consumer (A) takes one 128-bit word per controller clock through a
  nine-stage pipeline (S1, D, S2-S8): decode (80 dense5 or 64 baseline2 lanes), lane check
  against the regenerated stream, +1/-1 counts, dot product with the activations `(i mod 8) + 1`
  and the rotate-xor checksum; per run it reports cycles, words, bus, payload, padding and
  scale/metadata bytes, logical trits, command, wait and consumer stalls, and the acks since
  calibration (all, and those outside the fill and read phases). Several of these counters are
  fixed by the design (words = W, padding = bus - payload, consumer stalls 0): `docs/hardware.md`
  says which. The dense5 decoder
  and encoder are constant tables (a division would become carry chains in the DDR3 flow's
  synthesis), checked on every code against `bram_trit_codec.t27`. `tests/test_ddr3_reader.py`
  checks its functions in C against `tools/ddr3_read_model.py` and runs the whole top in Icarus
  against the behavioural Wishbone memory with injected faults. See `docs/hardware.md`,
  "DDR3 read path (#62)".

- UART loader (`fpga_uart_rx.t27`, `fpga_uart_loader.t27`, `fpga_loader_store.t27`,
  wired by `fpga/ax7203/tms_uart_loader.v`, issue #63): an 8N1 receiver with
  start-bit validation and framing-error count, a receive FIFO in block RAM, a
  frame parser (`A5 5A`, command, sequence, address, length, payload, CRC-32 one
  byte per clock), a 4096-byte staging buffer committed through a write port
  (valid/ready/last/idle) only after the CRC matched, duplicate detection, an
  inter-byte timeout, ack/nak and status lines with a check byte through the line
  emitter, read-back as CRC frames through a read port, a baud change with a
  fallback, and a 256 KiB block-RAM store behind the two ports.
  `tests/test_uart_loader.py` checks the functions in C against
  `tools/uart_loader_protocol.py` and runs the board top and the cores behind a
  stalling memory in Icarus, every device byte against the model. See
  `docs/uart-loader.md`.

- DDR3 loader (`fpga_ddr3_loader.t27`, `fpga_loader_wb.t27`, `fpga_wb_arbiter.t27`, wired by
  `fpga/ax7203/ddr3/tms_ddr3_loader_ax7203.v`, `make ... DDR3_APP=loader`, issue #63 part 2):
  the loader above with marked changes (`not_ready` until UberDDR3 calibrates, protocol 3 with
  the calibration word and the Wishbone counters in the status lines, registered compares, a
  parallel CRC step, a pipelined line check byte and a status multiplexer tree for 83.33
  MHz); a Wishbone master behind its write and read ports (16-byte words with byte selects,
  `wr_idle` only when every write is acknowledged, a kept read word invalidated by writes, a
  late read ack dropped after the loader's watchdog); and an arbiter of UberDDR3's single user
  port between two masters that changes owner only with nothing outstanding.
  `tests/test_ddr3_loader.py` checks the functions in C, that every difference from
  `fpga_uart_loader.t27` is marked, and runs the whole top in Icarus against our Wishbone
  memory model, every device byte against the protocol model (except the status values that
  depend on timing and the reads of the reader's region, which the reader rewrites), with the
  #62 reader as second master. See `docs/uart-loader.md`, "DDR3 (part 2, built)".

- Device matvec (`fpga_ddr3_matvec.t27`, issue #64, consumer (B) of #65; wired into the DDR3
  matvec build below, not yet on the board): y = W x for a ternary matrix streamed one
  128-bit bus word per controller clock (the word #62's reader delivers) as baseline2 (64 lanes)
  or dense5 (80 lanes) with every row padded to whole words, and int8 activations held on chip in
  ten block-RAM banks read 80 at a time (one read per word in both formats). Decode (the dense5
  constant tables of `fpga_ddr3_reader.t27`), padding mask, biased 8-bit terms (+1: x xor 0x80,
  -1: x xor 0x7F, the missing +1 of each -1 lane added as a count), an adder tree over 80 lanes in
  pipeline stages, 32-bit signed row accumulators, a 1024 x u32 result memory, then one Y line per
  row and eleven Z counter lines through `fpga_line_emitter.t27`. Words are taken on the
  in_valid / in_ready handshake (in_ready: RUN and words still expected); a run with no word for
  65,536 clocks, or an `abort`, ends with status 2 and only its Z lines, and the module is idle
  again. Its counters (idle clocks, latency, consumer stalls: words held off before RUN, stray
  words: words offered outside a run) measure what it was offered; cycles = words + idle clocks by
  construction. `tests/test_ddr3_matvec.py` checks its functions in C and runs it in Icarus on the
  real q_proj chunk (rows 0-319) in both formats, on 6,912-column rows, on +1 codes in every
  padding lane, at the 1,024-row and 1,024-word limits, and on the handshake, a short stream and
  an abort. See `docs/bridge.md`, "Device matvec (#64)".

- DDR3 matvec build (`make ... DDR3_APP=matvec`, issue #64: the DDR3 loader's top read with
  `define DDR3_MATVEC`): the loader above at protocol 4 (`[matvec]`-marked changes: activation
  frames X into the matvec's banks, matvec frames M that check the region and start the run,
  X, M and B refused `not_ready` while a run is busy and M before the calibration), the device
  matvec, `fpga_matvec_feed.t27` (Wishbone master 1: after the matvec's in_ready it reads exactly
  rows x words per row in address order, at most 64 outstanding, and hands each acknowledged word
  to the matvec in the next clock; a watchdog pulses the matvec's abort, keeps a request it presented
  until it is taken and drops the late acks) and
  `fpga_line_arbiter.t27` (the line emitter shared by the loader's and the matvec's lines: a side
  sees idle only while it holds the grant and no go is in flight; the grant alternates, and stays
  with the loader while it sends a read-back frame). `tests/test_ddr3_matvec_top.py` checks the
  loader's new functions in C and runs the whole top in Icarus against our Wishbone memory model,
  every device byte against `tools/bridge_link_protocol.MatvecDevice` (runs in both formats,
  refusals, frames while a run is busy or while the feed drains, a B during a run, the
  calibration, aborts on withheld acks with a few and with a full cap of requests in flight,
  activations at nonzero blocks and in parts of blocks, a load and a read-back during a run, and
  the real q_proj chunk with the fixture cache). For this build's timing at 83.33 MHz the
  Wishbone master takes each byte into an input register, the outstanding counts are narrow,
  the line emitter shifts its digits out, the matvec registers its Z value a clock early, the X
  header rule's bound is registered a clock ahead of the rule, and the feed hands on each word
  from a register. See `docs/bridge.md`, "The DDR3 matvec build" and its "Timing".

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
- `gen-c` does not lower the module-level assignments of a clocked module, so
  the generated C `on_clock` of such a module is not a model of it (the C runner
  checks its functions; the module as a whole is checked in Verilog).
- Locals of `on_clock` are declared where they appear in the generated Verilog,
  which plain Verilog rejects after a statement; intermediate values are
  module-level assignments instead.
- `*` lowers to a 64-step shift-add function and a division of a `u64` by a
  constant to a 64-bit divider; constant multiplies are written as shifts and
  adds, digit arithmetic is 16 bits wide. A heavy function called inside a
  branch of another function makes yosys spend minutes in `proc`; such values
  are computed into locals first and selected afterwards.
- An ordering compare (`>=`, `>`, `<`) with arithmetic on a literal on one side
  (`round + 1 >= rounds`, `a - 1 >= b`) is lowered by gen-verilog to a signed
  compare (`$signed((round + 1)) >= $signed({1'b0, rounds})`) while gen-c keeps
  it unsigned, so the two differ from 2^31 on (and `a - 1` at `a` = 0). `==`,
  `a >= b` and `a + c >= b` stay unsigned; assigning the sum to a typed local
  first (`var n: u32 = a + 1; ... n >= b`) gives an unsigned compare. In
  `fpga_ddr3_pattern.t27` the round stop is such a compare; it is unreachable
  below 2^31 rounds, and the Makefile accepts only `DDR3_PATTERN_ROUNDS` below
  2^31 (rewriting it would change the netlist of the builds that ran).
- A module-level assignment of constants only (`ready = true;`) becomes an
  `always @(*)` with an empty sensitivity list, which never runs in Icarus (the
  output stays X); `fpga_loader_store.t27` uses registers with an initial value
  for such outputs instead.

Evidence from these tests is RTL simulation. Board runs, block RAM mapping and
place-and-route of the FPGA designs built from these modules are recorded in
`docs/hardware.md` and `reports/fpga/`; DDR/HBM throughput and power are unmeasured.
