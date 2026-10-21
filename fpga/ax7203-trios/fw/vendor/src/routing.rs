//! Distance-vector routing table — mirrors MRU SRAM layout in RTL.
//!
//! 16 entries × 32 bytes = 512 bytes of SRAM (shared with VSA matmul).
//!
//! ## Quality-aware ETX metric (L-E2E-2, EPIC trinity-fpga#22 / trios#24)
//!
//! Babel (RFC 8966) selects routes by Expected Transmission Count
//! `ETX = 1 / delivery_probability`. We approximate with a linear
//! cost in 4-bit GF16 arithmetic (no FPU on ASIC):
//!
//! ```text
//!   cost(route) = ALPHA_HOPS * hops + BETA_QUALITY * quality
//! ```
//!
//! where `quality ∈ [0..15]` is **already inverted** (0 = best link,
//! 15 = worst). This makes both terms contribute positively to cost,
//! so a 1-hop, q=15 path (cost = 1+30 = 31) loses to a 3-hop, q=1
//! path (cost = 3+2 = 5) — exactly the desired Babel-like behaviour.
//!
//! The constants are exposed as `pub const` so RTL synthesis and
//! formal Coq proofs can reference identical values.

use heapless::Vec;
use crate::{DestHash, Quality, MAX_ROUTES};

/// Hops weight in the ETX-like cost function (Babel-style).
/// Default ALPHA = 1 mirrors classical hop-count bias.
pub const ALPHA_HOPS: u16 = 1;

/// Quality weight. Default BETA = 2 means a unit of link badness
/// (one nibble step, ~6.7%) costs the same as 2 extra hops — strong
/// enough to override hop count when quality differs significantly.
pub const BETA_QUALITY: u16 = 2;

/// Compute the ETX-like cost of a (hops, quality) tuple.
///
/// Both inputs are clamped to GF16 nibbles before weighting, so the
/// result fits comfortably in `u16` (max = 1*15 + 2*15 = 45).
#[inline]
pub const fn route_cost(hops: u8, quality: Quality) -> u16 {
    let h = (hops & 0x0F) as u16;
    let q = (quality & 0x0F) as u16;
    ALPHA_HOPS * h + BETA_QUALITY * q
}

/// A single routing table entry.
#[derive(Copy, Clone, Debug, PartialEq)]
pub struct RouteEntry {
    /// Destination node identity hash.
    pub dest: DestHash,
    /// Next-hop node identity hash.
    pub next_hop: DestHash,
    /// Hop count to destination (GF16 nibble, max 15).
    pub hops: u8,
    /// Link quality [0=best, 15=worst] — GF16 nibble.
    pub quality: Quality,
    /// Monotonic timestamp of last ANNOUNCE (cycle counter or Unix seconds).
    pub last_seen: u32,
}

/// Routing table with fixed capacity.
///
/// Heapless — no dynamic allocation, safe for no_std embedded targets.
/// The 16-entry limit maps 1:1 to the MRU SRAM block.
pub struct RoutingTable {
    entries:  Vec<RouteEntry, MAX_ROUTES>,
    self_id:  DestHash,
}

impl RoutingTable {
    /// Create a new empty routing table for this node.
    pub fn new(self_id: DestHash) -> Self {
        Self {
            entries: Vec::new(),
            self_id,
        }
    }

