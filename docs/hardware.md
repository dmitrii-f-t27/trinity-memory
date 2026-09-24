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

The physical comparison is the block-RAM packing bench below ("Block-RAM trit
packing"): the same tensor in three 36-bit word layouts, synthesized, placed,
routed and run on the AX7203.

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

The generic Vivado script above has not been run. The open-flow board runs and
the block-RAM resource comparison are in the next section; no power result or
inference speedup is claimed by this package.

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

A start is accepted only while the player is idle (since 2026-09-22): a trigger
byte has up to five falling edges, and the player used to queue one of them
behind the run it had just started, so one byte gave two runs
(`reports/fpga/trigger-ab-2026-09-22.json`: the f07a667 player sends two runs
after "r" or 0x55, the 941c16c player one). The report is a stream of fixed 20-byte lines (`tag`, 8 hex digits, 10 hex
digits, LF): `H` format and totals, `V` vector start, `C` observed word per
cycle, `E` device mismatches, six `K` counters (dot: beats accepted, results
delivered, input stalls, output holds, error results delivered, reset cycles;
storage: loads accepted, words delivered, lanes delivered, invalid words
delivered, starts accepted, reset cycles; join: beats fired, results delivered,
activation stalls, output holds, loads accepted, reset cycles; all evaluated on
the handshake values at the consuming edge), `F` free-run mismatches, `W`/`R`/`T`
workload frames, results and totals, `X`/`A` Edge label with latency and the
three accumulators, `D` totals. A run starts after configuration and again
when a start bit arrives on the UART while the player is idle. LEDs: heartbeat,
run active, run complete, any mismatch.

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

**Block-RAM trit packing (2026-09-22).** The stream modules keep every group in
a 16-bit array element, so the players above say nothing about the memory a
packed layout saves. The packing bench measures it in the device's block RAM
([`fpga/ax7203/tms_bram_bench.v`](../fpga/ax7203/tms_bram_bench.v)): three
engines store the same 1 013 760 trits, each in its own array of 36-bit words,
the width of a RAMB36E1 in its 1K x 36 configuration.

| Layout | Trits per word | Word | Words | Bits per trit | RAMB36 at 1K x 36 |
| --- | ---: | --- | ---: | ---: | ---: |
| `b2` | 18 | two bits per trit (the lane code of the RTL decoders) | 56 320 | 2.000 | 55 |
| `d5` | 20 | four dense5 bytes; bits 35:32 unused, as in any byte-oriented layout | 50 688 | 1.800 | 50 |
| `d5d2` | 22 | four dense5 bytes and a dense2 nibble (two trits, 3^2 = 9 <= 16) in bits 35:32, the parity bits | 46 080 | 1.636 | 45 |

`d5d2` is the 22-trit, 36-bit allocation that [format.md](format.md) computes
for `dense22`, built from five small decoders (four dense5, one dense2) instead
of a base-3^22 divider; the 35-bit `dense22` stream codec itself is not used.
log2(3) = 1.585 bits per trit is the floor.

Each engine writes its words from one trit stream (a 64-bit Fibonacci LFSR,
feedback s0^s1^s3^s4, two stream bits per trit, the value 11 read as zero, so a
trit is 0 with probability 1/2 and +1 or -1 with 1/4 each; the state 2K steps
later is 2K four-input XORs, so one word is produced per clock), reads every
word back one per clock, decodes it, compares the lanes with the regenerated
stream and reports the ticks of both phases, the words that differ, the invalid
groups, the counts of +1 and -1, the dot product with the activations
`(i mod 8) + 1` and a rotate-xor checksum of the stored words. The same trits
are stored by all three, so all three must report the same counts and dot
product; the host recomputes everything from
[`tools/bram_trit_model.py`](../tools/bram_trit_model.py), an independent
statement of the rules. Every rule is t27:
[`bram_trit_codec.t27`](../t27/rtl/bram_trit_codec.t27) (encoder and decoder of
the three layouts), [`bram_trit_engine.t27`](../t27/rtl/bram_trit_engine.t27)
(the store, specialized per layout by
[`tools/generate-bram-bench.py`](../tools/generate-bram-bench.py)) and
[`fpga_bram_bench.t27`](../t27/rtl/fpga_bram_bench.t27) (sequencer and report);
the arrays become block RAM with yosys `read_verilog -nomem2reg`.

```sh
make -C fpga/ax7203 bram-sim                     # all three layouts in Icarus, host comparison
make -C fpga/ax7203 bram-sim BRAM_ONLY=2         # one layout (0 b2, 1 d5, 2 d5d2), as built for the device
make -C fpga/ax7203 bram-bit bram-flash BRAM_ONLY=2        # open flow into build/fpga/bram-2, SRAM configuration
make -C fpga/ax7203 bram-capture BRAM_ONLY=2 PORT=/dev/cu.usbserial-110
make -C fpga/ax7203 bram-ooc                     # cells of each layout and of the codec, out of context
```

The tools run in the `regymm/openxc7` image by default; `YOSYS='cd $(ROOT) && yosys'`
and `NEXTPNR='cd $(ROOT) && <nextpnr-xilinx built for the host>'` run them natively
(the builds below used a native build of the image's nextpnr-xilinx revision 45a986b:
router2 finished each single-layout design in 24-148 s, while under emulation the
image's router1 had still 12.5k of 64.8k arcs left on the all-three design after
56 minutes, `routing-attempts.txt` in the 375cf00 directory).

**Mapping.** Each engine is written against 36-bit words, so the synthesis maps
every layout the same way: `fpga/ax7203/brams_x36.txt` restricts yosys'
block-RAM library to the RAMB36E1 1K x 36 configuration, and the store is four
banks of up to 16 384 words. Left to itself, yosys put the 56 320-word b2 array
into 63 blocks of 8K x 4 (fewer output multiplexers, but a RAMB36 holds 32 Kbit
in a 4-bit mode instead of 36, and a word is spread over nine blocks); a single
array deeper than 16 blocks sometimes got one block more than ceil(words/1024)
(23 552 words -> 24, 46 080 -> 46, 56 320 -> 56), while every bank of up to 16
blocks maps exactly.

**Synthesis (2026-09-22, commit 941c16c, yosys 0.69, `make bram-ooc`: one
engine with its encoder and decoder at a time; the stats are in each
`reports/fpga/build-2026-09-22-941c16c-bram-*` directory).**

| Layout | RAMB36E1 | LUT | FF | Encoder LUT | Decoder LUT |
| --- | ---: | ---: | ---: | ---: | ---: |
| `b2` | 55 | 2 524 | 576 | 0 | 96 |
| `d5` | 50 | 2 715 | 573 | 133 | 125 |
| `d5d2` | 45 | 2 530 | 648 | 146 | 136 |

The encoder and decoder columns are the codec synthesized alone
(`codec_encoder.stat` and `codec_decoder.stat` in each directory; `make bram-ooc`
writes them as `codec_<f>_<op>.stat`, f 0/1/2, op 0 encoder, 1 decoder); the `b2` decoder's LUTs count and clear invalid
`11` lanes, the `b2` encoder is wiring. 72 of the `d5d2` flip-flops hold its
unused fourth bank (one word). LUT counts move by 1-2 % between syntheses of
identical Verilog (yosys and ABC ordering); block counts do not. The whole bench with all three engines
(`BRAM_ONLY=-1`, commit 375cf00) synthesizes to 150 RAMB36E1, 8 959 LUTs and
2 188 flip-flops
([`reports/fpga/build-2026-09-22-375cf00-bram-all`](../reports/fpga/README.md)).

**Place and route, and the device (2026-09-22).** With all three engines in one
bitstream (150 RAMB36E1, ~9k LUTs) nextpnr-xilinx did not finish routing
(router1 still had 12.6k of 64.4k arcs left after 35 minutes, router2 reported
27.6k overused wires in its first iteration; `routing-attempts.txt` in the
375cf00 directory), so each layout is built on its own (`BRAM_ONLY=0/1/2`, the
same harness in every bitstream). Builds of commit 941c16c
([`reports/fpga/build-2026-09-22-941c16c-bram-*`](../reports/fpga/README.md):
yosys 0.69; nextpnr-xilinx 45a986b built natively, `heap` placer seed 1,
`router2`; prjxray) and four runs per layout on the AX7203
([`reports/fpga/bram-capture-2026-09-22-941c16c-*`](../reports/fpga/README.md)):
the run that starts at configuration, captured while flashing, writes into
block RAM that configuration has just cleared, and three triggered runs; the
four reports are identical except for the run number in the header.

| Layout | RAMB36E1 placed | LUT | FF | Fmax estimate | Words written | Read ticks | Trits per read | Bad words | Invalid groups |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `b2` | 55 | 3 319 | 1 038 | 38.8 MHz | 56 320 | 56 321 | 18 | 0 | 0 |
| `d5` | 50 | 3 537 | 1 035 | 35.6 MHz | 50 688 | 50 689 | 20 | 0 | 0 |
| `d5d2` | 45 | 3 448 | 1 110 | 29.8 MHz | 46 080 | 46 081 | 22 | 0 | 0 |

LUTs and flip-flops include the harness every bitstream shares (UART, line
emitter, sequencer, tick and reset). All three report +1 = 253 229,
-1 = 253 385 and the dot product -674, the host model's values, after every
stored word was read back and matched the regenerated stream lane by lane: the
three bitstreams hold the same tensor, and every layout decodes on the device
at one word per enabled clock. (Every run writes the same tensor to the same
addresses, so a later run's write path is shown by the run at configuration,
whose reads could only match if every write landed.)

For this 1 013 760-trit tensor, dense5 bytes need 5 blocks fewer than two bits
per trit (50 instead of 55, -9.1%) and dense5 with a dense2 nibble in the
parity bits 10 fewer (45, -18.2%): 18 432, 20 480 and 22 528 trits per RAMB36.
At one word per read, the tensor streams out in 56 321, 50 689 and 46 081
enabled clocks (2.25, 2.03 and 1.84 ms at 25 MHz from the board's oscillator):
18, 20 and 22 trits per read port per clock. The price is the codec: 125 and
136 LUTs to decode 20 and 22 trits per word (133 and 146 to encode) in a
133 800-LUT device. The Fmax column is nextpnr's estimate for the 25 MHz
clock, not a measurement; the device ran every layout at 25 MHz. Not measured
here: external memory (the same argument for DDR needs a DDR3 controller,
below), power, and the 72-bit simple dual-port mode, where nine dense5 bytes
would hold 45 trits (1.600 bits per trit) but a t27 word is at most 64 bits.

**Real weights from the bitstream (2026-09-23).** The packing bench writes an LFSR
stream; a deployed accelerator loads fixed weights. The read-only store
[`bram_trit_rom.t27`](../t27/rtl/bram_trit_rom.t27) holds a tensor as t27 array
initializers, which yosys maps to the RAMB36E1 INIT and INITP contents, so
configuration loads the weights and a run only reads. It has the engine's ports (the
adapter is unchanged), reports the same fields, and checks itself: the rotate-xor
checksum of the decoded lanes must equal the host's checksum of the tensor, otherwise it
reports one bad word. A wrong expected checksum makes the Icarus run fail, so the check is
not vacuous. The tensor is rows 0-395 of the layer-0 query projection of BitNet b1.58
2B4T (396 x 2560 = 1 013 760 trits, exactly the bench size; +1 349 720, 0 315 515,
-1 348 525), written by [`tools/extract-bram-trits.py`](../tools/extract-bram-trits.py)
from the pinned packed checkpoint through the t27 decoders; the I2_S tensor of the GGUF
gives the same trits.

```sh
python3 tools/extract-bram-trits.py --rows 396 --output build/fpga/rom/trits.bin
make -C fpga/ax7203 bram-sim BRAM_ROM=$PWD/build/fpga/rom/trits.bin                 # all three layouts, first 1 980 trits
make -C fpga/ax7203 bram-bit bram-flash BRAM_ONLY=2 BRAM_ROM=$PWD/build/fpga/rom/trits.bin
make -C fpga/ax7203 bram-capture BRAM_ONLY=2 BRAM_ROM=$PWD/build/fpga/rom/trits.bin PORT=/dev/cu.usbserial-110
```

Builds of commit ab52409 (yosys 0.69, nextpnr-xilinx 45a986b native, `heap` seed 1,
`router2`, 25 MHz) and four runs per layout on the AX7203 (the run at configuration and
three triggered runs, identical apart from the run number;
[`reports/fpga/bram-rom-capture-2026-09-23-ab52409-*`](../reports/fpga/README.md)):

| Layout | RAMB36E1 | LUT | FF | Fmax estimate | Words | Read ticks | Bad / invalid | +1 / -1 / dot |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `b2` | 55 | 2 885 | 878 | 37.7 MHz | 56 320 | 56 321 | 0 / 0 | 349 720 / 348 525 / 4 040 |
| `d5` | 47 | 2 850 | 875 | 32.4 MHz | 50 688 | 50 689 | 0 / 0 | 349 720 / 348 525 / 4 040 |
| `d5d2` | 45 | 3 415 | 878 | 32.4 MHz | 46 080 | 46 081 | 0 / 0 | 349 720 / 348 525 / 4 040 |

The FASM of the d5d2 build carries all 5 760 INIT and 720 INITP words of its 45
blocks, and the first run after configuration returns the exact trits, so block-RAM
initial contents work through prjxray on this board (DNA 0x00389c0c2d85e85c, die
38.2 C, VCCINT 0.995 V with the store loaded). The d5 store takes 47 blocks, not the
50 of the write-read bench: its parity bits are constant in the initializers, so yosys
narrows the words to 32 bits and packs them across the parity columns of other address
ranges (15 blocks per 16K-word bank; every block of bank 0 drives its four parity
outputs and 14 have nonzero INITP). A writable memory cannot be packed that way, and
d5d2 is still two blocks smaller.

**Pairs of words: 45 trits in 72 bits (d5p, 2026-09-23).** Four parity bits hold two
trits; eight hold five as one more dense5 byte. The store's `PAIR` mode (`BRAM_ONLY=3`, read-only
store only) keeps 45 trits in a pair of 36-bit words: four dense5 bytes per word, decoded by
the adapter's d5 decoder, and the two parity nibbles as one more dense5 byte (low nibble in the
first word, high nibble in the second), which the store decodes into lanes 20-24 of the second
word after keeping the first nibble in a register. That is the density of the 512 x 72 mode
(1.600 bits per trit) in the 1K x 36 mode: the tensor needs 45 056 words, 44 RAMB36E1 (16 +
16 + 12), the floor ceil(1 013 760 * log2(3) / 36 864) = 44. Builds of commit ae754b0: 3 529
LUT, 881 FF, estimate 28.3 MHz at 25 MHz; with `PIPE 1` at 50 MHz 3 036 LUT, 1 356 FF,
estimate 60.1 MHz. Four runs each on the AX7203 (25 and 50 MHz), all PASS and identical:
45 057 and 45 060 read ticks, 0 bad words, 0 invalid groups, +1 349 720, -1 348 525, dot 4040
([`reports/fpga/bram-rom-capture-2026-09-23-ae754b0-d5p*`](../reports/fpga/README.md)).

**Every layout that fills a word (2026-09-23).** [`tools/bram-layout-study.py`](../tools/bram-layout-study.py)
enumerates the multisets of base-3 group widths (2, 4, 5, 7, 8, 10, 12, 13, 15, 16 bits for 1-10 trits)
that reach floor(w / log2 3) trits: 5 layouts for 18-bit words (11 trits) and 32 for 36-bit words (22
trits). It writes each decoder as executable t27, checks the generated C against a Python statement of
the rule on 3 002 words (all 37 match) and synthesizes it alone with yosys 0.69, LUT-only and with
MUXF7/MUXF8 ([`reports/fpga/layout-study-2026-09-23-a548df3`](../reports/fpga/README.md)). The cheapest
36-bit layout is two bytes and four 5-bit groups of three trits, `8, 8, 5, 5, 5, 5`: 94 LUTs at four
levels, against 132 for `8, 8, 8, 8, 4` (d5d2 written the same way) and 136 for four plain bytes (20
trits). The cheapest 18-bit layout is `8, 5, 5`, 47 LUTs. Groups wider than eight bits need division by
powers of three and cost 126 to 318 LUTs.

