# Q16 FFN on AX7203 (issue #92)

Generated serial FFN executed on the board from real BitNet weights: both
full-layer runs (seed 27 and the zero vector) match the corrected integer
oracle exactly, and an independent offline replay re-decoded every memory
readback and re-checked every signed value. Source of record
`eecc619f6e2bc49958f969b455ac67f8158be5d9` on `codex/ffn-fpga-q16`
(draft PR #93); host tooling at `43441b0b3b17b4d2a85a61a6ca7416bfe2475941`.
Dimensions H=2560, I=6912, O=2560, baseline2 packing, corrected Q16.16/Q32.32
contract from #91. Not covered: ActQuant/bf16, attention, residual paths,
full-model inference.

## Build of record

- `a138be21`: synthesis done, placement stopped (UART RX XDC missing in
  `DDR3_APP=ffn`); no bitstream. Fixed in `a6eed6b9`.
- `a6eed6b9`: routed 28.76 MHz, below the 60 MHz requirement; critical path
  crossed rounding and wide sequential carry operations. No FFN bit loaded.
- `eecc619f`: limb arithmetic and rounding pipelined without changing numeric
  boundaries. Routed 70.41 MHz (PLL 6/5) against the 60 MHz target; final
  Yosys CHECK zero problems; 42 RAMB36E1. Bitstream whole-file SHA-256
  `bbdc958d3f8d965b877ca04b6622a3f267ebb9cfb60a07d111ee0750c0ef2447`
  (`build/build.json`).
- Tool pins: t27c `bff21b85b206a0dd367876343e56dcd8312d81ba`, nextpnr
  `0eae9fbb19dfb83cdd30d5048d8b0ba744180ad0`, host yosys `0.69+post`
  `143eb14f9cc55d6f8927e68523b0c9d2166ed02c`. Full source hashes:
  `source-manifest.json`.

## Boot

`boot/boot.json`, `boot/uart-raw.txt`: SRAM load, all boot checks pass,
build ID and F(2560,6912) header match, DDR3 calibration complete,
XADC 48.97 C after boot. DNA `0x00389c0c2d85e85c`, IDCODE `0x3636093`.
Modeled CK-DQS offsets -1077/-687 ps (route model only, not PHY signoff).
UART 921600 during transfers, cable `digilent_hs3`, `/dev/cu.usbserial-110`.
No configuration flash was written.

## Simulation (`stage-5-build-gates.json`, `stage-5-pipelined-*-tests.log`)

| Run | Result |
|---|---|
| `ffn-full-sim-pipeline/` (seed 27) | 32768/32768 stage values exact, 0 saturations, all f64 guards pass, 122814202 cycles; raw capture SHA-256 `9b49e97fe06acd7c3dde14f29537375a26e5a5028dfdb08b3dabcddfaadb4d7f` |
| `ffn-zero-sim-pipeline/` | 32768/32768 exact, 0 saturations, same 122814202 cycles |
| `ffn-saturation-sim-pipeline/` | 27650 values, all 6912 expected `a` saturations to -2^63 at 8863510 cycles (139-bit sum-of-squares bound) |

Six unittest methods pass under both Icarus and Verilator (259 wide
arithmetic vectors, six small complete FFN vectors, capture corruption
checks, real UART RX timing constraints).

## Board runs

Both runs from the same `eecc619f` SRAM load; each run loads and reads back
every payload before the doorbell.

| | `seed27/` (run 1) | `zero/` (run 2) |
|---|---|---|
| Stage values | 32768/32768 exact | 32768/32768 exact |
| Saturations | 0 in all six stages | 0 in all six stages |
| Capture SHA-256 | `19ab76eca5ec9e06a758a5091c6084c751218b54b7cd2c4a17e7344f1e3d0fe7` | `e1a6beeeb0d813b7d2d48d4cbd9196745dea3bc5c7099047f390f8083233034c` |
| Clocks (60 MHz, incl. UART reporting) | 860531932 | 860531932 |
| f64 error | all guards pass (`outside_guard` false everywhere) | all errors exactly 0 |
| Die temperature | 49.20-50.12 C, 35 samples | 49.98-50.37 C, 35 samples |

`independent-verification.json` in each run directory: replay decodes every
loaded memory block from the compressed raw UART with CRC (qualification 16,
gate/up/down 2160 each, scales 1, post 20, sub 54, x 20 valid blocks), checks
all payload hashes and bitstream identity, recombines signed halves and
compares every scalar, verifies doorbell acknowledgement, no retransmits,
UART restored to 115200.

## Layout

- `vectors-seed27/`, `vectors-zero/`: `inputs.json` (sizes, addresses, SHA-256
  of every payload) and `reference.json.gz` (exact integer stages, saturation
  counts, f64 error report).
- `seed27/`, `zero/`: `command.json`, `identity.txt`, receipts
  `qualification.json.gz`, `load-*.json.gz` (SHA-256 inside is of the
  uncompressed file), `capture.txt.gz`, `thermal.jsonl`, `uart-fast.*`,
  `uart-restored.*`, `result.json`, `independent-verification.json`.
- `build/`: `build.json`, `nextpnr.log`, `yosys_stat.txt`, XDC.

## CI

All checks pass on `eecc619f`: bitstream, ddr3, native, spec, fixtures,
python 3.10/3.12/3.14, sdk, ternary-check and upstream-15193
(runs 36516270821, 36516273193). `43441b0` adds the generated FFN RTL
regressions to the native CI job.

## Limitations

Clock counts include UART report backpressure and exclude host uploads; they
are not a compute-only throughput measurement. Fabric timing is not DDR3 PHY
signoff. This is the FFN block only, not model inference.
