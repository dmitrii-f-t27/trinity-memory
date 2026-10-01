# GF16 wide product and normalization — iteration 4

Issue [#109](https://github.com/dmitrii-f-t27/trinity-memory/issues/109).
This standalone kernel addresses the product overflows in the
[real activation capture](gf16-bitnet-runtime.md). Its arithmetic profile is
`gf16-wide-norm-v1`; scalar encoding remains `gf16-rne-gradual-v1`.


Iteration 5 integrates this arithmetic into the [full GF16 FFN](gf16-ffn.md).
The kernel now uses a clocked scalar datapath and separate reduction stages to
shorten combinational paths. The numerical profile is unchanged. The iteration-4
reports below retain their original source hashes and resource measurements;
they do not describe the current integrated board implementation.

## Contract and bounds

Accept 1..6912 pairs of finite GF16 gate/up values and finite GF16 norm weights.
For a finite GF16 number, the unsigned significand has at most ten bits, and
the smallest nonzero magnitude is 2^-39; maximum magnitude is below 2^32.
`relu(g)^2` therefore has at most twenty significant bits and is exact in FP32.
Its product with `u` needs at most thirty significant bits and has magnitude
in [2^-117, 2^96). Round that product once to FP32, RNE. It is always a normal
FP32 value or signed zero: widening here is justified for **every finite GF16
input**, beyond just the captured texts. Nonfinite/oversized input words are
rejected. Zero product takes the sign of `u`.

Squaring that product directly in FP32 can overflow. Instead, let E be the
largest binary exponent of the nonzero products in the row and choose
`B = max(E - 40, -54)` (all-zero row uses B=-54). Quantize each magnitude to
`Q_i = RNE(abs(a_i) / 2^B)` with a separate sign. Then:

1. `S = sum(Q_i^2)` exactly; `Q_i < 2^41`, `S < 2^95`.
2. `M = RNE(S/N)`. Scale epsilon from **binary32(1e-5)** exactly as a dyadic
   rational: `eps = RNE(10995116 * 2^(-40-2B))`.
3. `D = M + eps < 2^93`; `R = floor(sqrt(D << 64))`, with `0 < R < 2^79`.
4. Round the rational signed value `Q_i * 2^32 / R` directly to GF16. The
   divider calculates quotient/remainder of `(Q_i << 71) / R`; discarded
   quotient bits **and the original remainder** determine a single final RNE.
5. Multiply the rounded GF16 unit by the GF16 weight, then round once to GF16.
   A final scalar overflow is reported explicitly; it is not silently clamped.

All sums use two u64 limbs. Reuse the existing exact 64×64 / 192÷128 / sqrt192
sequential engine. The largest square-root radicand is below 2^157; numerator
for unit rounding is below 2^112. Its divisor is well below the engine's 2^127
limit. The block floor preserves epsilon in at most 92 bits and guarantees a
positive root for zero/tiny rows. At large B epsilon may round to zero, but the
row then contains a nonzero maximum-scale element, so the root remains positive.

This profile deliberately changes normalization arithmetic from the previous
FP32 control. Agreement with the integer contract is exact; differences from
the calibrated runtime must be measured separately. It does not choose GF16
for attention, ActQuant, matvec accumulators or the whole model.

## Implementation and validation

The source is `t27/rtl/gf16_wide_norm.t27`, sharing `ffn_wide.t27` with the
validated Q16 FFN. `rtl/t27/gf16_wide_norm.v` only wires the two generated
modules. Ten-bit significand multiplication uses ten shift/add terms rather
than the compiler's general 64-bit multiplication expansion. Row stores have
one registered read port each; weights store only their sixteen validated bits.

The [numeric report](../reports/numeric/gf16-wide-normalization.json) replays
all **45 real captured token rows / 311,040 elements**:

Gate/up inputs to this kernel are recomputed by the host GF16 candidate from
the captured BF16 `x`. Input normalization and gate/up projections remain on
the host in this experiment; they are not hardware implemented by this kernel.

- Every product bit agrees with the previous FP32 wide-product control.
- Generated C helpers agree with the integer oracle for product, block scaling,
  rational-to-GF16 rounding and gamma multiplication on every lane.
- The full generated RTL executes all 45 rows with Verilator: every product,
  rounded unit and scaled output agrees, as do the exact sum of squares and root.
- The integer normalization changes **161 of 311,040** scaled GF16 values versus
  the software FP32 normalization. On these inputs the following ActQuant
  absorbs those differences (zero changes in its integer codes): **all 115,200 final FFN outputs remain identical**
  to the earlier software GF16 wide-product candidate.

| Text | Changed `s` values | Final `y` NMSE vs FP32+ActQuant | BF16 / candidate NMSE |
|---|---:|---:|---:|
| English explanation | 29 | 1.37214e-4 | 2.03× |
| Arithmetic | 42 | 2.06047e-4 | 2.39× |
| Russian | 38 | 1.57626e-4 | 2.32× |
| Code | 52 | 1.25749e-4 | 2.66× |

Only the product/sub-norm kernel is RTL here. The final down projection and
ActQuant comparison remain in the calibrated host implementation. This is
not an entire GF16 FFN on FPGA, and four short prompts do not establish general
model accuracy. Captured BF16 stages are recalibrated before each replay.

Eight standalone tests cover bounds, epsilon, ties/sticky remainder, all GF16
words paired with a finite operand and 100,000 random C pairings, **161,397
RTL arithmetic pairs**, C ASan/UBSan, clocked mixed/extreme rows, input gaps,
backpressure, enable pauses, invalid input, arithmetic faults and mid-operation
reset. Three adversarial rows also run after generic Yosys mapping. The standard
native gate requires the compiler/tools; adapter-only discovery can skip the
generated tests when the compiler is not installed.

The pinned C backend cannot emit a valid initializer for the controller's RAM
arrays. C conformance therefore translates the **unchanged pure-function prefix**
of the same specification; the complete controller is verified in RTL. A local
`shift` variable also avoids the pinned Verilog backend hoisting a repeated
expression before its dependency is initialized. Generated files are not edited.

## Interface

All handshakes occur on rising edges with `en=1`; `en=0` freezes both modules.
Pulse `start` while `busy=0`, with `row_length` in 1..6912. A start while busy is
ignored. Then hold each `gate_in`, `up_in`, `weight_in` tuple with `in_valid`
until `in_ready`; exactly N tuples are accepted. Inputs are u32 ports carrying
GF16 words: upper sixteen bits must be zero. Computation begins after the last
input. The entire row is buffered so its common exponent can be determined.

For each output, `out_valid` holds `index`, `out_product` (FP32 bits), `out_unit`
and `out_value` (GF16 words) stable until `out_ready`. `done` pulses for one
enabled clock after the final accepted output, with `busy=0`. `overflow_count`
and `fault` describe the row. Error codes: 1 invalid length, 2 invalid/nonfinite
input, 3 arithmetic-engine fault, 4 final GF16 gamma-product overflow. Codes
1–3 abort the row; code 4 emits signed infinity for each overflowing lane and
flags the completed row. A new start clears error state. Reset cancels an
in-flight row and math operation; RAM contents need not be reset because a new
row must load all its entries before calculation.

`cycles` counts enabled busy clocks, including input/output waiting. The real
rows took about 3.63 million such clocks under the testbench's imposed gaps
and stalls; this is not a measured device latency or a throughput claim.

## Synthesis and reproduction

The [synthesis report](../reports/numeric/gf16-wide-synthesis.json) records
source/compiler/tool fingerprints and standalone Xilinx xc7 mapping:
**7,793 LUTs, 2,112 flip-flops, 826 CARRY4, 12 RAMB36E1, no DSP or distributed
row RAM**. This includes the existing serial wide-arithmetic engine. Generic
mapping and Xilinx mapping both pass `check -assert`, without inferred latches.
There is no placement, routing, clock constraint, bitstream or board measurement
for this kernel yet. Integration and timing closure are subsequent work.

Use the pinned optional capture environment and `native/compiler.lock` compiler.
Icarus, Yosys and a C compiler are required; the real-row RTL replay also needs
Verilator. As in the existing AX7203 flow, synthesis must use
`read_verilog -nomem2reg` to preserve the clocked arrays.

```sh
T27_ROOT="$PWD/build/compiler" python -m unittest tests.test_gf16_wide -v
python tools/capture_bitnet_layer0.py --fetch --output build/bitnet-capture/local-report.json
python tools/replay_gf16_wide.py --rtl all --capture-report build/bitnet-capture/local-report.json
python -m tools.gf16_wide_build
```

Without `--fetch`, capture is offline and rehashes the pinned cache. The replay
verifies every captured tensor hash before executing. `--rtl selected` runs the
common BOS and the last token of each text (five rows); `--rtl none` is explicitly
numerical-only. Reports retain tool/runtime versions and real-row hashes.
Simulator diagnostics include host timing and paths, so compare arithmetic
metrics/hashes rather than assuming whole JSON bytes are portable. The dedicated
Linux workflow requires all 45 RTL rows and records a separate numeric/resource
artifact. The production Q16 board path remains independently validated.
