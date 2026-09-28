# Stage-2 measurement erratum (2026-09-28)

The v0.5.0 measurement writer assigns two counters to the wrong names. The
UART contract in `t27/rtl/fpga_ddr3_matvec.t27` (`run_a` / `run_b`, also present
at the measured build's commit `57627bee`) is:

| Line | First field (`a`, 32 bits) | Second field (`b`, 40 bits) |
|---|---|---|
| c | words | cycles |
| o | maximum outstanding requests | command stalls |
| w | issue-hold cycles | request-wait cycles |
| n | bad words | consumer stalls |
| u | activation words | stray acknowledgements |

The old writer saved `w.a` as `wait_stalls` and `w.b` as `consumer_stalls`.
It did not save `n.b` or raw summary lines in the 12-run measurement files.
Those original files are preserved with their original hashes. The stage-2
report v2 corrects the recoverable labels; the actual consumer-stall counts
are **unknown**, not zero. The independent golden captures retain the raw
lines and show 19 consumer stalls for dense5 and 65 for baseline2. These
belong to different runs and do not fill the missing series.

The measured throughput is still 1,478,696.8 and 1,478,726.3 weights/s. The
dense5/baseline2 ratio rounds to 1.0000. Mean cycles are 33,240,080.583 and
33,239,417.000, calculated from all 12 runs rather than the first run.
Issue-hold cycles occupy 99.9440% / 99.9310%; request-wait cycles occupy
0.2465% / 0.3082%. Counters can overlap and must not be added as a breakdown.

**The earlier DDR3-latency-bound conclusion is withdrawn.** The timed matvec
emits a 20-byte UART line per result row; `y_pending` holds the consumer until
the emitter can accept that line. At 115200 baud (8N1), transmitting 320 such
lines alone takes about 0.556 s, close to the measured 0.554 s. This is a
diagnostic indication of output backpressure, not an isolated measurement
of memory service time. Buffer results or time a checksum-only run before
attributing throughput to DDR3 or testing the 1.25 packing advantage.

The arithmetic x16 ceilings at controller 60 MHz are 80 * 60M = 4.8G dense5
and 64 * 60M = 3.84G baseline2 weights/s. The old measurement JSON's ceiling
mixed bits and bytes; the original field is not used as a measurement.

The measurement writer now emits schema v2, checks required summary lines,
decodes the correct fields and retains raw lines for every run. No new board
measurements are claimed by this correction. Bit-exact output comparisons
are unaffected.

The statement that consumer A never measured pipelined delivery was also
incorrect. The committed `ddr3-reader-summary-2026-09-24-a6d9745f-x16.json`
and its three seed-6 captures contain 1653 passing, model-checked runs
(825 complete baseline2/dense5 pairs), zero bad words and consumer stalls,
0.944726–0.944888 words/clock, and up to nine outstanding requests. This
direct reader connects to UberDDR3 without the loader's Wishbone arbiter,
at controller 83.33 MHz (DDR3 333.33 MHz), a different design and operating
point from the 60 MHz integrated matvec. The seed-1 captures with zero runs
never calibrated. They are not evidence of failed pipelined data delivery.
Each load was captured for only 20 seconds; this is not a long soak.

The integrated loader/matvec's overlapping-request failure therefore needs
an integration-level reproducer. It must not be generalized to all UberDDR3
pipelined reads, or taken as proof that the arbiter is the root cause.
