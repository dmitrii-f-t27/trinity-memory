# FPGA reports (AX7203 measurement track)

Decision record and captured device output for issue #10. Protocol and flow:
[`docs/hardware.md`](../../docs/hardware.md), section "FPGA measurement track".

## Decision record (2026-09-11)

- **Board / part:** ALINX AX7203, `xc7a200tfbg484-2`. Owned hardware, on the
  bench of the build machine; the same board `gHashTag/trinity-fpga` builds with
  (its verified pin map is reused in `fpga/ax7203/tms_trace_player.xdc`).
- **Clock:** 200 MHz LVDS oscillator through IBUFDS/BUFG; no PLL. Two variants of the same design (`CLOCK_MODE`): a divide-by-eight register on a second BUFG (25 MHz, single-cycle timing) or the 200 MHz clock with a tick enable one clock in eight. 25 M ticks/s either way.
- **Configuration:** on-board FT232H JTAG (`openFPGALoader -c digilent_hs2`),
  SRAM only. **Host link:** on-board CP2102N UART at 115200 8N1.
- **Toolchain:** open flow (yosys `-nowidelut -nosrl`, nextpnr-xilinx with the
  `sa` and `heap` placers over seeds 1..6, prjxray) from the `regymm/openxc7`
  image; no Vivado licence involved. The chip database for the part is
  generated once and cached by the `fpga-ax7203` workflow.
- **Workload:** every `dot_trace` and `storage_trace` vector of
  `conformance/memory_stream_compute.json` (27 vectors, 18 dot and 9 storage
  including the backpressure holds; 288 cycles),
  replayed by `fpga/ax7203/tms_trace_player.v`; counters as listed in
  `build/fpga/tms_trace_manifest.json`.
- **Rented hardware:** none.
- **Player:** every state machine is executable t27 (`t27/rtl/fpga_*.t27`, tables
  generated as `build/fpga/fpga_trace_rom.t27`); the Verilog under `fpga/ax7203/`
  only wires the generated cores. Phases of one run: stepped and free-run replay
  of all 34 trace vectors (dot, storage, join), the throughput workload (64
  words, 16 frames x 64 beats, results recomputed by the host) and the Edge Demo
  (three template rows in lockstep, t27 argmax, six fixtures).
- **Not measurable on this bench:** DDR (no trusted DDR3 PHY in the open flow)
  and power (no instrument); see `docs/hardware.md`.

## Files

| File | Content |
| --- | --- |
| `capture-<date>-<commit>.json` | `trinity.fpga-capture.v1` report written by `tools/fpga-capture.py --port …` on the board |
| `capture-<date>-<commit>.txt` | the raw UART byte stream of that run |
| `build-<date>-<commit>/` | `yosys_stat.txt`, `nextpnr.log` (utilisation, achieved Fmax estimate, seed), `.fasm` hash and the `.bit` hash of the flashed bitstream |

## Builds

