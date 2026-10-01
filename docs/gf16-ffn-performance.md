# GF16 FFN performance — iteration 6

Issue [#113](https://github.com/dmitrii-f-t27/trinity-memory/issues/113).
This iteration measures and accelerates the qualified layer-0 `gf16-ffn-v1`
FFN. Attention, residuals, later layers, logits and generation remain outside
its scope. The arithmetic contract in [gf16-ffn.md](gf16-ffn.md) is unchanged.

## Measurement contract

Full mode emits every stage and ActQuant code; result mode checks the same
arithmetic faults but buffers final y until the active window ends. A capture
in result mode proves the returned y words, not unreported intermediate words.
Both modes must pass against the same saved integer oracle. The clocked
regressions also compare full traces and independently count the eight phases.

Phase counters include controller overhead and memory waits. Full-mode clocks
also include UART backpressure; subtracting report time does not isolate work
because normalization may overlap it. Result-mode total is the active FFN
window, including input/weight memory access and the initial start report, but
excluding host upload/readback and output draining. End-to-end time is larger.

Run the source-hashed paired simulation with existing verified full-shape
vectors (for example the prior BOS fixture):

```sh
python tools/measure_gf16_ffn.py --vectors build/gf16-ffn/bos-run6-vectors --output build/gf16-performance/baseline
```

The driver verifies the binary payload hashes and descriptor, reconstructs its
memory image, runs both modes against the same reference, and rejects source
changes during measurement. Its configurable report delay is a simulation
parameter, not a model of physical UART throughput.

## Projection schedule

The previous loop uses five clocks per trit: registered vector read, registered
Q39 magnitude, signed term, low/high partial sum, carry update. There are
53,084,160 trits across gate/up/down at shape2560→6912→2560; the original inner
loop therefore takes265,420,800 clocks. Phase counters measure the additional
word fetch, scalar rounding and row/controller overhead separately.

Implementation and qualification results are recorded after testing. No new
board speed or timing result is claimed by this design note.

## Serial benchmark control

This branch intentionally retains the original five-clock-per-trit loop. It
only registers the vector address selection and bounds internal RAM addresses
to 13 bits, to remove the same address-path timing obstacle as the accelerated
branch. The testbench checks the unmasked live indices and projection flag.
All arithmetic, report phases and serial cycle counts must match fd591b3.
