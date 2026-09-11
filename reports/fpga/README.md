# FPGA reports (AX7203 measurement track)

Decision record and captured device output for issue #10. Protocol and flow:
[`docs/hardware.md`](../../docs/hardware.md), section "FPGA measurement track".

## Decision record (2026-09-11)

- **Board / part:** ALINX AX7203, `xc7a200tfbg484-2`. Owned hardware, on the
  bench of the build machine; the same board `gHashTag/trinity-fpga` builds with
  (its verified pin map is reused in `fpga/ax7203/tms_trace_player.xdc`).
- **Clock:** 200 MHz LVDS oscillator divided to 50 MHz in fabric; no PLL.
- **Configuration:** on-board FT232H JTAG (`openFPGALoader -c digilent_hs2`),
  SRAM only. **Host link:** on-board CP2102N UART at 115200 8N1.
- **Toolchain:** open flow (yosys, nextpnr-xilinx, prjxray) from the
  `regymm/openxc7` image; no Vivado licence involved. The chip database for the
  part is generated once and cached by the `fpga-ax7203` workflow.
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

Status: harness built and verified in Icarus (27 vectors, 288 cycles, 0
mismatches in both passes). Device capture: none recorded yet in this directory.
