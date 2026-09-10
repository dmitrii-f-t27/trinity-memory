# Python API backed by native t27

Version 0.3.0 preserves the public codec, TMEM, TensorPack, Bridge, sparsity,
benchmark, conformance, Edge and RTL functions as Python representation adapters.
The implementations execute in the shared library generated from `t27/*.t27`.
Python converts objects to bounded C arrays or JSON, manages buffers, reads and
writes files, and launches the native CLI. It contains no codec, checksum,
TensorPack schema, Bridge dispatcher, dot-product or report implementation.

`tools/build-t27.sh` builds the library and CLI using the exact compiler commit
in `native/compiler.lock`. From a checkout, the adapters find `build/t27`.
Platform wheels carry their own `_native_runtime` library, CLI, compiled WASM,
and generated RTL resources. The wheel uses `py3-none-<platform>` tags because
ctypes is independent of the CPython extension ABI. It is not a universal wheel.
Building a wheel verifies the recorded source and artifact hashes; source edits
require a new native build. `_native_runtime/manifest.json` lists hashes of every
packaged native resource. An absent or incompatible library is an error. There
is no Python fallback.

The runtime can be selected explicitly with `TRINITY_MEMORY_NATIVE_LIBRARY`
and `TRINITY_MEMORY_NATIVE_CLI`. The RTL resource override is
`TRINITY_T27_RTL_ROOT`; the previous `TRINITY_MEMORY_RTL_ROOT` name remains a
fallback alias. An explicit missing resource directory does not silently select
another copy. Actual RTL execution additionally requires Icarus `iverilog` and
`vvp`; the wheel does not install simulator tools.

The native JSON parser has a maximum depth of 64 and represents integers using
signed i64 or unsigned u64. Larger JSON integer literals are rejected; they are
not rounded or saturated. Experiment seeds are signed i64. RTL seeds remain
unsigned 32-bit. Python bool values are excluded from integer lane inputs.
Lossy projection marshals integer magnitudes as big-endian bytes and compares
them exactly in t27, including mixed float/integer inputs above 2**53. Integer
magnitudes are bounded to 128 bytes and must have a finite f64 representation,
consistent with the original finite-real input requirement. No rounding is used
for the ranking of integer inputs.
TensorPack retains its bounded metadata, payload, tensor-count and decoded-trit
limits. Python implementation details such as a mockable `_dispatch`, `_http`
object, or mutable parser constants are not part of the native interface.
`BridgeServer.url`, `stored_bytes`, and `object_count` expose the relevant live
native state. Exact exception prose and CLI JSON whitespace may differ; error
classes, RPC numeric codes, container bytes and logical results are checked.

Verification:

```sh
python -m unittest discover -s tests -v
python -m unittest discover -s tests/reference/legacy_tests -v
python -m build --wheel
python tests/native/test_installed_wheel.py --wheel dist/<platform-wheel>.whl --rtl
```

The independent Python v0.2.0 implementation is frozen under
`tests/reference/trinity_memory_reference`, with its file hashes recorded in
`tests/reference/manifest.json`. Differential tests import that distinct package.
Reference implementations and original RTL are absent from production wheels.
The installed-wheel test runs in a new virtual environment outside the checkout,
verifies all packaged hashes, and exercises TMEM, TensorPack, actual loopback
HTTP, the CLI and optional actual Icarus simulation. These checks do not measure
physical FPGA, RAM, DDR, power, or trained-model accuracy.
