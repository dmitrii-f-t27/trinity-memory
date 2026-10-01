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
fixture/capture caches.

```sh
python -m unittest tests.test_gf16_scalar tests.test_gf16_ffn tests.test_gf16_wide -v
python tools/replay_gf16_ffn.py --rtl selected
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

Board execution requires a hash-matching build report with **routed timing
PASS**, fresh XADC and matching board DNA. Use `--gf16-ffn` for both
`tools/fpga-matvec-boot.py` and `tools/fpga-ffn-run.py`, and `--max-temp 70`.
Only SRAM configuration is used. Diagnostic timing-failed builds are never
loaded. Functional RTL agreement alone is not evidence of working board timing.


Implementation checkpoint: small/adversarial clocked simulations and scalar
conformance pass. The first integrated combinational prototype also matched
one full captured BOS input. Routed timing is still being closed; these
checkpoints do not claim a qualified GF16 board run. Final source-bound replay
and board reports will replace this checkpoint before hardware qualification.
