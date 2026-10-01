# GF16 FFN performance evidence — 2026-10-01

Issue [#113](https://github.com/dmitrii-f-t27/trinity-memory/issues/113),
PR [#114](https://github.com/dmitrii-f-t27/trinity-memory/pull/114).

Simulation evidence is complete for source `b60e06fc` against the measured
five-clock baseline `fd591b3e`. Result-mode active clocks fall from 288,502,758
to 183,993,318 (1.56801×). Projection-loop clocks fall from 265,420,800 to
160,911,360 (1.64948×). These are simulation measurements.
Physical qualification is in progress; there is no new board performance claim.
All commits and this work are recorded as dmitrii-f-t27.

The source-hashed comparison is in
[reports/numeric/gf16-ffn-performance.json](../../numeric/gf16-ffn-performance.json).
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
[gf16-ffn-performance-replay.json](../../numeric/gf16-ffn-performance-replay.json).
See [the measurement contract](../../../docs/gf16-ffn-performance.md) for the
active window and scope limitations. Simulation report-delay parameters must
not be interpreted as physical UART throughput.

## Physical qualification in progress

The first routed `b60e06fc` candidate (heap seed 6) passed fabric timing at
63.85 MHz against 60 MHz but failed DDR3 calibration on two SRAM loads.
No FFN run or input upload occurred. Its build, boot and status evidence is
retained under [diagnostics/seed6](diagnostics/seed6/README.md).
Reloading the prior qualified `24917fa7` bitstream on the same board immediately
passed boot and emitted the GF16 header; its diagnostic receipt is retained
under `diagnostics/known-good`. A new placement of the same netlist is being
evaluated, together with a separately routed baseline.

The synthesis tool reports an ABC `&mfs` assertion after writing its mapped
`output.aig`; Yosys reintegrates that saved mapping and finishes with zero final
CHECK problems. This warning also occurred in the prior qualified build and
is retained as a toolchain limitation. Fabric timing does not certify PHY/I/O
paths, and PLL multiplier 6 still has no listed Vivado reference value.
