# GF16 conversion contract — iteration 1

Issue [#103](https://github.com/dmitrii-f-t27/trinity-memory/issues/103).
Profile identifier: **`gf16-rne-gradual-v1`**. Status: executable candidate
for FFN experiments, not a replacement for the validated Q16 board FFN.

The layout alone does not specify rounding, underflow or NaN handling.
This profile makes those choices explicit before using GF16 for activations
and scales. It is not bit-compatible with every existing GF16 arithmetic
implementation; see [the pinned source audit](gf16-legacy-audit.md).

## Values and conversion

GF16 is `[S:1 | E:6 | M:9]`, exponent bias 31. Inputs and outputs are integer
bit patterns, independent of host byte order. The RTL interface carries GF16
in the low 16 bits of a 32-bit port; the input high bits are ignored and output
high bits are zero. Serialization to memory or UART is outside this contract.

| Encoding | Value / policy |
|---|---|
| `E=1..62` | `(-1)^S * 2^(E-31) * (1 + M/512)` |
| `E=0, M!=0` | `(-1)^S * M * 2^-39` (gradual underflow) |
| `E=0, M=0` | Signed zero |
| `E=63, M=0` | Signed infinity |
| `E=63, M!=0` | NaN; all GF16 NaNs are treated as quiet |
| `1.0` | `0x3e00` |
| Minimum positive subnormal | `0x0001 = 2^-39` |
| Minimum positive normal | `0x0200 = 2^-30` |
| Maximum finite | `0x7dff = 2^32 - 2^22` |

Binary32 to GF16 rounds to nearest, ties to even, once. A significand carry
propagates into the exponent. Magnitudes at or above `2^32 - 2^21` round to
infinity; exactly `2^-40` rounds to signed zero. A value halfway between the
largest subnormal and smallest normal rounds to the smallest normal.
Finite overflow/underflow and zeros preserve sign. Every binary32 NaN becomes
GF16 `0x7e01`, discarding sign/payload. Every GF16 NaN decodes to binary32
quiet NaN `0x7fc00000`. Signaling exceptions/status flags are not provided.
Every finite GF16 value decodes exactly to binary32.

There is no PHI_BIAS adjustment in this converter. This deliberately follows
the ordinary value formula above, not a claim of equivalence to an upstream
phi-biased multiplier. Frozen silicon's ties-to-zero arithmetic is a distinct
profile; this iteration does not redefine it.

## Source and hardware interface

[`t27/rtl/gf16_codec.t27`](../t27/rtl/gf16_codec.t27) is the executable source
for both generated C and Verilog. No generated artifact is maintained by hand.
The compiler is pinned by [`native/compiler.lock`](../native/compiler.lock).

The module converts `f32_in` to `gf16_out` and `gf16_in` to `f32_out` in parallel.
On each rising edge with `en=1`, outputs capture the current input conversions.
With `en=0` they hold. Active-low asynchronous `rst_n` clears both outputs,
including while disabled. There is no request queue or variable latency.
The generated `ready` port is tied high; it is not a response-valid signal.
No FPGA timing target has been established yet.

## Verification

Run with the pinned compiler, a C compiler, Icarus and Yosys installed:

```sh
python3 -m unittest tests.test_gf16_codec -v
```

`T27_ROOT` may select the pinned compiler checkout (default `build/compiler`).
Missing tools fail this gate. `tools/test-t27.sh` also invokes it in native CI.
[`tools/gf16_reference.py`](../tools/gf16_reference.py) decodes via the value
formula and encodes by searching a sorted table of representable values.
It does not duplicate the RTL shift-and-mask encoder. All input binary32
values and GF16 values are represented exactly by Python's binary64 values.

Coverage includes:

- All 65,536 GF16 decodes and round trips, with explicit NaN canonicalization.
- Each adjacent positive finite pair's exact midpoint and immediate binary32
  neighbors, both signs; the overflow boundary is included separately.
- All 65,536 BF16 bit patterns used as binary32 inputs, plus 8,192 seeded random
  binary32 inputs and directed NaNs. This exercises range handling; it is not
  a GF16-versus-BF16 accuracy benchmark.
- 314,883 unique encode vectors against generated C and Verilog, then the same
  corpus after generic Yosys logic mapping. Reset and enable hold are checked.
- Deterministic C/Verilog generation and Yosys `check -assert`.
- ASan/UBSan execution of the entire encode corpus and all GF16 round trips.

Passing conversion checks does not prove GF16 addition/multiplication, FFN
accuracy, LUT cost on the target FPGA, routed frequency or board throughput.
Next: compare GF16 and BF16 quantization of real FFN activations/scales, report
overflow/underflow and layer error, then choose explicit arithmetic and
accumulation rules before replacing the Q16 reference implementation.
