# Ternary Check CLI contract, version 1

Contract id: `trinity.ternary-check-cli.v1`.

This contract says how the Ternary Check runs a decoder that is not part of
this repository. The runner is `trinity-memory ternary-check run`, which the
[GitHub Action](README.md) calls. It feeds every vector of
`conformance/formats_*.json` to the decoder, one process per call, and
compares what comes back with what the vector expects.

A decoder can be written in any language. It reads and writes files with the
byte layouts below. It reports a refusal with an exit status and a class
token.

The reference implementation is
`python3 -m trinity_memory ternary-check`, also installed as
`trinity-memory ternary-check`. It reads and writes through the t27 functions
`tk_decode` and `tk_encode` of `t27/ternary_contract.t27`, and it passes every
vector: `tests/test_ternary_check_run.py` checks this natively, and
`tests/ternary_contract_wasm_replay.mjs` checks it in WASM.

The runner compares outputs and chooses each verdict with the same t27 module:
`tk_compare_bytes`, `tk_compare_words`, `tk_parse_flags`, `tk_flag_state`,
`tk_verdict` and `tk_fails`. Python only moves files around.

## Calls

The runner takes the decoder command, such as `./my-decoder` or
`python3 decode.py`, and appends the call's arguments to it. Each call runs:

- in a fresh, empty working directory;
- with stdin empty;
- with stdout ignored;
- with stderr kept only for calls that fail, in the report's `run` block;
- under a time limit, 60 s by default (`--timeout`).

A relative path in the decoder command that names an existing file is made
absolute against the directory the runner starts in. All file arguments are
absolute paths.

| command | required | purpose |
|---|---|---|
| `PROG formats` | no | Print one line `decode NAME` or `encode NAME` per supported operation and format, then exit 0. If this command exits nonzero or prints no such line, the runner assumes `decode` for every format and `encode` for none. Formats and operations the decoder does not list are reported as `not_run: unsupported`. |
| `PROG decode FORMAT COUNT INPUT VALUES SCALES [key=value...]` | yes | Read the stored bytes in INPUT. Write COUNT values to VALUES and the scale words to SCALES. |
| `PROG encode FORMAT COUNT VALUES SCALES OUTPUT [key=value...]` | no | Read COUNT values and, for the block formats and I2_S, the scale words. Write the stored bytes to OUTPUT. |

`COUNT` is the number of weights, in decimal. A decoder must ignore any
`key=value` argument it does not use. Later minor versions may add keys, but
never remove or change them.

## Formats

| FORMAT | upstream layout | INPUT (decode) / OUTPUT (encode) | COUNT | keys | SCALES |
|---|---|---|---|---|---|
| `TQ1_0` | llama.cpp, ggml type 34, 256 weights per 54-byte block | whole blocks | multiple of 256 | none | one fp16 word per block |
| `TQ2_0` | llama.cpp, ggml type 35, 256 weights per 66-byte block | whole blocks | multiple of 256 | none | one fp16 word per block |
| `Q2_0` | llama.cpp, ggml type 42, 64 weights per 18-byte block | whole blocks | multiple of 64 | none | one fp16 word per block |
| `Q1_0` | llama.cpp, ggml type 41, 128 weights per 18-byte block, values ±1 | whole blocks | multiple of 128 | none | one fp16 word per block |
| `PQ2_0` | PrismML fork, ggml type 142, 128 weights per 34-byte block | whole blocks | multiple of 128 | none | one fp16 word per block |
| `PTQ1_0` | PrismML fork, ggml type 143, 128 weights per 28-byte block | whole blocks | multiple of 128 | none | one fp16 word per block |
| `I2_S` | bitnet.cpp, ggml type 36, x86 ACT_PARALLEL layout | COUNT/4 code bytes, the f32 scale, then 28 trailer bytes | multiple of 128 | none | one f32 word |
| `HF_PACKED` | transformers BitNet `uint8` weights `[rows/4, cols]` | the packed tensor | `rows * cols` | `rows`, `cols`, `scale_kind`, `scales` | the `weight_scale` word, copied back |
| `MLX2` | MLX 2-bit affine `uint32` words `[rows, cols/16]` | the words' bytes | `rows * cols` | `rows`, `cols`, `group`, `scale_kind`, `scales`, `biases` | one word per group, copied back |
| `ONNX2` | ONNX Runtime `MatMulNBits`, `bits=2`, B `[N, k_blocks, block_size/4]` | B | `n * k` | `n`, `k`, `block_size`, `scale_kind`, `scales`, optional `zero_points` | one word per block, copied back |

