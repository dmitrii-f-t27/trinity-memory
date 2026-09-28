# BitNet down_proj on the matvec datapath

The host tools support layer-0 `q_proj` and `down_proj` from the pinned BitNet
b1.58 2B4T packed checkpoint. They compute the complete tensor's integer
reference and verify its accumulator SHA-256 against the committed stage-1
report before selecting rows or opening the serial port.

`down_proj` has 2560 rows and 6912 columns. Both device encodings use 7040
columns: append 128 zero weights per row and 128 zero activations. This common
multiple of 64 and 80 preserves every integer accumulator while allowing an
identical matrix for dense5 and baseline2. It fits the existing activation
store. Throughput reports count the original 6912 logical weights per row and
record the padding overhead separately.

Build the board application with `DDR3_APP=matvec`, `DDR3_MATVEC_COLS=7040`,
`DDR3_MATVEC_ROWS=2560` for the full matrix (or the selected chunk size), and
`DDR3_MATVEC_FMT=1` for dense5 or `0` for baseline2. Keep clock/read/output
settings explicit as described in [matvec-pipeline.md](matvec-pipeline.md).
`tools/fpga-matvec-boot.py` checks the report’s bitstream hash and routed
timing before loading SRAM. Pass `--cols 7040 --rows 2560 --fmt 1` for
the dense5 build (`--fmt 0` for baseline2) and a fresh output directory.
After the boot record confirms calibration and the expected shape, run:

```sh
python3 tools/fpga-matvec-run.py --tensor down_proj --fmt d5 --rows 2560 \
  --design-hz 60000000 --port /dev/cu.usbserial-10 \
  --output build/down-proj-capture.json
```

`--fmt b2` selects baseline2 and requires the matching bitstream. The host
checks every Y value and row index, the run ID, word count, invalid words and
stray acknowledgements. It saves all decoded lines, including failed captures.
The measurement tool also accepts `--tensor down_proj` and records the actual
shape and row range instead of a hard-coded q_proj label.

Simulation checks 16 real rows in both formats through the generated arbiter,
with a competing master, random stalls and acknowledgement latency. An
independent integer dot verifies the padding, and every simulated result must
equal the unpadded golden reference. The full-matrix board captures below
provide a separate check of the physical datapath.
This is an integer matrix-vector product, not a complete transformer inference.

## Full matrix on AX7203, 2026-09-28

Both bitstreams were generated from the clean source tree at
`8d1bdf5fa08da5318a1a9c7fb8628e030f852de0`: controller 60 MHz, DDR3 240 MHz,
x16, CAP=4, pipelined reads, gap 0, deferred Y output. The dense5 build passes
routed fabric timing at 66.92 MHz (seed 1); baseline2 at 67.56 MHz (seed 5).
Hash-checked SRAM loads and retained boot headers confirm each build's identity,
2560 × 7040 device shape and DDR3 calibration.

Every weight and activation payload was read back byte-for-byte, with every
chunk acknowledged and no retransmissions. Both formats then passed 12
successive full-matrix runs: **24 runs / 61,440 bit-exact row results**, zero
bad words and zero stray acknowledgements. For every run, the SHA-256 of all
2560 signed 64-bit accumulators equals the committed unpadded golden tensor.

| Format | Weight payload, bytes | Words/run | Cycles/run | Logical M weights/s | Max outstanding |
|---|---:|---:|---:|---:|---:|
| dense5 | 3,604,480 | 225,280 | 5,181,463 | 204.900 | 4 |
| baseline2 | 4,505,600 | 281,600 | 5,350,423 | 198.430 | 4 |

All 12 cycle counts repeat within each format. Rates count 17,694,720 original
weights, excluding the 128 padding columns; activation prefetch, payload
transfer and UART result emission are outside the computation timer. The
format ratio is 1.0326. These runs establish the tested integer matvec's
correctness and throughput, not complete model inference or DDR3 saturation.

The compact evidence index is
[`down-proj-board-2026-09-28.json`](../reports/fpga/down-proj-board-2026-09-28.json).
Its linked measurement reports include hashes for the retained raw Y/summary
UART captures. Build reports and raw boot captures are adjacent. The index
also records payload and loader-readback hashes and successful transfer checks;
compressed full loader readbacks, which contain the matrix payload, are
retained locally rather than added to the repository.

The source commit's CI passed all 24 push/PR checks. Its native job runs the
protocol and synthetic RTL regressions; the two fixture-dependent RTL cases
are explicitly skipped there. The real q_proj and down_proj cases were run
locally with cached fixtures, followed by the full board checks above.
