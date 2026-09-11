# Native t27 migration validation — 2026-09-10

These are v0.3 native results. The v0.2 report snapshots in the parent directory
are historical and have not been relabeled as native measurements.

Compiler: [`bff21b85b206a0dd367876343e56dcd8312d81ba`](https://github.com/gHashTag/t27/tree/bff21b85b206a0dd367876343e56dcd8312d81ba).
Independent oracle: Memory [`b0ace7dfc4844472ab10614f3b966a2566ad0703`](https://github.com/dmitrii-f-t27/trinity-memory/tree/b0ace7dfc4844472ab10614f3b966a2566ad0703).
The original Python/RTL algorithms are frozen under `tests/reference`; packaged
runtime artifacts contain generated native implementations only.

## Reproduction

Follow [the toolchain/build guide](../../docs/T27-MIGRATION.md), then:

```sh
make t27-test
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s tests/reference/legacy_tests -v
build/t27/trinity-memory-t27 benchmark --count 65536 --repeats 3 --output reports/t27/benchmark.json
python3 scripts/render_report.py reports/t27/benchmark.json --output reports/t27/index.html
build/t27/trinity-memory-t27 edge-demo --rtl --output reports/t27/edge.json --html reports/t27/edge.html
build/t27/trinity-memory-t27 conformance --rtl --output reports/t27/conformance.json
python3 -m pip wheel --no-deps . --wheel-dir dist
python3 tests/native/test_installed_wheel.py --wheel dist/<platform-wheel>.whl --rtl
```

## Specification

The conformance experiment above (fixture schema, codec order, random cases,
sparse transfers, six corruptions, report fields) is stated in
[`specs/memory/conformance.t27`](../../specs/memory/conformance.t27); the lab
report produced by `tools/conformance-lab.py` aggregates this experiment with
the codec, container, bridge and stream vectors under evidence labels.

## Observed local evidence

| Check | Result |
|---|---|
| Native code generation | Exact compiler pin; zero lexer loss; complete parsing; strict C/C++ build; repeated C output matches |
| Codec/TMEM | All six codecs, exhaustive small codebooks, longer randomized vectors, independent CRC/bit layout and rejected malformed inputs |
| TensorPack | Full JSON binding; 81 packs byte-matched; 195 resealed metadata mutations; 147 CLI documents |
| Float metadata | 199,901 finite values from 200,000 raw IEEE patterns match Python JSON presentation |
| Native Bridge | 708 differential RPC checks; real TCP client/server in both directions; bounds, malformed framing and timeout recovery |
| CLI | 176 process invocations covering file commands and actual HTTP; three experiment commands separately execute end-to-end |
| Edge | 6 fixtures × 2 codecs = 12 predictions; 3 classifier rows each = 36 actual Icarus results |
| Conformance | 46 positive checks, 6 corrupt-container rejections, 22 actual RTL computations |
| Browser target | 243 dense words × encode/decode + 13 reserved words + 4 invalid trits + 1 short buffer = 504 checks |
| Storage | All logical sizes 1–320 for the stock build; selected sizes through 20,480 for 1/65/820/4096-group specializations |
| Exact top-k | Native mixed integer/float ordering; 2,000 randomized selections and 6,000 pair comparisons, including integers beyond 2^53 |
| Public Python API | 68 tests pass through native adapters; 28 private historical tests separately pass against the frozen reference |
| Upstream SDK | Pinned TrinityChip calls SDKMemoryBackend over actual native HTTP; upstream SDK/node unchanged |
| Installed macOS wheel | arm64 binary architecture verified; 9 packaged hashes; isolated venv outside checkout exercises TMEM/TensorPack/HTTP/CLI and both RTL codec modes |

Native C harnesses and the integrated experiment/network path execute with
AddressSanitizer and UndefinedBehaviorSanitizer. Functional observations above
are backed by the persistent tests and `build/t27` logs/JSON artifacts. GitHub
CI rebuilds on Ubuntu and uploads its own evidence; consult the run for the
specific commit rather than assuming a local result proves a remote run.

The benchmark records one local run on the platform in its JSON metadata.
Encode/decode timings are native payload operations, median of three repeats
after warm-up; CRC, container construction and file I/O are excluded. They are
not FPGA throughput or a controlled cross-language speed comparison.

The HTML was opened in BrowserOS neo. Dataset controls and the embedded WASM
encoder updated correctly; changing `[-1,0,1,0,-1]` to `[1,0,1,0,1]` changed the
encoded byte from 48 to 212. These values can be independently checked as
`0+3+18+27+0` and `2+3+18+27+162` respectively.

No physical FPGA, DDR/HBM, P&R, resource utilization, power or model-quality
measurement was performed. Vivado control flow was smoke-tested with stubs;
Vivado synthesis itself was not run. Native allocation/integer/RTL bounds are
explicit in the migration and Python adapter documentation.
