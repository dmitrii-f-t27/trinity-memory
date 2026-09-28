# Matvec read modes and timing

The AX7203 matvec can keep its original serial read policy or issue several
ordered reads through the existing Wishbone arbiter. The default stays serial;
selecting the experimental path is explicit in the build parameters and build
report. The implementation is checked in simulation and by the paired board captures
below.

| Make parameter | Default | Meaning |
|---|---:|---|
| `DDR3_MATVEC_SERIAL_READS` | 1 | 1: one request in flight; 0: up to CAP |
| `DDR3_MATVEC_READ_GAP` | 8 | Idle clocks after an ack; must be 0 for pipelined mode |
| `DDR3_MATVEC_DEFER_RESULTS` | 0 | 1: buffer row results and emit Y lines after timing stops |
| `DDR3_MATVEC_CAP` | 4 | Outstanding-request limit, 1..4 |

Deferred output holds up to 4096 rows in a synchronous-read memory, one signed
21-bit result per row. Columns must fit the 7168-byte activation store and be
aligned to 80 lanes (dense5) or 64 lanes (baseline2). The build rejects unsupported
shapes. Row results, Y line ordering and the doorbell protocol are unchanged.
The result buffer is overwritten each run; activation prefetch starts afresh.
A repeated-doorbell regression also fixes `issue_done` remaining set after a
completed run, which prevented the next activation prefetch in RTL simulation.
An unchanged-doorbell regression fixes a second defect found on the board:
`last_run = doorbell_run` read the previous tick's value, so a completed command
ran twice. Both registers now capture the descriptor's run ID directly.

The `c.b` cycle counter now includes the weight phase and the consumer tail,
including the fixed 12 drain clocks. It excludes activation prefetch. With
`DEFER_RESULTS=1`, it also excludes Y emission; with `DEFER_RESULTS=0`, it still
includes the consumer's waits for UART output. Older builds stopped counting
at the final memory ack, before the tail: do not compare those counters directly
with the new scope. The measurement tool records the build variant and timing
scope and does not infer memory saturation from zero consumer stalls alone.

For a paired comparison at the existing 60 MHz controller operating point,
build both modes with `DEFER_RESULTS=1`, PLL multiplier 6, DDR divisor 5, the
same rows/format, and separate output directories. Record the placement seeds
and routed timing of both builds. The serial reference
uses `SERIAL_READS=1 READ_GAP=8`; the alternate uses `SERIAL_READS=0 READ_GAP=0`.
The Makefile includes shape, format, read policy and output policy in its app
stamp so changing any of them invalidates an old netlist. Both policies appear
in the build report's variant. SRAM loading still requires the bitstream hash
check; no configuration flash is written.

Validation covers signed results, both formats, invalid words, wide baseline2
rows, unsupported buffer sizes, changed activations on successive doorbells,
and a competing master through the actual generated arbiter. A fixed-latency
memory test requires identical cycle counts with 24-clock and 2000-clock UART
line delays when output is deferred. With fixtures cached, the golden test
computes the full q_proj reference, checks its committed accumulator hash, then
simulates 320 rows in both formats through the arbiter and compares every Y.
The native CI gate now runs the matvec suite; the golden case needs the fixture
cache and remains separately conditional on it.

`tools/fpga-matvec-boot.py` checks the bitstream hash and routed timing, loads
SRAM, and saves board identity, temperature and the raw boot stream. Its default
15-second window accommodates the observed 6.7-second DDR3 calibration. Each
measurement also keeps every received byte and decoded line before validating
the run ID, row order and counters, including evidence from failed runs.

This is still a matvec consumer processing eight lanes over multiple clocks.
A faster matvec is not by itself proof of saturated DDR3 delivery or a 1.25x
packing advantage; the direct-reader benchmark measures a different limit.

## Paired board run, 2026-09-28

Source `669a144d1a681936f2ab742e3feb5c8f3e030e0b`, clean tracked tree;
AX7203, controller 60 MHz, DDR3 clock 240 MHz, x16, CAP=4,
DEFER_RESULTS=1. Every case uses the same 320 × 2560 layer-0 q_proj matrix
and seed-27 activations. All **48 runs / 15,360 row results** match the
committed golden reference, with zero bad words and zero stray acknowledgements.
Raw UART bytes, decoded lines, hashes, boot captures and routed build reports
are retained under `reports/fpga`; the compact index is
[`matvec-read-policy-2026-09-28.json`](../reports/fpga/matvec-read-policy-2026-09-28.json).

| Format | Read policy | Mean M weights/s | Sample SD, weights/s | Cycles | Max outstanding | Consumer stalls |
|---|---|---:|---:|---|---:|---|
| dense5 | Serial, gap 8 | 208.664 | 6009.3 | 235552–235567 | 1 | 19–34 |
| dense5 | Pipelined, gap 0 | 208.674 | 0 | 235544 | 4 | 11 |
| baseline2 | Serial, gap 8 | 192.747 | 0 | 255008 | 1 | 11795 |
| baseline2 | Pipelined, gap 0 | 202.085 | 0 | 243224 | 4 | 11 |

Each row contains 12 successive doorbell runs. Pipelining removes almost all
baseline2 consumer starvation and improves its throughput by about **4.85%**.
The dense5 difference is only **0.005%**. The pipelined dense5/baseline2 ratio
is **1.0326**, not 1.25: this multi-clock consumer does not saturate DDR3.
Zero sample SD means the observed integer cycle counts repeated; it does not
mean the oscillator frequency was measured with zero uncertainty.

The first timing-passing placement was retained: serial dense5 seed 2
(64.93 MHz), pipelined dense5 seed 3 (60.05 MHz), serial baseline2 seed 1
(60.49 MHz), pipelined baseline2 seed 1 (61.69 MHz). All run at the same
60 MHz derived clock. These are nextpnr fabric-clock checks, not independent
DDR PHY timing sign-off or an instrument measurement of bandwidth.

The earlier `6b0586f` trial is kept as diagnostic evidence. Its first dense5
run had correct rows, but the unchanged doorbell started a duplicate command;
the second measurement detected the wrong run and stopped. Those captures
(`ddr3-matvec-measure-2026-09-28-pipeline-d5.uart`) are not included in the
48 passing runs. A five-second boot capture also ended before calibration;
a 15-second capture confirmed calibration. The two doorbell regressions and
the retained failure explain the fix instead of hiding the failed experiment.

The former approximately 1.48 M weights/s result included UART backpressure
and used a different timer boundary. It cannot establish DDR3 latency or be
used as the baseline of a claimed 140× compute speedup.
