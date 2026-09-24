# Ternary Check Live

Issues [#48](https://github.com/dmitrii-f-t27/trinity-memory/issues/48)–[#55](https://github.com/dmitrii-f-t27/trinity-memory/issues/55),
epic [#57](https://github.com/dmitrii-f-t27/trinity-memory/issues/57).

**What it answers.** For every public ternary GGUF model on the Hugging Face Hub, at the
pinned commits of llama.cpp, the PrismML fork and bitnet.cpp: will each of them read it,
and if not, why; which layout its ternary tensors really hold; are the PrismML fork's
Hadamard metadata valid, and does a runtime ignore a rotation the model declares. A model
is one GGUF file, or the parts of a split model (`<prefix>-KKKKK-of-NNNNN.gguf`), which
are judged together, as llama.cpp's loader reads them. The answer comes from the files'
headers alone: no model is run.

**How.** `python3 -m trinity_memory.live` (or `make live-scan`) finds repositories through
the public Hub API: by name (ternary, bitnet, bonsai, 1.58, 1_58, TriLM, Falcon-E, TQ1_0,
TQ2_0, PQ2_0, PTQ1_0, I2_S, Q2_0, Q1_0, among GGUF repositories), and among the most
downloaded GGUF repositories (`--gguf-pages` pages of up to 1,000) by file name. It keeps
those with at least `--min-downloads` downloads over 30 days and picks up to 12 models
named after a ternary layout (or up to three quantized models when none is; the report
counts the models it did not check). Each file's header is read with HTTP range requests
at the commit the Hub reports, until the t27 reader stops asking for bytes: the first read
is 1 MiB (the whole file when it is smaller), each later read at most doubles what was
read, and nothing past 256 MiB of a file is read, so the weights are not downloaded.
Access is anonymous and throttled (`--interval`, `Retry-After` honoured); a request that
fails, also in the middle of its body, is retried. Byte-identical models (the same LFS
SHA-256 for every file) are read once. Headers are cached by their file's LFS SHA-256.
The report (`trinity.ternary-check-live.v2`) goes to `build/live/scan.json`;
`--recheck report.json` makes its verdicts again from the cached headers, offline, and
checks each header against the SHA-256 the report recorded for it.

**Who decides.** Executable t27 (`t27/live.t27`), over tables of the pinned runtimes
(`t27/runtimes.t27`, generated from `specs/runtimes/*.json` by
`tools/generate-runtime-tables.py`: ggml type ids, block sizes, byte-count rules, the
architecture names each model mapping turns into a model class and those refused as a
main model, each with its commit and file blobs). `tlv_walk` reads a header as that
runtime's `gguf.cpp` reads it, rule by rule and in its order, including the reader's own
limits (strings and arrays of at most 2^30, non-negative counts, the data section inside
the file) and bitnet.cpp's two differences (no guard for tensors without elements;
`general.alignment` read with `gguf_get_val_u32`); `tlv_model` applies the model loader in
its order: the architecture key's type, the file-end rule, the split rules across the
parts, the architecture mapping, in the PrismML fork the Hadamard rules (`tlv_hadamard`,
over the tensors of every part), and clip refused as a main model; `tlv_native` names the
runtime a model is written for and `tlv_file_verdict` folds the verdict there. Where a
reader's own signed arithmetic overflows (an element count past 2^63, bitnet.cpp's TL2
size of an absurd shape) the outcome depends on the compiler, and t27 gives no verdict
(`limit`). Python only moves bytes.

**Checked against the real readers.** `sh tools/live-replay.sh` builds
`tests/upstream/gguf_replay.c` against the GGUF reader of every pinned runtime, and
`python3 -m trinity_memory.live --replay build/replay` feeds each cached header to them
(written at the start of a sparse file of the real size) and records, per file and
runtime, whether their answer and the t27 reader's (`tlv_walk`'s `reader`: the loader's
rules are not part of it) agree. A t27 walk with no verdict is counted apart, and so is a
reader that crashes (an assertion ends it) or runs out of time. The report names the
runtimes whose reader was built and replayed. The weekly workflow
`ternary-check-live.yml` does both; its log and step summary hold counts only.

**Verdicts per runtime.** `accepts`: its reader and loader rules accept the model;
`refuses` with a status token and, where it has one, the file and the record it names;
`ignores_rotation`: it accepts a model that declares `prism.hadamard.*` but does not
apply the rotation, so the model computes the wrong function; `no_verdict` with
`truncated` or `limit`.

**Verdict per model**, in the runtime it is written for: the PrismML fork when a key
starts `prism.`; bitnet.cpp when the architecture is `bitnet-b1.58`; the fork when a
tensor has id 142 or 143; bitnet.cpp when one has id 36 or 38, or has id 42 (Q2_0 in
llama.cpp, TL2 in bitnet.cpp) and bitnet.cpp accepts the model while llama.cpp does not;
else llama.cpp. A model written for software none of them is (a type id none of them
defines, an architecture none of them maps, no architecture at all) is `other_runtime`.
The verdicts: `ok`, `refused`, `no_ternary_layout`, `undecided` (no verdict: a scanner
limit, or arithmetic whose outcome depends on the compiler), `other_runtime`, `unread`
(a header could not be fetched) or `error`. `problems` lists, per status, the ternary
records whose bytes do not fill their place and the layout that fills it instead (for
example `extent` with `PQ2_0`: type 42 declared, 128-weight groups stored,
PrismML-Eng/llama.cpp#167). TL1 and TL2 count as ternary layouts: their places are
checked, their weights have no storage contract in `t27/formats.t27`. Status tokens are
in [`specs/formats/OWNERS.md`](../specs/formats/OWNERS.md#status-classes).

**What a verdict does not say.** Nothing about model quality, speed or the tensors'
values. `accepts` means the headers pass the rules restated here, not that the model
loads and runs: the loader's per-architecture hyperparameters, vocabulary and tensor
shapes are not checked. A model without `prism.hadamard.*` keys passes even when its
weights were stored rotated, because a header cannot show that
(PrismML-Eng/llama.cpp#242). Nothing is reported to a file's owner before an independent
confirmation and the founders' approval (issue #55).
