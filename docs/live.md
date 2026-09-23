# Ternary Check Live

Issue [#48](https://github.com/dmitrii-f-t27/trinity-memory/issues/48) and the
[epic #57](https://github.com/dmitrii-f-t27/trinity-memory/issues/57).

**What it answers.** For every public ternary GGUF file on the Hugging Face Hub:
is the header well-formed, which layout do its ternary tensors really hold, and are
the PrismML fork's Hadamard metadata valid? The answer comes from the file's header
alone; tensor data are never downloaded, and no model is run.

**How.** `python3 -m trinity_memory.live` (or `make live-scan`) finds repositories
through the public Hub API by name (ternary, bitnet, bonsai, 1.58, TQ1_0, TQ2_0,
PQ2_0, PTQ1_0, I2_S, Q2_0, Q1_0) with at least `--min-downloads` downloads over 30
days, picks the files named after a ternary layout (or the smallest GGUF file when
none is), and reads each header with HTTP range requests at the commit the Hub
reports, until the t27 reader stops asking for bytes. Access is anonymous and
throttled (`--interval`, `Retry-After` honoured). Byte-identical files (the same LFS
SHA-256) are read once. The report is `reports/live/scan.json`
(`trinity.ternary-check-live.v1`); `--recheck report.json` makes its verdicts again
from the cached headers, offline.

**Who decides.** Every verdict is made by executable t27: `tf_gguf_nth` for each
tensor record, `tlv_record_check` for each record (the rules of `tf_gguf_check`,
then the contiguous offsets `gguf.cpp` requires), `tlv_layout_fit` for a record
whose bytes do not fit its declared type, and `tlv_hadamard` for the
`prism.hadamard.*` keys (`t27/live.t27`). Python only moves bytes.

**Verdicts per file.**

| Verdict | Meaning |
|---|---|
| `ok` | every ternary record passes, and the Hadamard metadata, if present, pass the fork's rules |
| `rejected` | at least one ternary record or the Hadamard metadata fail; `rejections` counts each status token with examples, and for `extent` and `offsets` the layout the bytes really fit |
| `no_ternary_layout` | the file holds no ternary layout this repository has a contract for |
| `container` | the header does not parse |
| `unread` | the header could not be read (network, size, gated repository) |

Status tokens are listed in [`specs/formats/OWNERS.md`](../specs/formats/OWNERS.md#status-classes).

**What a verdict does not say.** It says nothing about model quality or speed, and
nothing about the tensors' values. `ok` means the header is consistent with the
layouts it declares; it does not mean a given runtime will load the file (issue #51),
and a file without `prism.hadamard.*` keys passes even when its weights were stored
rotated, because a header cannot show that (PrismML-Eng/llama.cpp#242). Nothing is
reported to a model's owner before an independent confirmation and the founders'
approval (issue #55).
