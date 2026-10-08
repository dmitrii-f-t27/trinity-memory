# Ternary Check Live at the llama.cpp that Qualcomm GenieX ships

Snapshot of the scan that started 2026-10-08T13:15:55Z (workflow run 37782303670, trinity-memory
`d6bc7c59`), kept here because the published `scan.json` is replaced every day.

- Pin: `llama.cpp-94256114` = ggml-org/llama.cpp@`94256114c229674ef96e76eb2dea596e65b43818`, the commit
  `third-party/llama.cpp` of qualcomm/GenieX points to (checked 2026-10-08). Its spec is
  `specs/runtimes/llama_cpp_94256114.json`; the reader is built from that commit and replayed on every
  cached header (see `replay` in `scan.json`).
- `scan.json`: the whole report (schema `trinity.ternary-check-live.v2`). The verdict of one model at the
  pin is `runtimes["llama.cpp-94256114"]` of its entry in `repositories[].models[]`.
- `models-at-pin.tsv`: one row per model: repository, file, the model verdict (`ok`, `refused`,
  `other_runtime`, `no_ternary_layout`), the runtime it is written for, the verdict at the pin and, when
  refused, the reader's status.
- Loading verdicts only: no inference, speed, power or accuracy, no device run.
