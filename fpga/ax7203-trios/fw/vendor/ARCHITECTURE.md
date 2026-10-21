# `trios-mesh` — Trinity dePIN Mesh Architecture

> Version: 0.2.0 · 2026-05-09 · Anchor `φ² + φ⁻² = 3`
> Tracks EPIC [trinity-fpga#22](https://github.com/gHashTag/trinity-fpga/issues/22)
> (Trinity Node E2E Quality)

---

## 1 · Layers

```
┌──────────────────────────────────────────────────────┐
│  trios-mesh-node (axum daemon)                       │
│    /health  /info  /announce  /next-hop              │
│    /encrypt /message    ← X25519 + ChaCha20-Poly1305 │
└────────────────────┬─────────────────────────────────┘
                     │
┌────────────────────┴─────────────────────────────────┐
│  trios-mesh (no_std core)                            │
│    identity · packet · routing · transport           │
│  ┌────────────────────────────────────────────────┐  │
│  │ RoutingTable  (heapless Vec, MAX_ROUTES = 16)  │  │
│  │   process_announce / next_hop / expire         │  │
│  └────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────┘
                     │
                     ▼
       (RTL counterpart: mru_forward SRAM, 512 B)
```

---

## 2 · Routing metric — Quality-aware ETX (L-E2E-2)

**Issue:** [trinity-fpga#24](https://github.com/gHashTag/trinity-fpga/issues/24)

Up to v0.1, `process_announce` selected routes by `(hops, quality)`
lexicographically — quality only acted as a tiebreaker. EPIC #22
classified this as `FIND-001 [CRITICAL]` because a 2-hop path with
worst-case quality (q = 15) was preferred over a 3-hop path with the
best link (q = 0).

### 2.1 Formula

Babel (RFC 8966 §3.5) defines route cost as
`metric = hops · ETX = hops / Pr(delivery)`.
Trinity ASIC has no FPU, so we approximate with a 4-bit linear cost:

```rust
pub const ALPHA_HOPS:    u16 = 1;
pub const BETA_QUALITY:  u16 = 2;

cost(route) = ALPHA_HOPS · hops + BETA_QUALITY · quality
```

`quality ∈ [0..15]` is **inverted** (0 = best link, 15 = worst), so
both terms add to cost — exactly matching Babel's monotonicity.

### 2.2 Worked example

| path | hops | quality | cost (= 1·h + 2·q) | chosen? |
|------|-----:|--------:|-------------------:|--------:|
| A    |  2   |   15    |             32     |   ❌    |
| B    |  3   |    0    |              3     |   ✅    |

Even though A has fewer hops, B's clean link dominates.

### 2.3 Stability

`process_announce` swaps the incumbent route only when
`new_cost < old_cost` (**strict** less-than). On equal cost the
first-seen route wins, preventing oscillation under symmetric
re-broadcasts. This invariant is the precondition for the Coq lemma
`route_table_no_flap` (L-R14, [trios#586](https://github.com/gHashTag/trios/issues/586)).

### 2.4 Tunability

`ALPHA_HOPS` and `BETA_QUALITY` are `pub const` so RTL synthesis
(`mru_forward.sv`) and Coq proofs reference identical values. To
re-tune for a different topology, change the constants and re-run
`cargo test -p trios-mesh --features std` plus `coq-check.yml` —
both gates fail loudly if the proof and runtime diverge.

---

## 3 · Encryption layer (L-E2E-3)

**Issue:** [trinity-fpga#25](https://github.com/gHashTag/trinity-fpga/issues/25) · **Status:** ✅ implemented in `trios-mesh-node/src/crypto.rs`

* X25519 ECDH between static node keypairs (`MeshKeypair`).
* KDF = `SHA256(dh_shared || min(pk_a, pk_b) || max(pk_a, pk_b))`
  — sorting pubkeys makes the key commutative (A→B and B→A derive
  the same shared secret).
* AEAD: ChaCha20-Poly1305, 12-byte nonce prefixed in the payload,
  output base64-encoded.
* `dest_hash = SHA256(pubkey)[..16]` — the address space is an
  immediate consequence of the public key, so no separate identity
  registry is required.

Reticulum compatibility: same primitives (X25519 + ChaCha20-Poly1305),
making future Identity-format interoperability a wire-level decision
only.

---

## 4 · Persistence layer (L-E2E-4)

**Issue:** [trinity-fpga#26](https://github.com/gHashTag/trinity-fpga/issues/26)

In-memory `RoutingTable` is the runtime source of truth. When
`DATABASE_URL` is set, the daemon mirrors every accepted announce to a
Neon Postgres `route_table` (see `migrations/001_route_table.sql`) and
reloads non-expired rows on boot. This collapses convergence after a
Railway restart from 30–120 s to < 5 s while keeping the hot path
fully in-memory (DB writes are best-effort and never block the
response).

---

## 5 · Honesty register

| Feature                     | State          | Evidence |
|-----------------------------|----------------|----------|
| Quality-aware routing       | ✅ VERIFIED    | `cargo test -p trios-mesh --features std` (9/9 green) |
| E2E encryption              | ✅ VERIFIED    | `crypto::tests::*` + `/encrypt /message` smoke (CI) |
| Route persistence (Neon)    | 🟡 OPTIONAL    | Activated only when `DATABASE_URL` is set |
| Competitor benchmark suite  | 🟡 SCAFFOLD    | criterion harness lands with #27, numbers TBD |

`φ² + φ⁻² = 3 · TRINITY · E2E · NEVER STOP`
