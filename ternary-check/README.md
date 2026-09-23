# t27 Ternary Check: GitHub Action

This Action checks whether a decoder or encoder reproduces the public ternary
weight-packing formats bit for bit. It covers these formats:

- llama.cpp TQ1_0, TQ2_0, Q2_0 and Q1_0;
- PrismML PQ2_0 and PTQ1_0;
- bitnet.cpp I2_S;
- transformers BitNet packed `uint8`;
- MLX 2-bit;
- ONNX Runtime `MatMulNBits` with 2 bits.

The Action runs the conformance vectors of this repository
(`conformance/formats_*.json`) against a decoder. The decoder must implement
the [CLI contract](CONTRACT.md): it takes file arguments, writes signed bytes
and scale words, and refuses bad input with a nonzero exit and a class token.

For each vector, the Action reports whether the values, the scale words and the
flags match. For each input that must be refused, it reports whether the
decoder refused it with the right class, or accepted it silently. The results
go to the job summary and to a JSON report.

## Usage

The Action is available once v0.4.0 is released:

```yaml
jobs:
  ternary-check:
    runs-on: ubuntu-latest          # Linux x86_64 or macOS arm64 for runtime: release
    steps:
      - uses: actions/checkout@v4
      - run: make my-decoder        # build your decoder
      - uses: dmitrii-f-t27/trinity-memory/ternary-check@v0.4.0
        with:
          decoder: ./build/my-decoder
          formats: TQ1_0,TQ2_0      # optional: only the formats you implement
          report: ternary-check.json
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: ternary-check
          path: ternary-check.json
```

By default the Action uses the vectors of its own ref. At `@v0.4.0` those are
the `conformance/formats_*.json` files of the v0.4.0 tag.

The Action appends the contract arguments to the `decoder` command. For
example, a call might be `decode TQ2_0 512 /tmp/.../input.bin
/tmp/.../values.out /tmp/.../scales.out`. The command runs in a fresh
directory for each call.

### Inputs

| input | default | meaning |
|---|---|---|
| `decoder` | required | Decoder command. A relative path that names an existing file resolves against the working directory. |
| `vectors` | the Action's `conformance/formats_*.json` | Whitespace-separated files, directories or globs. |
| `formats` | all | Contract format names to run: `TQ1_0 TQ2_0 Q2_0 Q1_0 PQ2_0 PTQ1_0 I2_S HF_PACKED MLX2 ONNX2`. |
| `report` | `ternary-check-report.json` | Path of the JSON report, schema `trinity.ternary-check-run.v1`. |
| `summary` | a temporary file | Path the Markdown summary is appended to. The summary also goes to the job summary. |
| `fail-on` | `mismatch` | `mismatch`: fail on any outcome except `match`, `match_unflagged` and `rejected`. `silent`: fail only when the decoder accepts an input it must refuse. `never`: report only. |
| `timeout` | `60` | Seconds allowed for one decoder call. |
| `runtime` | `release` | Where the checker comes from; see below. |
| `release-url` | this repository's releases | Base URL for `runtime: release`. |
| `python` | `python3` | Python 3.10 or later. |

### Outputs

| output | meaning |
|---|---|
| `report` | Path of the JSON report. |
| `summary` | Path of the Markdown summary. |
| `passed` | `true` when no call fails under `fail-on`. |
| `cases` | Number of decoder calls. |
| `failures` | Number of calls that fail under `fail-on`. |
| `mismatches` | Number of calls whose values, scale words, stored bytes or reported flags differ. |
| `silent` | Number of vectors that had to be refused but were accepted. |

The step fails when a call fails under `fail-on`, or when the check cannot run.

### Runtime

- **`release`** (the default):
  - The Action reads its version from the `pyproject.toml` of its own ref.
  - It downloads `SHA256SUMS` and the platform wheel from that GitHub release.
  - It checks the wheel's SHA-256 against `SHA256SUMS`, unpacks the wheel into `$RUNNER_TEMP` and runs the checker from there. It does not use pip.
  - Wheels exist for Linux x86_64 and macOS arm64. Other runners stop with an error that says to use a local build.
  - The macOS wheel loads only on macOS versions at or above the deployment target it was built for. Before the checker runs, the Action loads the native library once and stops with an error if it does not load.
