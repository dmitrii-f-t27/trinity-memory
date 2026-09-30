# FFN clock split on AX7203 (issue #94)

The same full-layer FFN as #92 (H=2560, I=6912, O=2560, baseline2, Q16.16/Q32.32
contract), now reporting where its clocks go. Both board runs match the
integer oracle exactly, and an offline replay re-checked every value, every
loaded payload and the clock split from the raw captures. Source of record
`757f191b9c0b41b5c5bf92b2bd0660e567aa8a18` on `feat/ffn-compute-timing`
(PR #95). Not covered: ActQuant/bf16, attention, residual paths, full-model
inference.

## Result

Clocks at the 60 MHz controller clock, from the doorbell to the last `y` value.
Every active clock is in exactly one bucket, by the controller state it runs.

| Bucket | `seed27/` (run 1) | `zero/` (run 2) | Share | Time |
|---|---:|---:|---:|---:|
| Compute | 118308783 | 118308783 | 13.75% | 1.972 s |
| DDR3 read wait | 9979270 | 9979102 | 1.16% | 0.166 s |
| Report (UART) wait | 732243879 | 732244068 | 85.09% | 12.204 s |
| Total (`c`) | 860531932 | 860531953 | | 14.342 s |

- Compute clocks are identical in both runs, as the simulation test
  (`test_compute_clocks_do_not_depend_on_memory_or_report_latency`) requires:
  they do not depend on the input values or on memory and report latency.
- The total equals the stage 5 board runs (860531932, `eecc619f`), so the
  counters did not change what the controller does.
- Compute here is this serial design's own work: one ternary term per two
  clocks, with the wide arithmetic unit's fixed latencies. It is not a
  throughput figure for any other design, and the report wait is the price of
  printing all 32768 stage values over 921600 baud UART, not of the FFN.
- The memory bucket includes at least one request clock per read; with the
  measured 9.98M clocks for about 0.83M weight words plus vector reads, DDR3
  waits cost 1.16% of the run.

## Build of record

- `12c5ec4a` (first clock-split build): routed 58.81 MHz on seed 6, below
  60 MHz; the state decode drove the 64-bit counter enables
  (`build/failed-12c5ec4a-seed6.txt`). Not loaded.
- `757f191b`: bucket flags registered, equality compares only; each counter
  adds its flag one clock later, which leaves the totals unchanged because the
  last active clock is compute. Seed sweep: seed 1 59.94 MHz FAIL, seed 2
  62.74 MHz PASS (first to meet timing; later seeds not run;
  `build/seed-sweep-757f191b.txt`).
- Gates (`build-gates.json`): bitstream SHA-256
  `8af9abc912cbf4d6f4472e6775e76df5cc169ef6965b5607276779e2b5d78e91` matches
  `build/build.json`; final Yosys check 0 problems; 42 RAMB36E1; 0 latches;
  modeled CK-DQS offsets -439/-342 ps (stage 5: -1077/-687; route model only,
  not PHY signoff). Same tool pins, PLL 6/5 and Docker image as #92.

## Boot

- `boot-refused-93C/`: on 2026-09-29 the boot tool refused to load before any
  SRAM write: XADC read 93.36 C (session maximum 94.93 C) against the 70 C
  cutoff. `xadc-wait.log` is the read-only polling that followed: about
  94 C for more than nine hours with the old image, then no reply while the
  board was off, then 35.5 C after a power cycle on 2026-09-30.
- `boot/`: SRAM load of `757f191b` at 2026-09-30 ~07:53 -03; all checks pass,
  build ID `0x757f191b` and F(2560,6912) header match. DNA
  `0x00389c0c2d85e85c`, IDCODE `0x3636093`, cable `digilent_hs3`, UART
  `/dev/cu.usbserial-10`. No configuration flash was written.
- Temperatures during the runs: 41.1-49.4 C (seed27), 49.6-50.6 C (zero).

## Board runs

| | `seed27/` (run 1) | `zero/` (run 2) |
|---|---|---|
| Stage values | 32768/32768 exact | 32768/32768 exact |
| Saturations | 0 in all six stages | 0 in all six stages |
| Capture SHA-256 | `f1692e5e1f8244db36b0aa4fa85b566a4c0303c2931687e369dd04b16c7ba6ed` | `554dcc98131921c7cd8b50552cf6afa8246e4c18dc9a30d836850caae718be93` |
| Capture bytes | 1310960 (stage 5: 1310920 + two 20-byte `k` lines) | 1310960 |

Vectors are the stage 5 sets (`../ffn-q16-2026-09-29-eecc619f/vectors-*`).

## Independent replay

`verify-ffn-board.py <run>` (paths are this machine's: repository checkout,
`build/stage6`, stage 5 vectors on the transfer SSD) checks the boot identity
and hashes, re-validates the capture, recombines every signed value, rebuilds
the clock split from the raw `c` and `k` lines, and re-decodes every loaded
payload from the raw readback stream. Output: `*/independent-verification.json`.

One difference from stage 5: some readback frames arrived corrupted on the
board-to-host line and failed CRC (seed27: 1 in gate, 1 in up; zero: 1 in
gate, 4 in up). The host re-requested each one, and the repeat matched the
payload byte for byte. The replay records these frames
(`crc_rejected_readback_frames`) and never uses them; it still requires every
byte of every payload to be covered by CRC-valid frames that match. Stage 5
had none; the cause (line quality after the overheating, cable, USB) is not
known.