The byte-level rules for each format are in `specs/formats/<family>.t27`.
Those specs cite the pinned upstream sources in
`specs/formats/upstream.lock.json`.

### Keys

- `rows`, `cols`: the size of the weight matrix after unpacking.
- `group`: the MLX group size: 32, 64 or 128.
- `n`, `k`, `block_size`: ONNX Runtime's N, K and block size.
- `scale_kind`: `F16`, `BF16` or `F32`. It sets the type of the words in the `scales` and `biases` files.
- `scales`, `biases`, `zero_points`: paths to the separate tensors, in their stored bytes. The scale and bias files hold little-endian words of `scale_kind`. The `zero_points` file holds ONNX Runtime's packed `uint8` zero points, 2 bits per block. When there is no `zero_points` key, every block uses the default zero point, 2.

## Byte formats

- **VALUES** holds COUNT signed bytes, one per weight, in two's complement.
  - The order is the order the upstream dequantizer writes weights in. For the block formats, weight `e` of block `b` is at `b * block_weights + e`. For `HF_PACKED`, `MLX2` and `ONNX2`, the matrix is unpacked in row-major order: `[rows, cols]`, or `[n, k]` for ONNX2.
  - Each value is the stored code minus the format's zero point:
    - 1 for the 2-bit codes of `TQ2_0`, `Q2_0`, `PQ2_0`, `I2_S`, `HF_PACKED` and `MLX2` (MLX's ternary convention is `bias = -scale`);
    - the block's zero point for `ONNX2`;
    - the base-3 digit minus 1 for `TQ1_0` and `PTQ1_0`;
    - ±1 for the bits of `Q1_0`.
  - A code outside {-1, 0, +1} is written as its value, never clamped. Code 3 of a 2-bit layout is written as +2. Under ONNX2's default zero point, code 0 is written as -2.
- **SCALES** holds the scale words, little-endian, exactly as stored.
  - For the block formats: one 2-byte fp16 word per block, in block order.
  - For I2_S: one 4-byte f32 word.
  - For `HF_PACKED`, `MLX2` and `ONNX2`, the scales are a separate tensor. The decoder writes back the words it applies, in group order, each 2 bytes wide (`F16`, `BF16`) or 4 bytes wide (`F32`):
    - `HF_PACKED`: 1 word;
    - `MLX2`: `rows * cols / group` words;
    - `ONNX2`: `n * ceil(k / block_size)` words.
  - For `encode`, the SCALES input of these three formats is empty.
- **INPUT** and **OUTPUT** hold exactly the stored bytes. For I2_S that includes the 28 trailer bytes, which the encoder writes as zeros.

## Refusals

To refuse an input, the decoder does two things:

1. It exits with a nonzero status. 1 is recommended.
2. It writes a file named `error` in its working directory. The file holds exactly one class token, optionally followed by whitespace.

The tokens are the keys of `constants.errors` in `conformance/formats_*.json`.
They match the `TF_ERR_*` statuses of `t27/formats.t27`.

| token | status | meaning |
|---|---|---|
| `format` | -50 | unknown format |
| `length` | -51 | the byte count does not fit COUNT or the shape, COUNT is not whole blocks, or a separate tensor has the wrong number of words |
| `capacity` | -52 | an output buffer is too small |
| `code` | -53 | (encode) a value the format cannot store |
| `padding` | -54 | a nonzero padding digit in a TQ1_0 or PTQ1_0 qh byte |
| `truncated` | -55 | a container header is cut short |
| `container` | -56 | a malformed container |
| `not_found` | -57 | the tensor is not in the container |
| `scale_nonfinite` | -58 | a NaN or infinite scale (or MLX bias) |
| `type_ambiguous` | -59 | a GGUF type id whose meaning differs between the file's namespace markers |
| `layout_unsupported` | -60 | a layout without a storage contract (bitnet.cpp TL1, TL2) |
| `misaligned` | -61 | a misaligned tensor offset |
| `extent` | -62 | tensor bytes run into another tensor or past the end of the file |

A refusal should leave VALUES, SCALES and OUTPUT unwritten; the runner does
not read them after a refusal. The class of each
reject vector is its `error_class`. The vectors state which check comes first
when an input breaks more than one rule: the payload reader's checks, and then
the scale check.

## Flags

Flags describe conditions that upstream readers accept silently. They never
change a decoded value.

After a successful `decode`, a decoder may write a file named `flags` in its
working directory. The file has one line per flag, `<token> <count>`, with the
count in decimal. A missing file means "not reported". An empty file reports
every count as 0.

| token | counts |
|---|---|
| `outside_ternary` | weights whose value is outside {-1, 0, +1} |
| `noncanonical_base3` | TQ1_0 and PTQ1_0 bytes that the upstream quantizer never writes |
| `scale_negative` | negative scale words |
| `scale_zero` | zero scale words |
| `trailer_nonzero` | nonzero bytes among I2_S's 28 trailer bytes |
| `padding_nonzero` | nonzero ONNX2 codes in the padding of a row's last block |
| `affine_not_ternary` | MLX2 groups whose bias is not `-scale` |

## Outcomes

Each call ends in one of the outcomes below. `tk_verdict` computes it. The
`--fail-on` policy (`tk_fails`) decides whether that outcome fails the run.

| outcome | when | fails under `mismatch` | fails under `silent` |
|---|---|---|---|
| `match` | exit 0; values, scale words or stored bytes are equal; any reported flags are equal | no | no |
| `match_unflagged` | as `match`, but no `flags` file and the vector expects a nonzero flag | no | no |
| `rejected` | a reject vector, refused with its class token | no | no |
| `mismatch` | exit 0 on a vector that must decode, with values, scale words, stored bytes or reported flags that differ (missing or extra bytes count) | yes | no |
| `wrong_class` | a reject vector, refused with another class token | yes | no |
| `unexpected_reject` | a vector that must decode, refused with a class token | yes | no |
| `unclassified` | nonzero exit without an `error` file that names a class | yes | no |
| `silent` | exit 0 on a vector that must be refused | yes | yes |
| `crashed` | stopped by a signal or by the time limit | yes | no |

With `--fail-on never`, nothing fails the run. The runner exits 0 when no call
fails, 1 when one does, and 2 when the run itself cannot proceed: unreadable
vectors, an unknown format in `--formats`, or a decoder that cannot be
started.

"Silent" vectors (`silent_class`) are inputs that no reader can tell apart from
valid data, such as a corrupted payload or a layout confusion. For a decoder
they are ordinary decode vectors: the expected values are the wrong weights
that every correct reader produces.

## Scope of version 1

- **Covered:** every payload vector of the six `formats_*.json` files, both decode and encode. Positive, reject, flag and silent vectors are all included. The encode round trip runs for each positive vector marked `"encode": true`.
- **Not covered:** the GGUF and safetensors header vectors, which test lookups in a container. They are listed in the report under `not_run` with reason `container`.

## Report

`--report` writes JSON with schema `trinity.ternary-check-run.v1`:

- `summary`: the counts per outcome and per format, `failures` and `passed`;
- `cases`: one record per call, in vector order. Each record holds the file, vector id, class, operation, format and outcome. For a difference, it also gives the count, the first differing index, and the expected and actual value at that index;
- `not_run`: the calls that did not run, each with its reason;
- `vectors`: the files used, with their SHA-256;
- `decoder_formats`: the formats and operations the decoder declared.

Everything outside the `run` block is reproducible. Two runs with the same
decoder and vectors give byte-identical JSON once `run` is removed. The `run`
block holds the timestamp, duration, platform, decoder command and the stderr
of failing calls.

`--summary` appends the same numbers as Markdown.

## Example

Suppose you have a C library with a TQ2_0 row decoder. A short wrapper
program can:

1. read `argv[4]`;
2. call the decoder;
3. write `argv[5]` (one `int8_t` per weight) and `argv[6]` (the fp16 `d` of each block);
4. on a bad length, print `length` to `error` and exit 1.

Then run:

```sh
trinity-memory ternary-check run --decoder ./tq2-wrapper --formats TQ2_0 \
    --report tq2.json --summary tq2.md
```

`tests/action/wrong_group_decoder.py` is a deliberately wrong decoder that
fails this check.
