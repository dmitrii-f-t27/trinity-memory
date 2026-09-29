# Generated Q16 FFN (issue #92)

Implementation sources are `t27/rtl/fpga_ffn.t27` and `ffn_wide.t27`.
`rtl/t27/ffn.v` only connects the generated modules. This is a serial
reference implementation, with baseline2 weights and stage-by-stage UART
inspection. It does not implement AutoBitLinear ActQuant/bf16, attention or
residual paths. It is not a full model inference or an optimized FFN benchmark.

## Arithmetic contract

`tools/ffn_reference.py::fpga_q16` is the independent integer reference from
#91. All projections accumulate signed Q16.16 in signed 64 bits. Scales and
norm weights are signed Q16.16. Every division/shift rounds to nearest, ties
to even; sign is restored after magnitude rounding. h/g/u/s/y saturate to
signed 32 bits; a saturates to signed 64 bits and retains Q32.32.

For the first norm, square inputs exactly, divide the sum by N with RNE,
add EPS_Q32=42950, then take floor(sqrt(value << 32)). Divide the exact
`(x * gamma) << 16` by that root with RNE, then clamp. For subnorm, square
the signed 64-bit a values exactly, divide by N with RNE, add
EPS_Q64=184467440737096, and take floor(sqrt(value << 64)); the output
numerator is `(a * gamma) << 32`. Epsilon is inside the square root.

The sum of 6912 squares of -2^63 needs 139 unsigned bits. It is accumulated
in explicit u64/u64/u32 limbs (160 bits), never an unsupported wide scalar
type. The square-root radicand fits 192 bits and its root fits 96 bits.
The generated wide unit implements unsigned multiply64x64 (128 cycles),
divide192by128 (384 cycles, divisor below 2^127), and sqrt192 (192 cycles).
Each iteration registers independent limb operations before propagating the
carry or borrow on the second clock. These are operation cycles, excluding
the controller handshake. The controller delays completion by five clocks
to drain registered rounding, carry and clamping paths. The arithmetic
contract and rounding boundaries are unchanged.

## Memory and protocol

All addresses below are 128-bit **word** addresses; multiply by 16 for the
existing UART loader's byte addresses. Inputs are loaded before the doorbell.

| Region | Word base | Representation |
|---|---:|---|
| Doorbell | 0x40 | low u32 nonzero run ID, next u32 0x46464e31 |
| gate/up/down scales | 0x50..0x52 | one signed Q16.16 in each word's low u32 |
| x | 0x1000 | one signed Q16.16 per word, low u32 |
| post gamma | 0x2000 | same |
| sub gamma | 0x3000 | same |
| gate weights | 0x10000 | row-major baseline2, rows padded to 64 trits |
| up weights | 0x60000 | same |
| down weights | 0xb0000 | same |

Baseline2 lane code 0/1/2 means 0/+1/-1; code 3 inside logical columns is
a fatal error. Padding is ignored. The board wrapper fixes H=2560, I=6912,
O=2560. Generic simulation dimensions must stay within buffer and region
limits; they are not a separately qualified board configuration.

The FFN master only reads DDR3, with one outstanding request held until
accepted. A 2^24-cycle bus watchdog aborts a stuck transaction. Calibration
loss, a math fault, or an invalid lane halts the run and emits E; reset is
required after a fatal error. The loader remains the only write master.

F announces H/I after calibration; d marks a new run ID. Each h/g/u/a/s/y value is emitted as two
lines: lowercase tag for low u32, uppercase for high u32, both with element
index in line a. The high half of a uses J to avoid the loader's A acknowledgement.
h/g/u/s/y are sign-extended to 64 bits. After all stages,
c reports the run ID and clocks, two k lines split those clocks, six t lines
give stage saturation counts, and z marks completion. Clocks count every
controller clock from the doorbell to the last y value and exclude host
uploads. `k` index 0 counts clocks spent waiting for the report line (the run
start and every value emit, including the minimum three clocks per value);
index 1 counts clocks spent on DDR3 reads, from request to response. The
remaining `c - k0 - k1` clocks are compute: every other active state. Each
clock falls in exactly one bucket, and a simulation test checks that compute
clocks do not change when memory latency and report backpressure do. Compute
clocks are this serial controller's own work outside the two waits; even a
zero-latency memory would still need at least one request clock per read,
which the memory bucket holds. None of this is a throughput claim for a
different design. Captures
from before the split (eecc619f) have no k lines and still validate.

## Validation and build

`python3 -m unittest tests.test_ffn_rtl -v` generates actual RTL with the
pinned t27c and runs Icarus against independent Python integer operations
and the corrected FFN oracle. It covers carry/borrow boundaries, 192-bit
division/sqrt, zero/epsilon inputs, signed extremes, saturation of both
signs, partial weight words, memory latency/stalls and output backpressure.
Inline .t27 tests cover helpers; they do not replace clocked RTL tests.

Set `TRINITY_FFN_SIMULATOR=verilator` to also exercise the same small full
pipeline vectors with Verilator. The pinned compiler emits unsized constants;
large constants in this spec use shifts of positive small literals to avoid
the different sign extension of unsized decimals in Icarus and Verilator.

Full-layer reproducible commands (fresh output directories):

```
python3 tools/fpga-ffn.py prepare --output build/ffn-vectors --seed 27 --run 1
python3 tools/fpga-ffn.py simulate --vectors build/ffn-vectors --output build/ffn-sim
```

Prepare a second vector with `--zero --run 2`. The host tooling frames data
and checks captures; FPGA arithmetic stays in the executable specifications.
Input files have sizes/hashes in inputs.json; reference.json records exact
integer stages, saturation counts, source tensor hashes and f64 error.

Build the full layer with `make -C fpga/ax7203 ddr3-bit ddr3-report
DDR3_APP=ffn DDR3_PLL_MULT=6 DDR3_DDR_DIV=5`. Use a fresh build/report
directory, source commit and tool pins. Check routed timing, final synthesis
CHECK, bitstream hash, board identity and temperature before SRAM loading.
Never infer board success from a simulation or fabric timing alone.

Use `tools/fpga-matvec-boot.py --ffn` with the explicit build report, bitstream,
port/cable, fresh output directory and temperature limit. Then run
`tools/fpga-ffn-run.py --vectors build/ffn-vectors --boot <boot.json>
--port <port> --cable <cable> --output <fresh-directory>`. This runner qualifies
921600 baud, loads and reads back all payloads before the doorbell, checks XADC
at ten-second intervals, retains raw UART, validates every stage and restores
115200 on success. A failed attempt preserves evidence and may require a fresh
boot before retrying. No configuration flash is written by these commands.

Both full-layer board runs passed on the AX7203 on 2026-09-29: seed 27 and
the zero vector each produced 32768/32768 stage values exactly matching the
integer reference with zero saturations, in identical 860531932 clocks
(including UART reporting), and an independent offline replay re-decoded every
memory readback with CRC and re-checked every signed value. Evidence, hashes
and the retained failed builds (`a138be21`, `a6eed6b9`) that led to the
routed 70.41 MHz of `eecc619f`:
`reports/fpga/ffn-q16-2026-09-29-eecc619f/`.