**Clock.** The benches decode, check and accumulate a word in the tick after its read;
the longest path runs through the per-word check built from LUT-only adders (`-nocarry`).
Carry chains do not help in this flow: the d5d2 write-read bench without `-nocarry`
maps to 493 CARRY4 and 6 947 LUTs and nextpnr estimates 9.7 MHz
([`reports/fpga/build-2026-09-23-37e8316-bram-d5d2-carry`](../reports/fpga/README.md)).
The store's `PIPE 1` registers the decoded lanes, the sums over each half of the lanes and
the per-word sums before the accumulators (read ticks = words + 4): the estimate for d5d2
rises to 71.2 MHz (49.6 MHz with one sum stage). `CLOCK_MODE` 2 and 3 clock the bench at
50 and 100 MHz from bits 1 and 0 of the tick counter (`BAUD_DIV` 434 and 868). Built for
50 MHz (commit 844d6ea, `BRAM_PIPE=1 CLOCK_MODE=2 BAUD_DIV=434 FREQ_MHZ=50`: 45 RAMB36E1,
2 750 LUT, 1 316 FF, estimate 62.4 MHz, PASS at 50 MHz), the d5d2 store ran four times on
the AX7203 at 50 MHz with the results of the 25 MHz runs: 46 084 read ticks (0.92 ms),
0 bad words, 0 invalid groups, the same counts, dot product and word checksum
([`reports/fpga/bram-rom-capture-2026-09-23-844d6ea-d5d2-pipe-50mhz-*`](../reports/fpga/README.md));
22 trits per clock at 50 MHz are 1.1 G trits per second from one read port. The 100 MHz
build (estimate 71.2 MHz) was not flashed.

**Matrix-vector product on the device (2026-09-23).** The stores above count and check
the trits; [`bram_trit_matvec.t27`](../t27/rtl/bram_trit_matvec.t27) multiplies them. It
computes y = W x for the same 396 x 2560 slice and a real int8 vector: the input of the
layer-0 attention projections for token 128000 (`<|begin_of_text|>`), which
[`tools/extract-bram-activations.py`](../tools/extract-bram-activations.py) builds from the
pinned checkpoint (embedding row, RMSNorm in float32 with bf16 rounding, absmax int8
quantization: 2 560 values in [-86, 127], 75 zeros). The matrix is stored column by column
in groups of 22 rows (word k of group g holds column k of rows 22g to 22g + 21), so the 396
rows are 18 groups and the words need no padding: 46 080 d5d2 words, 45 RAMB36E1 as in the
stores. The vector is four signed bytes per 32-bit word in one more block. Each clock reads
one word and one activation; 22 accumulators (20-bit two's complement, three per register)
add +x, -x or nothing for their lane's trit, and after a group's last column the 22 sums are
held and added up one per clock while the next group accumulates. The store reports how
many outputs are above and below zero, their sum and the rotate-xor checksum of all 396
outputs in row order, and compares that 64-bit checksum with the host's value, which the
generator writes into the bitstream (`bad_words` 1 on a mismatch). The host's product is
computed by the model and checked against numpy. In the whole-bench Icarus run (two groups,
44 outputs) one flipped trit changed the sum and raised `bad_words`; the reported 40 bits
of the checksum did not change, which is why the store checks all 64.

```sh
python3 tools/extract-bram-activations.py --token 128000 --output build/fpga/rom/act.bin
make -C fpga/ax7203 bram-sim BRAM_ONLY=2 BRAM_ROM=$PWD/build/fpga/rom/trits.bin BRAM_MATVEC=$PWD/build/fpga/rom/act.bin
make -C fpga/ax7203 bram-bit bram-flash BRAM_ONLY=2 BRAM_ROM=$PWD/build/fpga/rom/trits.bin BRAM_MATVEC=$PWD/build/fpga/rom/act.bin \
    CLOCK_MODE=2 BAUD_DIV=434 FREQ_MHZ=50
```

Built from commit 88e8ba4 for 50 MHz: 46 RAMB36E1, 4 055 LUT, 1 882 FF, 0 CARRY4, estimate
80.2 MHz ([`reports/fpga/build-2026-09-23-88e8ba4-bram-matvec-50mhz`](../reports/fpga/README.md)).
Four runs on the AX7203 at 50 MHz, all PASS and identical apart from the run number: 46 105
read ticks (words + 25, 0.92 ms), 0 bad words, 0 invalid groups, 202 outputs above zero and
194 below, sum 3 441, outputs in [-3 356, 2 225]
([`reports/fpga/bram-matvec-capture-2026-09-23-88e8ba4-d5d2-50mhz-*`](../reports/fpga/README.md)).
That is 22 ternary multiply-accumulates per clock, 1.1 G per second from one read port, for
about 1 300 LUTs more than the pipelined store that only counts (2 750 LUTs). nextpnr's
worst path for the estimate is the reset net spread across the die (11.7 of its 12.4 ns are
routing), not the multiply-accumulate datapath.

**What this track does not measure, and why.** DDR and power. DDR3 on the
AX7203 needs a DDR3 PHY (IDELAYE2/ISERDESE2/OSERDESE2 with calibration); the open
flow's support for those primitives is partial (gHashTag/trinity-fpga records an
IDDR path that never fires, openXC7 issue 114) and no DDR3 controller has been
brought up on this board with it beyond a memory test (the DDR3 flow of issue #60,
next section, calibrates on the board and passes our write/read-back pattern test
over all of U6 in x16 and over all 1 GiB in x32 for some placements, and #62's read
path counts its words and clocks per run; the stage-2 weights-per-second comparison, #65,
has not been made), so any DDR figure would rest on primitives
whose timing nothing analyses; the protocol above ("If testing DDR bandwidth …") stays the plan for
a Vivado-built controller or a repaired open PHY. Power needs an instrument on
the 12 V input or the board's rails; none is attached to the bench, and the USB
ports power only the bridges. Both are therefore separate experiments with their
own protocol, not claims. The player's clock is the board's crystal, so every
tick figure is exact by construction but no instrument measured it; nextpnr's
Fmax is an estimate, not a measurement.

### DDR3 in the open flow

Issue #60 is the build: synthesis, place and route and a bitstream with recorded
hashes. Issue #61 is the board. **State on 2026-09-24 (all nextpnr-xilinx 0.9.7,
SRAM loads only, AX7203 DNA `0x00389c0c2d85e85c`):**

- **x16 (chip U6, 512 MiB):** the build with our pattern test (`7deeef16`)
  calibrated on 4 of 4 loads and then wrote, read back and compared every one of
  the 2^25 bursts with address-unique data, true and complement: 881 passes of
  512 MiB, 0 wrong bits (3 captures of 60 s and one of 600 s); a rebuild of the
  same netlist at c7d5db4 (the same bitstream from the sync word) passed a fifth
  load (63 passes). The
  `UART_DEBUG_BIST` build (`0e94ee86`) printed `correct_read_data` =
  33,554,431, the self-test's checked reads exactly, and no wrong-read report, on
  3 of 3 loads.
- **x32 (both chips, 1 GiB):** placement decides it. With our pattern test, 5 of
  the 10 placements (seeds) of the same netlist loaded on the board passed the
  full 1 GiB test on every load (13 loads, 1,858 passes of 1 GiB, 0 wrong bits,
  two captures of 600 s); 5 failed: 4 never calibrated, and 1 calibrated after 255 or
  more retries and then read wrong bits on U5 only. What separated them was, in
  nextpnr's delay model, the skew between CK and each byte lane's write-DQS clock,
  both of which leave the clock network through one fabric LUT (below). A
  prediction from it, committed before four more seeds ran, was right as written
  for two (19 and 12 pass); 9 failed as predicted but in another way (it never
  left alignment), and 10, predicted "pattern test uncertain", passed.
- Not run for #61: the 45a986b8 line, any VREF change, `BIST_MODE 2`, a hold between
  write and read, a descending address order, a soak longer than 600 s, and x32
  with `UART_DEBUG_BIST`.
  Details, evidence and limits of the pattern test: "Board runs of the pattern test", below.
