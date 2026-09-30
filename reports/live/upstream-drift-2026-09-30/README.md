# New reader pins: issue #98 / PR #99

Date: 2026-09-30. Local platform: macOS arm64.

## Combined validation after PR #101

Final adapter configuration: `fuzz-seed27-four-readers-final-macos.json`
sets `GGML_NO_BACKTRACE=1` for both reader adapters. Source ggml.c returns
from backtrace diagnostics under this flag, while ggml_abort still calls
abort(). A complete repeat produces exactly the same corpus and every
accept/refuse/agreement/no-verdict/crash counter as the earlier four-reader
run. Assertions remain visible in the JSON; debugger output is not part of
the worker protocol. Node/WASM checks also pass after adding browser labels
for the new -88/-89 refusals.

`public-replay-linux-a83.json` is independently inspected Linux evidence:
294 repositories, 509 models, 578 file entries, 13 split models, 447 replayed
headers x 4 readers = 1,788 agreements, no disagreements/no-verdicts/crashes/
timeouts. Artifact 11116754261 / run 36756798361 finished scan and replay;
the overall workflow was cancelled during fuzz to update diagnostics.
It is not recorded as a full-workflow success. Numeric t27/runtime/reader
sources are unchanged by the later browser/diagnostic-only adjustments.

Master advanced to 4b4b0eb during validation, adding mortar.cpp
`236418ec92aaf51f8635b965f5557457c8126ca7`. This work is integrated with the
new pins and loader rules. The fourth table explicitly declares
`tensor_extra=false`; all generated tables were regenerated from JSON.

- `cached-replay-four-readers-macos.json`: 55 files, 220/220 agreements,
  zero disagreements/no-verdicts/crashes/timeouts.
- `fuzz-seed27-four-readers-macos.json`: 160,000 headers, 640,000 attempted
  observations, 610,351 agreements, 29,649 without a t27 verdict, zero
  disagreements/timeouts. Bitnet.cpp and mortar.cpp each assert on 7,054
  malformed inputs; those exits are separately counted and treated as
  refusals where comparable. 96 worker-to-single-reader crosschecks pass.
  Same seed/corpus hash as the three-reader snapshot below.
- Fresh native and four-reader builds, native Live ASan/UBSan, Live WASM,
  81 targeted unit tests, all 12 specs/seals/conformance/reference/RTL
  checks, and page generation pass on the combined state.
- The fuzz driver uses `live.SPEC_NAMES` for every pinned runtime, rather
  than retaining a three-reader list. CI disables core dumps for malformed
  inputs while still recording every assertion signal.

The three-reader subsection below is a preserved earlier snapshot, not the
final combined-state acceptance. Remote CI and full public scan status are
tracked in PR #99, separately from these local results.

## Reader provenance

| Runtime | Pinned source commit |
|---|---|
| llama.cpp | 4364bf7232e65c34eca8d9500c5464389662de6b |
| PrismML | 87268f775d74cf8f7ffc6c22a95684aa55995533 |
| bitnet.cpp | 390c307752ab78fd8189f359d6954c9ba1be74af |

The existing PR branch was merged with master 70647c3, including PR #97's
macOS bitnet.cpp linking fix. All three readers were freshly built.

## Fresh evidence

- `cached-replay-macos.json`: transferred headers rechecked on current t27,
  then replayed independently. 21 models, 55 files, 165 reader agreements,
  zero disagreements, no-verdicts, crashes or timeouts. No model was kept
  without rechecking its cached headers. This is a cached corpus, not a
  claim of a new complete public Hub scan.
- `fuzz-seed27-macos.json`: 160,000 seeded synthetic headers, 480,000
  attempted reader observations. 457,717 agreements, 22,283 without a t27
  verdict, zero disagreements or timeouts. The bitnet.cpp reader's 7,054
  assertion exits are recorded separately and counted as refusals when a
  comparison is available; they are not evidence of crash-free parsing.
  All three runtimes have both accepted and refused cases. The persistent
  worker was crosschecked against the original single-file adapter on 72
  observations. Corpus SHA-256:
  `c2be9d6f6fb20807bc91c4187bec5288a7340d41e2e0dbd37c1a4a91256eb667`.
- Native Live harness: ASan/UBSan PASS, including Hadamard v2/tied output,
  tensor-extra policy, runtime-specific refusals and truncated record inputs.
- 79 targeted unit tests PASS (Live, page, upstream harness, Ternary Check
  report generation and decoder orchestration).
- Specification gate PASS: 12 specs, seals/conformance, C/RTL generation,
  differential reference harnesses and Icarus traces.
- `make ternary-check`: all 550 fixture ranges valid; 36 cells retain
  23 match / 4 mismatch / 9 not-representable. Regenerated JSON/HTML change
  only llama.cpp provenance (commit and ARM source SHA). A second offline
  generation reproduces the saved reports byte for byte.

The reader replay and fuzz evidence checks GGUF reader acceptance. It does
not validate the complete model loader, tensor values, model inference or
FPGA operation. New loader rules are covered by the source-linked native
Live vectors; the reader agreement counts do not independently prove them.
The historical 312/312 and 480,000 figures for old pins are not reused here.

## Reproduce

Use the compiler revision in `native/compiler.lock`, then:

```sh
sh tools/build-t27.sh
sh tools/live-replay.sh
ulimit -c 0
python3 tools/live-fuzz.py --seed 27 --cases 160000 --out build/live/fuzz.json
```

The fuzz JSON preserves pin, source, executable, shared library and corpus
hashes. A discrepancy saves the failing header and its zero-based case
index. `python3 -m trinity_memory.live --recheck PREVIOUS_SCAN --out SCAN`
and `python3 -m trinity_memory.live --out SCAN --replay build/replay` are
separate commands: recheck returns before the replay option is processed.
The weekly/manual Live workflow now preserves its own fuzz evidence.

Full remote validation is recorded in the PR checks and workflow artifacts:
https://github.com/dmitrii-f-t27/trinity-memory/pull/99.
