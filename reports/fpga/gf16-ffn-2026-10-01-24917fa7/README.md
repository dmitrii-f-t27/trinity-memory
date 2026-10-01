# GF16 full FFN on AX7203 — issue #111

Hardware source: `24917fa7ba8ec15bf4e2c8b73d49451fb3a33240`.
Its RTL is unchanged at the board runner's commit
`49dd499916598a9b1a80522f8dcfdd75dc1f2d27`.
The profile is `gf16-ffn-v1`: input RMS normalization, rational ActQuant,
ternary gate/up, widened ReLU² product, sub-normalization, ActQuant and down.
Shape: 2560 → 6912 → 2560. This is the layer-0 FFN, without attention,
residuals, later layers, logits or a whole-model quality claim.

## Build and boot

- AX7203, XC7A200T-FBG484-2, x16 DDR3; controller 60 MHz, DDR3 240 MHz.
- Yosys 0.68+27 (`72ce1b63d-dirty`, the installed OSS CAD Suite build),
  nextpnr-xilinx 0.9.7, heap seed 5, router2.
- Routed fabric timing: **62.64 MHz, PASS at 60 MHz**, minimum slack 0.702 ns.
  Seeds 1 and 4 failed timing; seed 3 was stopped without a routed result.
- Synthesis: 18570 LUT2–LUT6 cells, 13331 flip-flops, 38 RAMB36E1,
  no DSP48E1. These are synthesis counts, not nextpnr LUT5/LUT6 BEL counts.
  The final Yosys check reported zero problems.
- Bitstream SHA-256:
  `2616f03dd870e94af26021072dc9b4f3537392e8798399c330cada220f4f278c`.
- FASM SHA-256:
  `620e7d8d05cfd31989681ddc89bca05d6d4552ed5a47cdc88a9badf39d67731a`.
- SRAM load only. Boot checks pass: build ID `24917fa7`, header
  `G(2560,6912)`, DNA `0x00389c0c2d85e85c`.
  XADC: 51.372 °C before loading, 51.0105 °C afterward.

See [build/build.json](build/build.json), its retained constraint/statistics/
route logs, and [boot/boot.json](boot/boot.json). Fabric timing does not qualify
the PHY primitive and I/O timing; physical readback and execution checks are
reported separately below. The PLL table also retains the existing flow's
"no Vivado reference" status for multiplier 6.

## Physical execution

Both runs use the standard `tools/fpga-ffn-run.py --gf16-ffn` CLI, with fresh
full uploads and CRC-checked readback at 921600 baud, result capture at 460800,
and XADC sampling every ten seconds. The FPGA was not reconfigured between
these runs. Exact commands and receipts are in [bos/](bos/) and [zero/](zero/).

| Measurement | Real BOS, run 6 | Zero input, run 7 |
|---|---:|---:|
| Stage values, exact | 32768 / 32768 | 32768 / 32768 |
| ActQuant values and signed codes, exact | 9472 / 9472 | 9472 / 9472 |
| Independently reconstructed readback bytes | 13496368 | 13496368 |
| Total controller clocks | 1126760796 | 1126523584 |
| Report backpressure clocks | 838499536 | 839345528 |
| Memory wait clocks | 8487599 | 8473442 |
| Other controller clocks | 279773661 | 278704614 |
| Total execution at 60 MHz | 18.779347 s | 18.775393 s |
| XADC samples | 37 | 37 |
| Sampled temperature range | 52.024–52.268 °C | 52.074–52.516 °C |
| Retained hardware temperature maximum | 52.7542 °C | 52.8696 °C |

Both pass strict indexed stage/code comparison, doorbell acknowledgement,
input/build/capture hashes, thermal limit 70 °C, and checked baud transitions.
The BOS reference hashes also match the shared BOS row of the committed
[numerical replay](../../numeric/gf16-ffn.json). The two capture SHA-256 values
are `48ff06096c8ec5e29760bb84d203a9f064155b29605a13c30c29b60932ccfb11`
and `dcc939288a0b73c88da4e1f1cf186a3d995517fb563bd6401b77d994f86685b4`.

The independent raw-stream verifier rejects two CRC-invalid readback frames
in the zero run, at gate address 3600384 and up address 10174464. Successful
repeat requests provide complete CRC-valid coverage and exact final bytes.
The BOS run rejects no frames. Neither run needs load retransmits; that load
counter does not include the two readback retries. Both restore UART to
115200, and the port is released after completion.

Total execution includes the full diagnostic stage trace, about 14 seconds
of report backpressure. `controller_other` includes normalization-engine
waits and overlap with UART reporting; it is not pure compute time. These
measurements do not establish production output-only throughput or a speedup
over the earlier parallel Q16 matvec implementation.

## Rechecking the evidence

From the repository root, validate both compact captures without hardware or
weight files:

```sh
python reports/fpga/gf16-ffn-2026-10-01-24917fa7/verify-captures.py
```

The directory retains compressed references, captures, input manifests and
transfer receipts, plus thermal samples, baud receipts and independent
verification results. Raw weight payloads, full readback streams and the
bitstream remain local under `build/gf16-ffn/`; they are not embedded here.
The retained [verify-board.py](verify-board.py) independently reconstructs
every byte from the original raw CRC streams. For example, from this original
checkout, choose a new output filename and run:

```sh
python reports/fpga/gf16-ffn-2026-10-01-24917fa7/verify-board.py \
  --run build/gf16-ffn/board-run-bos6 \
  --vectors build/gf16-ffn/bos-run6-vectors \
  --boot reports/fpga/gf16-ffn-2026-10-01-24917fa7/boot/boot.json \
  --output build/gf16-ffn/bos6-recheck.json
```

Use `board-run-zero7` and `zero-run7-vectors` for the second run. The compact
verifier checks the published numerical captures and evidence hashes; full
raw DDR3 reconstruction requires the original local payload/stream files.

## UART diagnostics

The initial 921600-baud result captures lost bytes and were rejected. The first
receiver also blocked UART reads during XADC calls and repeatedly scanned the
entire growing capture; it now uses a continuous reader and incremental
completion matching. A repeat still lost six bytes, and a no-JTAG repeat also
lost bytes, so JTAG is not established as the cause. The retained failed traces
are in `diagnostics/run1-921600`, `run2-921600`, and `run3-921600-no-jtag`.

Two diagnostic replays at 460800 passed all values, including a replay with
XADC queried during execution. They reused the unchanged, readback-verified
resident BOS input from the second upload and changed only the run-ID doorbell.
Their records explicitly state that reuse. The final qualification above uses
fresh uploads through the normal CLI. Uploads remain CRC-protected at 921600;
continuous result capture defaults to 460800 and still rejects malformed data.
