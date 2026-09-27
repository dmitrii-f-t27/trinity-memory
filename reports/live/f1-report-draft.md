Title: Q2_0 files fail to load in every reader we tried — tensor records declare type 42 but the offsets walk past the extents

Body:

Hi! We run an independent header-conformance checker for public ternary GGUF models (t27 Ternary Check Live, bit-exact against the real readers), and the `Q2_0` files of this repo and of Ternary-Bonsai-8B-gguf show a consistent refusal we'd like to understand.

**What we checked** (the header only — no weights were downloaded; every byte number below re-derives from the GGUF records themselves):

- In `Ternary-Bonsai-27B-Q2_0.gguf` (header sha256 e085e126..., LFS 868c1171..., 7,165,121,600 bytes) 498 ternary tensors declare GGML type 42 (`Q2_0`).
- Walking the records in offset order, the walk reaches `output_norm.weight` expecting byte **357,580,800** (what the declared `Q2_0` extents of the preceding tensors sum to under both stock llama.cpp and the PrismML fork's own loader) but the record's actual offset is **337,715,200** — a 19,865,600-byte mismatch, so every reader refuses the file. bitnet.cpp's different signed arithmetic expects 267,523,456 and refuses at the same record.
- The declared extents do fit `PQ2_0` — the fork's 128-weight-group layout — for 498 of 498 tensors. The same repos' `PQ2_0` files load fine in the PrismML fork (stock llama.cpp refuses them by type, as documented).

**Verified against the real readers, not a re-implementation:** we replay every cached header through the actual `gguf.cpp` of llama.cpp @ e6ab7c1a, the PrismML fork @ bdc23b56, and bitnet.cpp @ 390c3077; all three refuse both `Q2_0` files at `output_norm.weight` with the numbers above (110/110 answers agree with our walk on the 55-file scan).

**Our question:** are the `Q2_0` files intended to load in a specific runtime we haven't pinned? The PrismML docs warn that on stock llama.cpp "a `Q2_0` file loads silently and outputs gibberish" — here it refuses outright instead, which looks like the type field says `Q2_0` while the bytes are packed as `PQ2_0`. If that's right, would you consider re-tagging those tensors (or a re-export)? Happy to share the full per-record walk.

The finding, the protocol it followed (independent replay before any report) and the evidence hashes: https://github.com/dmitrii-f-t27/trinity-memory/blob/master/docs/live/findings.md (entry F-1). Our checker: https://github.com/dmitrii-f-t27/trinity-memory (v0.5.0, Ternary Check Live).
