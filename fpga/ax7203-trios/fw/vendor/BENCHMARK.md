# `trios-mesh` — Competitor Benchmark Suite

> L-E2E-5 · [trinity-fpga#27](https://github.com/gHashTag/trinity-fpga/issues/27)
> Anchor `φ² + φ⁻² = 3` · Honesty mode `R5`

---

## How to run

```bash
cargo bench -p trios-mesh --features std --bench competitor_bench
```

CI (`.github/workflows/mesh-node.yml`) runs the same harness with
`--quick --noplot` and uploads `target/criterion/` as a build artifact
(`trios-mesh-bench-<sha>`).

---

## What we measure (Trinity in-process)

| Bench id                       | What it isolates                           | Tag |
|--------------------------------|--------------------------------------------|-----|
| `trinity_announce_path/*`      | ETX-cost compare + optional table swap     | [VERIFIED] (CI) |
| `trinity_lookup_full_table`    | next_hop linear scan over 15-entry SRAM    | [VERIFIED] (CI) |
| `trinity_encrypt_256B`         | ChaCha20-Poly1305 seal of 256 B payload    | [VERIFIED] (CI) |
| `trinity_decrypt_256B`         | ChaCha20-Poly1305 open of 256 B payload    | [VERIFIED] (CI) |

The lookup is bounded by `MAX_ROUTES = 16` (mirrors the MRU SRAM block
in RTL), so its complexity is `O(1)` with a tiny constant — useful as a
control measurement when comparing future branchier algorithms.

---

## Reference numbers from the literature

Direct in-process comparison with Reticulum / MeshCore / Babel is not
possible (different hosts, transports, tooling). We therefore record
peer-reviewed reference numbers and compute *relative* claims from
those.

| Stack       | Routing metric        | Encryption        | Background overhead | Source |
|-------------|-----------------------|-------------------|--------------------:|--------|
| Trinity     | ETX-like α·hops+β·q   | X25519+ChaCha20   | **0 B/s** (pull)    | this repo, CI artifact `trios-mesh-bench` |
| Reticulum   | distance-vector       | X25519+ChaCha20   | 0 B/s (announce-driven) | [Reticulum Manual §3](https://reticulum.network/manual/) [CITED] |
| MeshCore    | multi-path, max 64 h  | yes               | minimal             | [MeshCore docs](https://meshcore.co.uk) [CITED] |
| Babel       | ETX (RFC 8966)        | n/a               | ~750 B/s broadcast  | [RFC 8966 §4.1](https://www.rfc-editor.org/rfc/rfc8966) [CITED] |

Numbers from these sources are tagged `[CITED]` — they were not
re-measured here. Our own claim that Trinity adds **0 B/s** of
background broadcast is `[VERIFIED]` by inspection of the daemon
(`trios-mesh-node/src/main.rs` registers no periodic timer that emits
unsolicited packets).

---

## Routing-quality regression (FIND-001 fix)

Before L-E2E-2 the cost function was `(hops, quality)` lexicographic.
After L-E2E-2 it is `α·hops + β·quality` with `α=1, β=2`. The unit
tests in `routing.rs` cover the four canonical scenarios:

| scenario                                            | old behaviour | new behaviour |
|-----------------------------------------------------|---------------|---------------|
| 1 hop noisy (q=15) vs 3 hops clean (q=0)            | picks noisy   | picks clean   |
| identical cost, second announce arrives             | swap (flap)   | no swap       |
| GF16 clamp on `hops=0xFF, q=0xFF`                   | accept        | accept (parity) |
| 5 hops clean vs 1 hop with q=10                     | picks short   | picks clean   |

`[DERIVED]`: with `β=2`, a single nibble of link-quality penalty (≈ 6.7 %
delivery hit) costs the same as 2 extra hops. This reproduces Babel's
observation that hop-counting alone is misleading on lossy links
([RFC 8966 §A.2](https://www.rfc-editor.org/rfc/rfc8966#appendix-A.2)).

---

## What is NOT in this suite (yet)

* End-to-end HTTP latency between Railway regions — that is a deployment
  measurement, not a library benchmark; the EPIC documents the verified
  Singapore→Ko Samui RTT (~426 ms avg, ~631 ms p95) in its top-level
  `[VERIFIED]` table.
* LoRa airtime — measured in `crates/trios-fpga/benches/`.
* Multi-hop convergence with N > 2 nodes — tracked separately as
  `[ASPIRATIONAL]` until a Tailscale-backed staging mesh exists
  (see `docs/infrastructure/tailscale-funnel.md`).

`φ² + φ⁻² = 3 · TRINITY · BENCHMARK · HONEST`