- **Read path (#62), x16 only:** a t27 burst reader and consumer (A) on the user port
  filled and read back a 2560 x 6912 region (17,694,720 trits) in baseline2 and in dense5:
  bursts 0-276,479 and 0-221,183, 0.82 % and 0.66 % of U6, the same bursts in every run.
  The build of record (42b6f5a9 seed 11) ran 1,549 runs in 3 loads of 20 s: 0 bad words,
  0 stray acks, every run equal to the host model, 0.9447-0.9449 words per controller clock.
  The earlier netlist a6d9745f passed the same way on one placement (1,653 runs, 3 x 20 s) and
  never calibrated on another. No soak longer than 20 s ("DDR3 read path (#62)", below).

**What is built.** [UberDDR3](https://github.com/AngeloJacobo/UberDDR3) at
`79d8fd3e30ebba6acd84eecac2fa57b7f95f4544`: `rtl/ddr3_top.v`,
`ddr3_controller.v`, `ddr3_phy.v` and the `LICENSE`, each pinned by sha256 in
[`fpga/ax7203/ddr3/uberddr3.lock`](../fpga/ax7203/ddr3/uberddr3.lock) and fetched
into `build/uberddr3/` by `make -C fpga/ax7203 ddr3-fetch`.
[`tools/fetch-uberddr3.sh`](../tools/fetch-uberddr3.sh) keeps a file whose hash
matches, replaces one whose hash does not with a fresh download, and stops when
a download does not match. Before every synthesis `fetch-uberddr3.sh --verify`
checks the four hashes again, so a locally edited UberDDR3 file (a VREF or PHY
experiment, say) stops the build instead of being synthesized under the pinned
commit's name; the report also exits with an error on a file that does not match.
UberDDR3 is GPL-3.0-or-later and this repository Apache-2.0, so none of it is
committed, and the demo files are not used: the top, the clocking and the
constraints under [`fpga/ax7203/ddr3/`](../fpga/ax7203/ddr3/) are ours.
`tms_ddr3_ax7203.v` only wires `ddr3_top` (ROW 15, COL 10, BA 3; `BYTE_LANES` 2
for x16 = chip U6, DQ[15:0], or 4 for x32; `SDRAM_CAPACITY 4` = 4 Gb, for which
UberDDR3 uses tRFC 300 ns instead of the inherited 8 Gb's 350 ns; `SPEED_BIN 3`;
`ODELAY_SUPPORTED 0`; `BIST_MODE 1`; `NO_IOSERDES_LOOPBACK 1`, which is the
`ddr3_phy` default at 79d8fd3 (`ddr3_top` does not pass it; a `chparam` only pins
it in case a later commit changes the default); DIC RZQ/7, RTT_NOM RZQ/4; the user
Wishbone port idle in the #60 builds, driven by the pattern test with
`PATTERN_TEST` 1, below), one `PLLE2_ADV`, the t27 reset generator on T6 (LVCMOS15)
and a status line: [`t27/rtl/fpga_ddr3_status.t27`](../t27/rtl/fpga_ddr3_status.t27)
sends `H` (build id, byte lanes, DDR3 clock period) and then `S` lines
(`o_calib_complete`, the calibration state from `o_debug1`, the highest state
reached, the returns to IDLE, controller clocks) through the t27 line emitter
and UART transmitter on N15 at 115200 baud. A line takes about 1.7 ms and
calibration moves through its states far faster, so changes are coalesced: an
`S` line carries the latest state whenever the emitter is free (and one goes out
periodically); the highest state and the count of returns to IDLE are kept every
clock and are exact, the sequence of intermediate states is not. LEDs:
heartbeat, `o_calib_complete`, PLL locked, a return to IDLE since reset (the
netlist calls it `recalibrated` and the count `recals`; the names are kept so the
netlist of the reported builds does not change). A return to IDLE has two causes
in UberDDR3 79d8fd3e: a wrong read in the self-test (`reset_from_test`, L3657)
and a failed read-alignment step (`reset_from_calibrate` in ANALYZE_DATA and
CHECK_STARTING_DATA, L3047 and L3153); the `S` line keeps the highest state, not
the state before a return, so a nonzero count does not say which. Both can fire
only before calibration completes. Calibration state 23 (DONE_CALIBRATE) with
`BIST_MODE 1` and no return to IDLE means the one self-test pass had no wrong
read, and that pass is weaker than "the whole memory works" in two ways
(self-test coverage, below). The Wishbone pattern test the host checks is #61. The
build id on the `H` line is `BUILD_ID`, baked into the netlist (by default the
short HEAD at synthesis; the netlist is rebuilt when it changes). The report
takes its `commit` from the netlist, stops when they differ, and records the
source tree the synthesis ran on (`ddr3.source_tree`: HEAD and tracked changes).

**Pins and I/O.** The 71 DDR3 assignments of the LiteX AX7203 platform
(litex-boards 9f84c87, `alinx_ax7203.py` L102-L125; a third-party AX7203 MIG
project agrees pin for pin, the ALINX manual excerpts seen confirm only DQ0-DQ2
and DQS0_P): DQ, DQS and DM in bank 35 (byte lane *n* in byte group T*n*),
address, command and CK in bank 34, both banks at 1.5 V. SSTL15 / DIFF_SSTL15
(RESET# LVCMOS15), `IN_TERM UNTUNED_SPLIT_50` on DQ and DQS (HR banks have no
DCI), `SLEW FAST` everywhere. x32 adds `tms_ddr3_x32.xdc` (lanes 2-3). Clock,
reset, LEDs and UART are those of the verified `tms_trace_player.xdc`.
`tests/test_ddr3_flow.py` checks the pins against the LiteX list, the I/O
properties and the ports of the top. Unused bank-34/35 pins, among them U5's
DQ, DQS and DM in x16 and the bank-35 VREF pins E3 and N3, get the all-zero IOB
configuration (weak pull-down, no IN_TERM, no input standard) plus STEPDOWN,
which `fasm2frames` adds to every unused IOB of a bank that has any STEPDOWN
feature (so the FASM alone does not show it). Checked by decoding the frames of
the x16 build and of the AX7103B probe against prjxray-db a90f27c1 (U5's tiles
RIOB33_X105Y157/163/169, E3's RIOB33_X105Y187 IOB_Y1; a local decoder, not
committed). In x16, U5 still receives CK, the commands and ODT; with ODT on
during writes it terminates its undriven DM near VTT and may store what it
samples. Its contents are never read, so this is harmless.

**Clocks.** 200 MHz (R4/T4) -> PLL, CLKFBOUT_MULT 5 (VCO 1000 MHz): CLKOUT0 /12
= 83.33 MHz controller clock, CLKOUT1 /3 = 333.33 MHz DDR3 clock, CLKOUT2 /5 =
200 MHz IDELAYCTRL reference, CLKOUT3 /3 at 90 degrees; UberDDR3's periods
12,000 / 3,000 ps (DDR3-667, the 4:1 controller the demo uses). The Makefile
appends `create_clock` on the four BUFG outputs to the XDC it hands nextpnr
(`DDR3_PLL_MULT` / `DDR3_DDR_DIV` change the PLL and the constraints together;
MULT 8 with DIV 5 would be 80 / 320 MHz, not built here).

**CK and the write-DQS clock leave the clock network through a fabric LUT.**
With `ODELAY_SUPPORTED 0`, `ddr3_phy.v` inverts the DDR3 clock for the CK output
buffer (L473, `.I(!i_ddr3_clk)`) and for the DQS OSERDES (L1126,
`.CLK(!i_ddr3_clk)`). yosys makes one inverter of it. nextpnr-xilinx 0.9.7 folds
inverters into IDDR C and ISERDESE2 CLKB/OCLKB only (`pack_io_xc7.cc`), not into
OSERDESE2 CLK or an OBUFDS input, so it stays a LUT1 in the fabric (x16
`SLICE_X163Y168/D6LUT`, x32 `SLICE_X162Y147/A6LUT` in the builds below). The
333 MHz CK and the write-DQS OSERDES clock therefore pass through that LUT and
general routing: the DQS OLOGIC clocks come from `IOI_IMUX` (2 byte-group tiles
in x16, 4 in x32), CK reaches its OBUFDS through an OLOGIC D1 route-through
(`RIOI3_TBYTESRC_X105Y143`, pins R3/R2). The report lists both
(`ddr3.fabric_clocks`) with the LUT's placement and nextpnr's delays: buffer to
LUT 1.22 ns, LUT to the DQS OSERDES and to CK 1.2-2.6 ns, against 1.12-1.15 ns
from the same buffer to every leaf-clocked SERDES pin. That route sets the phase
of CK against the commands (launched from `clk_ddr` on the leaf), of write DQS
against DQ (`clk_ddr_90` on the leaf), and of the DQS OSERDES CLK against its
CLKDIV. Nothing times it, the seed search does not see it, and it moves with the
seed: over seeds 1-8 of the same netlist (the seed sweeps below) the delay from
the clock buffer to the DQS OSERDES and CK pins spans 2.47-4.63 ns in x16 and
2.26-3.94 ns in x32 with 0.9.7, and up to 5.61 ns with 45a986b8 (x32 seeds 3
and 5, where the LUT sits 2.9 ns from the buffer), while every leaf-clocked
SERDES pin stays at 1.12-1.15 ns in every run; at 333 MHz the period is 3 ns.
These are nextpnr's delay model, not silicon. The AX7103B demo, which its author
reports passing on its board, has the same structure (its FASM has the same
fabric-fed DQS clocks). For #61:
keep two or three native seeds built and hashed (for example
`make -C fpga/ax7203 ddr3-bit ddr3-report DDR3_WIDTH=16 DDR3_SEEDS=2
DDR3_BUILD=$PWD/build/fpga/ddr3-x16-seed2 DDR3_REPORT=...`) so that a calibration
failure can be told apart from seed luck. Folding the inverter into the OSERDES
(`ZINV_CLK`, which `fasm.cc` already writes from `IS_CLK_INVERTED` and prjxray-db
has) would take DQS off the fabric; CK would stay on it. Not done here.

**Why nextpnr-xilinx 0.9.7 for this design, and the 45a986b8 fallback.** The
`regymm/openxc7` image and our block-RAM builds use nextpnr-xilinx 45a986b8
(0.8.2-79). Its `fasm.cc` writes the same PLLE2 lock and loop-filter values into
every PLL (`LKTABLE 0xB5BE8FA401`, `TABLE 0x3B4`, marked FIXME), which are
Vivado's values for CLKFBOUT_MULT = 8; this PLL runs at MULT = 5, for which
Vivado programs `0x73BE8FA401` / `0x1EC` (tables harvested from Vivado bitstreams
per MULT, openXC7/nextpnr-xilinx#138, first in 0.9.3). 0.9.4 and later derive
clocks through PLLs (#155), 0.9.5 and later carry the fix of the constant-holdout
bug of 0.9.1-0.9.4 (#180 / #184, a constant-routing fix, not a DDR3 PHY fix).
The default DDR3 line (`DDR3_PNR_LINE=release`) therefore uses nextpnr-xilinx
**0.9.7** (`0eae9fbb19dfb83cdd30d5048d8b0ba744180ad0`, built with the same CMake
options as the 45a986b8 build: `-DARCH=xilinx -DBUILD_GUI=OFF -DBUILD_PYTHON=OFF
-DUSE_OPENMP=OFF`), a chip database from its own `bbaexport.py` with its
submodules (prjxray-db `a90f27c1`, nextpnr-xilinx-meta `a4af910c`), and
`fasm2frames` / `xc7frames2bit` of the image reading that same prjxray-db. The
report of every DDR3 build decodes the PLL from the FASM and compares its tables
with Vivado's values for the programmed MULT (`pll_check`).

This departs from the only hardware precedent. The one report of UberDDR3
working with openXC7 is its author's AX7103B BIST pass of 2025 (the commit "all
example demos passing openxc7 run!" of 2025-03-02, the openiphub post of
2025-03-21); no independent evidence was found. The constant tables are in
`fasm.cc` from 2019 until e33b5f1a (merged 2026-08-12, first tag 0.9.3), so every
nextpnr-xilinx of 2025, 3374e5a6 (the revision toolchain-nix pinned when the post
was written) and 45a986b8 included, writes the MULT = 8 values, and that
bitstream ran its MULT = 5 PLL with them (#61's Risks rely on this). 0.9.7 is 256 commits past
45a986b8 (reworked carry packing, constant routing), tagged on 2026-09-22, and no
UberDDR3 build from it is known to have run on hardware. #60 left "stay on
45a986b8, or also build 0.9.5 or later" open, and #61 lists "nextpnr 0.9.5 or
later" among the founders' options at its decision point; both lines are
therefore built. `DDR3_PNR_LINE=image` builds the same design with the image's
nextpnr-xilinx 45a986b8 (or a native build of it through `NEXTPNR`, as for the
block-RAM designs), the image's chip database (`make chipdb`) and the image's
prjxray-db (`6c02fde2`), into `build/fpga/ddr3-x<width>-nextpnr-45a986b8/`; its
reports are `...-nextpnr-45a986b8/` below, `pll_check` FAIL with the note that
the tables are the MULT = 8 values. Either line is a candidate for #61; the 0.9.7
x16 build was flashed first (below), the 45a986b8 line has not run.

**VREF.** nextpnr ignores `INTERNAL_VREF` in the XDC (`xdc.cc`) and sets
`HCLK_IOI3.VREF.V_675_MV` for every bank with a single-ended SSTL input, in
45a986b8 and in 0.9.7 alike. Every DDR3 FASM here has exactly one such line,
`HCLK_IOI3_X263Y182.VREF.V_675_MV`: that HCLK tile sits in the middle row of
bank 35 (IOB rows Y150-Y199 of the package file), so the DQ inputs compare
against an internal 0.675 V instead of the board's 0.75 V VTTREF on the bank-35
VREF pins (AX7103 core-board schematic; no AX7203 schematic was found). Bank 34
has no single-ended SSTL input (address and command are outputs, CK and the 200
MHz clock differential), so it gets no VREF bit. Not patched here: #61 decides
whether a patch (`V_750_MV`, which prjxray-db has, or no internal VREF) is
needed.

**Build (native, on the Mac).** yosys 0.69 `synth_xilinx -flatten -abc9 -arch
xc7` (the demos' synthesis, carry chains and wide muxes allowed), native
nextpnr-xilinx `--placer heap --router router2 --timing-allow-fail` with a seed
search that keeps the first seed of 1-8 whose routed design meets every clock
constraint (`sa` stops with "No wire found for port O6" on this design), prjxray
of the image under emulation. Commit `f07e91cd`; reports in
[`reports/fpga/ddr3-build-2026-09-24-f07e91cd-x16/`](../reports/fpga/ddr3-build-2026-09-24-f07e91cd-x16/)
and `-x32/` (nextpnr-xilinx 0.9.7) and
[`...-x16-nextpnr-45a986b8/`](../reports/fpga/ddr3-build-2026-09-24-f07e91cd-x16-nextpnr-45a986b8/)
and `...-x32-nextpnr-45a986b8/` (45a986b8, built at 170e1a2 and 9302b72 with
`BUILD_ID=f07e91cd`: the same netlists). Each holds `build.json`
(`trinity.fpga-build.v1` with a `ddr3` block), `yosys_stat.txt`, `nextpnr.log`
and the XDC nextpnr read; `make ddr3-report` writes all four. The 0.9.7 reports
were rewritten after the review from the same build directories (no rebuild; the
SDF and routed netlist behind `fabric_clocks` come from a rerun of seed 1 whose
FASM is byte-identical), which is why their `source_tree` is null.

One command builds and reports both widths. The reported 0.9.7 builds were made
with this command line at f07e91cd. `BUILD_ID=f07e91cd` rebuilds that netlist
only at a commit whose DDR3 sources equal f07e91cd's (the #60 branch up to
master 00262ff); without it the netlist carries the current commit and its
FASM, resource counts and Fmax differ. From 7deeef1 on (#61) the top has the
pattern test and the Makefile builds it by default, so the same command at a
later commit builds the pattern-test netlist and labels it `f07e91cd`, and
`DDR3_PATTERN=0` gives the #60 builds' idle port but not their netlist (the
second reset stage and the Wishbone nets stay; at 01f44eb x16 seed 1 gave FASM
`f3d04a37…` at 73.50 MHz against the report's `4af339a6…` at 90.93 MHz, a review
build, not committed). So the #60 builds are rebuilt from a checkout of 00262ff
(as the x32 rebuild of the board runs was, below). `make ddr3-report` refuses to
replace an existing report directory (the default name is today's date and the
build id, which a same-day rebuild would reuse) unless `DDR3_REPORT_OVERWRITE=1`:

```sh
git worktree add ../trinity-memory-60 00262ff && cd ../trinity-memory-60   # the #60 sources
make -C fpga/ax7203 ddr3-fetch ddr3-cores T27_ROOT=/path/to/t27     # UberDDR3 at the pinned commit, the t27 cores
make -C fpga/ax7203 ddr3-all BUILD_ID=f07e91cd T27_ROOT=/path/to/t27 \
  YOSYS='cd $(ROOT) && yosys' \
  IMAGE=regymm/openxc7@sha256:eced1cdd4727549f2d983328e0cf170fb6f6f67d87f19b2bf24365163368c70c \
  DDR3_TOOLS=$T/nextpnr-xilinx-0.9.7 DDR3_XRAY=$T/prjxray-db-a90f27c1 DDR3_META=$T/nextpnr-xilinx-meta-a4af910c
# the 45a986b8 line (image chip database from `make -C fpga/ax7203 chipdb`, prjxray-db of the image):
make -C fpga/ax7203 ddr3-all DDR3_PNR_LINE=image BUILD_ID=f07e91cd T27_ROOT=/path/to/t27 \
  YOSYS='cd $(ROOT) && yosys' NEXTPNR='cd $(ROOT) && /path/to/nextpnr-xilinx-45a986b8/build/nextpnr-xilinx' \
  IMAGE=regymm/openxc7@sha256:eced1cdd4727549f2d983328e0cf170fb6f6f67d87f19b2bf24365163368c70c
```

`$T` is a directory outside the repository (here `tools-native/`). Without the
`DDR3_TOOLS`/`DDR3_XRAY`/`DDR3_META` overrides, `make ddr3-nextpnr ddr3-chipdb`
clones 0.9.7 with both submodules into `build/tools/nextpnr-xilinx-0.9.7/`, builds
it (built-from-<commit> stamp) and writes the chip database to
`build/fpga/chipdb-nextpnr-0.9.7/` with a `.inputs` file naming the nextpnr,
prjxray-db and meta revisions it was built from (the database is remade when one
of them changes). Two things differ on this Mac. nextpnr-xilinx 0.9.7's CMake
finds eigen3 through `pkg-config` (`pkg_check_modules`, which 45a986b8 did not
use), and this Mac has none: the reported binary was configured with a small
stand-in that answers only for eigen3 (`tools-native/pkg-config-shim`);
`brew install pkgconf` is the ordinary way. And the submodule clone did not
finish (git ran at about 24 KB/s): the artix7 part of prjxray-db `a90f27c1` and
of the meta `a4af910c` was assembled from the image's older database plus the 26
files that differ, every file checked against the git blob hash of the pinned
tree (their `ASSEMBLED-*.txt` names the commit, which the report reads), and a
`.source-<commit>` stamp was placed by hand in the source tree so that make does
not clone again. Leaving out `YOSYS` runs the image's yosys 0.62 under
emulation, which gives another netlist (4,387 LUTs instead of 4,424 in x16, in
a local run); leaving out the `IMAGE` digest runs `:latest`, which resolved to
that digest here on 2026-09-24.

| Variant | yosys LUT / FF / CARRY4 | nextpnr SLICE_LUTX / SLICE_FFX | OSERDESE2 / ISERDESE2 / IDELAYE2 | Routed Fmax estimate, controller clock (83.33 MHz) | PLL tables | FASM sha256 |
| --- | --- | --- | --- | --- | --- | --- |
| x16, 0.9.7 | 4,424 / 2,622 / 153 | 5,792 / 2,622 | 45 / 18 / 18 | 90.93 MHz (seed 1) | PASS (MULT 5) | `4af339a6115480144923a2d8ea54b5780f7658b53b41f9138a7a2c7530ff9602` |
| x32, 0.9.7 | 6,818 / 4,094 / 156 | 8,165 / 4,094 | 65 / 36 / 36 | 86.51 MHz (seed 1) | PASS (MULT 5) | `e18284b089e863d48bae16feb4e3522e272ec2cc2cc089f0cfa6e2aba5e2de5e` |
| x16, 45a986b8 | same netlist | 5,719 / 2,622 | 45 / 18 / 18 | 86.99 MHz (seed 1) | FAIL (MULT 8 tables) | `3e097bea0de48556a708e5e8b89313b9389f48869db583d644438e195d57aaa9` |
| x32, 45a986b8 | same netlist | 8,136 / 4,094 | 65 / 36 / 36 | 87.44 MHz (seed 1) | FAIL (MULT 8 tables) | `f9a9846195745cb46934e7574ecf625caf5c27b9eb576b4d0b0332e2481224b5` |

**Identity.** `xc7frames2bit` writes the date and time it ran into the `.bit`
header, so no two runs give the same whole-file sha256, on one machine or two.
What a rebuild reproduces is the FASM, the frames and the `.bit` from its sync
word (0xAA995566) on; `build.json` records them as `reproducible_identity`. The
whole-file sha256 identifies only the one file in `build/fpga/` that a report
was written for, the file to flash:

| Variant | frames sha256 | `.bit` sha256 from the sync word | `.bit` sha256, whole file (local file of the report) |
| --- | --- | --- | --- |
| x16, 0.9.7 | `336e3c6d253dc98355b1bd42d9d1a8d022cc68b88fb3e7683b13fd820e389a4b` | `440ee6c1c4b6efec92b05234c710dd2b289046eb2a42f5fba9761b499ffe57c4` | `0ae4ed6c8d59f6dc5974dc2b59f732ecb5c1c7e93cc2d9c3c08e24e33e496ff0` |
| x32, 0.9.7 | `b95fc020c74249a7e74a8861b602c1218cd652fda06ec83554bdbad12ba83e21` | `f409b7bb57341be55db0a054e02e9ea7a9f31eff6b53256827166999785f9291` | `b9c89cc9d54eb21182b97bddff9a50fdd59cc7051cda2c568f90b8c9ffa4edae` |
| x16, 45a986b8 | `af39f28141d61e25054174dd6447d04a3981b60663c2b0ed9ddde7514f25d0b7` | `a6c2db67fdd360973c480033046e642dc36c10002bf275b6509638867c7a7fbe` | `53b3f95734b475ac7f996188d55163314dfb0bb98687cf89995c0cb6550d923f` |
| x32, 45a986b8 | `75ca9039e8ba5130a5474c47542ea4a38a9b2fd774391dfde044288c85e2b5ba` | `157fe0954517ba8f59bc74dcfbc5100de3d666b5b95f6538233e1b48a7771b16` | `675e5c00e8129976ec0eb74283f3906c86e20a0b31510f6517341014143a6ac0` |

A clean clone of 9d09da1 (the review fixes) with the command line above
reproduced the FASM, frames and from-sync sha256 of both 0.9.7 builds exactly;
its `.bit` files differed from the reported ones only in the header time
(whole-file x16 `897a20b5…`, x32 `29c36161…`). `make -C fpga/ax7203 ddr3-flash
DDR3_WIDTH=16 DDR3_FLASH_REPORT=<build.json>` flashes the existing `.bit` only
when its whole-file sha256 or its sha256 from the sync word on matches the
report (so a clean-clone rebuild of the reported build loads, and the line
printed says which hash matched), SRAM only, and never builds;
`DDR3_EXPECT_SHA256=<sha256>` checks the whole file only
([`tools/ddr3-flash-check.py`](../tools/ddr3-flash-check.py)). That is #61, with
the board and approval.

All four: 1 PLLE2_ADV, 1 IDELAYCTRL, 4 BUFG; 9,730,786-byte bitstreams
(9,730,803 for 45a986b8, whose `.bit` header names a longer path);
chip database sha256 `5c88a26d…a14786a7` (0.9.7) and `81dc7e2c…fc530df499`
(the image's); 32 "multiple conflicting drivers" warnings from yosys in
`ddr3_controller` (as in the probe; UberDDR3's demos were validated with yosys
0.44).

**What the Fmax covers.** The Fmax column is nextpnr's estimate after routing
for register-to-register paths inside the fabric on the controller clock, and
nothing else. nextpnr-xilinx 0.9.7 gives the ports of OSERDESE2, ISERDESE2,
IDELAYE2 and IDELAYCTRL no timing class (`arch.cc` `getPortTimingClass` returns
`TMG_IGNORE`), so no path into or out of the PHY primitives is analysed, on any
clock. On the 83.33 MHz controller clock these are not trivial: with `DLL_OFF 0`
the command reaches the command OSERDES D1-D4 combinationally from the scheduler
(`ddr3_controller.v` L1654-1658, `ddr3_phy.v` L201-204), up to 6 LUT levels from
a flip-flop in both widths, and ISERDES/IDELAY outputs reach flip-flops through up
to 5 (x16) and 6 (x32) levels (`ddr3.phy_paths` in the reports, counted on the
yosys netlist; the timed critical path has 11). They are probably inside 12 ns,
but nothing checks it. The 333 MHz, 90-degree and 200 MHz domains show no Fmax
at all: their loads are PHY primitives (and the fabric LUT above), so nextpnr
finds no register-to-register path in them, and the I/O timing of the PHY is not
analysed.

**Seed spread (committed sweeps).** `make ddr3-sweep` places and routes the
same netlist with every seed of 1-8 without the seed search's early stop and
summarises each run (`tools/fpga-seed-sweep.py`: routed Fmax, critical path,
utilisation, packer LUT1 count, PLL tables, the fabric-clock LUT with its
placement and delays, sha256 of log and FASM). The sweeps of the f07e91cd
netlists, run in a clean clone of 170e1a2, are
[`reports/fpga/ddr3-seed-sweep-2026-09-24-f07e91cd-x16.json`](../reports/fpga/ddr3-seed-sweep-2026-09-24-f07e91cd-x16.json),
`-x32.json` and the `-nextpnr-45a986b8` pair; their seed-1 FASMs are the
reported builds' FASMs.

| Netlist | Routed Fmax, controller clock, seeds 1-8 | Seeds meeting 83.33 MHz | SLICE_LUTX | Packer LUT1 |
| --- | --- | --- | --- | --- |
| x16, 0.9.7 | 80.83-90.93 MHz | 6 of 8 | 5,792 | 1,348 |
| x32, 0.9.7 | 82.90-96.83 MHz | 7 of 8 | 8,165 | 1,338 |
| x16, 45a986b8 | 86.99-98.00 MHz | 8 of 8 | 5,719 | 1,292 |
| x32, 45a986b8 | 83.42-93.82 MHz | 8 of 8 | 8,136 | 1,315 |
| probe x16 (demo top, 45a986b8, seed 1) | 107.83 MHz | 1 run | 4,932 | 680 |
| probe x32 (demo top, 45a986b8, seed 1, checked at 12 MHz) | 82.07 MHz | 1 run | 7,269 | 689 |

The critical path moves with the seed between the scheduler's bank and
precharge logic (10-13 LUT stages to `stage1_do_pre_d`, `stage2_do_pre_d` or
`o_wb_stall_int_d`) and, in x32, the calibration lane logic and its carry
chains (6-12 stages); routing is 81-91 % of the path delay, 64-67 % on the
carry-chain paths of three 45a986b8 x32 seeds. On these netlists 45a986b8 is not
slower than 0.9.7: it meets 83.33 MHz with every seed in both widths.

**Against the probe of 2026-09-23** (#60: the unmodified AX7103B demo on
45a986b8, heap seed 1, summarised in
[`reports/fpga/ddr3-probe-2026-09-23-ax7103b-x16.json`](../reports/fpga/ddr3-probe-2026-09-23-ax7103b-x16.json)
and `-x32.json` from its local logs, FASMs and netlists). x32: 7,269 SLICE_LUTX /
3,782 FF / 82.07 MHz, but that run had no constraint on the controller clock and
was checked against nextpnr's 12 MHz default; its critical path starts at
`write_by_byte_counter[0]` and runs through a split CARRY4 chain and 8 LUTs, 3.8
ns logic and 8.4 ns routing. Our x32 netlist on the same 45a986b8 gives 87.44
MHz at seed 1 and 83.42-93.82 MHz over seeds 1-8, all timing-driven towards
83.33 MHz; the probe's 82.07 MHz, from a run that was not, is just below that
range, so x32 shows no gap beyond what the constraint and the seed explain.
x16: 4,932 / 2,310 / 107.83 MHz, critical path
`stage2_pending` -> `stage2_do_pre_d`, 6 LUT levels, 1.0 ns logic and 8.3 ns
routing. Our x16 netlist on the same binary and seed gives 86.99 MHz and
86.99-98.00 MHz over seeds 1-8; the probe is 10 % above the best of them and
24 % above the same seed, so the x16 difference is **not** seed spread. It is
structural: the deepest
flip-flop-to-flip-flop path of the probe's x16 netlist is 9 LUT levels, ours is
13 (`netlist_ff_to_ff_max_lut_levels` in the sweep files; the same endpoints,
`stage1_do_pre` and `stage2_do_pre` in the controller's scheduler, are 9 levels
in the probe and 12 in ours). Re-synthesizing the probe's top with our yosys
command gives 9 again, so the difference comes from our top, not from the flow.
Which part of the top is **open**: in a local bisect (not committed), setting
`SDRAM_CAPACITY` back to the default took the deepest path from 13 to 12
levels, while `AUX_WIDTH` 16, driving the Wishbone inputs from registers (with
and without using its outputs), narrowing `o_debug1` to the state and leaving
`i_user_self_refresh` open each left it at 13. The resource differences are
explained: our top replaces the demo's
9600-baud UART with the t27 reporter, emitter and transmitter (32- and 64-bit
counters: +312 FF in both widths, yosys CARRY4 79 -> 153 in x16), ties the user
Wishbone port and sets `SDRAM_CAPACITY 4`; yosys LUTs +173 (x16) and +239 (x32);
and on 45a986b8 the packer creates 1,292 LUT1 cells (x16) against the probe's
680 (+612; yosys writes none), which with the +173 yosys LUTs is the +787
SLICE_LUTX (5,719 against 4,932; x32: 689 -> 1,315
and +239, the +867); 0.9.7 packs another 56 (x16) and 23 (x32) LUT1 on the same
netlists.

**CI.** [`.github/workflows/fpga-ax7203-ddr3.yml`](../.github/workflows/fpga-ax7203-ddr3.yml)
runs `make ddr3-all` in `regymm/openxc7@sha256:eced1cdd4727549f2d983328e0cf170fb6f6f67d87f19b2bf24365163368c70c`:
the image's yosys (0.62) and prjxray, and nextpnr-xilinx 0.9.7 built inside the
same image (Ubuntu 20.04, CMake 3.16, g++ 9.4; checked locally, under
emulation) and cached by commit and image digest, with its chip database and
`.inputs` cached by nextpnr and prjxray-db commit (generated in the image,
locally under emulation, the database is byte-identical to the native one:
sha256 `5c88a26d…a14786a7`). It publishes the reports, the XDC, the logs and
sha256 values only, never a `.bit` or `.fasm`: Actions artifacts of a public
repository can be downloaded by any signed-in GitHub user, and publishing
bitstreams built from UberDDR3 is the founders' decision. The step summary shows
the FASM and frames sha256 next to the whole-file `.bit` sha256. The CI builds
differ from the native ones in their FASM and frames too, because yosys 0.62
gives another netlist, and every `.bit` differs from every other in its header
time anyway. Until the founders decide, the bitstream flashed for #61 is a
native build, the one file whose whole-file sha256 its report records, with the
tool revisions of that report and no Tier-E claim. The workflow has not run on
GitHub yet (nothing pushed).

**First board runs (#61).** The x16 0.9.7 bitstream (whole-file sha256
`0ae4ed6c…`, the file its `build.json` names), loaded into SRAM on the AX7203
(DNA `0x00389c0c2d85e85c`, IDCODE `0x3636093`). Evidence in
[`reports/fpga/ddr3-bringup-2026-09-24-f07e91cd-x16/`](../reports/fpga/ddr3-bringup-2026-09-24-f07e91cd-x16/),
written by [`tools/fpga-ddr3-capture.py`](../tools/fpga-ddr3-capture.py): IDCODE,
DNA and XADC before the load, the UART recorder opened before `make ddr3-flash`
(so the `H` line is captured), every line with the host time of its newline, the
whole load output, XADC after; the record holds the sha256 of the transcript.

- `reload/` (10:42:52 UTC): the load check matched the whole-file sha256 of the
  report, openFPGALoader wrote SRAM ("Load SRAM", `done 1`) and returned at
  10:43:06.83; the design left reset 2 ms later by the clock count. `H` line:
  build id `f07e91cd`, 2 byte lanes, 3,000 ps. The first `S` line, 1.6 ms after
  reset, is already in BURST_WRITE (state 17): the reset, initialization and read
  and write alignment took less than that. The self-test phases were first seen
  at 0.0016 s (17), 1.71 s (18), 1.81 s (19), 3.29 s (20), 4.35 s (21) and 5.196 s
  (22), and `o_calib_complete` came between 5.1958 and 5.1975 s after reset; about
  174 s of lines after it (217 `S` lines, the last 179.5 s after reset) show state
  23, highest state 23 and no return to IDLE. The
  controller clock, fitted over the 228 `S` lines against host time, is
  83.33164 MHz (standard error 0.8 kHz, -20 ppm from 83.33333): the PLL runs at
  MULT 5. Die 49.3 °C before and after, VCCINT 0.994 V. A load at 10:38 UTC with
  an earlier revision of the tool (absolute paths in its record, no calibration
  timeline) passed the same checks (83.33261 MHz, reset 8 ms after the load); its
  files were replaced by this run's, so no record of it is kept (these figures
  are from the session log only).
- `first-load-resident/` (10:37:39 UTC): the first load, read with the same tool
  before the reload (no load in that run, so the tool leaves the wrap count open).
  The first load was done at 08:51 UTC outside any tool: a bare
  `openFPGALoader -c digilent_hs2 <bit>` whose UART capture failed (termios
  EINVAL), so its `H` line and calibration were not recorded, and the session log
  is its only load record (returned 08:51:13.455 UTC; the die read 35.95 °C just
  before).
  The clock count of this read puts the reset at 08:51:13.44 UTC with no wrap of
  the 40-bit count (05:11:19 or 01:31:25 UTC with one or two), 17 ms from the
  logged load; state 23, highest 23, no return to IDLE, 1.78 h after the load. The
  status lines first read at 10:11 UTC (not committed; `build/`) were 1.34 h
  (4,821 s) after the load, not "about 8.7 hours": that figure assumed two wraps of
  the 40-bit count, which the load time rules out.

What these runs show and what they do not. They show that this build
configures, locks its PLL at the intended frequency, calibrates, and passes one
self-test pass inside calibration (about 5.2 s). After DONE_CALIBRATE UberDDR3
checks no data: the state stays at 23 (L3358-3360), the wrong-read check and its
reset act only while `!final_calibration_done` (L3644, L3657), the second
Wishbone port that could repeat the test is off (`SECOND_WISHBONE 0`), and this
build's user port is idle (`i_wb_stb` 0). So "no return to IDLE" after state 23
is fixed at the end of calibration: the hours after it show that nothing reset
the design, not that memory kept data. No soak or retention test has run.

**Self-test coverage.** [`tools/uberddr3-bist-model.py`](../tools/uberddr3-bist-model.py)
models the pinned self-test (and `tests/test_ddr3_bringup.py` checks the model
against an operation-by-operation run with injected faults on a reduced
geometry). The Wishbone address counts BL8 bursts, 25 bits {row, bank, col[9:3]}:
2^25 bursts of 16 bytes (x16, U6's 512 MiB) or 32 bytes (x32).
- *Coverage.* With `BIST_MODE 1` the three tests share one sweep of the counter
  (it is zeroed only in IDLE): burst write/read on [0, 2^23), rows 0-8191 of
  every bank; random write/read on [2^23, 3·2^23), where the row/column swap
  (row = counter[14:0], {bank, col} = counter[24:15]) reaches banks 2-5 only, all
  rows; alternating write/read on [3·2^23, 2^25), rows 24576-32767. Rows
  8192-24575 of banks 0, 1, 6 and 7 are never written or read: 8,388,608 of the
  33,554,432 bursts, 128 MiB of U6 (256 MiB of the x32 GiB). 25,165,823 bursts
  are read back (the last address is written as the test leaves and never read),
  8,388,608 of them twice. UberDDR3's parameter comment "run through all address
  space ONCE" is true of the counter, not of the addresses. `BIST_MODE 2`
  sweeps every burst in each test (not built here).
- *Address faults.* The data written and expected is a function of
  counter[7:0] only (`calib_data_randomized` L3553-3561, `correct_data`
  L3594-3602), repeated in every byte of an 8-byte group. In the burst test
  counter[7:0] is {bank[0], col[9:3]}, in the random test row[7:0], and the
  alternating test reads each address right after writing it. Two addresses that
  differ only in bank[1], bank[2] or row[8]..row[14] therefore always carry the
  same data, and a stuck or undecoded line there (a 2 Gb part without A14, an
  open A11-A14 or BA2) gives no wrong read, in either BIST mode. At the pins: A8
  and A9 also carry col[8] and col[9] at read and write, so they are seen; a lost
  bank bit also upsets the controller's open-row bookkeeping, which the model
  does not cover. The self-test's "no wrong read" is not a test of address
  decoding, nor of 512 MiB of separate storage.

**What the #61 pattern test needs.** From the above: every one of the 2^25
bursts; data unique to the address, the beat and the byte lane (the self-test
repeats every 8 bytes, so x16 beats k and k+4 and x32 beats k and k+2 are
identical) with a seed per run; the whole region written before any read, then
a complement pass; a hold of a stated length (many refresh intervals) between
write and read for any retention claim; pass and fail counts on the UART with
host timestamps, and elapsed time from the recorded load, never from the 40-bit
field (it wraps every 3.665 h at 83.33 MHz). The Icarus model of the Wishbone
memory should inject an aliased address bit (for example `wb_addr[24]` ignored)
and a stuck DQ, and the test should assert that the generator reports both. The
port it drives (79d8fd3e): a request is taken when `i_wb_stb` and not
`o_wb_stall` (`stage1_update = i_wb_cyc && !o_wb_stall`, L1625), and stall is
high through calibration and before refresh; writes and reads are both
acknowledged, in order, through one pipeline (L1834-1839), acks cannot be held
off, and `o_wb_data` is valid only with a read's ack; dropping `i_wb_cyc` after
calibration discards pending requests and acks in flight (L1441-1444,
L2312-2319), so `cyc` stays high until every request is acknowledged; the
address unit is one burst in both widths, 25 bits; data is `64 * BYTE_LANES`
bits with beat k, lane l at bits `[8*(BYTE_LANES*k+l) +: 8]`, and `i_wb_sel` is
one bit per byte and drives DM (L1296). The #60 top tied `i_wb_cyc` to 1 and
`i_wb_sel` to all ones, harmless while `i_wb_stb` is 0. UberDDR3's `UART_DEBUG_BIST` counters are no substitute: they cannot see
the address faults above, and defining `UART_DEBUG` sets `reset_from_test` to 0
(L3655), so a wrong read would no longer return the controller to IDLE.

**Pattern test (#61).**
[`t27/rtl/fpga_ddr3_pattern.t27`](../t27/rtl/fpga_ddr3_pattern.t27) is an
`on_clock` Wishbone master on UberDDR3's user port, wired by
`tms_ddr3_ax7203.v` when `PATTERN_TEST` is 1 (`make ... DDR3_PATTERN=1`, the
default of the Makefile since this change; `DDR3_PATTERN=0` gives the idle port
of the #60 builds, their port behaviour, not their netlist: see the build command
above). How it meets the list above:

- *Coverage and order.* After `o_calib_complete`, a round writes every burst
  address `0 .. PATTERN_BURSTS-1` in order (default 2^25: all of U6 in x16, all
  1 GiB in x32), waits until every write is acknowledged and then `PATTERN_HOLD`
  controller clocks, reads every burst back in the same order and compares it; a
  second pass does the same with the complement. `PATTERN_ROUNDS` rounds (0, the
  default, runs until reset), each with a new key. No burst is read before all
  bursts of its pass were written and acknowledged, and a request is accepted at
  most once per clock, so every burst's read request is accepted at least
  `PATTERN_HOLD + PATTERN_BURSTS - 1` controller clocks after its write request:
  0.40265 s at 2^25 bursts, the nominal 83.333 MHz and hold 0. That bound follows
  from the order of the requests; the Icarus runs observe it (below), the board
  runs do not measure it. It is the only retention figure the test supports; a
  longer one needs a hold.
- *Address order.* Every phase goes up from address 0 (writes, then reads, in
  both passes of every round); there is no descending pass. A write that also
  disturbs a higher address (a write-side decoder fault, write-induced coupling
  upwards) is overwritten when that address is written later in the same phase,
  before any read, so such one-way faults are not covered (a fault that also
  misdirects reads is, through the address-unique data).
- *Data.* The burst's `64 * BYTE_LANES` bits are `BYTE_LANES` 64-bit words; word
  w of burst a is `kdata ^ lin((w << 32) | a)`, `lin` being nine xorshift steps
  (a bijection of 64 bits, linear over GF(2)) and `kdata` the round key or its
  complement. So every 64-bit word of a pass (4 beats x 2 lanes in x16, 2 beats x
  4 lanes in x32) has its own value, and x16 beats k and k+4 or x32 beats k and
  k+2 lie in different words; the bytes inside a word are pseudo-random, not
  unique (a byte has 256 values). A burst read from another address's storage (a
  stuck, open or aliased address or bank line) differs from what was expected in
  every word; every stored bit is written and read as 0 and as 1 in a round, so a
  stuck DQ or a dropped write shows. The round key comes from `PATTERN_SEED`,
  the controller clock count at which `o_calib_complete` was seen (reported; on
  the board it often came out the same from load to load, so loads can repeat
  the same key sequence: "The round keys repeat across loads", below) and the
  round number. [`tools/ddr3_pattern_model.py`](../tools/ddr3_pattern_model.py)
  states the same rules in Python.
- *Port.* `stb`, `we`, address and data stay unchanged until a clock with `stb`
  and not `stall` takes the request; the n-th ack belongs to the n-th request;
  `cyc` is high from reset (as in the calibrated #60 build) and falls only when the
  watchdog stops the test (no request taken and no ack for `PATTERN_WATCHDOG`
  clocks, default 2^24, about 0.2 s), which aborts the requests in flight; `sel`
  is all ones from reset. The data of the next two bursts is precomputed
  (registered), so the path into `i_wb_data` starts at a flip-flop. The test's
  flip-flops (and the status reporter's, so that both leave reset on the same
  clock and the test sees the reporter's `H` line) take their reset from the
  two-flip-flop stage of a second `fpga_reset` instance fed by `rst`, not from
  the comparator behind `rst`: in a
  trial place and route of the x16 top with the test on the reset net directly,
  the routed critical path of seeds 1 and 3 of four ran from that comparator to
  a flip-flop's SR pin, and none of the four met the controller clock.
- *Counting.* A four-stage compare pipeline counts, per pass, the bursts and the
  64-bit words with a wrong bit, the wrong bits, the DQ bits ever wrong (bit j of
  the mask = DQ j: byte b of a word is byte lane `b % BYTE_LANES`) and the first
  failing burst with the index and low 32 bits of (read xor expected) of its
  first wrong word. Controller clocks of each phase are counted from the first
  request presented to the last ack.
- *Report.* After the status reporter's `H` and `S` lines, which keep coming
  (the module holds a status line in a one-line buffer and hands it to the
  emitter before its own; in every simulated run the pattern lines arrived
  complete and in order): `G` (bursts; format, byte lanes and
  rounds), `K` (seed; clock count at calibration), `L` (hold; watchdog) once,
  then per pass `W` and `R` (round << 1 | pass; write / read phase clocks), `E`
  (wrong bursts; wrong bits), `M` (wrong 64-bit words; DQ mask), `F` (first
  failing burst or `ffffffff`; word index and xor), and `T` on a watchdog stop,
  `Z` after the last round. [`tools/fpga-ddr3-capture.py`](../tools/fpga-ddr3-capture.py)
  decodes them into `pattern` of the `trinity.ddr3-capture.v1` record (every pass,
  totals, the minimum write-to-read separation from the header) and fails the run
  on a wrong bit (also in a last pass the capture cut off after its `E` or `M`
  line), a watchdog stop, a line out of order, a pass missing or repeated (they
  must come as round << 1 | pass = 0, 1, 2, ...), a missing test in a build whose
  report names `PATTERN_TEST 1`, or no complete round; and, for every build, on a
  controller clock fitted more than 5,000 ppm from the nominal one. The 40-bit
  clock count of the `S` lines is unwrapped before the fit, so a capture longer
  than 3.665 h keeps its clock and reset time. The MB/s it derives
  (`pattern_test_write_MBps`, `pattern_test_read_MBps`: region bytes over the
  phase's clocks at the nominal controller clock, `clock_hz_used`) is this test's
  in-order single-master throughput with refresh and row changes, not the
  stage-2 measurement. Elapsed time comes from the host times of the lines
  against the recorded load, as for the status lines.

*Simulation.* [`tests/test_ddr3_pattern.py`](../tests/test_ddr3_pattern.py) runs
the whole top in Icarus with the t27 cores and
[`tests/sim_ddr3_top_model.v`](../tests/sim_ddr3_top_model.v) in place of
UberDDR3: our behavioural Wishbone memory with the port list of `ddr3_top`,
random stalls, a refresh-like stall window, in-order acks 6-13 clocks after the
request, and checks that a stalled request stays presented unchanged; it also
records, per burst address, the clock its write request was taken, and reports
the smallest separation to a later read request of that address. The UART
lines go through the capture decoder. Clean runs pass: x16 with 256 and 4096
bursts, x32 with 256 bursts in one and in two rounds, x16 and x32 with a hold
(`PATTERN_HOLD` 1000 and 777), and `PATTERN_ROUNDS` 0, the setting of every
bitstream, over three rounds (the bench stops it after six passes). The observed
write-to-read separation is at least the `hold + bursts - 1` the decoder derives
in every clean run (332 against 255 clocks at 256 bursts, 5,225 against 4,095,
1,317 against 1,255, 1,093 against 1,032). Each injected fault is detected, and
the counts of every pass (wrong bursts, words and bits, DQ mask, first failing
burst and word) equal those of a Python replay of the same fault on the model's
data: stuck-at 0 and 1 on burst-address bits 0-7 (x16 and x32, 256 bursts) and
8-11 (x16, 4096 bursts), half the region wrong in every pass; stuck DQ 0, 7, 8,
15 (x16) and 16, 31 (x32), exactly 8 wrong bits per burst over a round and only
that DQ in the mask; a write dropped in a true pass and in a complement pass
(then every bit of the burst is wrong), with a hold, and in round 2 of a
`PATTERN_ROUNDS` 0 run (whose counts depend on the hardware's keys of rounds 1
and 2, so they check them); a port that stops taking requests in the write phase
or in the read phase, and one that loses an ack (the watchdog's `T`, with the
phase). The generated C of the module's functions agrees with the Python model
on random inputs. Address bits 12-24 (x16) and 8-24 (x32) are not simulated (the
region would be too long for Icarus); the board run's region of 2^25 bursts has
aliasing pairs for every one of the 25 bits. The model is not UberDDR3: its
stall and ack timing are invented, and nothing here simulates the PHY or the
memory chips.

*Builds with the test (commit `7deeef16`).* Same flow and tools as
the 0.9.7 builds above (native yosys 0.69, native nextpnr-xilinx 0.9.7 with the
seed search, prjxray of the image), `DDR3_PATTERN=1` with the defaults: 2^25
bursts, hold 0, rounds 0 (until reset), watchdog 2^24 clocks; clean source tree
at 7deeef16. Built into `build/fpga/ddr3-x16-pattern/` and `-x32-pattern/` (so
the #60 x16 file that the board runs used is kept); reports in
[`reports/fpga/ddr3-build-2026-09-24-7deeef16-x16/`](../reports/fpga/ddr3-build-2026-09-24-7deeef16-x16/)
and `-x32/` (their `variant` names `PATTERN_TEST 1`, which the capture tool reads
to expect the test's lines). An earlier build of both widths at d5f3423f was
discarded before any use: in it the status reporter's `H` line could not reach
the UART (the reset fix of 7deeef16, found in Icarus).

| Variant | yosys LUT / FF / CARRY4 | nextpnr SLICE_LUTX / SLICE_FFX | Routed Fmax, controller clock (83.33 MHz) | Seed search | PLL tables | FASM sha256 |
| --- | --- | --- | --- | --- | --- | --- |
| x16, 0.9.7, test on | 6,907 / 4,135 / 317 | 9,434 / 4,135 | 83.74 MHz | seed 3 (1: 79.88, 2: 82.02 MHz; logs of seeds 1 and 2 not committed) | PASS (MULT 5) | `4e837c96b762653c39389a37c36d214f501117a6e0520d32538eb6c9c3f87645` |
| x32, 0.9.7, test on | 9,987 / 5,850 / 325 | 12,605 / 5,850 | 86.16 MHz | seed 1 | PASS (MULT 5) | `3e4ad24659aec2e8d247abfc8e666171b65125a24fce4d5876beef93a100316d` |

| Variant | frames sha256 | `.bit` sha256 from the sync word | `.bit` sha256, whole file (local file of the report) |
| --- | --- | --- | --- |
| x16, test on | `b6edba61f98978b10f8ec0db4fecd0abd6426e644198545b07850b90f6a3be51` | `f59a7743f9a9ec81005b2b84918364b00f655c355a8019bc39849763c3624563` | `5715d9f7736c4cfa1dd0b8228b31723cf6f94f8b8174c831499674b4505016a6` |
| x32, test on | `3ac103a8e26097156f07a9c1a8ed1919249deee6b4f4eb71c5139d5dc29d346a` | `c48eb9668b661cfb57f306c56a997cc640aebfb7a80cde8e06c1e0354160fa13` | `52bd1734b1ee159ca1db663d0137656f48860dc8b9ce93deefbd555e8900f2b7` |

The test costs about 2,480 yosys LUTs and 1,510 flip-flops in x16 (3,170 and
1,760 in x32) over the #60 netlists. The x16 build meets the controller clock by
0.41 MHz, with the third seed. Where the routed critical path starts: x16 seeds 1
and 2 at UberDDR3's calibration `lane` counter (their logs are not committed;
`build/`), seed 3 at its `stage2_pending`;
x32 seed 1 at the `o_debug1` net (the calibration state, read by UberDDR3 and by
the status reporter; the test does not read it). Before the commits, trial x16
runs (logs not committed) with the test's reset taken straight from `rst` gave
80.68 / 77.02 / 83.02 / 82.53 MHz for seeds 1-4 (stopped during seed 5), seeds 1
and 3 with the critical path from the `rst` comparator to an SR pin and seed 2
from an unnamed flip-flop through 13 LUT levels (not traced); after the reset
change the critical paths of the trial and committed runs started in UberDDR3 or
at `o_debug1`. No seed sweep of the x16 netlist was run; the x32 one was (below). The same limits as above
apply: Fmax covers fabric register-to-register paths on the controller clock
only. The fabric-clock LUT moved (x16 `SLICE_X162Y152/C6LUT`, x32
`SLICE_X162Y188/A6LUT`). Buffer to pin through it: x16 DQS OSERDES 3.58-3.60 ns,
CK 3.25-3.29 ns, inside the 2.47-4.63 ns of the x16 0.9.7 sweep; x32 DQS OSERDES
2.41-3.24 ns but CK 4.74-4.78 ns, above the 2.26-3.94 ns of the x32 0.9.7 sweep
(and below the 5.61 ns seen with 45a986b8). The x16 build and the x32 seed-1
build ran on the board, and the x32 netlist was placed with more seeds (next).

*Rebuilding the builds that ran.* 11303ca (the `UART_DEBUG_BIST` option) added
two public wires and a multiplexer to the top that do nothing with `UBER_UART`
0, but their names alone moved nextpnr's placement: at 01f44eb,
`BUILD_ID=7deeef16` gave x16 seed 3 FASM `cebca343…` (82.26 MHz, missing 83.33)
and x32 seed 19 `d92dd70b…` (lane 0 at -0.69 ns), netlists that never ran on the
board (review builds, not committed). From c7d5db4 the top declares them only
under `` `ifdef UART_DEBUG_BIST `` (which `DDR3_UART_DEBUG_BIST=1` defines), and
at c7d5db4 `BUILD_ID=7deeef16` x16 `DDR3_SEEDS=3`, x32 `DDR3_SEEDS=19` and
`BUILD_ID=0e94ee86 DDR3_PATTERN=0 DDR3_UART_DEBUG_BIST=1 DDR3_SEEDS=9` reproduce
their reports' FASM, frames and `.bit` from the sync word
([`ddr3-rebuild-identity-2026-09-24.json`](../reports/fpga/ddr3-rebuild-identity-2026-09-24.json)).
The builds of record therefore rebuild from 7deeef1 or from c7d5db4 and later
commits whose DDR3 sources are unchanged; a different netlist needs its
placement checked on the board again (below).

**Board runs of the pattern test (#61, 2026-09-24).** Every run: an SRAM load
by `tools/fpga-ddr3-capture.py` through `make ddr3-flash`, which checks the
`.bit` against its report (whole-file sha256, or the sha256 from the sync word
for the rebuilds named below); IDCODE `0x3636093`, DNA `0x00389c0c2d85e85c`,
XADC before and after; the UART recorded from before the load; the transcript's
sha256 in `capture.json`. For every build with our status lines (7deeef16 and
f07e91cd), also: the `H` line's build id, byte lanes and 3,000 ps checked, and
the reset 2-13 ms after the load returned by the clock count (so no other reset
happened). The three `UART_DEBUG_BIST` loads have no `H` or `S` lines (N15
carries UberDDR3's text): their identity rests on the whole-file sha256 that
`make ddr3-flash` checked, and "no other reset" on UberDDR3's own messages (its
first phase message 1.82 s after the load returned, no `RESET` report), not on a
clock count. Transcripts above 256 KB are committed as `.gz` (`gzip -n`; the
sha256 values are those of the uncompressed files; the decoder, `--redecode` and
`tests/test_ddr3_bringup.py` read either). Die temperature in the single XADC
readings before and after each load: 49.9-63.0 °C (the stop limit was 80 °C);
XADC's maximum register, which restarts at each configuration, read up to
62.25 °C after the seed-19 600 s run and 61.94 °C after the seed-6 one, and
63.46 °C before the rebuild's x16 load at 13:42 UTC (the design resident before
it). VCCINT 0.992-0.995 V, in single readings and in XADC's minimum and maximum
registers. Elapsed times are host times from the recorded load, never the
40-bit field. A run as recorded (Python with pyserial; the record keeps the
command line in `argv` and the tool's revision in `tool_revision` from 1bc0673
on):

```sh
python tools/fpga-ddr3-capture.py --port /dev/cu.usbserial-110 \
  --bit build/fpga/ddr3-x16-pattern/tms_ddr3_ax7203.bit \
  --report reports/fpga/ddr3-build-2026-09-24-7deeef16-x16/build.json \
  --output-dir reports/fpga/ddr3-bringup-2026-09-24-7deeef16-x16-pattern/load1 \
  --seconds 60 --label "x16 pattern test, load 1"      # load4-600s: --seconds 600
```

*x16, pattern test* ([`ddr3-bringup-2026-09-24-7deeef16-x16-pattern/`](../reports/fpga/ddr3-bringup-2026-09-24-7deeef16-x16-pattern/),
build [`ddr3-build-2026-09-24-7deeef16-x16`](../reports/fpga/ddr3-build-2026-09-24-7deeef16-x16/),
`.bit` whole-file sha256 `5715d9f7…`):

| Load (load returned, UTC) | Capture after reset | Calibration | Passes (512 MiB each) | Wrong bursts / bits | Die °C before → after |
| --- | --- | --- | --- | --- | --- |
| 1 (11:51:09.98) | 59.9 s | done at 5.196 s, no return to IDLE | 63 (31 rounds + 1 true pass) | 0 / 0 | 49.9 → 50.5 |
| 2 (11:52:41.00) | 60.0 s | same | 63 | 0 / 0 | 51.0 → 51.4 |
| 3 (11:53:55.90) | 60.0 s | same | 63 | 0 / 0 | 51.4 → 51.8 |
| 4 (12:06:16.52) | 599.5 s | same | 692 (346 rounds) | 0 / 0 | 54.4 → 55.0 |

881 passes: 881 x 2^25 bursts written, read back and compared, 473 GB in each
direction (881 x 536,870,912 bytes = 440.5 GiB), with no wrong bit and no watchdog stop. The
first pass ended 6.06 s after reset. Each phase (all writes, or all reads) took
35,514,620-35,514,662 controller clocks for the 33,554,432 bursts: 0.4262 s,
**1,259.7 MB/s as the pattern test's own in-order single-master rate** (region
bytes over phase clocks at the nominal 83.333 MHz, refresh and row changes
included; 94.5 % of one burst per clock, and of DDR3-667 x16's 1,333 MB/s). This
is not the stage-2 measurement. By the order of the requests (hold 0), every
burst's read request was accepted at least 2^25 - 1 clocks, 0.40265 s, after its
write request; that bound, not the length of the run, is the retention the test
shows, and it is a property of the design, not a measurement of these runs. The
controller clock fitted from the `S` lines: 83.329-83.335 MHz.

*The round keys repeat across loads.* The key of round r depends on the clock
count at which calibration completed, and on this board that count came out the
same on x16 loads 1-3 (432,973,211 clocks) and differed by 33 clocks on load 4;
x32 seed 8 repeated its count on 3 of 3 loads, seed 19 on 3 of 4, seed 6 on 2 of 4. So within a load every
round has a new key (346 in the 600 s run), but loads 1-3 of x16 wrote the same
data sequence. A different sequence per load needs another `PATTERN_SEED`
(a rebuild) or a key source that differs; not done.

*x32, pattern test: seed-1 build fails, the placement decides.* The reported x32
build (`7deeef16`, seed 1, `52bd1734…`) never calibrated: in two loads the
controller returned to IDLE about every 0.8 ms (0.788 ms, a fit of the count
over the clock in both loads; the count was first read at its ceiling of 255
0.2017 and 0.2013 s after reset), highest state 14 (CHECK_STARTING_DATA, the write/read
alignment of a lane). To separate the board from the build, the #60 x32 netlist
was rebuilt from master (`00262ff`, `BUILD_ID=f07e91cd`, seed 1): its FASM,
frames and `.bit` from the sync word equal the committed report's (`e18284b0…`,
`b95fc020…`, `f409b7bb…`; the frames regenerated from its FASM afterwards, all
recorded in [`ddr3-rebuild-identity-2026-09-24.json`](../reports/fpga/ddr3-rebuild-identity-2026-09-24.json));
the whole file (`c9da358a…`) differs from the report's (`b9c89cc9…`) in its
header time. It calibrated on 3 of 3 loads with no return to
IDLE ([`ddr3-bringup-2026-09-24-f07e91cd-x32/`](../reports/fpga/ddr3-bringup-2026-09-24-f07e91cd-x32/);
it has no pattern test, so that is UberDDR3's self-test only). U5 and the x32
pins therefore work with some placements. The 7deeef16 x32 netlist was then
placed with seeds 1-20 ([`ddr3-seed-sweep-2026-09-24-7deeef16-x32.json`](../reports/fpga/ddr3-seed-sweep-2026-09-24-7deeef16-x32.json)
and [`...-seeds9-20.json`](../reports/fpga/ddr3-seed-sweep-2026-09-24-7deeef16-x32-seeds9-20.json)),
and ten of them were built and loaded (reports `ddr3-build-2026-09-24-7deeef16-x32-seed<n>/`,
captures `ddr3-bringup-2026-09-24-7deeef16-x32-pattern-seed<n>/`; seed 1 is the
reported build). The column "CK - DQS" is, per byte lane 0-3, nextpnr's delay
from the DDR3 clock buffer through the fabric inverter LUT to the CK output
minus the same to that lane's write-DQS OSERDES clock
([`ddr3-x32-skew-outcome-2026-09-24.json`](../reports/fpga/ddr3-x32-skew-outcome-2026-09-24.json)):

| Seed | Fmax, controller clock | CK - DQS, lanes 0-3 (ns, model) | Loads | Result on the board |
| --- | --- | --- | --- | --- |
| 1 | 86.16 MHz | +2.30 +2.37 +1.96 +1.54 | 2 | never calibrates (highest state 14) |
| 7 | 89.06 MHz | -1.19 -0.88 -0.28 +0.02 | 1 | never calibrates (highest state 14; a return about every 0.73 ms, 255 first read at 0.1860 s) |
| 9 | 84.47 MHz | +1.12 +1.51 +1.70 +1.30 | 1 | never calibrates (highest state 14; about every 0.81 ms, 255 first read at 0.2065 s) |
| 5 | 90.33 MHz | +0.92 +1.38 +1.86 +1.45 | 1 | aligns after 37 returns to IDLE, then the self-test reads fail: 66 returns in 19.5 s, highest state 18; 5 of them follow a BURST_READ line (a wrong self-test read, at 3.44, 6.85, 10.27, 13.68, 17.10 s), the other 61 are alignment retries (37 before the self-test began, 24 within about 2 ms after the restarts) |
| 2 | 86.20 MHz | +1.27 +1.67 +1.81 +1.04 | 1 | calibrated at 8.88 s after 255 or more returns to IDLE, then 13 passes with wrong data on DQ16-31 (U5) only: 769-2,816 wrong bursts per pass, 23,642 in all, 1,436,348 wrong bits |
| 6 | 81.79 MHz (**misses** 83.33) | -0.23 +0.09 +0.11 +0.60 | 4 (one 600 s) | 828 passes of 1 GiB, 0 wrong bits |
| 8 | 79.35 MHz (**misses** 83.33) | -0.55 -0.24 +0.07 +0.72 | 3 | 138 passes, 0 wrong bits |
| 19 | 83.72 MHz | -0.32 -0.01 +0.15 +0.64 | 4 (one 600 s) | 839 passes, 0 wrong bits |
| 10 | 87.08 MHz | -0.20 +0.16 +0.37 +0.89 | 1 | 27 passes, 0 wrong bits |
| 12 | 84.77 MHz | -0.42 -0.02 +0.38 +0.78 | 1 | 26 passes, 0 wrong bits |

The five placements that passed have every lane between -0.55 and +0.89 ns;
those two numbers are the extremes of the passing placements themselves (seed 8
lane 0, seed 10 lane 3), so the range describes them and predicts nothing. Every
placement that failed had a lane at +1.70 ns or more, or at -1.19 ns. For
comparison, the x16 builds that ran: 7deeef16 seed 3 -0.29 / -0.31, the
`UART_DEBUG_BIST` build -0.68 / -0.37 (both pass), #60's f07e91cd x16 +0.74 /
+1.09 and x32 seed 1 -0.11 … +0.99 (both calibrate; self-test only). Seeds 19,
10, 12 and 9 were chosen, and their outcome predicted, from this skew before
they were loaded ([`ddr3-x32-skew-prediction-2026-09-24.json`](../reports/fpga/ddr3-x32-skew-prediction-2026-09-24.json),
written 12:32:19 UTC and committed in 5fd7caa at 12:32:26 UTC; the first of the
four loaded at 12:42:16 UTC). The file put the pass window at roughly -0.6 …
+0.8 ns and said: 19 and 12 pass (both did; 12's lane 3 at +0.78 ns "at the
edge"); 10 calibrates with lane 3 at +0.89 ns, "pattern test uncertain" (it
passed, 27 clean passes, which widened the window to +0.89 ns); 9 fails "with
calibration retries or errors on U5, like seeds 2 and 5" (it failed, but never
left alignment: highest state 14, 255 returns within 0.21 s, like seeds 1 and 7).
So two predictions held as written, one failure came in another mode than
predicted, and the uncertain one passed. This is a correlation over ten
placements in a delay model, not a measured mechanism. A plausible one: CK and write DQS both leave the clock network
through one fabric LUT and general routing (above), and with `ODELAY_SUPPORTED 0`
nothing adjusts write DQS against CK afterwards, so the route sets tDQSS
(±0.25 tCK = ±0.75 ns at 333 MHz), on top of the board's own CK-to-U5 and
DQS flight times, which are unknown. Seeds 6 and 8 run on the board although
their fabric paths miss the 12 ns target in nextpnr's model; seeds 19, 10 and 12
meet it (19 by 0.39 MHz).

x32 on the builds that pass (seeds 6, 8, 10, 12, 19; 13 loads): 1,858 passes of
2^25 bursts of 32 bytes, 1,858 GiB (2.0 TB) in each direction, no wrong bit, calibration
done 6.90 s after reset with no return to IDLE, the first pass 7.76 s after
reset. Each phase: 35,514,620-35,514,659 clocks, **2,519.5 MB/s as the pattern
test's own rate** (94.5 % of DDR3-667 x32's 2,667 MB/s; not the stage-2
measurement). The two 600 s captures (seed 6 from 12:31:52 UTC, seed 19 from
12:47:59 UTC) ran 690 passes each, the die rising from 58.5 to 61.4 °C and from
58.6 to 61.7 °C.
The x32 build of record for what follows is seed 19: it meets the constraint and
passed 4 of 4 loads (`.bit` of `ddr3-build-2026-09-24-7deeef16-x32-seed19`).

*Rebuilds at c7d5db4 on the board* (after the review, with the decoder of
1bc0673 and the tool at f829ad1). The x16 seed-3 and x32 seed-19 netlists
rebuilt at c7d5db4 (the same FASM, frames and `.bit` from the sync word as their
reports; whole files `a9bc7927…` and `53df5df8…`) were loaded once each for 60 s
([`ddr3-bringup-2026-09-24-7deeef16-x16-pattern-rebuild-c7d5db4/`](../reports/fpga/ddr3-bringup-2026-09-24-7deeef16-x16-pattern-rebuild-c7d5db4/),
[`...-x32-pattern-seed19-rebuild-c7d5db4/`](../reports/fpga/ddr3-bringup-2026-09-24-7deeef16-x32-pattern-seed19-rebuild-c7d5db4/);
loads returned 13:42:28 and 13:43:49 UTC). x16: calibration at 5.196 s, no
return to IDLE, 63 passes of 512 MiB and a last pass cut off after its clean `E`
line, 0 wrong bits, the key count 432,973,211 again. x32: calibration at 6.900 s,
no return to IDLE, 61 passes of 1 GiB, 0 wrong bits, key count 575,034,201 (as
seed 19's load 2). Reset 6 and 4 ms after the load; die 63.0 → 59.4 and 59.2 →
60.3 °C. These passes are not in the totals above.

*`UART_DEBUG_BIST` build (the issue's counters, x16).* `make ... DDR3_WIDTH=16
DDR3_PATTERN=0 DDR3_UART_DEBUG_BIST=1` reads UberDDR3 with `define
UART_DEBUG_BIST` and hands N15 to its debug UART (`UBER_UART 1`); the `uart_tx`
module UberDDR3 then instantiates, which the pinned files do not contain, is
[`fpga/ax7203/ddr3/uart_tx_t27.v`](../fpga/ax7203/ddr3/uart_tx_t27.v), wiring
around our t27 transmitter (bit period `CLK_HZ / BIT_RATE` = 83,000,000 / 9600
= 8,645 clocks, 9,640 baud at the real clock; `tests/test_ddr3_uart_debug.py`
drives it with UberDDR3's handshake in Icarus). Build `0e94ee86`
([report](../reports/fpga/ddr3-build-2026-09-24-0e94ee86-x16-uart-debug-bist/),
`.bit` `1c59401f…`): seeds 1-8 all missed 83.33 MHz (61.8-83.13 MHz, logs not
committed), seed 9 of the next search met it (83.55 MHz); CK - DQS -0.68 /
-0.37 ns. Three loads ([`ddr3-bringup-2026-09-24-0e94ee86-x16-uart-debug-bist/`](../reports/fpga/ddr3-bringup-2026-09-24-0e94ee86-x16-uart-debug-bist/),
loads returned at 12:20:47, 12:21:29 and 12:22:04 UTC; 9600 baud), each the same: the five phase
messages in order (burst write per byte, burst read, random write, random read,
alternating write-read) 1.82-5.79 s after the load, then `DONE BIST_MODE=1,
correct_read_data=` 33,554,431 at 5.88-5.89 s, which is exactly the number of
reads the self-test checks in `BIST_MODE 1` (2^23 + 2^24 + 2^23 - 1, from
`tools/uberddr3-bist-model.py`), and no `RESET` report in the 14 s that followed.
`wrong_read_data` is printed only in those reports, which the controller sends
from its first wrong read on, so it stayed 0 while the capture ran. The DONE
message is sent from FINISH_READ in the clock that sets `final_calibration_done`
(= `o_calib_complete`); this build then repeated it about every 0.12 s with the
same count (122-123 times per capture); why UberDDR3 repeats it under
`UART_DEBUG` was not traced, and it is not a new self-test (the count does not
grow). The counters see the
same 3/4 of U6 as the self-test and none of its blind address bits; the pattern
test above is the stronger evidence. These three captures have no `H` or `S`
lines and no clock count (see "Every run" above for what identifies them).

*Decoder corrections during these runs* (both applied with `--redecode`, which
keeps the earlier checks and the reason in the record): the second x32 seed-1
load's `H` line arrived appended to a line the previous design was cut off in,
and is now recovered from the joined line; the first decode of the
`UART_DEBUG_BIST` loads also read the resident design's repeated DONE message
from before the load, and now reads from the load's first phase message on.
After the review every capture with status lines was decoded again by the
decoder of 1bc0673 (the clock count unwrapped before the fit, the check
`clock_near_nominal`, wrong bits in a cut-off last pass, pass continuity, MB/s
and seconds at the nominal clock): no check changed value, the fits lie between
-555 and +1,025 ppm, and every pass reads 1,259.7 MB/s (x16) or 2,519.5 MB/s
(x32) at the nominal clock, where the earlier records had 1,259.7-1,259.8 and
2,518.1-2,519.7 from each capture's fitted clock (x32 fits 83.287-83.341 MHz;
the short captures' fits are noisy). `tests/test_ddr3_bringup.py` now compares
every record's whole `decoded` with a fresh decode and pins the totals above.

**Limits.** What the board runs above show, per width, and what they do not.
- *Shown, x16:* calibration on every load of three builds; the pattern test over
  all 2^25 bursts of U6, so a stuck or aliased line on any address bit
  (`bank[1]`, `bank[2]` and `row[8..14]` included, which UberDDR3's self-test
  cannot see) would have made the aliased bursts wrong in every word (detection
  simulated in Icarus for bits 0-11 only; x32 for bits 0-7), every 64-bit word of a pass distinct, true and complement data, 881 passes without a wrong
  bit; `correct_read_data` 33,554,431 and no wrong-read report in the
  `UART_DEBUG_BIST` build.
- *Shown, x32:* the same test over all 2^25 bursts of both chips (1 GiB) without a
  wrong bit on 13 loads of five placements, and failure on five others.
- *Not shown:* retention beyond the test's 0.40 s write-to-read separation (no
  `PATTERN_HOLD`; a bound from the design, observed in Icarus only), behaviour
  over hours (the longest capture is 600 s per width), temperature beyond 63.0 °C
  die in single readings (XADC's maximum register 63.46 °C; the DDR3 case
  temperature is unknown), other boards, a second address order (every phase
  ascends, so a write that disturbs a higher address is overwritten before any
  read), and data patterns other than this generator's (no walking bits, no
  row-hammer or neighbour patterns). The key sequence repeated across loads of
  one bitstream. The MB/s figures are the test's own in-order single-master rate
  at the nominal clock, not the stage-2 measurement. `PATTERN_ROUNDS` of 2^31 or
  more would never stop (t27c lowers the stop compare to a signed one); the
  Makefile refuses such values, and the bitstreams run with 0 (until reset).
- *Not analysed:* the timing of the 333 MHz, 90-degree and 200 MHz domains and of
  the PHY's I/O, and the controller-clock paths into and out of its primitives
  (nextpnr gives them no timing class). CK and write DQS go through a fabric LUT
  with a seed-dependent delay; x32 works only with some placements, chosen here
  by nextpnr's modelled skew, and two of the passing placements (seeds 6 and 8)
  miss nextpnr's controller-clock target. A different netlist (any RTL change)
  needs its placements checked on the board again. Folding the inverter into the
  OSERDES (`ZINV_CLK`) would take DQS off the fabric; not done.
- *Unchanged:* the internal 0.675 V VREF on bank 35 against the board's 0.75 V
  (not patched; x16 and the good x32 placements pass with it). Only the 0.9.7
  line ran; the 45a986b8 line (the precedent's) has not run. The x16 Fmax gap to
  the probe is structural and not yet traced to a part of our top. The VREF,
  termination and VCCO facts come from the AX7103 core-board schematic Rev 1.0,
  not from an AX7203 one.

### DDR3 read path (#62)

Issue #62 is the read path of stage 2: a t27 Wishbone burst reader on UberDDR3's user
port, consumer (A) at bus rate, and the counters of step 5 of the protocol above
("If testing DDR bandwidth ..."). It is built for **x16** only (128-bit Wishbone words
at the 83.33 MHz controller clock; no 2:1 gearbox); x32 was not built. The weights-per-second
comparison of the two layouts is #65; this section reports the read path's own counters.

**What is built.** [`t27/rtl/fpga_ddr3_reader.t27`](../t27/rtl/fpga_ddr3_reader.t27) is the
whole application: sequencer, Wishbone master, the trit generator, the dense5 encoder,
consumer (A) with its decoders, the counters and the report arbiter (the status reporter's
`H` and `S` lines pass through it, as through the pattern test).
[`fpga/ax7203/ddr3/tms_ddr3_reader.v`](../fpga/ax7203/ddr3/tms_ddr3_reader.v) only splits the
128-bit words into the u64 ports the pinned compiler emits: `rdata[63:0]` is bytes 0-7,
`rdata[127:64]` bytes 8-15, byte n of a word at bits `[8n+7:8n]`, so byte k of the region is
byte k % 16 of the Wishbone word at burst address k / 16 (simulated, below). Where UberDDR3 then
puts a word's bytes on the DDR3 bus is read from its source at 79d8fd3e only
(`ddr3_controller.v`, `stage2_data[(DQ_BITS*LANES)*beat + 8*lane +: 8]`, and the OSERDES inputs
in `ddr3_phy.v`): byte n on beat n / 2, byte lane n % 2. It is neither simulated (the memory
model stores whole words) nor observable on the board (a write-read round trip cannot see a
permutation applied the same way in both directions). `tms_ddr3_ax7203.v` instantiates the
adapter in place of the pattern test when it is read with `` `define DDR3_READER ``; everything
of the reader in the top (the `READER_*` parameters, the instance, the reset choice) is under
that define, so the pattern-test and #60 builds see the same code as before. The text is not the
same: the added lines shift the line numbers that yosys writes into the netlist JSON's `src`
attributes, so those netlists' sha256 differ (for the x16 pattern test the FASM did not
change, below).

The build of record is 42b6f5a9, seed 11. Its commit is baked into the netlist (`BUILD_ID`, the
`H` line), and the reader's sources changed after it (comments in `tms_ddr3_reader.v`, which
move yosys's `src` line numbers), so it is rebuilt from a checkout of 42b6f5a9, as the #60
builds are from 00262ff; the directory names are the ones of the report and the board records
(`$T`: the directory of the natively built nextpnr-xilinx 0.9.7, its prjxray-db, meta and chip
database, here `tools-native/`). These commands are equivalent to the ones that made the sweep
and the build (run in the #62 worktree at 42b6f5a9):

```sh
git worktree add ../trinity-memory-62 42b6f5a9 && cd ../trinity-memory-62     # the reader of record
make -C fpga/ax7203 ddr3-fetch ddr3-cores T27_ROOT=/path/to/t27
make -C fpga/ax7203 ddr3-sweep DDR3_APP=reader T27_ROOT=/path/to/t27 YOSYS='cd $(ROOT) && yosys' \
  IMAGE=regymm/openxc7@sha256:eced1cdd4727549f2d983328e0cf170fb6f6f67d87f19b2bf24365163368c70c \
  DDR3_TOOLS=$T/nextpnr-xilinx-0.9.7 DDR3_XRAY=$T/prjxray-db-a90f27c1 DDR3_META=$T/nextpnr-xilinx-meta-a4af910c \
  DDR3_CHIPDB=$T/chipdb/chipdb-nextpnr-0.9.7/xc7a200tfbg484-2.bin \
  DDR3_BUILD=$PWD/build/fpga/ddr3-x16-reader-42b6f5a9 DDR3_SEEDS='1 2 3 4 5 6 7 8 9 10 11 12'
make -C fpga/ax7203 ddr3-bit ddr3-report DDR3_APP=reader T27_ROOT=/path/to/t27 YOSYS='cd $(ROOT) && yosys' \
  IMAGE=regymm/openxc7@sha256:eced1cdd4727549f2d983328e0cf170fb6f6f67d87f19b2bf24365163368c70c \
  DDR3_TOOLS=$T/nextpnr-xilinx-0.9.7 DDR3_XRAY=$T/prjxray-db-a90f27c1 DDR3_META=$T/nextpnr-xilinx-meta-a4af910c \
  DDR3_CHIPDB=$T/chipdb/chipdb-nextpnr-0.9.7/xc7a200tfbg484-2.bin \
  DDR3_BUILD=$PWD/build/fpga/ddr3-x16-reader-42b6f5a9-seed11 \
  DDR3_REPORT=$PWD/reports/fpga/ddr3-build-2026-09-24-42b6f5a9-x16-reader-seed11 DDR3_SEEDS=11
```

At a checkout of 42b6f5a9 `BUILD_ID` defaults to 42b6f5a9; `make ddr3-report` refuses to replace
an existing report directory unless `DDR3_REPORT_OVERWRITE=1` (for a rebuild to compare, give
`DDR3_REPORT` a new directory; the default name carries today's date). Without `DDR3_CHIPDB` the
Makefile builds its own chip database first. The a6d9745f builds below are rebuilt the same way
from a checkout of a6d9745 (seed 1: `DDR3_SEEDS=1`, `DDR3_BUILD=…/ddr3-x16-reader`; seed 6:
`DDR3_SEEDS=6`, `DDR3_BUILD=…/ddr3-x16-reader-seed6`). At another commit the same command
builds another `BUILD_ID` into the netlist, and so another placement.

`DDR3_APP` is `pattern` by default (the #61 builds); `reader` builds into
`build/fpga/ddr3-x16-reader/` and reports into `ddr3-build-<date>-<id>-x16-reader/` unless
`DDR3_BUILD` and `DDR3_REPORT` say otherwise, refuses
`DDR3_WIDTH=32`, `DDR3_PATTERN=1` and `DDR3_UART_DEBUG_BIST=1`, and bakes
`DDR3_READER_TRITS` (default 17,694,720), `DDR3_READER_SEED` (98), `DDR3_READER_CAP` (64) and
`DDR3_READER_RUNS` (0, until reset) into the netlist; the report's variant reads
`READER (trits ..., seed ..., cap ..., runs ...)`, which the capture tool reads.

**Default builds unchanged.** With the Makefile of this change, `make -n` prints the same
tool commands as before for the default, x32, `DDR3_PNR_LINE=image`, `DDR3_PATTERN=0`,
`DDR3_PATTERN=0 DDR3_UART_DEBUG_BIST=1` and `bram-bit BRAM_ONLY=2` (it adds only the touch of
an `app-pattern.stamp`), and the x16 pattern-test netlist rebuilt at ce3d889 with
`BUILD_ID=7deeef16 DDR3_SEEDS=3` gave FASM `4e837c96…`, the FASM of
`ddr3-build-2026-09-24-7deeef16-x16/` (a rebuild in the #62 session, not committed; the
block-RAM sources are untouched). Only that x16 FASM was rebuilt and compared; the x32 seed-19
and `UART_DEBUG_BIST` seed-9 identities of `ddr3-rebuild-identity-2026-09-24.json` were compared
through `make -n` only. The review fix after cd22f81 changes no file these builds read (the
Makefile, `tms_ddr3_ax7203.v`, the pattern-test and status cores). The netlist JSON of these builds differs from before in the `src`
line numbers of `tms_ddr3_ax7203.v` (see "What is built"), so a netlist sha256 recorded
for an earlier text of the top is not reproduced; the FASM is what identifies these builds.

**Fill mode (no UART).** After `o_calib_complete` the reader runs until reset. Run r writes
format r & 1 (0 baseline2, 1 dense5) with the key of pair p = r >> 1, so runs 2p and 2p+1 carry
the same logical trits in the two layouts. A run fills the region (word w at burst address w),
waits for every write's ack, then reads every word back through consumer (A) and reports.
The trits come from [`tools/ddr3_read_model.py`](../tools/ddr3_read_model.py)'s rule: trit i is
lane i & 15 of block i >> 4, the 16 lane codes of `(K xor post(lin(mix(b)))) mod 2^32` with a
pair 11 read as 00 (so 0 with probability 1/2, +1 and -1 with 1/4 each), `K =
lin(lin((seed << 32) | p) + KADD)`, `lin` the pattern test's nine xorshift steps and `mix`,
`post` one nonlinear step each; the lanes after `trits` are logical zeros. A baseline2 word
holds 64 lanes (four per byte, earliest lowest, `docs/format.md`), a dense5 word 80 (byte n
= `sum (t_i + 1) * 3^i` of lanes 5n .. 5n+4); a padding byte of dense5 is 121. The generator is
a four-stage pipeline (raw blocks, padding mask, encoded word, current word) that advances one
word per write taken or per read acknowledged, so its current word is the data presented
(fill) or the lanes the next ack must decode to (read). Keys depend on the seed and the pair
only, so every load of a bitstream writes the same sequence of keys; within a load every pair
has a new key, and a run's fill overwrites the previous run's region with other bytes.
The stream is not random-like over a region of aligned ranges: the key acts on each lane
separately (a xor, then `11` read as `00`) and the activation depends only on the lane, so
a run's +1 and -1 counts and dot product are sums of per-lane functions of the key over a
fixed histogram of `post(lin(mix(b)))`. On the board region they come out as multiples of
64, the dot products within +-6,464 (board records below); an independent stream with these
marginals would give a dot product with a standard deviation near 15,000 (arithmetic:
sqrt(17,694,720 x 0.5 x 25.5)). A single wrong trit still changes the counts or the dot
product; the lane compare, which covers every lane of every word, is the check of the data.

**Burst reader.** Requests are taken on a clock with `stb` and not `o_wb_stall`; a presented
request is never withdrawn or changed; the n-th ack belongs to the n-th request. A new request
is presented only while the requests taken and not yet acknowledged, counting the one being
taken, stay below `cap`, so at most `cap` (64) are outstanding. Acks cannot be held off, so
consumer (A) has no ready and no FIFO: it takes the acknowledged word into its first register
in the ack's clock, every clock if need be. The cap therefore only bounds the reader; with no
FIFO to protect it is set above what the controller keeps in flight (the board runs saw at most
9) and every clock it holds a request back is counted (`cap holds`, 0 on the board).

**Consumer (A)**, one word per controller clock, nine register stages: S1 the acknowledged
word and its expected lanes; D the decoders on four 32-bit chunks (dense5: 20 lanes and the
invalid bytes per chunk, baseline2: 16 lanes with every `11` cleared and counted); S2 the lanes
assembled (80 or 64) and the invalid groups added; S3 the compare with the expected lanes and
per activation class (lane mod 8) the counts of +1 and -1 lanes; S4-S6 the word's +1 and -1
counts and its activation-weighted sums; S7 the accumulators and the word's dot product; S8 the
dot accumulator. The definitions are `tools/bram_trit_model.py`'s, restated in
`tools/ddr3_read_model.py`: lane codes `00 = 0, 01 = +1, 10 = -1`, activation `(i mod 8) + 1`
for global trit index i, dot product `sum t_i * act_i`, and the rotate-xor checksum `chk =
rotl1(chk) xor x` over the stored data, here in 64-bit halves in bus order (low half of word
0, high half, low half of word 1, ...). A unit test recomputes each run's words, +1 and -1
counts, dot product and checksum with `bram_trit_model`'s own encoder, decoder, lane codes,
activation and `rotl1` on regions of 1 to 12,345 trits and checks them against the model. The dense5
decoder and encoder are constant tables, not the divisions of `bram_trit_codec.t27`: with
carry chains allowed (the DDR3 flow's `synth_xilinx -abc9 -arch xc7`), one codec decoder and
one encoder of four bytes each took 183 CARRY4 and 609 LUTs out of context, and an
out-of-context synthesis of the reader with 243 constant compares per byte instead ended after
4 minutes without a result (486,935 cells in its first optimisation; session logs, not
committed). The tables are checked in C on all 256 codes and all 243 valid lane groups against
`bram_trit_codec.t27`'s decoder and encoder and against the host model.

**Counters of a run** (report lines after the status reporter's; `a` 32 bits, `b` 40 bits;
the dot product has 64 bits over two fields). A run holds at most 2^31 - 1 trits (the Makefile
refuses more, and the region arithmetic works in 32 bits): 33,554,432 baseline2 words, which is
all of U6's 2^25 bursts, or 26,843,546 dense5 words, 80 % of them; the dense5 run of a pair
has the trits of its baseline2 run, so no configuration fills U6 in dense5. Every field holds
the counts of such a run; `x`'s all-acks count wraps at 2^40, no sooner than 3.66 h after
calibration (2^40 clocks at 83.33 MHz, arithmetic), and the decoder compares it modulo 2^40:

| Line | a | b |
| --- | --- | --- |
| `q` (once) | logical trits per run | (`READER_FORMAT` << 32) \| (byte lanes << 24) \| cap; `READER_FORMAT` 2 from 42b6f5a9 on (with the `x` line), 1 in a6d9745f |
| `k` (once) | seed | controller clocks since reset at `o_calib_complete` (low 40 bits) |
| `v` (once) | runs (0 = until reset) | watchdog clocks |
| `a` | run | (format << 32) \| (lanes per word << 24) |
| `f` | fill words: acks + 1 at the fill's last ack, so W (below) | fill cycles: clocks from the first write request presented to the last write ack, inclusive |
| `g` | fill: most requests outstanding | fill command stalls |
| `c` | **words**: acks + 1 at the read's last ack, so W (below) | **cycles**: clocks from the first read request presented to the last read ack, inclusive |
| `y` | **consumer stalls**: acks while consumer (A) is not ready (0 by construction: its ready is constant) | **bus bytes**: 16 per word consumed |
| `p` | **scale and metadata bytes**: 0, this region stores none | **payload bytes**: per word, the bytes holding at least one logical trit (ceil(trits / 4) or ceil(trits / 5) per run) |
| `d` | **bad words**: words whose decoded lanes differ from the regenerated lanes anywhere | **padding bytes**: 16 - payload bytes, per word |
| `n` | **invalid groups**: dense5 bytes >= 243, baseline2 lanes `11` | **logical trits** delivered: lanes below `trits` (padding lanes excluded) |
| `o` | most requests outstanding at a clock (extra) | **command stalls**: clocks with a request presented and `o_wb_stall` high |
| `w` | **cap holds** (extra): clocks with no request presented while requests remained | **wait stalls**: clocks with at least one request outstanding and no ack |
| `u` | -1 lanes | +1 lanes |
| `s` | dot product bits 63:40 | dot product bits 39:0 |
| `h` | checksum bits 63:32 | checksum bits 31:0 |
| `x` | **stray acks** since calibration (extra): acks while the reader is in neither FILL nor READ | **all acks** since calibration (extra; low 40 bits): the fill and read words of the runs so far + stray acks, by construction (below) |

and `t` (watchdog stop: acks of the open phase; read phase, format and run) or `z` (after
`runs` runs).

**Which counters are evidence.** Several fields are fixed by the design whatever the memory
does, and a check on them can only catch a broken report path or decoder:
- *words* and *fill words* are `acks + 1` latched at the ack that makes the phase's count W
  (`final_ack`), so every completed run reports W = ceil(trits / lanes) for both; a phase
  that gets fewer acks never completes and ends in a `t` line instead.
- *bus bytes* add 16 for every word through consumer (A)'s valid chain, which starts at the
  same read acks, so bus = 16 x words.
- *padding bytes* add 16 - payload bytes per word, in the same clock as the payload bytes, so
  padding = bus - payload; payload bytes are the region's arithmetic (compared with the host
  model's `ceil(trits / 4)` or `ceil(trits / 5)`, which checks the RTL's arithmetic once per
  configuration, not the memory).
- *consumer stalls* count acks while consumer (A) is not ready, and its ready is the constant
  1 (yosys removes the register: it is a constant 0 in the netlist). Consumer (A) keeps up
  by construction, one word in every clock, which the timing closure of the build covers;
  this counter measures nothing.
- *scale and metadata bytes* are the constant 0.
- *all acks* (`x` line): all acks - stray acks are the acks while the reader is in FILL or
  READ, and each of those phases takes exactly its W acks (it ends at the W-th), so all acks =
  the fill and read words of the runs so far + stray acks whatever the memory does. The
  decoder's `all_acks_equal_the_words` compares exactly that; it cannot fail on a working
  report path, and without the stray term it would say the same as stray acks = 0. It adds
  nothing to the stray-ack count.

The capture tool still runs these checks (`IDENTITY_CHECKS` in the decoder, listed in each
record) and reports them apart from the rest. What the memory can fail is: the lane compare
of every word (*bad words*), the *invalid groups*, the +1 and -1 counts, dot product and
checksum against the host model, the pairs of runs with the same logical trits in both
formats, and the `x` line's *stray acks* (acks while the reader is in neither FILL nor
READ), which must be 0. An extra ack
anywhere after calibration ends a phase one ack early, so a real ack lands outside the
phases and counts as a stray ack (Icarus, below); a lost ack stops the phase (`t` line).
The a6d9745f build (`READER_FORMAT` 1) has no `x` line; by the same argument an extra ack in
its fills or outside the phases changed none of its counters, and one in a read showed only as
bad words.

The `x` line comes with `READER_FORMAT` 2 on the `q` line (the b field's bits 39:32).
[`tools/fpga-ddr3-capture.py`](../tools/fpga-ddr3-capture.py) decodes the lines into `reader`
of its record (`trinity.ddr3-capture.v1`; format-1 records end a run at `h`) and fails a run
that has a bad word, an invalid group, a stray ack, logical trits other than the region, or words, bus, payload and padding bytes, +1 and -1
counts, dot product and checksum different from `tools/ddr3_read_model.py` for its format and
key, or that fails one of the identity checks; and a record without both formats, with runs
out of order, or with a pair whose two formats disagree on logical trits, +1, -1 or dot product.

**Simulation** ([`tests/test_ddr3_reader.py`](../tests/test_ddr3_reader.py), in
`tools/test-t27.sh`; skipped without T27_ROOT or Icarus). The generated C of the module's
functions agrees with the host model on random inputs (generator, region arithmetic, masks,
decoders, encoder, lane assembly, counts, weighted sums, checksum step). Icarus runs the whole
top with `` `define DDR3_READER `` against [`tests/sim_ddr3_top_model.v`](../tests/sim_ddr3_top_model.v)
(our behavioural Wishbone memory, now with `+ack_min`/`+ack_span` for the ack latency; its
defaults reproduce the #61 behaviour) and hands the UART lines to the capture decoder: 1,237
trits (not a multiple of 64, 80, 5 or 4: 20 baseline2 words with 310 payload and 10 padding
bytes, 16 dense5 words with 248 and 8), four runs, with the default random stalls (20 % plus a
refresh-like window) and acks 6-13 clocks after the request, with no random stalls, with acks
30-69 clocks late, and with a cap of 3 and acks 40-69 clocks late (at most 3 outstanding, cap
holds counted; with the cap of 64 and that latency more are outstanding and the cap never
holds); 160 trits (dense5 whole words, baseline2 padded); 1 trit. The stalls and latencies
come from the model's pseudo-random sequence, one fixed trace per seed; besides the default seed
the clean runs use five more (`+lfsr_seed`) and 75 % random stalls (with 1,237 and with 160
trits), and the bench checks that these give other stall counts. One configuration runs until
reset (`RUNS` 0, as the board builds do) and is stopped after its fifth run (`+stop_runs`).
Every run equals the host model, both formats of a pair deliver the same logical trits, +1, -1
and dot product, no ack is stray, and the bench's own count of every
phase at the Wishbone port (clocks from the first request to the last ack, requests, acks,
command stalls, wait stalls, most outstanding) equals the reader's counters. After the last
fill the memory model's words equal the model's dense5 words (bursts 0-15) and the baseline2
words the run before wrote (16-19) byte for byte, and the dense5 bytes read as a stream give
the trits in order, padding bytes 121: that is the Wishbone byte order above (the model has no
beats or lanes). A stuck DQ bit (3 at 1, 12 at 0; the model's own beat and lane placement), a
stuck address bit (2 at 1) and dropped writes (in a dense5 and in a baseline2 run) are
detected, each run's bad words, invalid groups, +1 and -1 counts, dot product and checksum
equal a Python replay of the fault. An extra ack with no request behind it (`+dup_ack`) inside
run 0's fill, inside its read, just after its last read ack, and inside run 1's fill shows each
time as one stray ack in that run, while its words, fill words and bus bytes stay W, W and 16 W
(the identities above). A port that stops taking requests, or loses an ack, in the fill or in
the read stops the reader with a `t` line. The memory model is ours, not UberDDR3: its stall
and ack timing are invented, nothing here simulates the PHY or the chips.

**Build.** Same flow as the #61 builds (native yosys 0.69, native nextpnr-xilinx 0.9.7
`heap`/`router2` with its own chip database, prjxray of the image). The first reader netlist
(ce3d8896) missed the controller clock with every seed 1-8 (65.82-77.96 MHz; session logs, not
committed): the decoders and the invalid-group sum shared a stage, and `o_wb_stall` went
through an adder and a compare with the cap before the next `stb`. Pipelined instead of
lowering the clock: the decoders got their own stage (D) and the next outstanding count and its
room under the cap are computed from the registered count and only selected by the request
taken and the ack. The netlist of a6d9745f (no `x` line) met 83.33 MHz with 9 of the 11 routed
seeds of a sweep of seeds 1-12 ([`ddr3-seed-sweep-2026-09-24-a6d9745f-x16-reader.json`](../reports/fpga/ddr3-seed-sweep-2026-09-24-a6d9745f-x16-reader.json);
78.67-91.02 MHz; seed 8 did not route).

The review of cd22f81 added the ack counts (the `x` line), which changed the netlist: 42b6f5a9,
yosys 11,122 LUT, 6,971 FF, 594 CARRY4 (a6d9745f: 11,143, 6,899, 574; the x16 pattern-test
netlist: 6,907 LUT, 4,135 FF); nextpnr 15,619 SLICE_LUTX; 45 / 18 / 18 OSERDESE2 / ISERDESE2 /
IDELAYE2; PLL tables PASS for MULT 5; VREF 0.675 V on bank 35 as before. Its sweep of seeds 1-12
([`ddr3-seed-sweep-2026-09-24-42b6f5a9-x16-reader.json`](../reports/fpga/ddr3-seed-sweep-2026-09-24-42b6f5a9-x16-reader.json))
routed all 12, and 6 meet 83.33 MHz (seeds 3, 4, 6, 7, 11, 12: 83.96-92.62 MHz); 6 miss it
(72.55-83.21 MHz). The routed critical path ends in UberDDR3's scheduler for seeds 1, 3, 4, 5
and 9; in the reader for seeds 2, 6, 8, 10 and 12 (the generator's `raw0`/`raw1`, or its advance
enable after `o_wb_stall` or `calib_complete`); nextpnr names no net of the design on it for
seeds 7 and 11. Of the six seeds that miss the clock, three have it in the reader (2, 8 and 10,
80.13-81.37 MHz: seed 10 through 11 LUT stages of the generator's `lin` to `raw1`, seeds 2 and 8
through its advance enable, 4 and 6 LUT stages with 11.6 and 11.3 ns of routing): the generator is a
timing limit of this design as much as UberDDR3's scheduler. It was not pipelined further here,
since the build of record meets the clock; it is an open item. As for every DDR3 build here, the Fmax covers
fabric register-to-register paths on the controller clock only; the other clocks have no such
paths and the PHY is not analysed.

| Build | Seed | Fmax, controller clock | CK - DQS, lanes 0/1 (ns, model) | FASM sha256 | `.bit` sha256 from the sync word | Board |
| --- | --- | --- | --- | --- | --- | --- |
| [`…-a6d9745f-x16-reader/`](../reports/fpga/ddr3-build-2026-09-24-a6d9745f-x16-reader/) | 1 (seed search) | 86.55 MHz | -0.96 / -0.74 | `cca91a28…` | `da86854d…` | never calibrates (2 loads) |
| [`…-a6d9745f-x16-reader-seed6/`](../reports/fpga/ddr3-build-2026-09-24-a6d9745f-x16-reader-seed6/) | 6 (`DDR3_SEEDS=6`) | 85.82 MHz | -0.18 / +0.12 | `85f0684c…` | `793707a3…` | 3 of 3 loads pass (format 1) |
| [`…-42b6f5a9-x16-reader-seed11/`](../reports/fpga/ddr3-build-2026-09-24-42b6f5a9-x16-reader-seed11/) | 11 (`DDR3_SEEDS=11`) | 87.44 MHz | +0.11 / -0.01 | `832a4872…` | `65ac66d6…` | 3 of 3 loads pass; **build of record** |

**Placement of a6d9745f.** In time order
([`ddr3-reader-summary-2026-09-24-a6d9745f-x16.json`](../reports/fpga/ddr3-reader-summary-2026-09-24-a6d9745f-x16.json),
`load_order`, from the capture records and the reports' `written_at`): the seed-search build,
seed 1 (whole-file sha256 `5efd0ef6…`), did not calibrate on its first load (15:11 UTC, die
38.8 °C before the load; [`ddr3-reader-2026-09-24-a6d9745f-x16/load1`](../reports/fpga/ddr3-reader-2026-09-24-a6d9745f-x16/)):
highest state 14 (CHECK_STARTING_DATA), the count of returns to IDLE at its ceiling of 255,
the failure mode of the x32 placements 1, 7 and 9 of #61 (seed 7 with a lane at -1.19 ns,
seeds 1 and 9 with every lane between +1.12 and +2.37 ns, in the model). In nextpnr's model its
lane 0 sits at -0.96 ns, below every x16 placement that had calibrated (-0.29 / -0.31, -0.68 /
-0.37, +0.74 / +1.09). The sweep of seeds 1-12 was then written (15:18:58 UTC), and seed 6 chosen
from it by this rule: among the seeds whose controller clock meets 83.33 MHz, the one whose
larger |CK - DQS| of the two lanes is smallest (seed 6, 177 ps; seed 5 next at 424 ps, although
one of its lanes is at -24 ps). Seed 6 was built on its own (`DDR3_SEEDS=6`: FASM equal to the
sweep's seed 6; report 15:22:44 UTC) and loaded three times (15:23-15:28 UTC, die 48.9-50.6 °C),
calibrating each time. After those, seed 1 was loaded a second time as a control at the same
temperature (15:31:50 UTC, die 51.0 °C before the load, a 10 s capture, `load2`): it did not
calibrate either, so the difference between the two placements is not the 10 °C between seed
1's first load and seed 6's. So x16 is placement-sensitive too. Choosing the seed by this skew
is a rule taken from #61's correlation, not a timing analysis; the summary lists every seed's
delays (`seeds`) and their order by the rule (`seed_rank`). These a6d9745f builds have no `x`
line (`READER_FORMAT` 1); the build of record is the one below.

**Placement of 42b6f5a9.** Seed 11 was chosen from its sweep (written 16:38:43 UTC) by the same
rule before any load of this netlist: 6 of the 12 seeds meet 83.33 MHz, and seed 11's larger
|CK - DQS| of the two lanes is the smallest of them (+0.109 / -0.012 ns; seed 12 next at 0.659
ns, then 7, 6, 3 and 4 at 1.01-1.53 ns). It was built on its own (`DDR3_SEEDS=11`: FASM equal to
the sweep's seed 11, yosys netlist sha256 `084b4e88…` equal to the sweep's) and loaded three
times; no other placement of this netlist was loaded
([`ddr3-reader-summary-2026-09-24-42b6f5a9-x16.json`](../reports/fpga/ddr3-reader-summary-2026-09-24-42b6f5a9-x16.json),
made by [`tools/ddr3-reader-summary.py`](../tools/ddr3-reader-summary.py) from the sweep and the
capture records).

**Board runs of the build of record (2026-09-24, 42b6f5a9 seed 11, `.bit` whole-file sha256
`66292207…`)**, each by `tools/fpga-ddr3-capture.py` as for #61 (the `.bit` checked against its
report, IDCODE `0x3636093`, DNA `0x00389c0c2d85e85c`, XADC before and after, UART recorded from
before the load), 20 s after the load
([`ddr3-reader-2026-09-24-42b6f5a9-x16-seed11/load1`-`load3`](../reports/fpga/ddr3-reader-2026-09-24-42b6f5a9-x16-seed11/);
loads returned 16:42:41.1, 16:44:57.2 and 16:46:54.2 UTC):

- `H` line build id 42b6f5a9, `q` line `READER_FORMAT` 2. Calibration complete 5.196-5.197 s
  after reset on every load. The `H` line and the first two `S` lines (stamped 4 and 137,417
  controller clocks after the reset) reached the host in one read, 4.7, 5.1 and 0.5 ms after
  the load returned; the three lines take 5.2 ms at 115,200 baud (3 x 20 characters of 10
  bits, arithmetic), so the lines cannot have arrived earlier than 5.2 ms after the reset: in
  every load the reset came before the load returned, at least 0.5, 0.1 and 4.7 ms before it
  (loads 1-3). The decoder's fitted reset (3-10 ms after the load
  returned) is a median over lines that reach the host late and falls after these first
  lines, so it is not used to select them. Of the 31 `S` lines per load from the `H`
  line on, 12 come before calibration (`calib_complete` 0; states 0 and 17-22) and 19 after it; each of
  those 19 shows `calib_complete` 1, state 23, highest state 23 and no return to IDLE, from the
  calibration to the last line 19.70-19.71 s after reset (14.50-14.51 s of lines per load), so
  DONE_CALIBRATE held while the reader ran (the lines are coalesced; a return to IDLE in between
  would have raised the count). Controller clock fits +17, -220 and -17 ppm from 83.333 MHz.
  Die 37.7-46.7 °C in single readings before and after the loads (the board had cooled since
  the a6d9745f runs), VCCINT 0.993-0.994 V.
- 515, 516 and 518 runs (1,549: 775 baseline2, 774 dense5, 774 complete pairs; the first
  report 5.23-5.25 s after the load; fewer than the a6d9745f loads' 549-553: a run waits for its
  report, now 14 lines of 20 characters, 24.3 ms at 115,200 baud by arithmetic, against 13),
  every run passing every check: 0 bad words and 0 invalid groups in
  214,272,000 baseline2 and 171,196,416 dense5 words read back; +1 and -1 counts, dot product
  and checksum equal to the host model in every run; in every pair both formats deliver the
  same 17,694,720 logical trits with the same +1 and -1 counts and dot product; 0 stray acks
  in every run. The identity checks (words, fill words, bus bytes, padding, consumer stalls 0,
  all acks = fill and read words + stray acks) pass as they must. Scale and
  metadata bytes 0 and padding 0 (the region is whole words in both formats); the padding
  counters were exercised in simulation only.
- **Read cycles per run** (the raw read path; first read request to last ack, refresh and row
  changes included): baseline2 276,480 words in 292,614-292,653 clocks, 0.94474-0.94486
  words per clock; dense5 221,184 words in 234,116-234,125 clocks, 0.94473-0.94476 words per
  clock. At the nominal 83.333 MHz (arithmetic, not a second measurement) that is
  3.5114-3.5118 ms and 2.8094-2.8095 ms per run and 1,259.6-1,259.8 MB/s of bus data, the rate
  of the #61 pattern test's phases (1,259.7 MB/s). Command stalls 16,125-16,164 (baseline2)
  and 12,923-12,932 (dense5) per read, wait stalls 3,412-3,422 and 2,730-2,740, at most 9
  requests outstanding, 0 cap holds. Fills: 292,610-292,649 and 234,081-234,120 clocks, at
  most 6 outstanding. Every load repeated these ranges.
- Coverage: every run fills and reads bursts 0-276,479 (baseline2) or 0-221,183 (dense5) of
  U6's 2^25, 0.82 % and 0.66 % of it, the same bursts in every run; 3 loads of 20 s.
- The captures' +1 and -1 counts and dot products are multiples of 64: 129 distinct count pairs
  and 32 distinct dot products, -6,464 to +6,464, over the 1,549 runs (the stream's structure,
  above); the baseline2 checksum was 0 in all 775 baseline2 runs, as it must be on this region,
  and the dense5 checksum took 259 values over the 259 pairs of the longest load.

The a6d9745f seed-6 loads
([`ddr3-reader-2026-09-24-a6d9745f-x16-seed6/`](../reports/fpga/ddr3-reader-2026-09-24-a6d9745f-x16-seed6/),
`READER_FORMAT` 1, 15:23-15:28 UTC, die 48.9-50.6 °C) gave the same kind of result on the
earlier netlist: 549, 553 and 551 runs, every run equal to the host model, 0 bad words and 0
invalid groups in 228,925,440 baseline2 and 182,476,800 dense5 words, 19 `S` lines per load
after calibration (of 31 since reset) all at state 23 with no return to IDLE, and read cycles in
the same ranges (292,614-292,652 and 234,085-234,125). Those runs had no ack counts: an extra
ack in a fill or outside the phases would not have shown there.

**Limits and open items.**
- The baseline2 checksum of this region is 0 for every key, on the board and in the model. The
  region has 552,960 64-bit halves, a multiple of 64, so a half's rotation depends only on its
  block index mod 128; a lane of a block is a function of the key's lane and the lane of
  `post(lin(mix(b)))`, and every (block mod 128, lane, lane value) cell of the region holds an
  even number of blocks, so every rotation class xors to 0 whatever the key (checked in
  `tests/test_ddr3_reader.py`). For baseline2 on this region the checksum therefore confirms
  nothing; the per-word lane compare, which covers every lane of every word, is the check. The
  dense5 checksum differs from pair to pair. A checksum with carries (rotate-add) would not
  cancel; not changed, since the issue asks for the block-RAM benches' rotate-xor. The +1 and
  -1 counts and the dot product depend on the key but are far from random-like (the trit
  stream paragraph above).
- Keys repeat from load to load (they depend on seed and pair only); within a load each pair
  has its own key. Nothing checks what the region held before a load's first fill: by #61's
  model of `BIST_MODE 1` ("Coverage" above) UberDDR3's self-test writes and reads bursts
  [0, 2^23) during calibration, which include the whole region, so the first run's fill
  overwrites self-test data. That a lost write in that fill would show as bad words rests on
  those data differing from the run's words; it is an assumption, not measured (no write was
  ever dropped on the board), and no retention across a load was measured.
- x16 only; placement-sensitive (a6d9745f seed 1 fails, seed 6 passes); one placement of the
  build of record ran (42b6f5a9 seed 11), for 3 x 20 s, on 0.82 % (baseline2) and 0.66 %
  (dense5) of U6, the same bursts in every run. No longer soak, no other region, no second
  board, no x32 build.
- The generator and its advance enable limit the clock in 3 of 12 placements of 42b6f5a9
  (Build, above); not pipelined further.
- Consumer (A) keeping up is by construction (one word taken in every clock, no ready) and by
  the build meeting 83.33 MHz; the consumer-stall counter cannot be nonzero in this design and
  is no evidence of it.
- A lost ack and an extra ack in the same fill cancel: in a review's scratch Icarus run
  (not committed) a fill with one ack dropped and one duplicated passed every check. A single
  extra ack or a single lost ack is caught (stray-ack count, stop condition); the pair is not.
- Padding and scale/metadata bytes are 0 on the board region; nonzero padding was only
  simulated. No scales are stored or read, so the scale/metadata counter is 0 by construction.
- Not done here: consumer (B), the matvec (#64); loading real weights over the UART (#63); the
  weights-per-second comparison of the two layouts (#65). The words-per-clock figures above
  are the read path's, measured by the reader's own counters at the controller clock.
