# TMEM v1 and raw payload specification

This is a Trinity experiment format, not GGUF, llama.cpp TQ1_0, PTQ1_0,
or a checkpoint conversion tool. Version 1 stores only flat integer trits.
Model shape, tensor names, layer scales and training metadata are not present.
The original real-valued model cannot be reconstructed from trits alone.

## Logical values and ordering

Input values are integers `-1`, `0`, `+1`. JSON floats and booleans are rejected.
Within a base-3 group, digit `d_i = trit_i + 1` and
`word = Σ d_i × 3^i`, with the earliest weight least significant.
RTL output lanes use `00 = 0`, `01 = +1`, `10 = -1`; `11` is invalid.
These two encodings serve different purposes and are not interchangeable.

The final group is padded with **logical zero trits**. For dense base-3,
these have digit 1; therefore, zero-filled bytes do not encode zero weights.
For example, five zero trits encode as `121 = 0x79`, five `-1`s as `0x00`,
and five `+1`s as `242 = 0xf2`. `[-1,0,1,-1,1]` encodes as `183 = 0xb7`.

## Codecs

| ID | Name | Weights/group | Bits/group | Valid group states | Asymptotic payload bpw |
|---|---|---:|---:|---:|---:|
| 0 | baseline2 | 4 | 8 | 3⁴ = 81 | 2 |
| 1 | dense5 | 5 | 8 | 3⁵ = 243 | 1.6 |
| 2 | dense17 | 17 | 27 | 3¹⁷ = 129,140,163 | 1.588235… |
| 3 | dense22 | 22 | 35 | 3²² = 31,381,059,609 | 1.590909… |
| 4 | sparse41 | 4 | 4 | 1 + 4×2 = 9 | 1 |
| 5 | sparse82 | 8 | 8 | 1 + 8×2 + C(8,2)×4 = 129 | 1 |

`baseline2` packs four RTL lane encodings into a byte, earliest lane lowest.
Dense words must be below `3^group_size`. Sparse words must be below their
number of states. All unused codes are rejected.

Sparse constraints mean **at most K nonzeros in each consecutive block of N**.
The codec rejects incompatible groups and never prunes. `(N,K)` here is explicitly
`(block length, maximum nonzeros)`, distinct from a paper's retained-weight `N:M` notation.

Sparse codebook order:

1. Increasing number of nonzeros, starting with the all-zero group.
2. Positions in lexicographic order of combinations.
3. Increasing sign mask; mask bit j corresponds to the j-th selected position,
   with 0 for −1 and 1 for +1.

Thus `sparse41`: 0 → all zeros; `1 + 2*position` → one −1;
`2 + 2*position` → one +1. Codes 9–15 are invalid.
`sparse82` permits 0, 1 or 2 nonzeros and needs 8 bits. An exactly-two-nonzero
codebook could use 7 bits for 112 states, but would implement a different contract.

## Continuous bit stream and storage accounting

Group words are concatenated least significant bit first; bytes are emitted
from the low end. There is **no byte padding between groups**, including
27-bit, 35-bit, and 4-bit words. Only unused high bits of the final byte are zero.

For W logical weights, G weights/group, B bits/group:

```text
groups = ceil(W / G)
payload_bytes = ceil(groups * B / 8)
payload_bpw = 8 * payload_bytes / W
container_bpw = 8 * (payload_bytes + 24) / W
```

For W=0 the payload is empty, the container is 24 bytes, and bpw is undefined
(`null` in metadata). Trailing bytes, nonzero padding trits, and nonzero unused
bits are rejected, giving a unique canonical representation.

`dense22` is a **35-bit stream codec**, not an implemented BRAM mapping.
If each 22-weight group occupies a separate 36-bit word, allocation is
36/22 ≈ 1.63636 bpw. Independently byte-aligned groups would cost
40/22 ≈ 1.81818 bpw. The block-RAM bench measures such a word on the AX7203 with
four dense5 bytes and a dense2 nibble in the parity bits instead of the base-3^22
code: 45 RAMB36E1 for 1 013 760 trits against 55 at two bits per trit, 136 LUTs to
decode a word ([hardware.md](hardware.md), "Block-RAM trit packing").
RTL implemented here covers dense5, sparse41 decoding and dense5/baseline5 streaming.

## File container

All multibyte integers are unsigned little endian. Header size: **24 bytes**.

| Offset | Bytes | Meaning |
|---:|---:|---|
| 0 | 4 | ASCII magic `TMEM` |
| 4 | 1 | Version, must be 1 |
| 5 | 1 | Codec ID |
| 6 | 1 | Flags, must be 0 |
| 7 | 1 | Reserved, must be 0 |
| 8 | 8 | Logical weight count |
| 16 | 4 | Payload byte length |
| 20 | 4 | CRC32 |
| 24 | payload length | Canonical payload |

CRC32 is `zlib.crc32(payload, zlib.crc32(header[0:20]))` (ordinary zlib CRC32).
It covers metadata and payload but excludes the stored checksum itself.
CRC32 detects accidental corruption; it is not authentication, encryption, or
a claim that weights cannot be extracted/modified by an adversary.
Payload length must agree with count/codec and exact file length before decoding.
Version 1 has a 32-bit payload length field; this in-memory reference implementation
is intended for experiments and does not stream multi-gigabyte checkpoints.

## Hardware memory export

`export-rtl` writes **raw hex words**, without a TMEM header:

```sh
python3 -m trinity_memory export-rtl examples/trits.json build/dense.mem --codec dense5
python3 -m trinity_memory export-rtl examples/trits.json build/baseline.mem --codec baseline5
```

`baseline5` is an export layout with five 2-bit lanes in a 10-bit word for a
fair fixed-five-lanes RTL comparison; it is not a TMEM codec ID. Its high two
hex-display bits are not stored in the 10-bit RAM word. The logical weight count
is provided separately to the RTL wrapper. Load the words through its loading port.

## Entropy and sparsity

`entropy_bpw` in reports is empirical **marginal symbol entropy**. It does not
capture positional dependence. Structured codebooks exploit a block constraint,
so 1 bpw can be below the 1.06128 bpw marginal entropy at 75% zeros without
violating the entropy bound. For example, four positions with exactly one signed
nonzero give eight equiprobable block states, or 3/4 = 0.75 bits/weight of
joint entropy; our at-most-one codebook includes a ninth state and uses 4 bits.

`project_topk` is an optional, explicitly lossy sign/top-K preparation helper.
It is not QAT. Codec round-trip tests say nothing about quality lost during projection.
