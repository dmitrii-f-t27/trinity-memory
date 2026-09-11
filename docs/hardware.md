# RTL reference and measurement protocol

This repository implements digital encodings of balanced ternary values in binary
memory. The RTL is a functional prototype: it does not implement physical ternary
cells, a DDR controller, an inference engine, or a board-specific design.

## Modules and representation

| Module | Input | Output | Invalid input |
| --- | --- | --- | --- |
| `ternary_dense5_decoder_t27` | 8-bit code | Five 2-bit trit lanes | Codes 243–255 |
| `ternary_sparse41_decoder_t27` | 4-bit code | Four 2-bit trit lanes | Codes 9–15 |
| `ternary_baseline5_decoder_t27` | Five 2-bit lanes | Five 2-bit trit lanes | Any lane equal to `11` |
| `ternary_dense5_stream_t27` | Clocked loading/start interface | Stream of five decoded lanes | Per-word `out_code_valid` |
| `ternary_baseline5_stream_t27` | Same interface, wider load data | Same decoded stream | Per-word `out_code_valid` |
| `ternary_stream_adapter_t27` | The stream interface with `out_ready` exposed | Same decoded stream, held while `out_ready` is low | Per-word `out_code_valid` |
| `ternary_stream_dot_t27` | Loading/start interface plus an activation stream | Framed dot results (`out_valid`/`out_ready`) | `out_error` per frame |

Balanced values are `−1, 0, +1`. On all decoder outputs, `00 = 0`, `01 = +1`,
`10 = −1`; the first trit occupies bits `[1:0]`. Invalid codes produce all-zero
data with `valid = 0`, so validity must be checked separately from the data.
In simulation, an input containing an unknown bit also fails this validity check.

Dense encoding is `code = Σ((trit[i] + 1) × 3^i)`, for `i = 0…4`.
The all-zero logical group is code **121**, because `1 + 3 + 9 + 27 + 81 = 121`.
Code 0 represents five `−1` values. Canonical file padding uses **logical zero**.

Sparse `(4,1)` encoding permits at most one nonzero trit in four positions.
Code 0 represents all zeros; at position `p`, `−1` has code `1 + 2p` and `+1`
has code `2 + 2p`. Its decoder is provided independently; there is no sparse
streaming memory wrapper in this prototype.

Decoder algorithms are executable [.t27 sources](../t27/rtl/), compiled by
[`generate_rtl.py`](../scripts/generate_rtl.py). The
[wiring adapters](../rtl/t27/decoders.v) preserve the external ports. Generated
Verilog is a build artifact; generation does not establish physical LUT use.
The original v0.2 Verilog is preserved only as a
[test oracle](../tests/reference/rtl/).

## Streaming interface

The sequencer timing table below, the tail-mask rule and the view packing are
restated in [`specs/memory/stream_compute.t27`](../specs/memory/stream_compute.t27)
and checked cycle by cycle by the storage traces in
[`conformance/memory_stream_compute.json`](../conformance/memory_stream_compute.json).

Instantiate a stream with a positive `TRIT_COUNT`. The derived memory depth is
`WORDS = ceil(TRIT_COUNT / 5)`. Keep the derived `WORDS` and `ADDR_WIDTH`
parameters at their defaults. Both variants use the native
[storage/sequencer](../t27/rtl/stream_storage.t27),
[decoder/masking functions](../t27/rtl/stream_view.t27), and
[wiring adapters](../rtl/t27/streams.v). The default native array has 64 groups,
so that build accepts logical `TRIT_COUNT=1..320`.

The pinned compiler needs the physical array bound at generation time. For a
larger build, specialize the capacity constant in the same `.t27` source:

```sh
# T27_ROOT points to the checkout pinned by native/compiler.lock.
python3 tools/generate-t27-storage.py --trits 4096 --output build/t27/storage-4096
```

This produces `stream_storage.v`, `stream_view.v`, and `streams.v` with 820
groups (`ceil(4096/5)`), default `TRIT_COUNT=4096`, and a maximum of 4100 logical
trits. Compile these three files together. The generator preserves the native
algorithm, verifies the compiler pin and lexer/parser accounting, and records
source/output hashes in `storage-specialization.json`.