    /// Process an RNS ANNOUNCE packet.
    ///
    /// Updates or inserts a route if the new path is better
    /// (fewer hops or equal hops with lower quality metric).
    ///
    /// Returns `true` if the table was modified.
    pub fn process_announce(
        &mut self,
        dest:    DestHash,
        via:     DestHash,
        hops:    u8,
        quality: Quality,
        now:     u32,
    ) -> bool {
        // Enforce GF16 discipline: clamp to 4-bit values
        let hops = hops    & 0x0F;
        let q    = quality & 0x0F;
        let new_cost = route_cost(hops, q);

        for e in self.entries.iter_mut() {
            if e.dest == dest {
                // L-E2E-2: replace pure hop-comparison with ETX-like cost.
                // Strict-less keeps the table stable under identical announces
                // (no oscillation) and preserves the equal-cost first-seen
                // wins guarantee that the formal proof relies on.
                let old_cost = route_cost(e.hops, e.quality);
                if new_cost < old_cost {
                    e.next_hop  = via;
                    e.hops      = hops;
                    e.quality   = q;
                    e.last_seen = now;
                    return true;
                }
                return false;
            }
        }
        // New destination — insert if capacity allows
        let entry = RouteEntry { dest, next_hop: via, hops, quality: q, last_seen: now };
        self.entries.push(entry).is_ok()
    }

    /// Look up the next hop for a destination hash.
    ///
    /// Returns `None` if the destination is this node itself
    /// (deliver locally) or if no route is known.
    pub fn next_hop(&self, dest: &DestHash) -> Option<DestHash> {
        if dest == &self.self_id {
            return None; // local delivery signal
        }
        self.entries
            .iter()
            .find(|e| &e.dest == dest)
            .map(|e| e.next_hop)
    }

