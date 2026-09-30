# Findings ledger (Ternary Check Live)

> RU: журнал находок. Каждая находка до огласки проверяется независимо (и
> цифры, и механизм), потом согласуется основателями, потом сообщается автору
> файла или рантайма. Ничего не уходит апстрим и не публикуется без согласия
> основателей.

One entry per finding, in the order found. Statuses:

| status | meaning |
|---|---|
| `observed` | the t27 verdict saw it in a scan; no independent confirmation yet |
| `confirmed` | replayed by an independent reader (the pinned upstream `gguf.cpp`, `bitnet.cpp`, or the PrismML fork) or reproduced by hand from the bytes; numbers and mechanism both check |
| `wording` | the entry text was reviewed; waiting for the founders' approval to report |
| `approved` | the founders approved reporting |
| `reported` | filed with the owner (HF discussion / upstream issue); the link is in the entry |
| `answered` | the maintainer answered; the answer is summarised |
| `withdrawn` | the finding did not survive verification; what was wrong is recorded |

Every finding shown on the public page (#53) links to its entry here by
anchor (`#F-<id>`). An entry may only appear on the page in `confirmed` or a
later status.

## The protocol

1. **t27 verdict** — the scan (`python3 -m trinity_memory.live`) records the
   verdict, the header sha256 and the byte numbers behind it.
2. **Independent replay** — `tools/live-replay.sh` builds the pinned readers
   and `--replay` feeds them the cached header: the finding is confirmed only
   when the real reader agrees (or the bytes are re-derived by hand from the
   recorded ranges). Both the numbers and the mechanism must check.
3. **Wording review** — the entry says exactly what the bytes say, no more:
   what a stock runtime does, what the fork does, who is affected.
4. **Founders' approval** — nothing is reported upstream or posted without it.
5. **Report** — an HF discussion on the model repo or an upstream issue; the
   link lands in the entry.
6. **Public note** — the page badge and this ledger update together.

---

## F-1 — PrismML's own `Q2_0` Bonsai files refuse to load in every reader

- **status**: reported (HF discussion
  [prism-ml/Ternary-Bonsai-27B-gguf#65](https://huggingface.co/prism-ml/Ternary-Bonsai-27B-gguf/discussions/65),
  filed 2026-09-27 with the approved wording; founders' approval 2026-09-27 —
  confirmed by the replay before that: 110 agree,
  0 disagree, 0 crashes over the 55-file scan, both built readers —
  llama.cpp e6ab7c1a and the PrismML fork bdc23b56 — refuse at the same
  record with the same numbers)
- **where**: `prism-ml/Ternary-Bonsai-27B-gguf` → `Ternary-Bonsai-27B-Q2_0.gguf`
  (header sha256 `e085e126…3a09a`, lfs `868c1171…1757`, 7,165,121,600 bytes),
  `prism-ml/Ternary-Bonsai-8B-gguf` → `Ternary-Bonsai-8B-Q2_0.gguf`
- **what the header says**: 498 ternary tensors declare type 42 (`Q2_0`), but
  the record offsets walk past the extent those declarations imply: at
  `output_norm.weight` the walk expects byte 357,580,800 (llama.cpp and the
  PrismML fork; bitnet.cpp's own arithmetic expects 267,523,456) and finds
  337,715,200 — every runtime's loader refuses the file.
- **mechanism (t27)**: the tensor records are packed as `PQ2_0` (the fork's
  128-weight-group layout, 498 of 498 fit the extents) while the type field
  says `Q2_0` — the same shape PrismML's documentation warns about for stock
  llama.cpp ("a `Q2_0` file loads silently and outputs gibberish"), except
  the offsets here refuse outright rather than load garbage.
- **who is affected**: anyone loading the official `Q2_0` Bonsai files with
  stock llama.cpp (any build without the PrismML type-42 layout), and — per
  the verdict — even the fork's own loader, whose offset arithmetic does not
  accept this header.
- **independent confirmation**: the replay of the cached header against the
  pinned readers (tools/live-replay.sh + `--replay`): both llama.cpp and the
  fork's own reader walk to `output_norm.weight`, expect the extents above
  and refuse — the numbers and the mechanism both check. bitnet.cpp's
  different signed arithmetic (267,523,456) refuses at the same record.
- **reported**: [prism-ml/Ternary-Bonsai-27B-gguf#65](https://huggingface.co/prism-ml/Ternary-Bonsai-27B-gguf/discussions/65)
  (2026-09-27, the approved wording verbatim)
- **answer**: —

## F-2 — llama.cpp TQ1_0/TQ2_0 garbage output on CPU (upstream #15193)

- **status**: confirmed (upstream; recorded here for the page's completeness)
- **where**: ggml-org/llama.cpp issue
  [#15193](https://github.com/ggml-org/llama.cpp/issues/15193)
- **what**: TQ1_0/TQ2_0 files produce garbage on CPU. Our stage-1 vectors
  ([docs/ternary-check.md](../ternary-check.md)) and the v0.4.0 release
  reproduce the loaders' byte-level behaviour exactly; the failure is
  upstream's, not a mis-decode.
- **independent confirmation**: the issue itself plus our committed vectors.
- **reported**: already public upstream; nothing to file from here.
- **answer**: open upstream.

## F-3 — unsloth `UD-TQ1_0` files of Qwen3-Coder-30B / Qwen3-30B-A3B: no ternary layout in the header

- **status**: observed
- **where**: `unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF` →
  `…-UD-TQ1_0.gguf`, `unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF` →
  `…-UD-TQ1_0.gguf`
- **what the header says**: the ternary tensor records do not carry a layout
  any of the three pinned readers recognises as ternary storage (the walk
  finds no valid ternary packing for them; verdict `no_ternary_layout`).
  This may be a new unpublished layout rather than a defect — the entry
  exists to drive the replay that decides which.
- **who is affected**: users trying to load these files with readers other
  than the one they were exported for; the verdict says which runtime the
  file is written for (`tlv_native`).
- **independent confirmation**: pending replay.
- **reported**: —
- **answer**: —

## F-4 — `dealignai/Bonsai-27b-Ternary-CRACK-GGUF`: `Q2_0` file in the legacy group-128 layout

- **status**: reported (HF discussion [dealignai/Bonsai-27b-Ternary-CRACK-GGUF#2](https://huggingface.co/dealignai/Bonsai-27b-Ternary-CRACK-GGUF/discussions/2), filed 2026-09-30
  with the wording the founders approved on 2026-09-29; confirmed 2026-09-29)
- **where**: `dealignai/Bonsai-27b-Ternary-CRACK-GGUF` @ `67c2f0ed38d9` → `Bonsai-27b-Ternary-CRACK-Q2_0.gguf`
  (8,247,797,056 bytes, lfs `bbaf2689…7e4e`, header sha256 `eb324299…a2dd`)
- **what the header says**: 496 records declare type 42; at `blk.0.attn_norm.weight` the walk
  finds offset 1,766,481,920, llama.cpp and the fork expect 1,766,973,440
  (bitnet.cpp 1,764,745,248).
- **mechanism (t27)**: every type-42 record fits the extent of the fork's
  `PQ2_0` (group 128, 34-byte blocks), none fits group-64 `Q2_0`; the walk
  therefore reaches the named record at a smaller offset than a group-64
  reader expects, and llama.cpp, the fork and bitnet.cpp refuse the file.
- **who is affected**: users of a current llama.cpp or PrismML fork build;
  the card sends them to the fork, which refuses the file.
- **independent confirmation (2026-09-29)**: the files are unchanged since the
  2026-09-24 scan (same HF revision and LFS sha256, checked through the HF API);
  a fresh t27 verdict at master bdd775e and the replay of the cached headers on
  the pinned readers (llama.cpp e6ab7c1a, PrismML fork bdc23b56, bitnet.cpp
  390c3077; `tools/live-replay.sh`, bitnet.cpp's `ggml-base` linked with
  `-undefined dynamic_lookup` on macOS) agree on every file: 36 of 36 answers
  over the 12 GGUF files of the six repositories of F-4..F-9, 0 disagree, 0
  crashes. The fork prints its own hint for each file ("this file matches the
  legacy Prism Q2_0 layout (group size 128 stored as ggml type id 42)"; it does
  so only when the offset it finds equals the legacy layout's). Rewriting the
  type field of the type-42 records to 142 (`tools/gguf-relabel-check.py`,
  4 bytes per record, offsets and weights unchanged) makes the fork's GGUF
  reader accept the header; stock llama.cpp and bitnet.cpp have no type 142 and
  refuse either way. This checks the GGUF reader only, not a model run.
  Today's fork head (88c4bc60, 2026-09-29) and mainline (19e28a27) still define
  `QK2_0` 64 and the fork `QK_PQ2_0` 128. Evidence: [`reports/live/legacy-q2_0-2026-09-29/`](../../reports/live/legacy-q2_0-2026-09-29/).
- **context**: the owner's card points users to the PrismML fork; the fork's
  `prism` branch switched id 42 to mainline's group-64 `Q2_0`, which PrismML
  explains in [PrismML-Eng/llama.cpp#167](https://github.com/PrismML-Eng/llama.cpp/issues/167).
- **reported**: [dealignai/Bonsai-27b-Ternary-CRACK-GGUF#2](https://huggingface.co/dealignai/Bonsai-27b-Ternary-CRACK-GGUF/discussions/2) (2026-09-30, the approved wording verbatim)
- **answer**: —

## F-5 — `Hikari07jp/Ternary-Bonsai-27B-Abliterated-LowDeg-GGUF`: `Q2_0` file in the legacy group-128 layout

- **status**: reported (HF discussion [Hikari07jp/Ternary-Bonsai-27B-Abliterated-LowDeg-GGUF#2](https://huggingface.co/Hikari07jp/Ternary-Bonsai-27B-Abliterated-LowDeg-GGUF/discussions/2), filed 2026-09-30
  with the wording the founders approved on 2026-09-29; confirmed 2026-09-29)
- **where**: `Hikari07jp/Ternary-Bonsai-27B-Abliterated-LowDeg-GGUF` @ `60b1880c4c2e` →
  `Ternary-Bonsai-27B-Abliterated-LowDeg-Q2_0.gguf` (7,165,121,600 bytes, lfs
  `527f276d…e81d`, header sha256 `e085e126…a09a`, byte-identical to the header of
  PrismML's `Ternary-Bonsai-27B-Q2_0.gguf` in F-1; the weights differ)
- **what the header says**: 498 records declare type 42; at `output_norm.weight` the walk finds
  337,715,200, llama.cpp and the fork expect 357,580,800 (bitnet.cpp 267,523,456).
- **mechanism (t27)**: every type-42 record fits the extent of the fork's
  `PQ2_0` (group 128, 34-byte blocks), none fits group-64 `Q2_0`; the walk
  therefore reaches the named record at a smaller offset than a group-64
  reader expects, and llama.cpp, the fork and bitnet.cpp refuse the file.
- **who is affected**: users of a current llama.cpp or PrismML fork build;
  the card sends them to the fork, which refuses the file.
  The card itself says "official Q2_0 g128 ternary pack" and "Prism fork required".
- **independent confirmation (2026-09-29)**: the files are unchanged since the
  2026-09-24 scan (same HF revision and LFS sha256, checked through the HF API);
  a fresh t27 verdict at master bdd775e and the replay of the cached headers on
  the pinned readers (llama.cpp e6ab7c1a, PrismML fork bdc23b56, bitnet.cpp
  390c3077; `tools/live-replay.sh`, bitnet.cpp's `ggml-base` linked with
  `-undefined dynamic_lookup` on macOS) agree on every file: 36 of 36 answers
  over the 12 GGUF files of the six repositories of F-4..F-9, 0 disagree, 0
  crashes. The fork prints its own hint for each file ("this file matches the
  legacy Prism Q2_0 layout (group size 128 stored as ggml type id 42)"; it does
  so only when the offset it finds equals the legacy layout's). Rewriting the
  type field of the type-42 records to 142 (`tools/gguf-relabel-check.py`,
  4 bytes per record, offsets and weights unchanged) makes the fork's GGUF
  reader accept the header; stock llama.cpp and bitnet.cpp have no type 142 and
  refuse either way. This checks the GGUF reader only, not a model run.
  Today's fork head (88c4bc60, 2026-09-29) and mainline (19e28a27) still define
  `QK2_0` 64 and the fork `QK_PQ2_0` 128. Evidence: [`reports/live/legacy-q2_0-2026-09-29/`](../../reports/live/legacy-q2_0-2026-09-29/).
- **context**: the owner's card points users to the PrismML fork; the fork's
  `prism` branch switched id 42 to mainline's group-64 `Q2_0`, which PrismML
  explains in [PrismML-Eng/llama.cpp#167](https://github.com/PrismML-Eng/llama.cpp/issues/167).
- **reported**: [Hikari07jp/Ternary-Bonsai-27B-Abliterated-LowDeg-GGUF#2](https://huggingface.co/Hikari07jp/Ternary-Bonsai-27B-Abliterated-LowDeg-GGUF/discussions/2) (2026-09-30, the approved wording verbatim)
- **answer**: —

## F-6 — `OS-Software/Ternary-Bonsai-27B-heretic-ja-GGUF`: `Q2_0` file in the legacy group-128 layout

- **status**: reported (HF discussion [OS-Software/Ternary-Bonsai-27B-heretic-ja-GGUF#1](https://huggingface.co/OS-Software/Ternary-Bonsai-27B-heretic-ja-GGUF/discussions/1), filed 2026-09-30
  with the wording the founders approved on 2026-09-29; confirmed 2026-09-29)
- **where**: `OS-Software/Ternary-Bonsai-27B-heretic-ja-GGUF` @ `d9aa6defc551` →
  `Ternary-Bonsai-27B-heretic-ja-Q2_0.gguf` (7,165,121,696 bytes, lfs `eadb6841…205a`,
  header sha256 `6755bf63…8f18`)
- **what the header says**: 498 records declare type 42; at `output_norm.weight` the walk finds
  337,715,200, llama.cpp and the fork expect 357,580,800 (bitnet.cpp 267,523,456).
- **mechanism (t27)**: every type-42 record fits the extent of the fork's
  `PQ2_0` (group 128, 34-byte blocks), none fits group-64 `Q2_0`; the walk
  therefore reaches the named record at a smaller offset than a group-64
  reader expects, and llama.cpp, the fork and bitnet.cpp refuse the file.
- **who is affected**: users of a current llama.cpp or PrismML fork build;
  the card sends them to the fork, which refuses the file.
  The same repository's `Ternary-Bonsai-27B-heretic-ja-Q2_g64.gguf` (group-64
  `Q2_0`, 498 records) is accepted by all three readers.
- **independent confirmation (2026-09-29)**: the files are unchanged since the
  2026-09-24 scan (same HF revision and LFS sha256, checked through the HF API);
  a fresh t27 verdict at master bdd775e and the replay of the cached headers on
  the pinned readers (llama.cpp e6ab7c1a, PrismML fork bdc23b56, bitnet.cpp
  390c3077; `tools/live-replay.sh`, bitnet.cpp's `ggml-base` linked with
  `-undefined dynamic_lookup` on macOS) agree on every file: 36 of 36 answers
  over the 12 GGUF files of the six repositories of F-4..F-9, 0 disagree, 0
  crashes. The fork prints its own hint for each file ("this file matches the
  legacy Prism Q2_0 layout (group size 128 stored as ggml type id 42)"; it does
  so only when the offset it finds equals the legacy layout's). Rewriting the
  type field of the type-42 records to 142 (`tools/gguf-relabel-check.py`,
  4 bytes per record, offsets and weights unchanged) makes the fork's GGUF
  reader accept the header; stock llama.cpp and bitnet.cpp have no type 142 and
  refuse either way. This checks the GGUF reader only, not a model run.
  Today's fork head (88c4bc60, 2026-09-29) and mainline (19e28a27) still define
  `QK2_0` 64 and the fork `QK_PQ2_0` 128. Evidence: [`reports/live/legacy-q2_0-2026-09-29/`](../../reports/live/legacy-q2_0-2026-09-29/).
- **context**: the owner's card points users to the PrismML fork; the fork's
  `prism` branch switched id 42 to mainline's group-64 `Q2_0`, which PrismML
  explains in [PrismML-Eng/llama.cpp#167](https://github.com/PrismML-Eng/llama.cpp/issues/167).
- **reported**: [OS-Software/Ternary-Bonsai-27B-heretic-ja-GGUF#1](https://huggingface.co/OS-Software/Ternary-Bonsai-27B-heretic-ja-GGUF/discussions/1) (2026-09-30, the approved wording verbatim)
- **answer**: —

## F-7 — `Danny-Dasilva/Ternary-Bonsai-27B-antidoom-DSpark`: `Q2_0` file in the legacy group-128 layout

- **status**: reported (HF discussion [Danny-Dasilva/Ternary-Bonsai-27B-antidoom-DSpark#1](https://huggingface.co/Danny-Dasilva/Ternary-Bonsai-27B-antidoom-DSpark/discussions/1), filed 2026-09-30
  with the wording the founders approved on 2026-09-29; confirmed 2026-09-29)
- **where**: `Danny-Dasilva/Ternary-Bonsai-27B-antidoom-DSpark` @ `663b5d9aca32` →
  `Ternary-Bonsai-27B-antidoom-Q2_0.gguf` (8,247,796,896 bytes, lfs `cca1827d…4359`,
  header sha256 `650c2303…cda7`)
- **what the header says**: 496 records declare type 42; at `blk.0.attn_norm.weight` the walk
  finds 1,766,481,920, llama.cpp and the fork expect 1,766,973,440 (bitnet.cpp
  1,764,745,248).
- **mechanism (t27)**: every type-42 record fits the extent of the fork's
  `PQ2_0` (group 128, 34-byte blocks), none fits group-64 `Q2_0`; the walk
  therefore reaches the named record at a smaller offset than a group-64
  reader expects, and llama.cpp, the fork and bitnet.cpp refuse the file.
- **who is affected**: users of a current llama.cpp or PrismML fork build;
  the card sends them to the fork, which refuses the file.
- **independent confirmation (2026-09-29)**: the files are unchanged since the
  2026-09-24 scan (same HF revision and LFS sha256, checked through the HF API);
  a fresh t27 verdict at master bdd775e and the replay of the cached headers on
  the pinned readers (llama.cpp e6ab7c1a, PrismML fork bdc23b56, bitnet.cpp
  390c3077; `tools/live-replay.sh`, bitnet.cpp's `ggml-base` linked with
  `-undefined dynamic_lookup` on macOS) agree on every file: 36 of 36 answers
  over the 12 GGUF files of the six repositories of F-4..F-9, 0 disagree, 0
  crashes. The fork prints its own hint for each file ("this file matches the
  legacy Prism Q2_0 layout (group size 128 stored as ggml type id 42)"; it does
  so only when the offset it finds equals the legacy layout's). Rewriting the
  type field of the type-42 records to 142 (`tools/gguf-relabel-check.py`,
  4 bytes per record, offsets and weights unchanged) makes the fork's GGUF
  reader accept the header; stock llama.cpp and bitnet.cpp have no type 142 and
  refuse either way. This checks the GGUF reader only, not a model run.
  Today's fork head (88c4bc60, 2026-09-29) and mainline (19e28a27) still define
  `QK2_0` 64 and the fork `QK_PQ2_0` 128. Evidence: [`reports/live/legacy-q2_0-2026-09-29/`](../../reports/live/legacy-q2_0-2026-09-29/).
- **context**: the owner's card points users to the PrismML fork; the fork's
  `prism` branch switched id 42 to mainline's group-64 `Q2_0`, which PrismML
  explains in [PrismML-Eng/llama.cpp#167](https://github.com/PrismML-Eng/llama.cpp/issues/167).
- **reported**: [Danny-Dasilva/Ternary-Bonsai-27B-antidoom-DSpark#1](https://huggingface.co/Danny-Dasilva/Ternary-Bonsai-27B-antidoom-DSpark/discussions/1) (2026-09-30, the approved wording verbatim)
- **answer**: —

## F-8 — `darkstarinitiative/AJAN-SIMIT-Ternary-Bonsai-Q2_0-GGUF`: four `Q2_0` files in the legacy group-128 layout

- **status**: reported (HF discussion [darkstarinitiative/AJAN-SIMIT-Ternary-Bonsai-Q2_0-GGUF#1](https://huggingface.co/darkstarinitiative/AJAN-SIMIT-Ternary-Bonsai-Q2_0-GGUF/discussions/1), filed 2026-09-30
  with the wording the founders approved on 2026-09-29; confirmed 2026-09-29)
- **where**: `darkstarinitiative/AJAN-SIMIT-Ternary-Bonsai-Q2_0-GGUF` @ `5d28f49b2d5c`:
  `…-1.7B-Q2_0.gguf` (lfs `d97d94eb…228a`, header `eb8b2b79…b363`),
  `…-4B-Q2_0.gguf` (lfs `4e0bf8b7…8b8b`, header `7fe69172…e076`),
  `…-8B-Q2_0.gguf` (lfs `3c8d7047…f60b`, header `43cb3666…da72`),
  `…-27B-Q2_0.gguf` (lfs `bac08d39…5834`, header `8bba99cc…6c2d`)
- **what the header says**: 197 / 253 / 254 / 496 records declare type 42. 1.7B: at
  `blk.0.attn_k.weight` found 82,516,128, expected 87,369,536; 4B: at
  `blk.0.attn_k.weight` found 103,145,184, expected 109,211,936; 8B: at
  `output_norm.weight` found 165,015,872, expected 174,722,688; 27B: at
  `blk.0.attn_norm.weight` found 1,766,481,920, expected 1,766,973,440
  (llama.cpp and the fork; bitnet.cpp's own figures are in the evidence).
- **mechanism (t27)**: every type-42 record fits the extent of the fork's
  `PQ2_0` (group 128, 34-byte blocks), none fits group-64 `Q2_0`; the walk
  therefore reaches the named record at a smaller offset than a group-64
  reader expects, and llama.cpp, the fork and bitnet.cpp refuse the file.
- **who is affected**: users of a current llama.cpp or PrismML fork build;
  the card sends them to the fork, which refuses the file.
  The card says the files were made with `llama-quantize` from the fork's `prism`
  branch. The repository's `…-4B-TQ2_0.gguf` and `…-8B-TQ2_0.gguf` are accepted
  by all three readers.
- **independent confirmation (2026-09-29)**: the files are unchanged since the
  2026-09-24 scan (same HF revision and LFS sha256, checked through the HF API);
  a fresh t27 verdict at master bdd775e and the replay of the cached headers on
  the pinned readers (llama.cpp e6ab7c1a, PrismML fork bdc23b56, bitnet.cpp
  390c3077; `tools/live-replay.sh`, bitnet.cpp's `ggml-base` linked with
  `-undefined dynamic_lookup` on macOS) agree on every file: 36 of 36 answers
  over the 12 GGUF files of the six repositories of F-4..F-9, 0 disagree, 0
  crashes. The fork prints its own hint for each file ("this file matches the
  legacy Prism Q2_0 layout (group size 128 stored as ggml type id 42)"; it does
  so only when the offset it finds equals the legacy layout's). Rewriting the
  type field of the type-42 records to 142 (`tools/gguf-relabel-check.py`,
  4 bytes per record, offsets and weights unchanged) makes the fork's GGUF
  reader accept the header; stock llama.cpp and bitnet.cpp have no type 142 and
  refuse either way. This checks the GGUF reader only, not a model run.
  Today's fork head (88c4bc60, 2026-09-29) and mainline (19e28a27) still define
  `QK2_0` 64 and the fork `QK_PQ2_0` 128. Evidence: [`reports/live/legacy-q2_0-2026-09-29/`](../../reports/live/legacy-q2_0-2026-09-29/).
- **context**: the owner's card points users to the PrismML fork; the fork's
  `prism` branch switched id 42 to mainline's group-64 `Q2_0`, which PrismML
  explains in [PrismML-Eng/llama.cpp#167](https://github.com/PrismML-Eng/llama.cpp/issues/167).
- **reported**: [darkstarinitiative/AJAN-SIMIT-Ternary-Bonsai-Q2_0-GGUF#1](https://huggingface.co/darkstarinitiative/AJAN-SIMIT-Ternary-Bonsai-Q2_0-GGUF/discussions/1) (2026-09-30, the approved wording verbatim)
- **answer**: —

## F-9 — `Doses-AI/Pestle-27B-Ternary-GGUF`: written for mortar.cpp, not a defect

- **status**: withdrawn (as a defect report; kept as a record of why)
- **where**: `Doses-AI/Pestle-27B-Ternary-GGUF` @ `736c65eadb0b` → `pestle-27b-ternary.gguf`
  (8,480,707,488 bytes, lfs `6e0977c8…14bb`, header sha256 `0934669c…61c2`)
- **what the header says**: 978 records of type 42 whose extents fit a
  group-128 layout, and `token_embd.weight` / `output.weight` of type 143
  ([5120, 248320], 635,699,200 bytes each: 4 bits per weight). llama.cpp and
  bitnet.cpp refuse type 143; the fork reads 143 as its `PTQ1_0` (1.75 bits
  per weight) and refuses at `output.weight` (found 1,390,770,176, expected
  1,033,189,376). The fork prints no legacy hint, and relabelling 42 to 142
  does not make it accept the file (evidence: [`reports/live/legacy-q2_0-2026-09-29/`](../../reports/live/legacy-q2_0-2026-09-29/)).
- **why withdrawn**: the card says the model runs with the owner's runtime
  [mortar.cpp](https://github.com/DosesAI/mortar.cpp). At its head 236418ec
  (2026-08-14), `ggml.h` defines 42 as `Q2_0` with `QK2_0` 128 and 143 as
  `G8_0` ("Pestle exact ternary: four BF16 scales per 32 values", 16-byte
  blocks of 32, i.e. 4 bits per weight) — both extents fit. The file is
  consistent with the program it is made for. The scan's `native` guess
  (`prismml`) was wrong because mortar.cpp was not among the pinned runtimes.
  It is pinned since 2026-09-30 (`specs/runtimes/mortar_cpp.json`): the model
  is now judged in mortar.cpp, whose reader accepts it, and its verdict is `ok`.
- **reported**: — (nothing to report)
- **answer**: —

<!-- The extractor (tools/live-findings.py) regenerates the "observed" entries
     from a scan report; hand edits below survive by entry id. -->
