# Rejected routed candidate: b60e06fc, heap seed 6

Fabric routing passed at 63.85 MHz against 60 MHz, but physical qualification
failed on two separate SRAM configurations. Both emitted the correct build ID
and no GF16 header. The first subsequent UART status reports `calib=3839`
(`0x00000eff`), with no completed calibration and no compute/input upload.
This bitstream is **not qualified** and contributes no performance measurement.

The build report retains the then-current `accelerated/build` path; it was
moved here after the failure. Boot receipts preserve the original command and
bitstream hash. All loads were SRAM only. A different placement of the exact
same synthesized netlist is being evaluated.