Actual Icarus tests cover physical capacities **1, 65, 820, and 4096 groups**,
with selected logical counts including 321, 4096, and 20480; this is not a claim
that every capacity in 1..4096 was tested. The default 64-group build separately
checks every logical count from 1 through 320. See
[the specialization tests](../tests/t27_storage.py) for the exact cases.
The native storage adapters accept code widths 8 and 10; they reject other
widths explicitly.

| Signal | Contract |
| --- | --- |
| `rst` | Active-high synchronous reset; clears control and observable outputs at the next rising edge. Does not clear memory. |
| `load_ready` | High when reset is inactive, `busy = 0`, and `start = 0`. |
| `load_en`, `load_addr`, `load_code` | A write occurs on the rising edge only when `load_en && load_ready` and `load_addr < WORDS`. Dense data is 8 bits; baseline data is 10 bits. |
| `start` | Accepted on a rising edge only when idle and reset is inactive. Takes priority over loading. Starts a complete fixed-length read from word 0. |
| `busy` | High from an accepted start through the cycle carrying the last output word, including cycles in which the word is held. Busy starts and writes are ignored. |
| `out_ready` | Downstream ready (`ternary_stream_adapter_t27`; tied high inside the dense5/baseline5 wrappers). While `out_valid` is high and `out_ready` is low, the word, `out_last`, the mask and the sequencer's address are held; the timing table below resumes where it stopped. Ignored while no word is presented. |
| `out_valid` | Marks a response from memory, including a response with an invalid encoded word. |
| `out_code_valid` | High only when `out_valid` is high and the encoded word is valid. |
| `out_last` | High with the last response, including an invalid last word. |
| `out_lane_mask` | Low five bits describe logical lanes present in that word. Unused final lanes and idle mask bits are zero. |
| `out_trits` | Five lanes. Masked lanes, invalid words, and idle output are zero. |

There is no downstream backpressure. The consumer must accept every valid word.
Load every word before starting; unread initialized state is not defined.
No initialization file is embedded in the RTL. Hex words exported by the Python
CLI can be read in a host/testbench and loaded using this interface.

Let edge **S** be the edge accepting `start`:

| After edge | `busy` | `out_valid` | Result |
| --- | --- | --- | --- |
| S | 1 | 0 | Read sequence started |
| S + 1 | 1 | 1 | Word 0; one registered memory-read cycle after start |
| S + k, for 1 ≤ k ≤ WORDS | 1 | 1 | Word k−1 |
| S + WORDS | 1 | 1 | Last word; `out_last = 1` |
| S + WORDS + 1 | 0 | 0 | Return to idle |

A new start may be accepted at the next edge after idle becomes visible.
Holding `start` high eventually starts another sequence once the module is idle;
pulse it for one cycle when requesting a single sequence. Reset during a stream
aborts that sequence. Previously loaded memory can then be read from the beginning.

For `TRIT_COUNT = 12`, the final mask is `00011`, and bits `[9:4]` of the final
output are zero even if the encoded padding contains nonzero trits. Decoder
validity still applies to the complete encoded word, including padding lanes.

## What the memory comparison measures

The current native array uses **16-bit elements for both codecs**. Dense and
baseline load interfaces carry 8 and 10 meaningful bits per group respectively;
decoding follows the registered read. These logical widths do not prove a
physical RAM reduction in the native implementation. Synthesis must establish
whether unused bits are removed and how the array maps to device memory.
The preserved v0.2 oracle declared separate 8-bit and 10-bit arrays, but it is
not a production runtime or synthesis fallback.

For complete five-trit groups:

- Dense payload: `8 / 5 = 1.6 bits/trit`.
- Baseline payload: `10 / 5 = 2 bits/trit`.
- Logical payload reduction: `(10 − 8) / 10 = 20%`.
- Ideal weights per payload bit ratio: `10 / 8 = 1.25`, or 25% more, **if** the
  entire workload is limited by this payload transfer and all other costs vanish.

Both demonstrators deliver **five lanes per active output cycle**, by design.
There is no measured throughput improvement in these simulations. Physical FPGA
memory blocks have discrete width/depth modes; a narrower logical array does not
guarantee fewer BRAM blocks. Controller registers, decoding, padding, bus packing,
scales, and metadata also affect the deployed cost. The 10-bit baseline uses the
same five-lane group boundary for a controlled RTL comparison; it is not the
Python byte-oriented `baseline2` file framing.

