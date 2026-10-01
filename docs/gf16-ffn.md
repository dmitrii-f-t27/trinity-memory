# Full GF16 FFN — iteration 5

Issue [#111](https://github.com/dmitrii-f-t27/trinity-memory/issues/111).
The `gf16-ffn-v1` controller executes the complete layer-0 FFN:
input RMS norm → ActQuant → ternary gate/up projections → ReLU² product →
sub-norm → ActQuant → ternary down projection. Inputs, weights and scales come
from DDR3; intermediate arrays stay in block RAM. Scalar storage uses
`gf16-rne-gradual-v1` (one sign, six exponent and nine fraction bits).

This covers FFN arithmetic. Attention, residual connections, other layers,
logits and generation quality are outside this experiment.

## Exact contract

1. Input RMS norm uses `gf16-wide-norm-v1` with gate=GF16 one and up=x,
   followed by the GF16 post-norm weight. The [norm contract](gf16-wide-normalization.md)
   defines block scaling, epsilon, integer mean/square root and final rounding.
2. ActQuant interprets each GF16 value exactly as a signed Q39 integer.
   Set `M = max(2*max(abs(Q39)), 10995116)` in Q40. The epsilon is exactly
   binary32(1e-5), not a decimal approximation. Codes are
   `RNE(127 * 2 * Q39 / M)` in [-127,127]. Dequantization rounds the rational
   `code * M / (127 * 2^40)` directly to GF16; zero code gives positive zero.
3. Each ternary projection accumulates exact signed Q39 in 128 bits. For at
   most 6912 columns, magnitude is below 2^84. Round the dot once to GF16,
   multiply by the GF16 matrix scale, then round once to GF16. An overflow
   at either boundary fails explicitly, even if a later scale could shrink it.
4. For the nonlinearity, compute `relu(g)^2*u` with a single FP32 rounding.
   GF16 inputs guarantee a finite FP32 product. Sub-norm uses the existing
   bounded integer kernel and GF16 gamma multiplication.
5. Apply the same ActQuant and projection contract to produce y.

All rounding is nearest, ties to even, including gradual underflow. This
contract replaces host FP32 reciprocal and GEMM rounding with exact rational
ActQuant and exact ternary sums. Consequently it does not promise identical
words to the earlier host GF16 experiment; the replay measures differences.

## Implementation

`t27/rtl/gf16_ffn.t27` owns the controller, signed accumulation and ActQuant.
`t27/rtl/gf16_scalar.t27` provides bounded multi-cycle multiplication,
normalization, guard/sticky rounding and GF16 packing. It is shared by the
FFN and `gf16_wide_norm.t27`; each controller has its own scalar instance.
`ffn_wide.t27` supplies sequential integer multiply, divide and square root.
The Verilog adapters contain wiring only. Never edit generated Verilog.

Row arrays use registered reads. Memory supports one outstanding read, with
stall/ack handling and a watchdog. This first full GF16 implementation favors
an explicit, testable arithmetic contract; it is not a throughput result for
the earlier parallel Q16 matvec. Pipelining scalar operations removes long
combinational paths without changing any numerical boundary.

## DDR3 and UART

Build with `DDR3_APP=gf16-ffn` (AX7203 XC7A200T, x16 DDR3,
PLL_MULT=6, DDR_DIV=5, 60 MHz controller). The distinct descriptor is
`GFF1` / `0x47464631`; the low 32 bits contain a nonzero new run ID.

| Region | 128-bit word address | Full-shape words |
|---|---:|---:|
| Doorbell | 64 | 1 |
| Gate/up/down scales | 80 | 3 |
| Input x | 4096 | 2560 |
| Post-norm gamma | 8192 | 2560 |
| Sub-norm gamma | 12288 | 6912 |
| Gate weights | 65536 | 276480 |
| Up weights | 393216 | 276480 |
| Down weights | 720896 | 276480 |

GF16 words occupy only the low sixteen bits, with all upper bits zero.
Weights use baseline2, 64 trits per word; each row is padded to that boundary.
The header is `G(2560,6912)` and the run start is `d(run,1)`, distinct from Q16.
One UART line contains a tag, eight hex index digits and ten hex value digits.
The order is h, p, g, u, interleaved a/s, v, y. Here p/v contain the dequantized
GF16 word plus the signed 8-bit ActQuant code in bits 16..23. Product a is FP32;
all other stage values are GF16. The capture ends with c, k0, k1, z.

`c` is total active clocks, `k0` time attributed to UART reporting, and `k1`
time attributed to memory wait. The remaining `controller_other` includes
norm-engine waits. It is not an isolated compute measurement: the norm engine
can compute its next lane while the controller reports the previous one.

Errors stop the controller until reset: E1 shape, E2 calibration/arithmetic
fault, E3 invalid trit, E4 memory timeout, E5 unexpected acknowledgment,
E6 invalid GF16 word, E7 scalar overflow, E8 norm index, E9 ActQuant bound.

## Reproduction and evidence

Use the pinned compiler from `native/compiler.lock`. Tests require Icarus,
Verilator, Yosys and a C compiler. The real-input replay additionally uses the
pinned optional runtime in `tools/bitnet-capture-requirements.txt` and verified
fixture/capture caches. The fixture decoder must be built with
`T27_ROOT=... sh tools/build-t27.sh`; populate its cache with
`python tools/fetch-fixtures.py --jobs 4` before the offline replay.

```sh
python -m unittest tests.test_gf16_scalar tests.test_gf16_ffn tests.test_gf16_wide -v
python tools/replay_gf16_ffn.py --rtl selected
python tools/fpga-ffn.py prepare --gf16-ffn --zero --run 2 --output build/gf16-zero
python tools/fpga-ffn.py simulate --vectors build/gf16-zero --output build/gf16-zero-rtl
```

The replay recalibrates every saved BF16 stage, compares all 45 real token rows
against the integer FFN, earlier GF16 candidate and FP32+ActQuant, and executes
five complete generated RTL FFNs (shared BOS plus each prompt's last token).
`--rtl all` executes all 45 rows. It writes numerical reports, source hashes,
strictly validated captures, DDR3 payload manifests and reference words.

The clocked scalar test checks 104931 cases, including every finite GF16 word,
random products/rationals, ties, signed zero, underflow, overflow and reset.
FFN tests cover tiny shapes, the 64-trit boundary, stalls, output backpressure,
calibration loss, invalid inputs/trits, unexpected ACK, reset and a second run.
Host preflight checks profile, dimensions, fixed addresses, sizes, payload
hashes and doorbell before UART/JTAG access. Old Q16 remains a separate mode.
The host drains UART in a dedicated thread while XADC is checked; completion
matching examines new bytes plus a partial line. A regression blocks the XADC
callback while the UART reader receives a complete stream. Truncated or
malformed captures still fail strictly.

Board execution requires a hash-matching build report with **routed timing
PASS**, fresh XADC and matching board DNA. Use `--gf16-ffn` for both
`tools/fpga-matvec-boot.py` and `tools/fpga-ffn-run.py`, and `--max-temp 70`.
Only SRAM configuration is used. Diagnostic timing-failed builds are never
loaded. Functional RTL agreement alone is not evidence of working board timing.
Uploads and CRC-checked readback use 921600 baud. The continuous result stream
defaults to 460800 (`--capture-baud`), with a checked switch and restoration to
115200. On this setup, 921600 dropped bytes in the unframed result stream,
including after continuous-thread reception and in a repeat without JTAG.
Those captures were rejected; none was repaired into a passing result.


## Measured numerical result

The [source-hashed report](../reports/numeric/gf16-ffn.json) contains all
45 token rows. Five selected complete FFN simulations pass every value:
163840 stage values and 47360 dequantized values/codes. A separate full zero
vector with run ID 2 also passes. The five simulations require
288579255–288749194 clocks in the variable-latency testbench; these are
simulation counters, not board throughput measurements.

| Captured text | New y NMSE vs FP32+ActQuant | BF16/new NMSE | Changed y words vs prior host GF16 |
|---|---:|---:|---:|
| English explanation (9 tokens) | 1.37214e-4 | 2.027× | 0 |
| Arithmetic (8 tokens) | 2.06047e-4 | 2.385× | 0 |
| Russian (17 tokens) | 1.57801e-4 | 2.314× | 3431 |
| Code (11 tokens) | 1.28934e-4 | 2.593× | 2168 |

Exact rational input ActQuant changes two and four integer codes in the last
two prompts; downstream ActQuant changes fourteen and seven codes respectively.
These measured differences follow the specified arithmetic boundaries. This
small captured sample supports a comparison of this FFN against its FP32
control, not a claim about whole-model accuracy or generation quality.

The [Linux x86 report](../reports/numeric/gf16-ffn-linux.json) uses the same
compiler and arithmetic source hashes. All 45 input, stage, ActQuant and code
hashes match macOS ARM; all five complete RTL captures and clock counters
also match. Host FP32 control metrics differ slightly between platforms.
The updated standalone norm also passes all 45 rows / 311040 values on both
[ARM](../reports/numeric/gf16-wide-iteration5.json) and
[Linux](../reports/numeric/gf16-wide-iteration5-linux.json), with identical
product, unit and output hashes.

## AX7203 board result

The complete FFN passes on the AX7203 at 60 MHz: routed fabric timing is
62.64 MHz with 0.702 ns minimum slack. The
[source-bound build and physical evidence](../reports/fpga/gf16-ffn-2026-10-01-24917fa7/README.md)
record one SRAM load and two standard CLI runs with fresh DDR3 uploads and
readback. Both the real BOS input (run 6) and zero input (run 7) match every
32768 stage value and 9472 ActQuant value/code. Independent reconstruction
checks all 13496368 readback bytes per run. The zero run rejects two damaged
CRC frames and successfully re-requests them; the BOS run rejects none.

Total execution takes 18.779 s and 18.775 s respectively, including the full
stage trace at 460800 baud. Roughly 14 s is report backpressure; the remaining
`controller_other` includes normalization waits and overlap with reporting,
so it is not a pure compute counter or a production throughput measurement.
The retained hardware temperature maximum is 52.87 °C, below the 70 °C gate.
UART is restored to 115200. Failed timing prototypes were not loaded, and no
flash writes were performed. This qualifies the layer-0 FFN on this board;
attention, residuals and whole-model inference remain outside this result.
