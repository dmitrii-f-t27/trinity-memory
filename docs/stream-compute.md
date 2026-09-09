# Framed ternary/int8 stream compute

`rtl/trinity_dot_stream.v` implements a dot product with five ternary weights
and five signed int8 activations per input beat. `DENSE5=1` reuses the existing
base-3 decoder; `DENSE5=0` reuses the five-lane 2-bit decoder. Both feed the same
checked addition/subtraction pipeline. This is a synthesizable RTL design
verified by Icarus simulation; synthesis, routed timing, physical device
measurements, DDR integration, and model-quality measurements are not provided.

## Input and output contract

All transfers occur at rising clock edges. A source transfer requires
`in_valid && in_ready`; a result transfer requires `out_valid && out_ready`.
The producer must hold every input field unchanged while valid and not ready.
The consumer may hold `out_ready` low indefinitely. The module keeps
`out_valid`, `out_result`, and `out_error` stable until the result is consumed.

| Signal | Meaning |
|---|---|
| `in_code[9:0]` | Dense5: low eight bits encode five trits; bits 9:8 must be zero. Baseline: five 2-bit lanes, `00=0`, `01=+1`, `10=-1`, `11=invalid`. |
| `in_activations[39:0]` | Five signed two's-complement int8 activations; lane 0 is bits 7:0. |
| `in_mask[4:0]` | Bit i marks lane i as valid. Only contiguous low-order masks are accepted. |
| `in_last` | Final beat of one dot product. A new dot product begins after it. |
| `out_result` | Signed accumulator result; 32 bits by default. Zero when `out_error=1`. |
| `out_error` | Frame was malformed or a group-boundary accumulation overflowed. |
| `rst` | Active-high synchronous reset. Flushes partial sums, buffered input, errors, and pending output. `in_ready` is low during reset. |

Every non-final beat must have mask `11111`. Final masks may be `00001`,
`00011`, `00111`, `01111`, or `11111`. Mask `00000` is permitted only for a
single-beat empty frame; its result is zero. It cannot terminate a nonempty
frame. Every inactive weight and activation must be logical zero, including
empty frames. Dense5 zero padding uses base-3 digit 1, so an all-zero group
has code 121, not code 0. Dense codes 243..255, nonzero high bits, and reserved
2-bit lanes are rejected even when those lanes are masked out.

Malformed input is consumed, marks its frame as invalid, and continues to
drain until `in_last`. Exactly one result then appears, with `out_error=1`
and `out_result=0`. A bad frame does not contaminate the next frame. If the
source never sends `in_last`, no result appears; there is no implicit timeout.
Unknown `in_last`/handshake control signals are outside the digital protocol.

## Arithmetic and buffering

The first register stage captures a decoded five-term group sum, validity,
and frame flags. Each activation is sign-extended before conditional negation;
in particular `(-1) * (-128) = 128`. A signed 12-bit group sum covers all five
products, whose exact range is -640..640. The second stage adds the group to
the frame accumulator and creates a result on the final group.

`ACC_WIDTH` must be at least 12; the Python runner supports widths 12..32 and
uses 32 for its public API. The accumulator checks overflow after each group
using a one-bit-wider addition. Overflow poisons the whole frame; it never
wraps or saturates. A later cancellation does not repair an earlier overflow.
Terms inside one five-lane group are summed exactly before this check.

When unstalled, the pipeline accepts one group per clock. A pending result
blocks accumulation, but one additional group can remain buffered in the
first stage. `in_ready` then falls and backpressure reaches the producer.
Consuming a result permits a buffered group to advance at the same edge.
The final group becomes an output after its accumulator-stage edge; the sink
can consume it at the following rising edge. These are protocol properties,
not a measured FPGA clock rate or throughput result.

The two modes deliberately use one common 10-bit input port. The dense format
has eight meaningful code bits per group versus ten in the baseline. A count
of encoded bits is not a bus-utilization or physical BRAM measurement. This
new compute block accepts a framed stream; the earlier synchronous storage
sequencer has no ready port and is not directly connected to it by this change.

