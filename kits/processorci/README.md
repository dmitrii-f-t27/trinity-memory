# Trinity arithmetic kit for ProcessorCI

Number-format units generated from executable t27 specifications, a harness that runs them inside
[ProcessorCI](https://github.com/LSC-Unicamp/processor_ci) in the slot of a RISC-V core, and an independent
reference model for every output. The aim is to let a ProcessorCI board check arithmetic blocks exhaustively,
the way it already checks cores.

## What is inside

| Unit | Source | What it does |
|---|---|---|
| FP8 E4M3 decode | `t27/rtl/fp8_e4m3_decode.t27` | OCP FP8 E4M3FN (`float8_e4m3fn`) code to the binary32 bits of its exact value |
| FP8 E4M3 encode | `t27/rtl/fp8_e4m3_encode.t27` | binary32 to E4M3FN, round to nearest, ties to even; the overflow rule is an input |
| Ternary dense5 decode | `t27/rtl/dense5_decoder.t27` | five trits per byte in base 3 |
| Ternary baseline5 decode | `t27/rtl/baseline5_decoder.t27` | five 2-bit lanes (00 = 0, 01 = +1, 10 = −1, 11 reserved) |
| Ternary sparse 4:1 decode | `t27/rtl/sparse41_decoder.t27` | four trits with at most one non-zero |

**The E4M3 overflow rule.** The rule is where conforming implementations part ways, so the encoder takes it as an input:
- `saturate = 1` sends overflow and infinities to ±448 (the AMD and tt-metal convention);
- `saturate = 0` sends them to NaN (Google's ml_dtypes).

Both are legal under OCP MX. See [jax-ml/ml_dtypes#400](https://github.com/jax-ml/ml_dtypes/issues/400).

**Generated code.**
- The Verilog in `rtl/generated/` comes from those specifications through the pinned t27 compiler (`native/compiler.lock`, `scripts/generate_rtl.py`).
- `rtl/generated/provenance.json` records the compiler revision and the SHA-256 of every source and output.
- CI regenerates the modules and fails if the copies here differ by a byte.

**Hand-written parts.** The harness (`rtl/trinity_arith_harness.v`), the testbenches and the host script are test infrastructure written by hand. They contain no decoding, encoding or rounding.

## Results (simulation, Icarus Verilog)

| Check | Comparisons | Mismatches |
|---|---:|---:|
| E4M3 decode, every code | 256 | 0 |
| E4M3 encode, every BF16 input, both overflow rules | 131,072 | 0 |
| E4M3 encode, every rounding boundary ±1 ulp, specials, binary32 subnormals, both rules | 2,140 | 0 |
| E4M3 encode, 65,536 seeded binary32 inputs, both rules | 131,072 | 0 |
| Harness tests 1–8 in the ProcessorCI bus model: output count and CRC-32 of every output | 8 | 0 |

**The reference.** The expected values come from `model/e4m3_ref.py`. It enumerates the exact rational value of every code and picks the nearest one, ties to the even code. That is a different algorithm from the RTL, which shifts and rounds bit fields.

**Checked against ml_dtypes.** The reference agrees with ml_dtypes 0.6.0, in its non-saturating mode, on:
- every decode;
- every BF16 input;
- every edge input;
- every seeded input.

The check is `tools/crosscheck_ml_dtypes.py`.

**Ternary references.** `model/ternary_ref.py` builds the ternary expectations from the format definitions.

## Reproduce

```sh
make -C kits/processorci sim     # Python 3 and Icarus Verilog; no t27 compiler needed
make -C kits/processorci check   # CI: also regenerates the vectors and checks the generated copies
```

## Running it in ProcessorCI

The harness speaks the core side of the ProcessorCI Wishbone bus:
- it reads a descriptor;
- it drives one unit over a whole input space from on-chip counters;
- it folds every output into a CRC-32;
- it writes a result block;
- only then does it touch the end address, which ends the run.

**Memory map** (inside the default 8 KiB):

| Address | Contents |
|---|---|
| `0x0000` | test id |
| `0x0004` | input count (tests 4 and 5) |
| `0x0008` | xorshift32 seed (tests 4 and 5) |
| `0x0100` | result block: magic `TRI1`, test id, outputs folded, CRC-32 (zlib), status (`0x600D` done, `0xBAD1` unknown id) |
| `0x1FFC` | end address |

**Tests:**

| Id | Test | Outputs |
|---:|---|---:|
| 1 | E4M3 decode, every code | 256 |
| 2 | E4M3 encode, every BF16 input, saturating | 65,536 |
| 3 | E4M3 encode, every BF16 input, NaN on overflow | 65,536 |
| 4 | E4M3 encode, seeded binary32, saturating | count |
| 5 | E4M3 encode, seeded binary32, NaN on overflow | count |
| 6 | dense5 decode, every byte | 256 |
| 7 | baseline5 decode, every code | 1,024 |
| 8 | sparse 4:1 decode, every byte | 256 |

Expected counts and CRCs are in `vectors/manifest.json` under `harness`. The longest test takes 65,565 cycles, about 1.3 ms at 50 MHz.

**Steps:**
1. Copy `processorci/config/trinity_arith.json` to `config/` in a `processor_ci` checkout and `processorci/rtl/trinity_arith.sv` to `rtl/`. The wrapper is their `template.sv`, with the harness in the core slot.
2. Build and flash as for a core (Arty A7-100T is their default board).
3. Run the host script with their communication library:

   ```sh
   PYTHONPATH=/path/to/processor_ci_communication \
     python3 kits/processorci/host/run_arith.py --port /dev/ttyUSB1 --junit results.xml
   ```

The harness does not execute RISC-V programs, so their program-based runner is not used for it; `host/run_arith.py` takes its place.

**Size.** yosys `synth_xilinx -family xc7` of the harness with all five units gives:
- about 915 LUTs, 317 flip-flops and 90 CARRY4;
- no latch cells in the netlist. yosys prints latch warnings for function temporaries in the dense5 and sparse 4:1 code; they do not survive synthesis.

This is not a timing result.

## Not done yet

- Not yet run on a ProcessorCI board. The host script follows their runner's calls but has only been exercised against the bus model in `sim/tb_harness.v`.
- No place-and-route or timing for the Arty A7-100T. Our own Artix-7 flow targets a different board.
- The ternary dot product (`t27/rtl/dot_stream.t27`, generated, tested in `tests/t27_rtl.py`) is not yet a harness test.

Licence: Apache-2.0, as the repository. `processorci/rtl/trinity_arith.sv` follows the MIT-licensed ProcessorCI template.
