# GF16 FFN performance evidence — 2026-10-01

Issue [#113](https://github.com/dmitrii-f-t27/trinity-memory/issues/113),
PR [#114](https://github.com/dmitrii-f-t27/trinity-memory/pull/114).

Simulation evidence is complete for source `539dbb46` against the measured
five-clock baseline `fbe03f89`. Both include the same registered RAM address
selection and 13-bit internal address bounds, isolating the projection schedule.
The earlier `b60e06fc` / `fd591b3e` measurements are retained for provenance. Result-mode active clocks fall from 288,502,758
to 183,993,318 (1.56801×). Projection-loop clocks fall from 265,420,800 to
160,911,360 (1.64948×). These are simulation measurements.
Physical qualification also passes: result-mode active time falls from
4.873065 s to 3.142136 s at the same 60 MHz
(1.55088×, excluding input transfers and output draining).
All commits and this work are recorded as dmitrii-f-t27.

The source-hashed comparison is in
[reports/numeric/gf16-ffn-performance-bounded.json](../../numeric/gf16-ffn-performance-bounded.json).
It records exact source revisions, the verified input payload/reference hashes,
full/result traces and all phase counters. Raw weights are not included.

```sh
python reports/fpga/gf16-performance-2026-10-01/verify-evidence.py
```

This verifier uses the saved integer BOS reference and compact traces; no
hardware, model download or local build cache is required. It checks every
reported word/code, exact phase partition, loop count and comparison ratio.
It also verifies five complete replay captures (163,840 stage values and
47,360 ActQuant values), their saved references and current source hashes.
All 45 integer input rows have the same input/stage/ActQuant/code hashes as
the published iteration-5 report. The new replay report is
[gf16-ffn-performance-bounded-replay.json](../../numeric/gf16-ffn-performance-bounded-replay.json).
The bounded-address variants reproduce every byte of the original four paired
captures and five full replay captures; compact captures are therefore shared.
The [Linux CI run](https://github.com/dmitrii-f-t27/trinity-memory/actions/runs/36939837659)
also passed for the earlier `b60e06fc` source: x86_64 and macOS arm64 match every source hash, all 45 integer
rows, all five raw RTL captures and both modes' clock/phase measurements.
The corresponding `*-linux.json` reports are checked by the portable verifier.
See [the measurement contract](../../../docs/gf16-ffn-performance.md) for the
active window and scope limitations. Simulation report-delay parameters must
not be interpreted as physical UART throughput.

## Physical qualification and retained diagnostics

The first routed `b60e06fc` candidate (heap seed 6) passed fabric timing at
63.85 MHz against 60 MHz but failed DDR3 calibration on two SRAM loads.
No FFN run or input upload occurred. Its build, boot and status evidence is
retained under [diagnostics/seed6](diagnostics/seed6/README.md).
Reloading the prior qualified `24917fa7` bitstream on the same board immediately
passed boot and emitted the GF16 header; its diagnostic receipt is retained
under `diagnostics/known-good`. The bounded-address accelerated source
`539dbb46` subsequently passed routed fabric timing at 62.57 MHz (heap seed 8);
the matching serial control `fbe03f89` passed at 63.20 MHz (heap seed 6).
Both final variants also passed physical boot, complete DDR input readback
and exact execution. The accelerated build additionally passed a fresh
zero-input upload and paired execution.

The synthesis tool reports an ABC `&mfs` assertion after writing its mapped
`output.aig`; Yosys reintegrates that saved mapping and finishes with zero final
CHECK problems. This warning also occurred in the prior qualified build and
is retained as a toolchain limitation. Fabric timing does not certify PHY/I/O
paths, and PLL multiplier 6 still has no listed Vivado reference value.

## Physical results

All loads were SRAM-only on AX7203 XC7A200T-FBG484-2, DNA
`0x00389c0c2d85e85c`, with a 60 MHz controller and 240 MHz x16 DDR3.
For each row below, the full run and following result-only run use the same
freshly uploaded, CRC-readback-verified inputs. Only the run descriptor changes.
The accelerated BOS and zero rows each have their own fresh full upload.

| Measurement | Serial BOS (10/11) | Accelerated BOS (20/21) | Accelerated zero (30/31) |
|---|---:|---:|---:|
| Full trace stage / ActQuant values | 32768 / 9472 | 32768 / 9472 | 32768 / 9472 |
| Result-only y values | 2560 | 2560 | 2560 |
| Reconstructed input bytes | 13496368 | 13496368 | 13496368 |
| Full-trace active clocks | 1126760796 | 1101419530 | 1101341533 |
| Result-only active clocks | 292383894 | 188528131 | 187441638 |
| Result-only active seconds | 4.873065 | 3.142136 | 3.124027 |
| Projection-loop clocks | 265420800 | 160911360 | 160911360 |
| CRC-invalid readback frames rejected/retried | 39 | 16 | 25 |
| Sampled temperature range, °C | 51.989–52.766 | 51.972–52.881 | 52.673–53.143 |
| Retained temperature maximum, °C | 53.216 | 53.239 | 53.469 |

The result-mode BOS active window is **1.55088× faster**
(35.520% lower latency). DDR wait clocks are
included; the transfer into DDR and the final UART drain are excluded. The
full diagnostic trace changes only from 18.779347 s to
18.356992 s because UART backpressure dominates it.
These are single paired measurements on the stated inputs, not a throughput
distribution, energy measurement or complete-model inference result.

The same board identity, bitstream hashes, successful baud transitions and
70 °C thermal limit are checked for every run. All three runs restore UART
to 115200. The independent verifier rejects invalid readback frames and
requires CRC-valid coverage of every byte; all input load retransmit maps
are empty (readback retries are recorded separately). Signed zero bits in
the zero-input trace are compared exactly with the oracle.

Synthesis resource counts (LUT1–LUT6, not nextpnr BEL counts): serial
19,637 LUT / 13,923 FF / 38 RAMB36E1 / 0 DSP48E1; accelerated
19,590 LUT / 13,923 FF / 38 RAMB36E1 / 0 DSP48E1. Accepted route seeds are
serial 6 and accelerated 8; failed timing seeds and tool warnings are retained
in their build logs. Exact source commits, commands, FASM/bitstream identities
and both successful boot receipts are under `baseline/` and `accelerated/`.

```sh
python reports/fpga/gf16-performance-2026-10-01/verify-evidence.py
python reports/fpga/gf16-performance-2026-10-01/verify-board-evidence.py
```

The second verifier checks all six compact physical traces and the published
[board-performance.json](board-performance.json), including identical BOS
input hashes and measured ratios. Local raw payloads/readback streams and
bitstreams stay under `build/gf16-performance/`; reconstruct them with
the retained `verify-raw-board.py` (byte-identical to the verifier recorded in
the receipts) and the saved run commands. They are not
embedded in this evidence directory.

The [current-source Linux CI run](https://github.com/dmitrii-f-t27/trinity-memory/actions/runs/36944415801) also passes. Its bounded-address reports match macOS on every source hash, all 45 integer rows, all five raw full-FFN captures and both modes’ clock/phase counters. The portable verifier checks these `*-bounded*-linux.json` reports in addition to the historical reports.
