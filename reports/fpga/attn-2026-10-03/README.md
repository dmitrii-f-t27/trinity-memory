# AX7203 layer-0 attention qualification — 2026-10-03

**PASS:** hardware source `2e2e336cd8f5b8d3404017f28a5ad9b473c3831e`; four physical
full/result pairs and eight full-dimension simulations. This is Q16.16
attention plus the first residual, not a complete model. See the
[contract, implementation, limits and measured table](../../../docs/gf16-attn.md).

## Offline verification

```sh
python3 reports/fpga/attn-2026-10-03/verify-evidence.py
```

The verifier needs Python and this repository, with no external packages,
compiler, model download or hardware. It checks every archive member's size
and SHA-256, replays the eight simulation captures against retained oracle
stages, and invokes `tools/verify_gf16_attn_board.py` on each extracted
physical pair. The latter reconstructs full expected input regions from
raw CRC frames, checks exact output stages, descriptors, paired input reuse,
clock splits, thermal bounds and UART restoration. Four transformed-model
control results and the 17-test log are retained as build-time evidence;
the offline command does not rerun synthesis or those test executables.

- [Manifest](evidence-manifest.json): 201 members, with 18 unique expected payloads.
- [Raw evidence archive](board-evidence.tar.gz): 21,491,652 bytes, SHA-256 `c35f822bec24ef14eb8a39a9bef528898262991cdaec4cc74317e4a277a5297d`.
- [Physical results](physical-results.json): original independent verifier receipts.
- [Build report](build-2e2e336c/build.json), [routing log](build-2e2e336c/nextpnr.log), [Yosys statistics](build-2e2e336c/yosys_stat.txt).

The archive includes the actual 9,730,799-byte bitstream, boot/preflight
receipts, expected inputs, raw DDR readbacks, full/result UART captures,
thermal samples, source snapshots and simulation captures. Binary expected
payloads are stored once by hash; the manifest restores per-case paths.
Each physical raw readback is retained independently.

Bitstream SHA-256: `10478dc03548194a6d6ec829197eeff507860ff748dde6d12cfc71005d4e1cc8`.
Board DNA: `0x00389c0c2d85e85c`; IDCODE: `0x03636093`.
SRAM configuration only. UART was restored to 115200 after each pair.

## Physical results

| Case | Full/result IDs | Input bytes | Full/result values | Full seconds | Result seconds | Retained peak C |
|---|---|---:|---:|---:|---:|---:|
| BOS | 101/102 | 4,221,008 | 19,860/2,560 | 8.632920050 | 2.315108150 | 48.9784 |
| ZERO | 111/112 | 4,221,008 | 19,860/2,560 | 8.632920050 | 2.315103717 | 49.1015 |
| S2 | 121/122 | 4,264,016 | 39,740/5,120 | 17.273991700 | 4.636004917 | 49.1938 |
| S8 | 131/132 | 4,522,064 | 159,440/20,480 | 69.302680583 | 18.694377817 | 49.5706 |

Seconds = total active controller clocks / 60,000,000, including UART-report
and DDR waits. Input bytes exclude the 16-byte doorbell and separate
32,768-byte qualification payload. All stages matched exactly; all expected
payload bytes were recovered with valid CRC and no rejected frames. The
highest retained temperature during these pairs was 49.5706 C. These values
are measurements of these runs, not a claim about current temperature.

## Build limits

Heap seed 5 reached 66.66 MHz final routed fabric Fmax for the 60 MHz target,
with 26 RAMB36E1 cells. The existing nextpnr legacy CARRY4 split fallback was
used. PHY/I/O paths remain outside its timing model, and the PLL filter/lock
table lacks a Vivado comparison at multiplier 6; these limitations are
recorded in `build.json`. No timing-failing artifact was loaded.
