# mortar.cpp as the fourth pinned runtime (issue #51), 2026-09-30

- `scan-replay-four-readers.json`: the six repositories of findings F-4..F-9 (12 GGUF
  files), t27 verdicts for four runtimes and the replay of the cached headers on the
  real readers built by `tools/live-replay.sh` (llama.cpp e6ab7c1a, PrismML fork
  bdc23b56, bitnet.cpp 390c3077, mortar.cpp 236418ec): 48 of 48 answers agree.
  `Doses-AI/Pestle-27B-Ternary-GGUF` is `ok`, written for mortar.cpp (978 records in
  128-weight groups, 2 of G8_0); the eight legacy `Q2_0` files stay `refused`
  (llama.cpp and the fork refuse them; mortar.cpp's reader accepts them).
- `fuzz-four-readers.txt`: the differential fuzzer of 2026-09-24 with a fourth reader
  and a generation mode for mortar.cpp's type table (`fuzz4.py`, seeds
  600000-799999): 200,000 headers, 800,000 comparisons, 0 disagreements; 3,218
  without a t27 verdict (`limit`: signed arithmetic whose outcome is the compiler's).
  Cases by mode: mortar 60,066, main 60,157, fork 39,986, bitnet 39,791.
- The rest of the 2026-09-24 scan was rechecked from the cached headers available
  locally (120 of 467 models): one verdict changed, the Doses-AI model above.
  The full scan with four readers runs in CI.