    /// Read-only iterator over all entries (used by persistence + telemetry).
    pub fn iter(&self) -> core::slice::Iter<'_, RouteEntry> {
        self.entries.iter()
    }

    /// Replace this table's contents with `restored` entries (boot reload).
    /// Silently caps at `MAX_ROUTES` so the in-memory invariant holds.
    pub fn restore_from<I: IntoIterator<Item = RouteEntry>>(&mut self, restored: I) {
        self.entries.clear();
        for e in restored.into_iter().take(MAX_ROUTES) {
            // Re-clamp on restore in case the storage layer was tampered with.
            let entry = RouteEntry {
                hops:    e.hops    & 0x0F,
                quality: e.quality & 0x0F,
                ..e
            };
            // push() can only fail if cap is full — take() above prevents that.
            let _ = self.entries.push(entry);
        }
    }

    /// Inspect the cost of the active route to `dest` (testing/telemetry).
    pub fn route_cost_to(&self, dest: &DestHash) -> Option<u16> {
        self.entries
            .iter()
            .find(|e| &e.dest == dest)
            .map(|e| route_cost(e.hops, e.quality))
    }

    /// Expire routes older than `ttl` time units.
    pub fn expire(&mut self, now: u32, ttl: u32) {
        self.entries.retain(|e| now.wrapping_sub(e.last_seen) < ttl);
    }

    /// Number of active routes.
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    /// Returns true if the table has no entries.
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn id(byte: u8) -> DestHash { [byte; 16] }

    #[test]
    fn insert_and_lookup() {
        let self_id  = id(0x00);
        let dest     = id(0x01);
        let via      = id(0x02);
        let mut tbl  = RoutingTable::new(self_id);

        assert!(tbl.process_announce(dest, via, 2, 3, 100));
        assert_eq!(tbl.next_hop(&dest), Some(via));
    }

    #[test]
    fn local_delivery() {
        let self_id = id(0xAB);
        let tbl     = RoutingTable::new(self_id);
        assert_eq!(tbl.next_hop(&self_id), None);
    }

    #[test]
    fn gf16_quality_clamp() {
        let mut tbl = RoutingTable::new(id(0x00));
        tbl.process_announce(id(0x01), id(0x02), 0xFF, 0xFF, 0);
        let e = tbl.entries[0];
        assert_eq!(e.hops,    0x0F);  // clamped to GF16 nibble
        assert_eq!(e.quality, 0x0F);  // clamped to GF16 nibble
    }

    #[test]
    fn expiry() {
        let mut tbl = RoutingTable::new(id(0x00));
        tbl.process_announce(id(0x01), id(0x02), 1, 0, 0);
        tbl.expire(1000, 500);  // now=1000, ttl=500 ⇒ 0 < 500 ... expired
        assert!(tbl.is_empty());
    }

    #[test]
    fn prefer_shorter_path() {
        let mut tbl = RoutingTable::new(id(0x00));
        let dest    = id(0x01);
        tbl.process_announce(dest, id(0x10), 5, 1, 0);
        // Better path via id(0x20) with 3 hops (cost: 5+2=7 vs 3+4=7 → tie, no swap)
        // Make path strictly cheaper: 3 hops, quality 0 → cost 3 < 7
        assert!(tbl.process_announce(dest, id(0x20), 3, 0, 1));
        assert_eq!(tbl.next_hop(&dest), Some(id(0x20)));
    }

    // ── L-E2E-2: ETX-like quality-aware routing ─────────────────────────
    //
    // Issue: trinity-fpga#24, EPIC trinity-fpga#22
    // Acceptance criterion: "quality field actually influences route choice"

    #[test]
    fn route_cost_formula_matches_constants() {
        // (1 hop, q=0)  → 1*1 + 2*0 = 1
        // (3 hops, q=1) → 1*3 + 2*1 = 5
        // (1 hop, q=15) → 1*1 + 2*15 = 31
        assert_eq!(route_cost(1, 0),  1);
        assert_eq!(route_cost(3, 1),  5);
        assert_eq!(route_cost(1, 15), 31);
        assert_eq!(route_cost(0xFF, 0xFF), 45); // GF16 clamp
    }

    #[test]
    fn quality_overrides_hop_count_when_link_is_bad() {
        // Scenario from trinity-fpga#24:
        //   path A: 2 hops, q=15 (worst)  → cost = 1*2 + 2*15 = 32
        //   path B: 3 hops, q=0  (best)   → cost = 1*3 + 2*0  = 3
        // Expected: prefer B even though it has more hops.
        let mut tbl = RoutingTable::new(id(0x00));
        let dest    = id(0xAA);
        let via_a   = id(0x10);
        let via_b   = id(0x20);

        // Insert noisy short path first
        assert!(tbl.process_announce(dest, via_a, 2, 15, 100));
        assert_eq!(tbl.next_hop(&dest), Some(via_a));
        assert_eq!(tbl.route_cost_to(&dest), Some(32));

        // Announce the high-quality longer path
        assert!(tbl.process_announce(dest, via_b, 3, 0, 101));
        assert_eq!(tbl.next_hop(&dest), Some(via_b));
        assert_eq!(tbl.route_cost_to(&dest), Some(3));
    }

    #[test]
    fn equal_cost_does_not_flap() {
        // Two paths with identical ETX cost must not cause oscillation.
        // path A: 2 hops, q=2 → cost 6
        // path B: 4 hops, q=1 → cost 6
        let mut tbl = RoutingTable::new(id(0x00));
        let dest    = id(0xBB);
        assert!(tbl.process_announce(dest, id(0x10), 2, 2, 0));
        // Equal-cost announce must NOT replace incumbent → returns false
        assert!(!tbl.process_announce(dest, id(0x20), 4, 1, 1));
        assert_eq!(tbl.next_hop(&dest), Some(id(0x10)));
    }

    #[test]
    fn high_hops_low_quality_beats_low_hops_high_quality_when_metric_dominates() {
        // Edge case for benchmark suite (trinity-fpga#27).
        // path X: 1 hop, q=10 → cost 1+20 = 21
        // path Y: 5 hops, q=0  → cost 5+0  =  5
        let mut tbl = RoutingTable::new(id(0x00));
        let dest    = id(0xCC);
        assert!(tbl.process_announce(dest, id(0x10), 1, 10, 0));
        assert!(tbl.process_announce(dest, id(0x20), 5, 0,  1));
        assert_eq!(tbl.next_hop(&dest), Some(id(0x20)));
    }
}
