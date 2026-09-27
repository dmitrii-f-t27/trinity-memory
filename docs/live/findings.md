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

- **status**: approved (founders' approval 2026-09-27; the report below is
  the agreed wording — confirmed by the replay before that: 110 agree,
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
- **reported**: —
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

<!-- The extractor (tools/live-findings.py) regenerates the "observed" entries
     from a scan report; hand edits below survive by entry id. -->
