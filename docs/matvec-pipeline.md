# Matvec read modes and timing

The AX7203 matvec can keep its original serial read policy or issue several
ordered reads through the existing Wishbone arbiter. The default stays serial;
selecting the experimental path is explicit in the build parameters and build
report. This document describes the implementation and simulation checks.
Board performance must be established by fresh captures of each build.

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

The `c.b` cycle counter now includes the weight phase and the consumer tail,
including the fixed 12 drain clocks. It excludes activation prefetch. With
`DEFER_RESULTS=1`, it also excludes Y emission; with `DEFER_RESULTS=0`, it still
includes the consumer's waits for UART output. Older builds stopped counting
at the final memory ack, before the tail: do not compare those counters directly
with the new scope. The measurement tool records the build variant and timing
scope and does not infer memory saturation from zero consumer stalls alone.

For a paired comparison at the existing 60 MHz controller operating point,
build both modes with `DEFER_RESULTS=1`, PLL multiplier 6, DDR divisor 5, the
same rows/format/seed, and separate output directories. The serial reference
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

This is still a matvec consumer processing eight lanes over multiple clocks.
A faster matvec is not by itself proof of saturated DDR3 delivery or a 1.25x
packing advantage; the direct-reader benchmark measures a different limit.