| Directory | Commit | Variant | Result |
| --- | --- | --- | --- |
| `build-2026-09-22-11e8e88-player/` | 11e8e88 (the t27 player of master with the idle-only trigger of cb19fea) | divided 25 MHz clock; yosys 0.69 and nextpnr-xilinx 45a986b native (heap seed 1, router1), prjxray from the image | 7815 LUTs, 3612 FFs, nextpnr Fmax 63.4 MHz for the 25 MHz clock; built in three minutes on the Mac |
| `build-2026-09-22-e8fecf2-bram-b2/`, `-d5/`, `-d5d2/` | e8fecf2 (branch `feat/fpga-bram-trit-packing`; the block-RAM trit packing bench, one layout per bitstream, `BRAM_ONLY=0/1/2`) | divided 25 MHz clock; yosys 0.69 with the 1K x 36 block RAM library (`fpga/ax7203/brams_x36.txt`), nextpnr-xilinx 45a986b built natively on the Mac, `heap` seed 1, `router2`; prjxray from `regymm/openxc7` | RAMB36E1 55 / 50 / 45; LUTs 3336 / 3563 / 3460; FFs 1039 / 1036 / 1111; nextpnr Fmax 37.7 / 31.9 / 31.4 MHz for the 25 MHz clock; `ooc_engine_codecs.stat` is the engine with its codecs synthesized alone (55 / 50 / 45 blocks, 2502 / 2673 / 2554 LUTs); all three layouts in one bitstream did not route |
| `build-2026-09-11-6d0cfa6/` (+ `-tick/`) | 6d0cfa6 (PR #25 head; the t27 player with join traces, workload and Edge) | `CLOCK_MODE=1` divided 25 MHz clock (device variant); `-tick/` the 200 MHz tick-enable variant, Fmax 60 MHz, not functional on the device | nextpnr: 7870 LUTs, 3612 FFs, Fmax 52.1 MHz for the 25 MHz clock (PASS); `heap` placer; bitstream sha256 in `build.json` |
| `build-2026-09-11-fb0533e/` (+ `-tick/`) | fb0533e (PR #21 head; the merged 27-vector set) | both variants: `CLOCK_MODE=1` divided 25 MHz clock and `CLOCK_MODE=0` tick enable | divided: nextpnr Fmax 71.9 MHz for the 25 MHz clock (PASS), 1024.6 MHz for the 200 MHz input clock; tick: Fmax 67.4 MHz on the 200 MHz domain (eight periods between enabled registers); bitstream sha256 in `build.json` / `bitstream.sha256` |
| `build-2026-09-11-e3e8cfb/` | e3e8cfb | tick enable on the 200 MHz clock (the only variant at that commit) | routed by `heap` seed 1 after six `sa` seeds failed; 2352 LUTs, 1019 FFs, 1 RAMB18; nextpnr Fmax 67.5 MHz for the 200 MHz clock (paths between tick-enabled registers have eight periods); 20230 frames, bitstream 9 730 785 bytes, sha256 in `build.json`; pre-silicon capture PASS |

## Captures

| File | Board / bitstream | Result |
| --- | --- | --- |
| `bram-capture-2026-09-22-e8fecf2-{b2,d5,d5d2}.json` (+ `.txt`, `-run2.json`, `-run3.json`) | ALINX AX7203 S/N 000469, bitstreams of `build-2026-09-22-e8fecf2-bram-*`, UART `/dev/cu.usbserial-110` | PASS for every layout: the same 1 013 760 trits written and read back, 0 bad words, 0 invalid groups, +1 253 229, -1 253 385, dot -674 (the host model's values); 56 320 / 50 688 / 46 080 words, read in 56 321 / 50 689 / 46 081 ticks (18 / 20 / 22 trits per read); three runs per layout identical except for the run number in the header |
| `capture-2026-09-22-11e8e88-player.json` (+ `.txt`), `trigger-ab-2026-09-22.json` | ALINX AX7203 S/N 000469, `build-2026-09-22-11e8e88-player` | PASS, 34 vectors, one run per capture with the byte "r" as trigger as well; A/B: after one trigger byte with three or five falling edges the old player (f07a667) sends two runs (30400 bytes in 5 s), the fixed one one run (15200 bytes) |
| `capture-2026-09-22-f07a667-div.json` (+ `.txt`, `-run2.json`, `-run3.json`) | ALINX AX7203 S/N 000469, the t27 player of master f07a667 (CI run 34625573482, divided clock), UART `/dev/cu.usbserial-110` | PASS, line for line the 2026-09-11 result (34 vectors, workload 1024 beats in 1089 ticks, Edge 6/6 in 7 ticks); 15200 bytes, one run per capture since `tools/fpga_uart.py` triggers with 0xff (the 2026-09-11 captures of 23040 bytes carry the head of a second run that the "r" trigger queued) |
| `xadc-2026-09-22-bram-d5d2-loaded.json` | XADC over JTAG with the d5d2 bench loaded | die 38.2 C, VCCINT 0.995 V, VCCAUX 1.791 V; the board's flash-resident design runs the die at 83-87 C |
| `capture-2026-09-11-6d0cfa6-div.json` (+ `.txt`, `-run2.json`, `-run3.json`) | ALINX AX7203, bitstream of `build-2026-09-11-6d0cfa6` (t27 player, divided clock) | PASS: 34 vectors incl. the 7 join traces, 406 cycles, 0 mismatches in both passes; workload 1024 beats in 1089 ticks (0.940 beats/tick), all 16 results match; Edge six fixtures correct, 7 ticks each; two runs byte-identical |
| `capture-2026-09-11-6d0cfa6-tick-FAIL.txt` / `.json` | bitstream of `build-2026-09-11-6d0cfa6-tick` (tick-enable variant, nextpnr Fmax 60 MHz on the 200 MHz domain) | FAIL (negative result kept on purpose): the device configures but emits six fragment lines in two captures; the tick enable fans out to ~3600 registers and does not hold at 200 MHz for this design size |
| `capture-2026-09-11-fb0533e-div.json` / `-tick.json` (+ `.txt` raw streams) | ALINX AX7203, idcode `0x3636093`, bitstreams of `build-2026-09-11-fb0533e` (divided clock) and `-tick` | PASS in both variants: 27 vectors, 288 cycles, 0 host mismatches, 0 device mismatches (stepped), 0 (free-run); the merged vector set, so the conformance lab marks these captures current |
| `capture-2026-09-11-e3e8cfb.json` (+ `.txt` raw stream, `-run2.json`, `-run3.json`) | ALINX AX7203, idcode `0x3636093`, bitstream `build-2026-09-11-e3e8cfb` (sha256 `198df230…`, header build id `0e6a6ed5` = the PR merge commit the CI built), UART `/dev/cu.usbserial-10` 115200 | PASS: 24 vectors, 243 cycles, 0 host mismatches, 0 device mismatches (stepped), 0 (free-run); three runs byte-identical (17518 bytes) |

Status: all 34 trace vectors (dot, storage and join), the throughput workload
and the six Edge fixtures run on the device with the t27 player and match the
reference; the conformance lab carries `device_evidence: fpga` for the
6d0cfa6 captures. The block-RAM packing bench stores one 1 013 760-trit tensor
in 55, 50 and 45 RAMB36E1 (2.000, 1.800 and 1.636 bits per trit) and reads it
back without error on the device. Not measured: DDR and power (see
`docs/hardware.md`).
