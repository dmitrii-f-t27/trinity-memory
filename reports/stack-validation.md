# Trinity Memory 0.2 validation

Local validation on 2026-09-09: macOS arm64, Python 3.14.3, Icarus Verilog 13.0.
Exact source/report SHA-256 values and environment are in
[stack-provenance.json](stack-provenance.json). These are local results;
remote results are independently available in [GitHub Actions](https://github.com/dmitrii-f-t27/trinity-memory/actions/workflows/ci.yml).

| Check | Observed result | Reproduce |
|---|---|---|
| Python suite | 60 tests passed, zero skipped locally | `python3 -m unittest discover -s tests -v` |
| Original decoder RTL | 256 + 16 + 1024 = 1296 binary patterns, unknown inputs; four memory-stream lengths | `python3 scripts/test_rtl.py` |
| New stream dot protocol | 2 codecs x (57 protocol + 6 overflow) = 126 checked outputs; 2 x (7 + 3) = 20 expected errors | `python3 scripts/test_dot_rtl.py` |
| Cross-boundary conformance | 11 vectors x 4 dense codecs + 2 sparse transfers = 46 positive checks; 6 corrupt uploads rejected; 11 x 2 = 22 RTL dots | `make stack` |
| Edge demo | 6 fixtures x 2 codecs = 12 matching predictions; 12 x 3 rows = 36 matching RTL dots | `make demo` |
| Existing SDK | actual upstream TrinityChip + our injected backend + real loopback HTTP passed | `PYTHONPATH=build/upstream-sdk python3 scripts/test_sdk_bridge.py` |
| Installed wheel | software and RTL demo passed from `/tmp`, outside the source tree | install wheel, then `python3 -m trinity_memory edge-demo --rtl` |
| Report | HTML rendered and inspected in BrowserOS; table agrees with JSON | open `reports/stack.html` |

The stream protocol suite covers bubbles, input/output stalls, held outputs,
reset mid-frame and with an output pending, invalid dense/reserved lane codes,
masks/padding, overflow and recovery. A 12-bit accumulator configuration makes
overflow cases short; the public runner uses 32 bits and checks each group.
[Raw protocol counters](stream-compute.json) include the seeded schedule.

The SDK source pin is `fa8476397ac69438315268342958759e91da9e20` in
`gHashTag/trinity-sdk`. It is fetched separately and not vendored. No upstream
SDK/node changes or acceptance are claimed. The synthetic identity's anchor
check is not evidence about a physical device.

## Reports and interpretation

- [Conformance JSON](conformance.json): each case, expected dot, optional RTL witness.
- [Edge JSON](stack.json): input samples, labels, raw scores, storage bytes and timings.
- [HTML report](stack.html): self-contained presentation of the same edge data.
- [Protocol JSON](stream-compute.json): simulation counters and expected-error counts.

For the 36-trit example, dense5 payload is `ceil(36/5)=8` bytes; baseline2 is
`ceil(36/4)=9` bytes. Complete TensorPack files are 231 and 235 bytes respectively.
The difference also includes the different codec-name lengths in metadata;
it is not a pure payload comparison. No physical memory allocation is measured.

All classification fixtures are synthetic and templates are hand authored.
Matching these labels establishes the example's behavior, not generalization
accuracy or model quality. HTTP timing includes Python/JSON/scheduling. RTL
clocks include artificial stalls and final drain clocks. No FPGA, DDR, power,
routed timing, BRAM/LUT utilization, trained checkpoint or inference acceleration
result is provided.