## Reproduce the functional checks

From the repository root, with the pinned compiler checkout in `T27_ROOT`,
Python, and Icarus Verilog (`iverilog`, `vvp`):

```sh
python3 scripts/generate_rtl.py
python3 scripts/generate_rtl.py --check
python3 tests/t27_rtl.py --generated-dir build/t27/rtl
python3 tests/t27_storage.py --compiler "$T27_ROOT/target/release/t27c"
```

The runner independently enumerates logical states, checks its dense5/sparse41
mapping against the Python codecs, then writes golden vectors for the simulator.
It checks all **256 + 16 + 1,024 = 1,296** binary decoder input patterns, plus
unknown-input handling. Valid logical state counts are **243** for dense5 and
**9** for sparse41. Baseline valid states are `3^5 = 243`.

Native default stream testbenches cover every `TRIT_COUNT=1..320`; separate
capacity-specialization tests cover the selected larger configurations above. They check reset, one-cycle first-read
latency, each word and mask, final/idle behavior, busy start/write rejection,
restart after reset, memory preservation, and invalid-code signaling. These are
simulation checks, not evidence of FPGA placement, timing closure, or board operation.

## Synthesis and board experiment

[`synth_vivado.tcl`](../rtl/synth_vivado.tcl) provides generic out-of-context
synthesis for both memory variants. Supply the exact part reported by the target
board/project; this repository does not assume a device or pin mapping.

```sh
vivado -mode batch -source rtl/synth_vivado.tcl \
  -tclargs "$FPGA_PART" "$CLOCK_PERIOD_NS" "$TRIT_COUNT" build/vivado
```

The script first specializes the native capacity to
`GROUPS=ceil(TRIT_COUNT/5)`, reads only the three generated native files, and
synthesizes the `ternary_dense5_stream_t27` and `ternary_baseline5_stream_t27`
adapters with explicit `TRIT_COUNT` and `WORDS`. It keeps the target, clock,
trit count, and virtual I/O timing budget equal.
The I/O budget is an experimental constraint: one quarter of the target period,
not a measured board delay. It records the settings and writes utilization,
synthesis timing, and checkpoint files. This script requires Vivado; its reports
must be produced and inspected before drawing device-resource conclusions.
Synthesis timing estimates are not placed-and-routed frequency measurements.

For an evidence-backed comparison:

1. Record commit, tool version, exact FPGA part, target clock, constraints,
   trit count, memory policy, and the hash of the same logical weight fixture.
2. Synthesize both variants. Record actual RAMB18/RAMB36, LUT, FF, and any inferred
   distributed memory; inspect whether the intended memory survived optimization.
3. Place and route both with equivalent constraints. Record timing slack, clock,
   decoded interface delay, and resource reports. Preserve the full reports.
4. Integrate the selected board clock/reset and a verified host loader. Compare
   every output lane with the Python reference, including the last mask and all
   validity flags. Use the same logical weights and consumer in both variants.
5. If testing DDR bandwidth, add an actual burst loader and counters. Measure
   payload bytes, bus bytes, stalls, words delivered, and elapsed cycles for the
   same workload. Include padding and scale/metadata transfers. The present
   stream sequencer cannot establish DDR or tokens-per-second gains.
6. Report power only with the measurement method, rails, workload, and uncertainty.
   Keep synthesis estimates, routed results, board measurements, and full-model
   accuracy/throughput results in separate columns.

No board run, resource saving, clock frequency, power result, or inference speedup
is claimed by this package.

## FPGA measurement track (AX7203)

