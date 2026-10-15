# Compiler pin t27c 0.5.2

`native/compiler.lock` pins
[`gHashTag/t27@d06eaedc9fb59cda13c3529e1c6ed5bc510f3e08`](https://github.com/gHashTag/t27/tree/d06eaedc9fb59cda13c3529e1c6ed5bc510f3e08)
(`t27c 0.5.2`). The previous pin was
[`bff21b85b206a0dd367876343e56dcd8312d81ba`](https://github.com/gHashTag/t27/tree/bff21b85b206a0dd367876343e56dcd8312d81ba)
(`t27c 0.2.0`). This page records what the move needed in the `.t27` sources,
how to reproduce the checks, and what was and was not verified.

## Reproduce

```sh
git clone https://github.com/gHashTag/t27.git build/compiler
git -C build/compiler checkout "$(cat native/compiler.lock)"
export T27_ROOT="$PWD/build/compiler" CC=clang CXX=clang++
rustup toolchain install 1.94.0 --profile minimal
(cd "$T27_ROOT" && cargo +1.94.0 build --locked --release -p t27c --bin t27c --target-dir target)
"$T27_ROOT/target/release/t27c" --version     # t27c 0.5.2

make t27
make t27-test
make check-specs

# Real-weight fixtures and pinned llama.cpp sources (sha256-checked), then the
# offline suite and the evidence registry.
python3 tools/fetch-fixtures.py
sh tools/fetch-upstream.sh
OFFLINE=1 TRINITY_FIXTURES_OFFLINE=1 TRINITY_UPSTREAM_OFFLINE=1 TRINITY_REQUIRE_CACHED=1 make test
python3 tools/evidence-proof.py
```

The Zig test report and `seal --save` of the 0.5.2 compiler need Zig 0.16.0 on
`PATH` (Zig 0.15.2 cannot build its test runner):

```sh
failed=0
for spec in $(find specs -name '*.t27' | sort); do
    "$T27_ROOT/target/release/t27c" test-report "$spec" || { echo "test-report failed: $spec" >&2; failed=1; }
done
test "$failed" = 0
```

The loop checks the exit status of every spec, not only the last one. Exit 0
is necessary but not sufficient: read each report and require tests N > 0,
failed 0, vacuous 0 and no BLOCKED. The 0.2.0 CLI can exit 0 with a BLOCKED
spec and has no vacuity counter. Test counts of this report, the C spec gate
and the Python suite overlap; do not add them up.

Use the Python 3.10+ interpreter with numpy and ml_dtypes that the test suite
needs (`PYTHON=... make ...`).

## Source changes

The compiler is stricter about integer narrowing and generates Zig tests with
different slice rules. No generated file was edited by hand, no expected value
or assertion was changed, and no test was removed.

| File | Change |
|---|---|
| `t27/rtl/fpga_ddr3_loader.t27` | The 21 `P_*` state constants are `u8` (values 0..20), the type of `pstate` and `resp_after`; previously `u32`, rejected by the 0.5.2 typecheck. |
| `t27/rtl/gf16_attn.t27` | Nine `u64` results of `pick(...)` assigned to `u32` registers are explicit `as u32` (low 32 bits, as before). Widths, arithmetic, rounding, scales and latency are unchanged. New test `pick_narrows_to_low32`. |
| `specs/formats/{bitnet_cpp,hf_bitnet,llama_cpp,mlx,onnx,prismml}.t27`, `specs/memory/{bridge,conformance,edge_demo,stream_compute,tensorpack}.t27` | Test-local arrays passed to slice parameters carry an array-literal initializer of exactly the declared size (70 zero literals: 69 new, 1 already present; largest 1152). Repeated loop counters in `llama_cpp` and `prismml` are fresh variables. The `error` parameter in `stream_compute` is `error_flag` (`error` is a Zig keyword). |
| `specs/memory/stream_compute.t27` | `tms_dot_pack_activations`, the lane byte of `tms_rtl_packet_word` and `tms_rtl_expected_word` widen the signed `i32` to `i64` before the mask. The previous `i32 -> u32` cast of a negative value is a checked cast in Zig (`integer does not fit in destination type`); the low byte and the uint32 two's-complement image are unchanged for every `i32`. Three boundary tests added (i32 min/max, -1, 0, positives, -129, 256, -256). |

Generated data that depends on the pin was rewritten by the existing tools, not
by hand:

- `.trinity/seals/`: 13 seals re-saved with `t27c seal --save` (no `--force`):
  the 11 specs above plus `graph/crypt` and `graph/scope`, whose generated Zig
  changes under 0.5.2 (their invariants become active). These 13 use seal
  schema 2; the other 17 seals are unchanged and verify under 0.5.2.
- `reports/ternary-check.json`, `reports/ternary-check.html`,
  `reports/ternary-check/matvec-2026-09-23.json`: rewritten by
  `python -m trinity_memory.ternary_check --write` and
  `python -m trinity_memory.matvec --output reports/ternary-check/matvec-2026-09-23.json`. The only difference is the
  producer commit `bff21b85 -> d06eaedc` in four places; the numerical payload
  and the repro files are byte identical.
- `reports/numeric/ffn-quantization-v1.json`: rewritten by
  `python tools/ffn_quantization.py --output reports/numeric/ffn-quantization-v1.json`.
  The report records the pin as `model.compiler_revision`, and the evidence
  claim `ffn_real_layer` compares the recomputed report byte for byte. The
  only changed line is `compiler_revision` `bff21b85 -> d06eaedc`; every other
  field (5 cases, 20 propagated paths) is identical and the size is unchanged.
  Recomputed with the old compiler, the report equals the previously committed
  file byte for byte.
- Evidence manifests, written by `tools/evidence-proof.py --claim NAME
  --write-manifest`; only sha256 values change:

  | Claim | Changed bound files |
  |---|---|
  | `ternary_check_matrix` | `native/compiler.lock` and the three Ternary Check reports |
  | `ffn_real_layer` | `native/compiler.lock`, `reports/numeric/ffn-quantization-v1.json` |
  | `ffn_oracles`, `gf16_ffn_performance`, `gguf_verdicts`, `prism_hadamard` | `native/compiler.lock` |
  | `matvec_pipeline` | `reports/ternary-check/matvec-2026-09-23.json` |

  Retained hardware measurements and their origin pins are not changed.
- GF16 FFN bounded simulation: `reports/fpga/gf16-performance-2026-10-01/verify-evidence.py`
  requires the current accelerated record of
  `reports/numeric/gf16-ffn-performance-bounded.json` and
  `reports/numeric/gf16-ffn-performance-bounded-linux.json` to hash the current
  `native/compiler.lock`, with counters and capture hashes identical to the
  committed ones. Both are rewritten only from real
  `tools/measure_gf16_ffn.py` runs at 0.5.2 (macOS locally, Linux in
  `gf16-ffn.yml`). The four generated GF16 Verilog modules (`gf16_wide_norm`,
  `ffn_wide`, `gf16_scalar`, `gf16_ffn`) are byte-identical under both
  compilers; that supports, but does not replace, the measurement. Historical
  measurements, physical captures and their producer pins are unchanged.
  Status: PENDING (measurement not yet adopted).
- `.github/workflows/gf16-ffn.yml`: "Recheck committed performance evidence"
  now runs after "Measure full and result-only clocks", and the artifact also
  keeps the prompt-0-token-0 input regions. Before, a pin change failed the job
  before the Linux measurement that the committed records must match could be
  produced. The recheck itself is unchanged and still fails the job while the
  committed records are stale.

## Checks on this branch

All 31 qualified paths (13 sources, 13 seals, the lock, 3 reports, 1
manifest) are byte-identical (sha256) to the qualified copies below. On the
branch: `make t27` pass; `make check-specs` pass (30 specs, all seals,
conformance, differential harnesses); Zig `test-report` of all 30 specs pass
(146 tests, 110 invariants, 0 failed, 0 blocked, 0 vacuous). `make t27-test`
and the root `make test` were not repeated on the branch: the source is
byte-identical to the qualified one and the only new generated change is the
producer line above, which the evidence registry checks. Evidence registry: 13
of 14 claims pass (435 acceptance vectors); `gf16_ffn_performance` waits for
the GF16 re-measurement above. CI results are reported in the pull request.

## Previous local qualification

Before this change was moved onto a branch, the same 13 source files were
qualified locally in two isolated clones of
`4542dd2360b021c3cf9ef855b1a6ac5fd5a9174d`, one per compiler, with the changes
as uncommitted working-tree edits. This is a local
measurement, not CI, and it applies to the branch only where the files are
byte-identical to the qualified copies.

| Check | t27c 0.2.0 (bff21b85) | t27c 0.5.2 (d06eaedc) |
|---|---|---|
| `make t27` | pass | pass |
| `make t27-test` | pass | pass |
| `make check-specs` (30 specs, 406 trace cycles, 20k differential beats) | pass | pass |
| root `make test`, offline caches | 599 tests: 594 passed, 5 skipped | 599 tests: 594 passed, 5 skipped |
| Zig `test-report`, 30 specs | 28 pass; `graph/crypt`, `graph/scope` blocked (old emitter) | 30 pass: 146 tests, 110 invariants, 0 vacuous |
| strict RTL typecheck, 37 inputs (direct probe) | 26/37 (11 with diagnostics) | 37/37 |
| claim `ternary_check_matrix` | pass, 76 vectors | pass, 76 vectors, same counts |
| `seal --verify`, 30 specs | 30/30 | 30/30 |

The first root run of the 0.5.2 clone failed two `test_deterministic_content`
tests (`tests/test_matvec.py`, `tests/test_ternary_check.py`) because the
committed reports named the old producer commit; the reports were then
regenerated as described above and the second run passed. The 5 remaining
skips are the optional Torch/transformers capture environment.

## Known limits

- Seal metadata: the 13 new seals record `toolchain.test_runner: "missing"`.
  `seal_toolchain` in the compiler hashes `bootstrap/src/test_report.rs`
  relative to the consumer directory, where it does not exist. The tests ran
  (each seal records N > 0, failed 0, vacuous 0, forced false).
- Whole-module probes of `t27/rtl/gf16_attn.t27` outside the Memory pipeline
  fail on the original source too: Zig rejects the global RAM registers without
  initializer, and the generated C fails `-Werror` (scalar initializers of the
  8192-word arrays, unsuffixed 64-bit literals). These are generator limits. The
  test `pick_narrows_to_low32` therefore did not execute through them; the casts
  are covered by the RTL numerical tests, the typecheck and the builds.
- Retained hardware measurements and their origin pins are unchanged. An offline
  replay of a hardware claim under 0.5.2 is not a new hardware qualification.
- Not validated: full-model quality, live hardware, the Rust runtime, other
  compilers. The known compiler workarounds in
  [`T27-MIGRATION.md`](T27-MIGRATION.md) were recorded at the previous pin and
  were not re-audited.

Dmitrii Fedorov (@dmitrii-f-t27)
