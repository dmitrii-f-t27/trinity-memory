//! Criterion benchmark: routing table lookup and insert.
//! Run with: cargo bench -p trios-mesh --features std

use criterion::{black_box, criterion_group, criterion_main, Criterion};
use trios_mesh::{routing::RoutingTable, DestHash};

fn id(b: u8) -> DestHash { [b; 16] }

fn bench_lookup(c: &mut Criterion) {
    let self_id = id(0x00);
    let mut tbl = RoutingTable::new(self_id);
    // Fill 15 entries
    for i in 1u8..16 {
        tbl.process_announce(id(i), id(i + 16), 2, 1, i as u32);
    }
    let target = id(15);
    c.bench_function("routing_lookup_15_entries", |b| {
        b.iter(|| black_box(tbl.next_hop(black_box(&target))))
    });
}

fn bench_insert(c: &mut Criterion) {
    c.bench_function("routing_insert_single", |b| {
        b.iter(|| {
            let mut tbl = RoutingTable::new(id(0x00));
            black_box(tbl.process_announce(
                black_box(id(0x01)),
                black_box(id(0x02)),
                black_box(2u8),
                black_box(1u8),
                black_box(0u32),
            ))
        })
    });
}

criterion_group!(benches, bench_lookup, bench_insert);
criterion_main!(benches);
