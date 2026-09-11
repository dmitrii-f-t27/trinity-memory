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

## Files

| File | Content |
| --- | --- |
| `capture-<date>-<commit>.json` | `trinity.fpga-capture.v1` report written by `tools/fpga-capture.py --port …` on the board |
| `capture-<date>-<commit>.txt` | the raw UART byte stream of that run |
| `build-<date>-<commit>/` | `yosys_stat.txt`, `nextpnr.log` (utilisation, achieved Fmax estimate, seed), `.fasm` hash and the `.bit` hash of the flashed bitstream |

## Builds

| Directory | Commit | Variant | Result |
| --- | --- | --- | --- |
| `build-2026-09-11-e3e8cfb/` | e3e8cfb | tick enable on the 200 MHz clock (the only variant at that commit) | routed by `heap` seed 1 after six `sa` seeds failed; 2352 LUTs, 1019 FFs, 1 RAMB18; nextpnr Fmax 67.5 MHz for the 200 MHz clock (paths between tick-enabled registers have eight periods); 20230 frames, bitstream 9 730 785 bytes, sha256 in `build.json`; pre-silicon capture PASS |

Status: harness built and verified in Icarus (24 vectors at e3e8cfb, 27 after
the storage join; 0 mismatches in both passes and both clock variants). Device
capture: none recorded yet in this directory.
