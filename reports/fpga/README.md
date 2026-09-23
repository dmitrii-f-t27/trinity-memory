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
| `build-2026-09-22-941c16c-bram-b2/`, `-d5/`, `-d5d2/` | 941c16c (branch `feat/fpga-bram-trit-packing`; the block-RAM trit packing bench, one layout per bitstream, `make bram-bit BRAM_ONLY=0/1/2`) | divided 25 MHz clock; yosys 0.69 with the 1K x 36 block RAM library (`fpga/ax7203/brams_x36.txt`); nextpnr-xilinx 45a986b built natively on the Mac, `heap` seed 1, `router2`; prjxray from `regymm/openxc7` | RAMB36E1 55 / 50 / 45; LUTs 3319 / 3537 / 3448; FFs 1038 / 1035 / 1110; nextpnr Fmax 38.8 / 35.6 / 29.8 MHz for the 25 MHz clock; `ooc_engine_codecs.stat` is the engine with its codecs synthesized alone (55 / 50 / 45 blocks, 2524 / 2715 / 2530 LUTs), `codec_encoder.stat` and `codec_decoder.stat` the codec alone (0 / 133 / 146 and 96 / 125 / 136 LUTs), all from `make bram-ooc` |
| `codec-depth-2026-09-23-880f9b9/` | 880f9b9 (the codec `t27/rtl/bram_trit_codec.t27`, unchanged since 941c16c) | yosys 0.69, `synth_xilinx -flatten -abc9 -nocarry -nodsp -nowidelut -nosrl`, the codec alone (`fpga/ax7203/tms_bram_codec_ooc.v`), then `ltp -noff` | `codec_<f>_<op>.ltp` and `.stat` (f 0 b2, 1 d5, 2 d5d2; op 0 encoder, 1 decoder): decoder 96 / 125 / 136 LUTs at 8 / 5 / 4 LUT levels, encoder 0 / 133 / 146 LUTs at 0 / 6 / 6 levels (ltp length minus the IBUF and the OBUF) |
| `build-2026-09-23-ab52409-bram-rom-b2/`, `-d5/`, `-d5d2/` | ab52409 (branch `feat/fpga-bram-real-weights`; the read-only store `t27/rtl/bram_trit_rom.t27` holding BitNet b1.58 2B4T layer-0 q_proj rows 0-395 as block-RAM initial contents, `make bram-bit BRAM_ONLY=N BRAM_ROM=build/fpga/rom/trits.bin`) | divided 25 MHz clock; yosys 0.69 (1K x 36 library), nextpnr-xilinx 45a986b native, `heap` seed 1, `router2`; prjxray from `regymm/openxc7` | RAMB36E1 55 / 47 / 45 (d5: yosys packs the 32-bit words across the parity columns, 15 blocks per 16K-word bank); LUTs 2885 / 2850 / 3415; FFs 878 / 875 / 878; nextpnr Fmax 37.7 / 32.4 / 32.4 MHz for the 25 MHz clock; `bram_bench_manifest.json` and `rom_trits.json` (the tensor's sources and sha256) alongside |
| `build-2026-09-23-ae754b0-bram-rom-d5p/`, `build-2026-09-23-ae754b0-bram-rom-pipe-d5p-50mhz/` | ae754b0 (`BRAM_ONLY=3`: d5p, 45 trits per pair of 36-bit words, the parity nibbles forming one more dense5 byte) | 25 MHz; and `PIPE 1`, `CLOCK_MODE=2`, `BAUD_DIV=434`, `--freq 50` | 44 RAMB36E1 (16 + 16 + 12, the floor for the tensor); 3529 / 3036 LUTs; 881 / 1356 FFs; nextpnr Fmax 28.3 MHz (25 MHz clock) and 60.1 MHz (PASS at 50 MHz) |
| `build-2026-09-23-844d6ea-bram-rom-pipe-d5d2-50mhz/` | 844d6ea (the store with `PIPE 1`: registers after the decoder, the half sums and the per-word sums) | `CLOCK_MODE=2` 50 MHz (bit 1 of the tick counter on the second BUFG), `BAUD_DIV=434`, nextpnr `--freq 50` | 45 RAMB36E1, 2750 LUTs, 1316 FFs; nextpnr Fmax 62.4 MHz (PASS at 50 MHz) |
| `build-2026-09-23-844d6ea-bram-rom-pipe-d5d2-100mhz/` | 844d6ea, `CLOCK_MODE=3` 100 MHz, `BAUD_DIV=868`, `--freq 100` | as above | 45 RAMB36E1; nextpnr Fmax 71.2 MHz (FAIL at 100 MHz); not flashed |
| `build-2026-09-23-37e8316-bram-d5d2-carry/` | 37e8316, the write-read d5d2 bench with carry chains (`YOSYS_SYNTH` without `-nocarry`) | as the 941c16c bench builds | 45 RAMB36E1, 493 CARRY4, 6947 LUTs; nextpnr Fmax 9.7 MHz: carry chains are slower than ABC's LUT-only adders in this flow; not flashed |
| `build-2026-09-22-375cf00-bram-all/` | 375cf00 (all three layouts in one design) | yosys 0.69 native | 150 RAMB36E1, 8959 LUTs, 2188 FFs (`yosys_stat.txt`); did not route: `routing-attempts.txt` has the last progress lines of each nextpnr attempt (router1 12.6k of 64.4k arcs left after 35 minutes, router2 27.6k overused wires in its first iteration, `sa` stopped by the A5FF bug) |
| `build-2026-09-22-941c16c-player/` | 941c16c (the t27 player with the idle-only trigger) | divided 25 MHz clock; yosys 0.69 and nextpnr-xilinx 45a986b native (`heap` seed 1, router1), prjxray from the image | 7842 LUTs, 3611 FFs, nextpnr Fmax 57.8 MHz for the 25 MHz clock |
| `build-2026-09-11-6d0cfa6/` (+ `-tick/`) | 6d0cfa6 (PR #25 head; the t27 player with join traces, workload and Edge) | `CLOCK_MODE=1` divided 25 MHz clock (device variant); `-tick/` the 200 MHz tick-enable variant, Fmax 60 MHz, not functional on the device | nextpnr: 7870 LUTs, 3612 FFs, Fmax 52.1 MHz for the 25 MHz clock (PASS); `heap` placer; bitstream sha256 in `build.json` |
| `build-2026-09-11-fb0533e/` (+ `-tick/`) | fb0533e (PR #21 head; the merged 27-vector set) | both variants: `CLOCK_MODE=1` divided 25 MHz clock and `CLOCK_MODE=0` tick enable | divided: nextpnr Fmax 71.9 MHz for the 25 MHz clock (PASS), 1024.6 MHz for the 200 MHz input clock; tick: Fmax 67.4 MHz on the 200 MHz domain (eight periods between enabled registers); bitstream sha256 in `build.json` / `bitstream.sha256` |
| `build-2026-09-11-e3e8cfb/` | e3e8cfb | tick enable on the 200 MHz clock (the only variant at that commit) | routed by `heap` seed 1 after six `sa` seeds failed; 2352 LUTs, 1019 FFs, 1 RAMB18; nextpnr Fmax 67.5 MHz for the 200 MHz clock (paths between tick-enabled registers have eight periods); 20230 frames, bitstream 9 730 785 bytes, sha256 in `build.json`; pre-silicon capture PASS |

## Captures

| File | Board / bitstream | Result |
| --- | --- | --- |
| `bram-rom-capture-2026-09-23-ab52409-{b2,d5,d5d2}-{config,run2,run3,run4}.json` (+ `.txt`), `xadc-2026-09-23-ab52409-bram-rom-d5d2.json` | ALINX AX7203 S/N 000469 (DNA 0x00389c0c2d85e85c read with the store loaded), bitstreams of `build-2026-09-23-ab52409-bram-rom-*`, UART `/dev/cu.usbserial-110` | PASS for every layout and run: the tensor comes from configuration (no write phase), 56 320 / 50 688 / 46 080 words in 56 321 / 50 689 / 46 081 ticks, 0 bad words (the store's own check of the decoded lanes against the checkpoint), 0 invalid groups, +1 349 720, -1 348 525, dot 4040, word checksums equal to the host's; the four runs of a bitstream are identical apart from the run number; die 38.2 C |
| `bram-rom-capture-2026-09-23-ae754b0-d5p-{config,run2,run3,run4}.json`, `...-d5p-pipe-50mhz-*` (+ `.txt`), `xadc-2026-09-23-ae754b0-bram-rom-pipe-d5p-50mhz.json` | the same board, the two d5p bitstreams at 25 and 50 MHz | PASS in all eight runs: 45 056 words in 45 057 / 45 060 ticks, 0 bad words, 0 invalid groups, the same counts and dot product; die 38.0 C |
| `bram-rom-capture-2026-09-23-844d6ea-d5d2-pipe-50mhz-{config,run2,run3,run4}.json` (+ `.txt`), `xadc-2026-09-23-844d6ea-bram-rom-pipe-d5d2-50mhz.json` | the same board, `build-2026-09-23-844d6ea-bram-rom-pipe-d5d2-50mhz` at 50 MHz | PASS in all four runs: 46 084 read ticks (words + 4), the same counts, dot product and word checksum as at 25 MHz; die 38.2 C |
| `bram-capture-2026-09-22-941c16c-{b2,d5,d5d2}.json` (+ `.txt`, `-run2.json`, `-run3.json`, `-config.json`, `-config.txt`) | ALINX AX7203 S/N 000469, bitstreams of `build-2026-09-22-941c16c-bram-*`, UART `/dev/cu.usbserial-110` | PASS for every layout and every run: the same 1 013 760 trits written and read back, 0 bad words, 0 invalid groups, +1 253 229, -1 253 385, dot -674 (the host model's values); 56 320 / 50 688 / 46 080 words read in 56 321 / 50 689 / 46 081 ticks (18 / 20 / 22 trits per read). `-config` is run 1, captured while flashing: it writes into block RAM that configuration has just cleared. Runs 1-4 are identical except for the run number |
| `capture-2026-09-22-941c16c-player.json` (+ `.txt`, `-run2.json`, `-run3.json`), `trigger-ab-2026-09-22.json` | ALINX AX7203 S/N 000469, `build-2026-09-22-941c16c-player` | PASS, 34 vectors, one run per capture with the byte "r" (0x72) as trigger as well as 0xff; A/B: after one trigger byte with three or five falling edges the old player (f07a667) sends two runs (30 400 bytes in 5 s), the 941c16c player one (15 200 bytes) |
| `xadc-2026-09-22-941c16c-*.json` | XADC over JTAG with each bitstream loaded | die 37.5-37.7 C, VCCINT 0.995 V, VCCAUX 1.79 V |
| `xadc-2026-09-22-flash-design.json` | XADC with the board's own flash-resident design running (transcribed from the session log) | die 83.5 C at the first reading; the XADC peak since power-on, read after the first SRAM configuration, was 87.4 C. Fit a fan before long runs of that design |
| `capture-2026-09-22-f07a667-div.json` (+ `.txt`, `-run2.json`, `-run3.json`) | ALINX AX7203 S/N 000469, the t27 player of master f07a667 (CI run 34625573482, divided clock), UART `/dev/cu.usbserial-110` | PASS, line for line the 2026-09-11 result (34 vectors, workload 1024 beats in 1089 ticks, Edge 6/6 in 7 ticks); 15200 bytes, one run per capture since `tools/fpga_uart.py` triggers with 0xff (the 2026-09-11 captures of 23040 bytes carry the head of a second run that the "r" trigger queued) |
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