## Run an actual RTL dot product

Install Icarus Verilog (`iverilog` and `vvp`) and run from this repository:

```python
from trinity_memory.rtl_compute import run_rtl_dot

weights = [-1, 0, 1, 1, -1, 1]
activations = [-128, 42, 127, -128, 127, 7]
dense = run_rtl_dot(weights, activations, codec="dense5", seed=27)
baseline = run_rtl_dot(weights, activations, codec="baseline2", seed=27)
assert dense["result"] == baseline["result"] == 7
assert dense["evidence"] == "rtl-simulation"
```

The function validates exact ternary weights and signed int8 activations,
writes temporary `.mem` fixtures, compiles the RTL with `iverilog`, executes
`vvp`, and parses the hardware-language result. A Python integer dot product
is an independent scoreboard oracle, not the returned execution path. Missing
tools or RTL files, compilation failures, simulation assertions, and timeouts
raise `RTLSimulationError`; there is no Python fallback for this explicit API.
Input values outside the contract raise `ValueError`. A signed32 prefix-group
overflow raises `OverflowError` before allocating simulator fixtures.

RTL resources are resolved from `TRINITY_MEMORY_RTL_ROOT` when set (a repository
root or `rtl` directory); otherwise from a packaged `trinity_memory/rtl`
directory or the source checkout. An explicit missing resource location fails
rather than silently using a different checkout. The four needed resources are
`trinity_dot_stream.v`, `tb_dot_stream.v`, `generated/ternary_dense5_decoder.v`,
and `ternary_baseline5_decoder.v`. An installed distribution must include these
files, or its caller must set the explicit resource location.

Returned counters describe the simulator's seeded testbench:

- `cycles`: clocks after the initial two-cycle reset, including source bubbles,
  intentional output stalls, and four final drain clocks. `result_cycle` is
  the clock when the result handshake occurred.
- `groups`: accepted input beats. An empty dot still sends one empty frame.
- `input_stalls`: clocks with `in_valid && !in_ready`.
- `output_stalls`: clocks with `out_valid && !out_ready`.
- `stalls`: input stalls plus output stalls. These may overlap, so this is a
  sum of two counters, not the number of distinct stalled clocks.
- `source_bubbles`: clocks where an available input beat was randomly withheld.
- `encoded_weight_bits`: accepted groups times 8 or 10, including tail groups
  and the empty-frame control beat. This is not TMEM container size.
- `seed`, `codec`, `accumulator_bits`, `weight_count`, `result`, `error`,
  `simulator`, and `evidence`: configuration and observed result metadata.

The testbench deliberately stalls every scored output for at least three
clocks and adds seeded source bubbles and longer output stalls. Different
seeds may change cycles without changing arithmetic. Both codec modes use
identical logical lanes and schedules, so this simulation does not establish
an acceleration ratio between them.

## Verification

```sh
python3 scripts/test_dot_rtl.py
python3 -m unittest discover -s tests -p 'test_rtl_compute.py' -v
```

The explicit RTL script fails clearly if Icarus is missing. Unit tests skip
simulation-only cases when it is absent and still check input validation and
the explicit failure behavior. CI must install Icarus and run the RTL script.

For each codec, the protocol suite scores 57 outputs: random ternary/int8
vectors, empty and partial groups, -128/+127 boundaries, seven malformed frames,
and valid frames after errors. It verifies stable results under stalls, source
bubbles, input backpressure, reset during an unfinished frame, and reset while
a discarded output is pending. A separate 12-bit accumulator suite scores six
outputs at exact limits 2047/-2048, positive/negative overflow, cancellation
after overflow, and recovery. Twelve-bit mode makes overflow testable with
short vectors using the same parameterized checked-adder logic. The public API
tests also compare both codecs with an independent integer dot oracle.
