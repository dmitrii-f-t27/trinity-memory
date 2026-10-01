# GF16 compatibility audit — 2026-09-30

This is a source audit of pinned artifacts, not a test of every GF16 product.
Existing upstream authorship is preserved. New conformance work belongs to
`dmitrii-f-t27` in trinity-memory, issue #103.

## Pinned inputs

- `gHashTag/t27`: `1170a8d60c603453141f52af0fa6b3c937178181`.
- `gHashTag/zig-golden-float`: `1b1b17c951d6f96686706420210e59cc7fcbcdd9`.

Read with `git show <revision>:<path>` so dirty local checkouts do not affect
the audit. The following findings concern the exact paths listed; generated
C in `tools/gen/` is not assumed to be the deployed shared library.

| Artifact | Observation | Consequence for this iteration |
|---|---|---|
| [t27 FORMAT-SPEC-001](https://github.com/gHashTag/t27/blob/1170a8d60c603453141f52af0fa6b3c937178181/conformance/FORMAT-SPEC-001.json) | GF16 is 1/6/9, bias 31; frozen silicon anchor records ties-to-zero; PHI_BIAS 60 is recorded separately. | A layout match is insufficient to claim arithmetic compatibility. |
| [t27 comparison protocol](https://github.com/gHashTag/t27/blob/1170a8d60c603453141f52af0fa6b3c937178181/docs/GF16_BFLOAT16_NMSE_PROTOCOL.md) | Calls for nearest/ties-even in the accuracy comparison; explicitly a protocol, not measured silicon results. | Use a named RNE comparison candidate, without claiming a measured advantage. |
| [zig-golden-float specification](https://github.com/gHashTag/zig-golden-float/blob/1b1b17c951d6f96686706420210e59cc7fcbcdd9/docs/spec-gf16.md) | Defines gradual subnormals, then notes that the shipped codec flushes them to zero. | Underflow policy must be named and tested. |
| [public C ABI header](https://github.com/gHashTag/zig-golden-float/blob/1b1b17c951d6f96686706420210e59cc7fcbcdd9/src/c/gf16.h) | `GF16_ONE` is `0x3c00`, despite documented bias 31. | That bit pattern is 0.5 under the stated layout; 1.0 is `0x3e00`. Add a directed regression vector. |
| [generated C artifact](https://github.com/gHashTag/zig-golden-float/blob/1b1b17c951d6f96686706420210e59cc7fcbcdd9/tools/gen/gf16_c.c) | Encoder truncates `mant >> 14`, returns positive infinity on finite overflow and positive zero on underflow. Decoder handles only all-zero exponent/mantissa as zero, otherwise inserts an implicit leading one. | It does not implement the RNE/gradual profile; negative range exceptions and subnormal decode also differ. Do not silently import it as the reference oracle. |

The following four source-derived cases were also reproduced by compiling
the pinned `tools/gen/gf16_c.c` together with its sibling header and calling
its conversion functions. This probes that artifact, not the shipped library:

| Operation | `tools/gen/gf16_c.c` source behavior | `gf16-rne-gradual-v1` |
|---|---|---|
| Encode binary32 `0x3f803000` | `0x3e00` (truncation) | `0x3e01` (nearest) |
| Encode binary32 `0xcf800000` (-2^32) | `0x7e00` | `0xfe00` |
| Encode binary32 `0xab800000` (-2^-40) | `0x0000` | `0x8000` |
| Decode GF16 `0x0001` | binary32 `0x30004000` | binary32 `0x2c000000` (2^-39) |

These findings justify a separate, tested conversion contract. They do not
justify rewriting upstream history, changing existing silicon semantics or
claiming all implementations are broken. Corrections to upstream source and
its generators are separate work; this iteration adds a reproducible candidate
codec and conformance gate in the user's repository.