The measurement track of the roadmap (issue #10) runs on one explicitly named
board. Everything below is reproducible from a clean clone; the device output
itself is recorded under [`reports/fpga/`](../reports/fpga/README.md) with its
provenance, and no number from this section appears anywhere else until the
captured output matches the reference.

| Decision | Value |
| --- | --- |
| Board / part | ALINX AX7203, `xc7a200tfbg484-2` (owned hardware; the board `gHashTag/trinity-fpga` builds with) |
| Clock | On-board 200 MHz LVDS oscillator (`R4`/`T4`) through `IBUFDS` and `BUFG`; no PLL or MMCM. Two variants of one design (`CLOCK_MODE`): the harness and the cores on a second `BUFG` fed by the tick generator's divide-by-eight bit (25 MHz, single-cycle timing; the device variant), or everything on the 200 MHz clock with every register enabled one clock in eight (the tick net is then a single-cycle path; works for the small e3e8cfb design, not for the full t27 player). The enabled rate is 25 M ticks/s either way |
| Configuration | On-board FT232H JTAG, `openFPGALoader -c digilent_hs2`, SRAM only (a power cycle removes the design) |
| Host link | On-board CP2102N UART, 115200 8N1; the design reports, the host only triggers |
| Toolchain | yosys `synth_xilinx -flatten -abc9 -nocarry -nodsp -nowidelut -nosrl -family xc7` (LUTs only: with MUXF7/MUXF8 cells every nextpnr seed failed its post-placement validity check on an `A5FF` bel), nextpnr-xilinx (`--router router1 --timing-allow-fail`, placers `sa` then `heap` over seeds 1..6; the first routed build came from `heap` seed 1), prjxray `fasm2frames` and `xc7frames2bit`, all from the `regymm/openxc7` image; Vivado is not used |
| Reset | `rst_n` push button (`T6`) synchronized, plus a power-on counter |

**What runs on the device.** Every state machine and rule of the player is
executable t27 in [`t27/rtl/fpga_*.t27`](../t27/rtl/): the tick generator
(`fpga_tick.t27`), the reset generator with the heartbeat (`fpga_reset.t27`), the
UART transmitter (`fpga_uart_tx.t27`), the report line serializer
(`fpga_line_emitter.t27`), the sequencer (`fpga_trace_player.t27`) and the Edge
argmax (`fpga_edge_argmax.t27`); the tables are a generated t27 module
(`build/fpga/fpga_trace_rom.t27`, from the conformance vectors). The Verilog under
[`fpga/ax7203/`](../fpga/ax7203/) only wires them, the generated cores and the two
Xilinx clock primitives. The sequencer replays every `dot_trace`, `storage_trace`
and `join_trace` vector of
[`conformance/memory_stream_compute.json`](../conformance/memory_stream_compute.json)
on the generated cores (`build/t27/specs/rtl/{dot_stream,stream_storage,stream_view}.v`,
pinned compiler). [`tools/generate-fpga-trace-player.py`](../tools/generate-fpga-trace-player.py)
turns the vectors into a ROM bank with one core instance per parameter set and a
vector table; the stimulus and expected words use the packing of
[`tests/tb_spec_dot_trace.v`](../tests/tb_spec_dot_trace.v) and
[`tests/tb_spec_storage_trace.v`](../tests/tb_spec_storage_trace.v), imported from
[`tests/spec_stream_replay.py`](../tests/spec_stream_replay.py) so the two cannot drift.
The wrappers in [`fpga/ax7203/tms_dut_wrappers.v`](../fpga/ax7203/tms_dut_wrappers.v)
repeat the wiring of `rtl/t27/dot_stream.v` and `rtl/t27/streams.v` with the cores'
`en` and `rst_n` exposed; arithmetic and state stay in `.t27`.

Each run makes two passes over every trace vector (34 vectors: 18 dot, 9 storage, 7 join; 406 cycles), then two measurements:

- *stepped*: a trace cycle is applied with `en` high for exactly one tick; the
  outputs are captured with the same stimulus still applied, which is the sampling
  rule of the Icarus testbenches (`in_ready` and `load_ready` are combinational in
  the inputs), compared with the expected ROM word on the device, and written to
  the UART as a `C` line;
- *free-run*: the same vectors without the UART pauses, two ticks per trace
  cycle (one enabled, one holding the stimulus while the outputs are sampled);
  only the on-device mismatch count per vector is reported.

- *throughput workload*: the joined path with 64 stored words (320 trits, dense5
  codes from a closed rule) runs 16 frames of 64 beats back to back with
  activations from a closed rule (`int8((beat*13 + lane*29 + frame*5 + 7) & 255)`),
  the host recomputes every frame result; the device reports the results and the
  ticks from the first start to the last result, beats fired, results delivered,
  activation stalls and the load ticks;
- *Edge Demo*: the three template rows of `specs/memory/edge_demo.t27` in three
  joined paths in lockstep, one fixture at a time (three beats), the t27 argmax
  labels the fixture; the device reports label, accumulators and the latency in
  ticks from the start to the result for each of the six fixtures.

The report is a stream of fixed 20-byte lines (`tag`, 8 hex digits, 10 hex
digits, LF): `H` format and totals, `V` vector start, `C` observed word per
cycle, `E` device mismatches, six `K` counters (dot: beats accepted, results
delivered, input stalls, output holds, error results delivered, reset cycles;
storage: loads accepted, words delivered, lanes delivered, invalid words
delivered, starts accepted, reset cycles; join: beats fired, results delivered,
activation stalls, output holds, loads accepted, reset cycles; all evaluated on
the handshake values at the consuming edge), `F` free-run mismatches, `W`/`R`/`T`
workload frames, results and totals, `X`/`A` Edge label with latency and the
three accumulators, `D` totals. A run starts after configuration and again
whenever a start bit arrives on the UART. LEDs: heartbeat, run active, run
complete, any mismatch.

**Host side.** [`tools/fpga-capture.py`](../tools/fpga-capture.py) triggers a run,
reads the stream, compares every `C` line with the manifest independently of the
device's own comparison, cross-checks the device mismatch counts and the free-run
counts, checks every workload result against its own recomputation and every
Edge label and accumulator against the fixtures, and writes a
`trinity.fpga-capture.v1` report with the throughput and latency figures. The same tool checks the
pre-silicon run: [`tests/tb_fpga_trace_player.v`](../tests/tb_fpga_trace_player.v)
simulates the whole design in Icarus, decodes the UART bytes and hands them to the
tool (`make -C fpga/ax7203 sim`).

```sh
make -C fpga/ax7203 gen      # generated cores (T27_ROOT), ROM bank, manifest
make -C fpga/ax7203 sim      # Icarus run of the whole design + host comparison
make -C fpga/ax7203 bit      # yosys -> nextpnr-xilinx -> fasm2frames -> xc7frames2bit
make -C fpga/ax7203 flash    # openFPGALoader over the board's FT232H
make -C fpga/ax7203 capture  # UART capture compared with the reference
```

The bitstream is also built by the `fpga-ax7203` workflow (chip database cached,
`.bit`, logs and the pre-silicon capture uploaded as artifacts; both clock
variants are built, the tick-enable one as `ax7203-trace-player-tick`).
`tools/fpga-build-report.py` turns a build into a provenance record under
[`reports/fpga/`](../reports/fpga/README.md) (cell counts, nextpnr utilisation
and Fmax, frames, bitstream hash).

**Device results (2026-09-11).** Bitstream of commit e3e8cfb (tick variant,
`reports/fpga/build-2026-09-11-e3e8cfb/`, sha256 `198df230…`) configured over
the on-board JTAG in 13 s (`openFPGALoader -c digilent_hs2`, `done 1`, idcode
`0x3636093`). Three consecutive captures over the CP2102N UART are byte-identical
(17518 bytes, 922 lines): all 24 vectors, 243 cycles, 0 mismatches on the host,
0 device-side mismatches in the stepped pass and 0 in the free-run pass
([`reports/fpga/capture-2026-09-11-e3e8cfb.json`](../reports/fpga/capture-2026-09-11-e3e8cfb.json),
raw stream alongside). The counters the device reported are the ones the traces
define, for example `two_beat_frame_dense`: 2 beats accepted, 1 result delivered,
0 input stalls, 0 output holds, 0 error results, 2 reset cycles;
`load_three_words_and_read`: 3 loads, 3 words, 12 lanes, 0 invalid words,
1 start, 2 reset cycles; `backpressure_holds_result_and_stalls_input`: 3 beats,
2 results, 3 input stalls, 4 output holds. The free-run pass replays the 243
cycles in 486 ticks, 19.4 µs at 25 M ticks/s from the board's 200 MHz
oscillator (a value that follows from the clock and the design, not from an
instrument). The evidence label `fpga` is attached to these captures by the
conformance lab (`tools/conformance-lab.py`, section `stream`, field `device`)
only while a capture replayed exactly the committed vector set; the Bridge
capabilities and the Edge report keep their `emulator`/`software` labels, since
neither runs on the device.

**Device results, merged vector set (2026-09-11, later the same day).** After
the storage join (#15) the player covers 27 vectors (288 cycles). Both clock
variants of commit fb0533e ran on the board: the divided-clock variant (nextpnr
Fmax 71.9 MHz for the 25 MHz clock, PASS) and the tick-enable variant; each
capture reports 27 vectors, 0 host mismatches, 0 device mismatches in both
passes ([`reports/fpga/capture-2026-09-11-fb0533e-div.json`](../reports/fpga/capture-2026-09-11-fb0533e-div.json),
`-tick.json`, raw streams alongside). The conformance lab marks these captures
current and carries `device_evidence: fpga`.

**Device results, t27 player (2026-09-11, late evening).** Bitstream of commit
6d0cfa6 (`reports/fpga/build-2026-09-11-6d0cfa6/`, divided-clock variant, 7870
LUTs and 3612 flip-flops after place and route, nextpnr Fmax 52 MHz for the
25 MHz clock) configured over the on-board JTAG (`done 1`). Three consecutive
captures are byte-identical (23040 bytes, 1152 lines)
([`reports/fpga/capture-2026-09-11-6d0cfa6-div.json`](../reports/fpga/capture-2026-09-11-6d0cfa6-div.json)):

- all 34 trace vectors (18 dot, 9 storage, 7 join; 406 cycles): 0 host
  mismatches, 0 device mismatches in the stepped and in the free-run pass, so the
  joined read -> decode -> dot circuit is now device-verified as well;
- throughput workload: 16 frames x 64 beats, every frame result equal to the
  host's recomputation; 1024 beats fired in 1089 ticks from the first
  start to the last result (0.940 beats per tick, 68.1 ticks per
  64-beat frame, 32 activation stalls = two start-up ticks per frame), load
  phase 64 ticks. At 25 M ticks per second that is 23.5 M beats per
  second, 117.6 M lane multiply-accumulates per second, on one joined path
  clocked by the board's oscillator;
- Edge Demo: all six fixtures labelled correctly with the accumulators of the
  fixtures (step_up [480, -480, 0], step_down [-480, 480, 0], alternating [0, 0, 360], offset_step_up [249, -249, 3], offset_step_down [-255, 255, 3], noisy_alternating [1, -1, 341]), latency 7
  ticks (280 ns) from the start of a fixture to its label, three template rows
  resident in three storage cores (nine 16-bit elements, 72 code bits, for 36
  trits) with the whole classifier datapath inside the player's 7870 LUTs.

**Negative result, tick-enable variant of the t27 player.** The same commit's
`CLOCK_MODE=0` bitstream (everything on the 200 MHz clock, registers enabled one
clock in eight; nextpnr Fmax 60 MHz on that domain) configures (`done 1`) but
emits only fragments of a report (six lines in two captures,
[`reports/fpga/capture-2026-09-11-6d0cfa6-tick-FAIL.txt`](../reports/fpga/capture-2026-09-11-6d0cfa6-tick-FAIL.txt)):
the tick enable now fans out to about 3600 registers and its single-cycle path
does not hold at 200 MHz. The earlier, ten-times smaller tick design (1019
flip-flops, e3e8cfb) did run; the variant stays a fallback for small designs
only, and the divided-clock variant is the device variant of the player.

**What this track does not measure, and why.** DDR and power. DDR3 on the
AX7203 needs a DDR3 PHY (IDELAYE2/ISERDESE2/OSERDESE2 with calibration); the open
flow's support for those primitives is partial (gHashTag/trinity-fpga records an
IDDR path that never fires, openXC7 issue 114) and no DDR3 controller has been
brought up on this board with it, so any DDR figure would rest on unverified
primitives; the protocol above ("If testing DDR bandwidth …") stays the plan for
a Vivado-built controller or a repaired open PHY. Power needs an instrument on
the 12 V input or the board's rails; none is attached to the bench, and the USB
ports power only the bridges. Both are therefore separate experiments with their
own protocol, not claims. The player's clock is the board's crystal, so every
tick figure is exact by construction but no instrument measured it; nextpnr's
Fmax is an estimate, not a measurement.
