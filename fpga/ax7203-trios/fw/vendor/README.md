# trios-mesh

> no_std Reticulum-compatible mesh routing for Trinity GF16 ASIC nodes  
> `φ² + φ⁻² = 3` — all data structures in GF16 arithmetic

Part of the [gHashTag/trios](https://github.com/gHashTag/trios) monorepo.  
See [Ph.D. Chapter 35](../docs/phd/chapters/ch_35_mesh_node.tex) for the full academic treatment.

## What is this?

`trios-mesh` is a minimal, `no_std` Rust crate that implements the core of the
[Reticulum Network Stack (RNS)](https://reticulum.network) routing table and
packet format, designed to mirror the silicon **Mesh Routing Unit (MRU)** that
will be co-integrated with the VSA inference core on the Trinity GF16 ASIC
(target: [Tiny Tapeout IHP 27a](https://tinytapeout.com)).

## Architecture

```
 crates/trios-mesh/
 ├── src/
 │   ├── lib.rs          # crate root, re-exports, constants
 │   ├── identity.rs     # Ed25519 pubkey → φ-DestHash (SHA-256[0:127])
 │   ├── routing.rs      # 16-entry heapless routing table (= MRU SRAM)
 │   ├── packet.rs       # ANNOUNCE / DATA header encode-decode
 │   └── transport.rs    # Transport trait + software Loopback
 └── benches/
     └── routing_table.rs # Criterion benchmarks
```

## Hardware ↔ Software correspondence

| Rust type            | RTL counterpart                     | SRAM  |
|----------------------|-------------------------------------|-------|
| `RoutingTable`       | `mru_forward` SRAM block (16×32B)   | 512 B |
| `DestHash` ([u8;16]) | `HASH_BITS = 128` port              | —     |
| `Quality` (u8, nibble) | `QUALITY_W = 4` GF16 field        | —     |
| `AnnounceHeader`     | S1: header parse stage              | —     |
| `Transport` trait    | S0/S4: RX/TX FIFO                   | —     |

## Quick start

```toml
# Cargo.toml
[dependencies]
trios-mesh = { path = "../crates/trios-mesh" }
```

```rust
use trios_mesh::{
    identity::NodeIdentity,
    routing::RoutingTable,
};

let pubkey = [0x42u8; 32];  // replace with real Ed25519 key
let node   = NodeIdentity::from_pubkey(&pubkey);
let mut tbl = RoutingTable::new(node.dest_hash);

// When an ANNOUNCE packet arrives from peer B about destination D:
tbl.process_announce(
    /* dest    */ [0xAB; 16],
    /* via     */ node.dest_hash,
    /* hops    */ 2,
    /* quality */ 3,
    /* now     */ 0,
);

if let Some(hop) = tbl.next_hop(&[0xAB; 16]) {
    // forward packet to `hop`
}
```

## Roadmap

| Milestone | Description                              | Target        |
|-----------|------------------------------------------|---------------|
| M35-1     | Crate builds `no_std` + all tests pass   | 2026-05-15    |
| M35-2     | Node-0 ↔ peer ANNOUNCE exchange (L-DPC2) | 2026-05-20    |
| M35-3     | MRU RTL iverilog sim 64/64 pass          | 2026-06-01    |
| M35-4     | TTIHP27a submission (MRU + VSA)          | 2026-Q4       |
| M35-5     | Silicon power measurement vs model       | 2027-Q1       |

## Licence

MIT — compatible with Trinity GF16 Apache-2.0 RTL (Article II).
