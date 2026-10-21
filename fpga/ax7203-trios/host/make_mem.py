#!/usr/bin/env python3
"""Turn the firmware binary into the four byte-lane memory files and ram_init.vh."""
import sys
from pathlib import Path

binfile, outdir = Path(sys.argv[1]), Path(sys.argv[2])
ram_bytes = 32 * 1024
data = binfile.read_bytes()
if len(data) > ram_bytes - 6 * 1024:
    sys.exit("image %d bytes leaves less than 6 KiB of stack in 32 KiB" % len(data))
data += bytes(-len(data) % 4)
outdir.mkdir(parents=True, exist_ok=True)
for lane in range(4):
    (outdir / ("ram_b%d.mem" % lane)).write_text("".join("%02x\n" % data[i] for i in range(lane, len(data), 4)))
(outdir / "ram_init.vh").write_text(
    "".join('    initial $readmemh("%s/ram_b%d.mem", m%d);\n' % (outdir.as_posix(), n, n) for n in range(4)))
print("image bytes", len(data), "words", len(data) // 4)
