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
