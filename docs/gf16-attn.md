# Layer-0 attention and residual

Hardware source `2e2e336cd8f5b8d3404017f28a5ad9b473c3831e` passed AX7203
qualification on 2026-10-03: all four BOS/zero/S2/S8 full/result pairs
matched the integer oracle exactly, with every uploaded input region
independently reconstructed from CRC-valid raw DDR readback frames.
See the [retained evidence and offline replay](../reports/fpga/attn-2026-10-03/README.md).

## Numerical scope

The `gf16-attn-v1` controller implements the explicit signed Q16.16 contract
in [`tools/attn_reference.py`](../tools/attn_reference.py). Its name does not
mean that these stages use the GF16 floating-point format used by the FFN.
The scope is attention and the first residual of layer 0. FFN integration,
the second residual, complete-model inference and token generation remain
outside this qualification.

The pinned BitNet b1.58 2B4T layer-0 weights have hidden dimension 2560,
20 query heads of 128 lanes and five KV heads. The controller supports up
to eight causal positions. Stages are input normalization, Q/K/V
projections, RoPE, scaled causal scores, integer softmax, context,
attention sub-normalization, O projection and residual addition. BOS/S2/S8
use the house deterministic seed-27 activation fixture; BOS means the
position-zero case here, not a captured prompt embedding. Zero uses zero
activations. These measured inputs are not a token-generation benchmark.

## RTL implementation and timing

[`t27/rtl/gf16_attn.t27`](../t27/rtl/gf16_attn.t27) is the source; regenerate
committed Verilog with the pinned compiler through
`tools.gf16_attn_build.generate`. Three 8192-by-32 arrays use registered
read ports and explicit prefetch states. The datapath shares an exact
64-step shift/add multiplier and pipelines carry propagation, rounding,
and the square-root shift count. Products retain their low 64 bits,
including signed two's-complement patterns; rounding and saturation
boundaries are unchanged.

Explicit finite continuation targets and independent state comparisons
allow the attention-only Makefile flow to recode the controller one-hot
with Yosys. Other board applications keep the previous synthesis flow.
Yosys ignores the numeric FSM INIT but recognizes async reset state 0;
the board wrapper supplies `app_rst_n` from `app_reset.stage2`, initially
zero. A model after that transformation passed full/result S8, reset and
calibration-loss checks, and the qualified SRAM boot passed on the board.

The [integrated build report](../reports/fpga/attn-2026-10-03/build-2e2e336c/build.json)
records 26 RAMB36E1 cells (24 attention, two loader), no conflicting-driver
warnings, and heap seed 5 routed at **66.66 MHz against 60 MHz**. The actual
PLL profile remains **60 MHz controller / 240 MHz DDR**, multiplier 6 and
DDR divider 5. The final `nextpnr.clocks_routed` verdict is authoritative;
the earlier 46.83 MHz placement estimate is not the routed result.

The pinned nextpnr uses its existing `XC7_LEGACY_CARRY4_SPLIT=1` fallback;
no tool patch or disabled ABC equivalence was used. Fabric timing excludes
DDR PHY/I/O paths that nextpnr does not model, and the report marks the PLL
filter/lock table for multiplier 6 as unchecked against a Vivado reference.
Physical calibration and these retained workload runs passed; they do not
establish timing closure for every PHY path or operating condition.

## Validation and measurements

All 17 oracle/RTL tests passed, including 39 product/carry patterns, 136
rounding cases and 432 square-root shift cases (nine roots times 48 even
bit positions). Eight real-dimension Verilator replays, shape
`(2560, 640, 20, 128, 5)`, passed in full/result modes for BOS, zero, S2
and S8 with zero oracle saturations. The archive retains their source
hashes and complete captures separately from physical measurements.

Physical measurements, copied from
[`physical-results.json`](../reports/fpga/attn-2026-10-03/physical-results.json):

| Case | Full/result IDs | Input bytes | Full/result values | Full seconds | Result seconds | Retained peak C |
|---|---|---:|---:|---:|---:|---:|
| BOS | 101/102 | 4,221,008 | 19,860/2,560 | 8.632920050 | 2.315108150 | 48.9784 |
| ZERO | 111/112 | 4,221,008 | 19,860/2,560 | 8.632920050 | 2.315103717 | 49.1015 |
| S2 | 121/122 | 4,264,016 | 39,740/5,120 | 17.273991700 | 4.636004917 | 49.1938 |
| S8 | 131/132 | 4,522,064 | 159,440/20,480 | 69.302680583 | 18.694377817 | 49.5706 |

Seconds are `clock_split.total / 60000000`, rounded to nine decimal places.
The active window includes DDR waits and UART-report backpressure, excludes
input upload/readback and final UART drain, and is not pure compute time.
`report_wait`, `memory_wait` and `controller_other` are disjoint counters
whose sum equals the total. Full/result runs share exactly the same freshly
uploaded and readback-verified inputs; only the 16-byte doorbell changes.

Input bytes are the sum of manifest regions excluding the doorbell. Each
case also has a separate 32,768-byte DDR qualification readback. Full traces
contain `19840*S + 20*S*(S+1)/2` values and result traces contain `2560*S`.
Softmax probabilities are internal and are not emitted: the S8 oracle has
720 more values than its 159,440-value full UART trace. No CRC-rejected
frames occurred in these retained input readbacks.

## Reproduction

Replay all retained proof without a board, compiler, network or model cache:

```sh
python3 reports/fpga/attn-2026-10-03/verify-evidence.py
```

Run live oracle/RTL regression tests with the pinned compiler and fixtures:

```sh
TRINITY_REQUIRE_CACHED=1 python3 -m unittest tests.test_attn_reference tests.test_gf16_attn_sim -v
python3 -m unittest tests.test_attn_capture tests.test_attn_board_evidence tests.test_ffn_boot -v
```

For new physical runs, generate vectors with `tools/gf16_attn_vectors.py`,
boot through `tools/fpga-matvec-boot.py --gf16-attn`, then run
`tools/fpga-attn-run.py --trace-pair` and independently verify with
`tools/verify_gf16_attn_board.py`. Use new output directories and exclusive
UART/JTAG ownership. Require a hash-matching build with final routed timing
PASS, the expected board DNA, and fresh current/retained temperatures below
70 C. Configuration is SRAM-only; do not write flash. The runner validates
descriptors before device access and attempts to restore UART to 115200
on success, transfer failures and interruptions. A failed capture or baud
restoration is not a passing qualification.
