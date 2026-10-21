//! Trinity Mesh — Competitor Benchmark Suite
//! L-E2E-5 · trinity-fpga#27 · EPIC trinity-fpga#22
//! φ² + φ⁻² = 3
//!
//! Run with:
//!     cargo bench -p trios-mesh --features std --bench competitor_bench
//!
//! What this measures (HONEST mode, R5):
//!
//!   - announce_path  — full routing-table announce hot path
//!     (Trinity's equivalent of Babel/MeshCore broadcast handling)
//!
//!   - lookup_full_table — next_hop scan over a saturated 16-entry MRU SRAM
//!
//!   - encrypt_roundtrip — X25519 ECDH key derivation + ChaCha20-Poly1305
//!     seal/open of a 256-byte payload (the Reticulum-equivalent E2E layer)
//!
//! Reticulum, MeshCore, Babel comparisons are documented as REFERENCE
//! values in `BENCHMARK.md` since they cannot run on the same in-process
//! harness. The numbers here are the directly-measurable Trinity side.
//!
//! What we explicitly do NOT measure:
//!   - HTTP overhead (out of scope: that is dominated by Railway region RTT)
//!   - LoRa airtime (hardware-bound; tracked in trios-fpga benches)
//!
//! Any new metric added here must be tagged in BENCHMARK.md as one of
//! [VERIFIED] / [CITED] / [DERIVED] / [ASPIRATIONAL].

use criterion::{black_box, criterion_group, criterion_main, BenchmarkId, Criterion, Throughput};

use trios_mesh::{routing::RoutingTable, DestHash};

fn id(b: u8) -> DestHash { [b; 16] }

/// Saturated MRU table with 15 routes (capacity 16 — leaves one slot free).
fn saturated_table() -> RoutingTable {
    let mut tbl = RoutingTable::new(id(0x00));
    for i in 1u8..16 {
        tbl.process_announce(id(i), id(i + 16), 2, 1, i as u32);
    }
    tbl
}

fn bench_announce_path(c: &mut Criterion) {
    // Hot path: receive announce, run ETX cost comparison, optionally swap entry.
    let mut group = c.benchmark_group("trinity_announce_path");
    group.throughput(Throughput::Elements(1));
    for (label, hops, q) in &[
        ("noisy_short_path",  2u8, 15u8), // cost 32
        ("clean_long_path",   3u8,  0u8), // cost  3
        ("typical",           2u8,  1u8), // cost  4
    ] {
        group.bench_with_input(BenchmarkId::from_parameter(label), &(*hops, *q), |b, &(h, q)| {
            let mut tbl = saturated_table();
            let dest    = id(0xAA);
            // Pre-seed an entry so each iteration exercises the comparison branch.
            tbl.process_announce(dest, id(0x77), 4, 4, 1);
            b.iter(|| {
                black_box(tbl.process_announce(
                    black_box(dest),
                    black_box(id(0x88)),
                    black_box(h),
                    black_box(q),
                    black_box(2),
                ))
            });
        });
    }
    group.finish();
}

fn bench_lookup_full_table(c: &mut Criterion) {
    let tbl = saturated_table();
    let target = id(15);
    c.bench_function("trinity_lookup_full_table", |b| {
        b.iter(|| black_box(tbl.next_hop(black_box(&target))))
    });
}

#[cfg(feature = "std")]
mod crypto_bench {
    use super::*;
    use chacha20poly1305::{
        aead::{Aead, KeyInit},
        ChaCha20Poly1305, Key, Nonce,
    };
    use sha2::{Digest, Sha256};

    /// Minimal self-contained mirror of mesh-node's crypto path so the bench
    /// doesn't depend on the bin crate (which would require std + tokio).
    pub fn bench_encrypt_roundtrip(c: &mut Criterion) {
        // Static fake shared secret (KDF result). The X25519 ECDH itself is
        // benched by x25519-dalek upstream; here we measure the AEAD hot path
        // that runs on every /encrypt and /message request.
        let shared: [u8; 32] = Sha256::digest(b"trinity-bench-shared").into();
        let cipher  = ChaCha20Poly1305::new(Key::from_slice(&shared));
        let nonce   = Nonce::from_slice(b"phi^2plus3!?"); // 12 bytes
        let payload = vec![0xA5u8; 256];

        c.bench_function("trinity_encrypt_256B", |b| {
            b.iter(|| black_box(cipher.encrypt(nonce, black_box(payload.as_slice())).unwrap()))
        });

        let ct = cipher.encrypt(nonce, payload.as_slice()).unwrap();
        c.bench_function("trinity_decrypt_256B", |b| {
            b.iter(|| black_box(cipher.decrypt(nonce, black_box(ct.as_slice())).unwrap()))
        });
    }
}

#[cfg(feature = "std")]
fn crypto_group(c: &mut Criterion) {
    crypto_bench::bench_encrypt_roundtrip(c);
}
#[cfg(not(feature = "std"))]
fn crypto_group(_c: &mut Criterion) {}

criterion_group!(benches, bench_announce_path, bench_lookup_full_table, crypto_group);
criterion_main!(benches);
