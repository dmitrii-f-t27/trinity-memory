# Browser GGUF check

`site/live/check.html` inspects a public Hugging Face GGUF with the same
`t27/live.t27` reader and runtime rules used by the native scanner. The module
worker loads `formats.wasm`; JavaScript only obtains bytes and presents results.
The model verdict is for one file. Split filenames explicitly withhold a model
verdict across parts; use the CLI for a complete split-model check.

Branch/revision names are resolved to immutable Hub commits before reading.
Requests omit credentials, append bounded byte ranges and require HTTP 206 and
an exposed, consistent Content-Range. A full-file HTTP 200 response is cancelled.
Each request has a 30-second timeout; Cancel terminates the worker. Header reads
stop at 256 MiB. Prefixes can include some weight bytes, but weights are not
executed or evaluated. Header acceptance does not establish successful inference.

Build with `sh tools/build-t27.sh`, copy `build/t27/formats.wasm` into `site/live`,
and serve that directory over HTTP. The WASM file is generated, not committed.
`tools/live-page.py page` copies the canonical HTML and JavaScript assets rather
than replacing the checker with an embedded placeholder.

## Verification on 2026-09-28

- `node tests/live_formats_wasm.mjs`: 16 headers × 3 runtimes agree with every
  native walk/model field, including truncation, malformed headers, rotation,
  large headers and unsigned 64-bit values. Mock HTTP checks cover appended
  ranges, revision pinning, bounded bodies and cancellation of full responses.
- Existing formats WASM replay: 171 vectors in 6 files, 90 classified rejections,
  16,777,216 weights in one call; zero imports.
- BrowserOS neo, local HTTP page, real anonymous Hub requests: Microsoft
  `bitnet-b1.58-2B-4T-gguf`, revision
  `a1f2f1c765812aa8af3f6eda4a313707064bba15`, `ggml-model-i2_s.gguf`.
  The checker read 8,388,608 of 1,187,801,280 bytes; bitnet.cpp accepted the
  metadata with 210/210 fitting ternary records. The pinned llama.cpp and
  PrismML readers refused the tensor type. This also exercises real browser
  CORS, not just a Node fetch simulation.

The existing Monday/manual scan workflow had duplicate YAML keys and targeted
an absent gh-pages branch despite Pages being configured for Actions. It now
uploads a Pages artifact and deploys only after success on master. PRs build
and test without publishing. Code pushes reuse the last published scan if it
exists, otherwise keep the committed snapshot; only scheduled/manual runs
perform discovery and upstream replay. Fetch failures other than a first-run
404 and drift errors stop publication, preserving the previous site.
