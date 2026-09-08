# Local validation — 2026-09-08

Scope: current reference codec, TMEM container, CLI, generated RTL and report.
Platform: Darwin 25.6.0 arm64, Python 3.14.3, Icarus Verilog 13.0.

## Executed locally

`make check` exited with code 0:

```text
Ran 22 tests
OK
Generated RTL is current.
PASS decoders: dense5=256 sparse41=16 baseline5=1024 plus unknown inputs
PASS stream: TRIT_COUNT=1 WORDS=1 reset/latency/masking/busy/end/invalid
PASS stream: TRIT_COUNT=10 WORDS=2 reset/latency/masking/busy/end/invalid
PASS stream: TRIT_COUNT=12 WORDS=3 reset/latency/masking/busy/end/invalid
PASS stream: TRIT_COUNT=320 WORDS=64 reset/latency/masking/busy/end/invalid
PASS all stream configurations
PASS RTL/Python format agreement: 243 dense5 and 9 sparse41 valid logical states
```

The 1,296 binary decoder inputs are 256 + 16 + 1,024. They include reserved
invalid codes. Unknown-bit inputs are checked separately in simulation.

Python coverage includes all 243 dense5 states, all valid/invalid 2-bit byte
patterns, all 3⁴ and 3⁸ candidate sparse groups, random lengths and tails,
dense17/dense22 contiguous fields, CRC-covered bit corruption, malformed headers,
noncanonical padding, CLI round trips, and explicitly lossy projection behavior.
See [`tests`](../tests) and [`RTL testbenches`](../rtl/tb) for reproducible checks.

`python3 -m trinity_memory benchmark --count 65536 --repeats 3` exited 0 and
verified 15 combinations across three synthetic datasets. Source artifact:
[`benchmark.json`](benchmark.json). Each row checks raw payload and TMEM restoration.

`python3 scripts/render_report.py` produced [`index.html`](index.html).
BrowserOS neo rendered it successfully. Dataset selection showed the expected
8,192-byte sparse payloads. Setting all five interactive trits to zero displayed
121 / 0x79 / 01111001 and five `00` output lanes. The complete desktop layout
was visually inspected.

After the final report wording correction, setting every interactive trit to +1
displayed the expected binary byte `11110010` (242). Browser console reported
no errors or warnings.

Editable installation into a local virtual environment succeeded using
`python3 -m pip install --no-deps -e .`. The installed `trinity-memory` entrypoint
packed, validated and unpacked the 13-trit example and exported three dense RTL
words. Its TMEM file is 24 header bytes + 3 payload bytes = 27 bytes, illustrating
why small-file overhead must be counted separately.

## Evidence not yet available

GitHub Actions have been prepared but have not run remotely. The Vivado Tcl
script has not been executed. No physical board, post-route timing, physical
BRAM/LUT utilization, power, DDR throughput, model quality, or tokens/s result
was measured in this iteration.