- **A directory:**
  - The directory holds the `trinity_memory` package with its native runtime. That can be a checkout of this repository after `sh tools/build-t27.sh`, which needs the t27 compiler pinned in `native/compiler.lock`, or an unpacked wheel.
  - This repository's CI uses `runtime: .` because the release assets do not exist before the release.

The checker needs only Python and the native t27 library. The comparisons and
verdicts run in that library, not in Python.

## What the outcomes mean

The full table is in [CONTRACT.md](CONTRACT.md#outcomes). In brief:

- `match`: the decoder reproduced the values, scale words and flags.
- `rejected`: the decoder refused a bad input with the right class token.
- `match_unflagged`: the values are right, but the decoder did not report a flag it should have. This does not fail the check, because flags are optional in v1.
- `mismatch`: the values, scale words, stored bytes or reported flags differ. The report gives the first differing index and both values.
- `silent`: the decoder accepted an input it must refuse. This is the "loads silently and outputs gibberish" failure.
- `wrong_class`, `unexpected_reject`, `unclassified`: the decoder refused, but not as the vector expects.
- `crashed`: the decoder was stopped by a signal or the time limit.

The GGUF and safetensors header vectors are outside contract v1. The report
lists them under `not_run` with reason `container`.

## Running it without GitHub

```sh
trinity-memory ternary-check run --decoder ./build/my-decoder --report r.json --summary r.md
# from a checkout (after sh tools/build-t27.sh):
PYTHONPATH=. python3 -m trinity_memory ternary-check run --decoder ./build/my-decoder
```

The reference decoder is `python3 -m trinity_memory ternary-check`. It passes
all 178 calls from the 126 payload vectors of the six files. It uses the t27
decoders and encoders of `t27/formats.t27` through `t27/ternary_contract.t27`.
`tests/test_ternary_check_run.py` checks this natively, and
`tests/ternary_contract_wasm_replay.mjs` checks the same functions in WASM.

## How this repository tests the Action

The `ternary-check-action` job in `.github/workflows/ci.yml` runs the Action
from this checkout on `ubuntu-latest` and `macos-15`, using two decoders:

- **The reference decoder.** It must pass.
- **`tests/action/wrong_group_decoder.py`.** This decoder reads PrismML PQ2_0 bytes (group 128) as ggml-org Q2_0 (group 64), the layout confusion behind the conformance vector `group_128_bytes_read_as_group_64`. The Action must fail. `tests/action/check_reports.py` then checks that only PQ2_0 calls fail and that the report names the first differing weight.

`tests/test_ternary_check_action.py` runs the Action's two scripts
(`resolve-runtime.sh` and `run.sh`) outside GitHub. It covers the release
runtime's SHA256SUMS check against a local stand-in release.

## Weekly re-check

`.github/workflows/ternary-check-weekly.yml` runs every Monday at 04:23 UTC,
and on demand. It has read-only permissions and runs two checks:

1. `tools/upstream-drift.py` compares, by git blob SHA, every pinned file in `specs/formats/upstream.lock.json` and `tests/upstream/llama.cpp.lock.json` with the same path at the upstream branch head. It also compares the Hugging Face revisions in `fixtures/manifest.json` with each model's current `main`. It writes `build/upstream-drift.json` and posts nothing.
2. The workflow rebuilds the t27 decoders and reruns every vector through this Action with the reference decoder.

A new head commit is not drift by itself. Drift is one of:

- a pinned file that changed or is missing;
- a moved submodule gitlink;
- a moved model revision.

Drift fails the run, but only after the vectors have been rerun.
`tests/test_upstream_drift.py` tests the drift tool against recorded API
responses from 2026-09-23 (`tests/data/upstream-drift/`), without network
access.
