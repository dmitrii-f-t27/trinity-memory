# Compute-pipeline evidence recheck

Result date: 2026-09-29 UTC. Refs #88, PR #89.

## Result

The retained dense5 q_proj comparison reproduces a 1.6427495396975953 speedup:
235544 reference clocks divided by 143384 pipelined clocks, at the same derived
60 MHz controller frequency. The matrix has 320 * 2560 = 819200 weights.
Thus throughput is 819200 * 60000000 / cycles, or 208.6743878 and
342.7997545 million weights per second, respectively.

During this recheck, all 24 raw UART capture SHA-256 values matched their
measurement manifests. Every capture's 320 indexed Y values matched the
reference recomputed from the pinned q_proj fixture (24 * 320 = 7680 checked
rows). The full-tensor reference hash also matched the earlier committed
matvec report. All runs record zero bad words and zero stray acknowledgements.
No hardware was accessed and no new board measurement is claimed.

## Correction to the ceiling statement

The measurement JSON already gives a dense5 arithmetic ceiling of
4800000000 weights/s: 80 weights per controller word * 60000000 clocks/s.
342799754.5 / 4800000000 = 0.07141661552, approximately 7.14%.
The former text claiming 47% of 730.7 M weights/s is unsupported and is
withdrawn. Neither fraction establishes measured bus utilisation or saturation.

## Validation provenance

The source revision d375d7ff988a74aadf8d884ddd7abceb6ba7c0fa has 26 successful
GitHub checks, including the bitstream and DDR3 workflows. Its successor
c81809476ffa31520ce5920be6f58ca319404db9 only adds documentation and retained
measurement artifacts; all files outside docs/ and reports/ are identical.
This correction also changes only documentation. The repeated bitstream
workflow on the documentation snapshot was still running when reviewed;
it must not be described as completed by this report.

Local golden decoding used the SHA-256-verified macOS arm64 v0.5.0 runtime.
The relevant formats, matvec, and native core sources are unchanged from the
release tag. No new local synthesis or routed build was performed.

## Evidence

- [PR #89](https://github.com/dmitrii-f-t27/trinity-memory/pull/89)
- [Source bitstream CI](https://github.com/dmitrii-f-t27/trinity-memory/actions/runs/36482982579)
- [Source DDR3 CI](https://github.com/dmitrii-f-t27/trinity-memory/actions/runs/36482982736)
- [Source main CI](https://github.com/dmitrii-f-t27/trinity-memory/actions/runs/36482982504)
- [Reference measurement](matvec-compute-baseline-2026-09-28-d5.json)
- [Pipelined measurement](matvec-compute-pipeline-2026-09-28-d5.json)
- [Original matvec reference](../ternary-check/matvec-2026-09-23.json)

## Remaining work

The new compute mode still needs the baseline2 paired board measurement and
full down_proj compute-pipeline measurements. Full FFN and transformer
inference are separate milestones. Merge status is recorded separately.
