# Ternary Check Live

Issues [#48](https://github.com/dmitrii-f-t27/trinity-memory/issues/48)–[#55](https://github.com/dmitrii-f-t27/trinity-memory/issues/55),
epic [#57](https://github.com/dmitrii-f-t27/trinity-memory/issues/57).

**What it answers.** For every public ternary GGUF file on the Hugging Face Hub: will
llama.cpp, the PrismML fork and bitnet.cpp read it, and if not, why; which layout its
ternary tensors really hold; are the PrismML fork's Hadamard metadata valid, and does a
runtime ignore a rotation the file declares. The answer comes from the file's header
alone: the weights are never downloaded, and no model is run.

**How.** `python3 -m trinity_memory.live` (or `make live-scan`) finds repositories through
the public Hub API: by name (ternary, bitnet, bonsai, 1.58, TQ1_0, TQ2_0, PQ2_0, PTQ1_0,
I2_S, Q2_0, Q1_0), and among the 2,000 most downloaded GGUF repositories by file name.
It keeps those with at least `--min-downloads` downloads over 30 days, picks the files
named after a ternary layout (or up to three quantized files when none is), and reads
each header with HTTP range requests at the commit the Hub reports, until the t27
reader stops asking for bytes: at most twice the header, never past 256 MiB. Access is
anonymous and throttled (`--interval`, `Retry-After` honoured). Byte-identical files
(the same LFS SHA-256) are read once. The report (`trinity.ternary-check-live.v1`) goes
to `build/live/scan.json`; `--recheck report.json` makes its verdicts again from the
cached headers, offline.

**Who decides.** Executable t27 (`t27/live.t27`), over tables of the pinned runtimes
(`t27/runtimes.t27`, generated from `specs/runtimes/*.json` by
`tools/generate-runtime-tables.py`: ggml type ids, block sizes, byte-count rules and
architecture names, each with its commit and file blobs). `tlv_walk` reads a header as
that runtime's `gguf.cpp` reads it, rule by rule and in its order; `tlv_runtime` adds
the model loader's file-end and architecture rules and, in the PrismML fork, the
Hadamard rules (`tlv_hadamard`); `tlv_file_verdict` folds them for the runtime the file
is written for. Python only moves bytes.

**Checked against the real readers.** `sh tools/live-replay.sh` builds
`tests/upstream/gguf_replay.c` against the GGUF reader of every pinned runtime, and
`python3 -m trinity_memory.live --replay build/replay` feeds each cached header to them
(written at the start of a sparse file of the real size) and records where their
answer and the t27 verdict agree. The weekly workflow `ternary-check-live.yml` does both
and reports the agreement.

**Verdicts per runtime.** `accepts`: its reader and loader rules accept the header;
`refuses` with a status token and, where it has one, the record the reader names;
`ignores_rotation`: it accepts a file that declares `prism.hadamard.*` but does not
apply the rotation, so the model computes the wrong function.

**Verdict per file** (in the runtime it is written for: the PrismML fork for `prism.`
keys or ids 142 and 143, bitnet.cpp for `bitnet-b1.58` or ids 36 and 38, else
llama.cpp): `ok`, `refused`, `no_ternary_layout`, `undecided` (past a scanner limit),
`unread` (the header could not be read) or `error`. `problems` lists, per status, the
ternary records whose bytes do not fill their place and the layout that fills it
instead (for example `extent` with `PQ2_0`: type 42 declared, 128-weight groups stored,
PrismML-Eng/llama.cpp#167). Status tokens are in
[`specs/formats/OWNERS.md`](../specs/formats/OWNERS.md#status-classes).

**What a verdict does not say.** Nothing about model quality, speed or the tensors'
values. `accepts` means the header passes the rules restated here, not that the model
runs correctly on every backend. A file without `prism.hadamard.*` keys passes even when
its weights were stored rotated, because a header cannot show that
(PrismML-Eng/llama.cpp#242). Nothing is reported to a file's owner before an independent
confirmation and the founders' approval (issue #55).
