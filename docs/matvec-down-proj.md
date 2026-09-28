# BitNet down_proj on the matvec datapath

The host tools support layer-0 `q_proj` and `down_proj` from the pinned BitNet
b1.58 2B4T packed checkpoint. They compute the complete tensor's integer
reference and verify its accumulator SHA-256 against the committed stage-1
report before selecting rows or opening the serial port.

`down_proj` has 2560 rows and 6912 columns. Both device encodings use 7040
columns: append 128 zero weights per row and 128 zero activations. This common
multiple of 64 and 80 preserves every integer accumulator while allowing an
identical matrix for dense5 and baseline2. It fits the existing activation
store. Throughput reports count the original 6912 logical weights per row and
record the padding overhead separately.

Build the board application with `DDR3_APP=matvec`, `DDR3_MATVEC_COLS=7040`,
`DDR3_MATVEC_ROWS=2560` for the full matrix (or the selected chunk size), and
`DDR3_MATVEC_FMT=1` for dense5 or `0` for baseline2. Keep clock/read/output
settings explicit as described in [matvec-pipeline.md](matvec-pipeline.md).
Flash the validated bitstream using its exact SHA-256 before running:

```sh
python3 tools/fpga-matvec-run.py --tensor down_proj --fmt d5 --rows 2560 \
  --design-hz 60000000 --port /dev/cu.usbserial-10 \
  --output build/down-proj-capture.json
```

`--fmt b2` selects baseline2 and requires the matching bitstream. The host
checks every Y value and row index, the run ID, word count, invalid words and
stray acknowledgements. It saves all decoded lines, including failed captures.
The measurement tool also accepts `--tensor down_proj` and records the actual
shape and row range instead of a hard-coded q_proj label.

Simulation checks 16 real rows in both formats through the generated arbiter,
with a competing master, random stalls and acknowledgement latency. An
independent integer dot verifies the padding, and every simulated result must
equal the unpadded golden reference. Board results require separate captures.
This is an integer matrix-vector product, not a complete transformer inference.
