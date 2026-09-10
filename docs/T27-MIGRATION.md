# Executable t27 migration

Tracking: [issue #1](https://github.com/dmitrii-f-t27/trinity-memory/issues/1).
The v0.3 implementation compiles executable `.t27` algorithms to native C,
Verilog and WebAssembly. Python exposes compatibility adapters; the original
v0.2 package is retained under `tests/reference` as an independent test oracle.
There is no Python fallback in the native executable or library.

## Build

The compiler revision is pinned by [`native/compiler.lock`](../native/compiler.lock):
[`gHashTag/t27@bff21b85b206a0dd367876343e56dcd8312d81ba`](https://github.com/gHashTag/t27/tree/bff21b85b206a0dd367876343e56dcd8312d81ba).
Prerequisites: Git, Rust/Cargo **1.94.0**, C11 and C++17 compilers, Python 3.10+
for build/test orchestration, and Icarus Verilog for explicit RTL runs.
Linux needs OpenSSL development libraries, Clang and LLD. On macOS, CommonCrypto
is used; Zig supplies the WebAssembly C target omitted by Apple's Clang.
Node.js executes the independent WebAssembly tests.

```sh
git clone https://github.com/gHashTag/t27.git build/compiler
git -C build/compiler checkout "$(cat native/compiler.lock)"
export T27_ROOT="$PWD/build/compiler"
rustup toolchain install 1.94.0 --profile minimal
make t27
make t27-test

build/t27/trinity-memory-t27 pack examples/trits.json build/example.tmem --codec dense5
build/t27/trinity-memory-t27 inspect build/example.tmem
build/t27/trinity-memory-t27 tensor-pack examples/tensorpack.json build/example.ttpk
build/t27/trinity-memory-t27 conformance --rtl --output build/native-conformance.json
build/t27/trinity-memory-t27 edge-demo --rtl --output build/native-edge.json --html build/native-edge.html
```

The output directory contains the native executable, platform shared library,
WebAssembly, generated RTL resources, compiler provenance and SHA-256 manifest.
Keep `rtl/resources/` beside the executable/library for installed RTL runs, or
set `TRINITY_T27_RTL_ROOT` to a generated resource directory. `iverilog` and
`vvp` must be on PATH. Missing tools, timeouts, nonzero simulator exits and
scoreboard mismatches are errors, never software substitutes for simulation.

The CLI implements all fourteen existing commands: `pack`, `unpack`, `inspect`,
`export-rtl`, `tensor-pack`, `tensor-unpack`, `tensor-inspect`, `serve`, `upload`,
`download`, `dot`, `benchmark`, `conformance`, and `edge-demo`.

## Source ownership

| Executable source | Behavior |
|---|---|
| `t27/codecs.t27`, `container.t27` | Six codecs, canonical padding, TMEM v1, CRC32, strict lengths and byte layout |
| `t27/compute.t27`, `sparsity.t27` | Checked dot/matrix arithmetic, scales, tie handling, entropy and lossy top-k preprocessing |
| `t27/json.t27`, `json_writer.t27` | UTF-8 JSON grammar, typed tokens, escaping and serialization |
| `t27/tensorpack*.t27` | Full TTPK framing, JSON/descriptors binding, metadata validation, deterministic float serialization and CLI documents |
| `t27/bridge.t27`, `client.t27`, `http.t27` | Object accounting, JSON-RPC methods/errors, base64, bounds, request validation and response handling |
| `t27/random.t27` | Python-compatible integer-seeded MT19937, rejection sampling and sampling without replacement |
| `t27/rtl_driver.t27` | Packet construction, simulator arguments, memory files, result parsing and scoreboard verification |
| `t27/experiments.t27`, `reports.t27` | Actual loopback Edge/Conformance, timed native benchmark, native report validation/rendering |
| `t27/cli.t27`, `python_api.t27` | Command parsing/dispatch and public API entry points |
| `t27/rtl/*.t27` | Decoder, storage, view and clocked dot-stream hardware |

`native/` supplies OS allocation, files, threads, sockets, monotonic time,
subprocess lifecycle, entropy and SHA-256. C++ standard-library primitives
convert binary floats to shortest decimal digits and parse decimals in the C
locale; `.t27` owns JSON grammar and formatting rules. Vendor Tcl, build scripts,
Python FFI object conversion and browser DOM code remain integration glue.
They are not alternative codec, protocol or compute implementations. Mixed
integer/float top-k ranking uses native IEEE/byte comparisons, preserving large
finite integer order without rounding integers to float before ranking.

The browser experiment calls WebAssembly compiled from the same generated
codec C. JavaScript handles controls and presentation. The HTML is standalone
and embeds both report data and the WebAssembly asset.

## Compatibility and explicit bounds

TMEM v1 and TTPK v1 byte layouts remain unchanged. The native Bridge exposes the
same local JSON-RPC methods and SDK adapter path. The checked-in v0.2 release
reports remain historical evidence; their timings are not native measurements.
New native reports identify their own runtime and timing scope.

The C ABI uses explicit pointer lengths/capacities. Callers must supply valid,
non-overlapping buffers. Do not infer a length from an upstream C slice pointer.
Generated `.abi.h` files describe actual types and signatures. Domain errors
are returned as status values; native Python adapters translate them to public
exceptions.

JSON nesting is bounded to 64 levels; integer tokens use i64/u64 rather than
Python's arbitrary-precision integers. Experiment seeds are signed i64 (explicit RTL seeds are u32; Edge reserves two
row-seed increments). Benchmark bounds are 4,194,304 trits and 1,000,000 repeats;
Conformance source input is at most 16 MiB. The CLI bounds source input and trit
allocations. TensorPack retains its metadata, tensor-count, rank and payload
limits. These are explicit native contracts, not a claim of equivalence for
unbounded Python objects or every `http.server` parsing quirk.

The stock storage build has 64 groups, or up to 320 logical trits. Larger
storage uses source-level literal-capacity specialization:

```sh
python3 tools/generate-t27-storage.py --trits 4096 --output build/t27/storage-4096
```

The specialization tests exercise capacities 1, 65, 820 and 4096 groups, with
selected logical counts up to 20,480. They do not exhaust every possible capacity. The dot simulation
API supports accumulator widths 12–32, as did the v0.2 runner; the underlying
native wiring adapter supports 2–32. Physical storage uses 16-bit words for the
8/10-bit ports. Simulation equivalence does not establish equivalent RAM
inference, utilization or timing. See [`t27/rtl/README.md`](../t27/rtl/README.md).

## Verification and compiler constraints

`tools/build-t27.sh` checks the compiler pin and rejects tracked compiler edits.
It checks lexer loss separately from parser completeness: the upstream parser
can report success after unsupported characters were discarded. It also rejects
incomplete code-generation markers and compiles C with strict warnings.
Generated C/Verilog algorithms are not patched.

`tools/test-t27.sh` runs independent native harnesses with ASan/UBSan, byte and
metadata comparisons against the frozen v0.2 oracle, real loopback HTTP/CLI
checks, actual Icarus tests and exhaustive browser-codec tests. It regenerates
C and compares output bytes. The GitHub workflow then tests Python adapters on
3.10, 3.12 and 3.14, the pinned upstream SDK, and an installed wheel outside the
checkout. See the workflow run for the result of a particular commit.

Known compiler workarounds at the pinned revision:

- `compile --backend python/rust` can emit Zig; explicit `gen-c` and
  `gen-verilog` entry points are used.
- Returned slices in local variables can become invalid C arrays; locals use
  pointer types and explicit lengths.
- Computed RTL array dimensions can become scalar registers; storage dimensions
  are specialized as literals in `.t27` before generation.
- `on_comb` can omit state dependencies; module-level combinational assignments
  are used in the native hardware.
- Unsupported widths can produce refused output with a successful process exit;
  the build checks generated text and runs the resulting hardware.
- Carriage-return escape generation differs from C expectations; HTTP emits
  explicit CR/LF bytes.

Upstream code inspected for these decisions:
[`compiler.rs`](https://github.com/gHashTag/t27/blob/bff21b85b206a0dd367876343e56dcd8312d81ba/bootstrap/src/compiler.rs),
[`main.rs`](https://github.com/gHashTag/t27/blob/bff21b85b206a0dd367876343e56dcd8312d81ba/bootstrap/src/main.rs).

Physical FPGA, DDR/HBM bandwidth, placed/routed timing, resource utilization,
power and model quality remain separate measurement work. No such measurement
is claimed by this migration.
