//! # trios-mesh
//!
//! no_std Reticulum Network Stack (RNS) compatible mesh routing
//! for Trinity GF16 ASIC nodes.
//!
//! ## φ-Anchor
//! Every data structure uses GF16 (4-bit) fields to stay algebraically
//! consistent with the VSA matmul core: `φ² + φ⁻² = 3`.
//!
//! ## Architecture
//! ```text
//!   ┌────────────────────────────────────────────────┐
//!   │  Trinity ASIC / embedded firmware                   │
//!   │                                                     │
//!   │  [identity] ──► [routing] ──► [packet] ──► [crypto]  │
//!   │       └───────────► [transport] ◄───────────┘       │
//!   │          LoRa SPI | UART | USB-serial                │
//!   └────────────────────────────────────────────────┘
//! ```
//!
//! ## Hardware-software correspondence
//! | Rust type           | RTL counterpart              | SRAM size |
//! |---------------------|------------------------------|----------|
//! | `RoutingTable`      | `mru_forward` SRAM block     | 512 B    |
//! | `DestHash` ([u8;16])| `HASH_BITS = 128` port       | —        |
//! | `Quality` (u4)      | `QUALITY_W = 4` GF16 field   | —        |

#![no_std]
#![deny(missing_docs)]

pub mod identity;
pub mod packet;
pub mod routing;
pub mod transport;

/// Maximum routing table entries — mirrors MRU SRAM (16 × 32 B = 512 B).
pub const MAX_ROUTES: usize = 16;

/// RNS destination hash: 16 bytes = 128 bits = GF16 word.
pub type DestHash = [u8; 16];

/// Link quality in GF16 range [0, 15]. Lower = better.
pub type Quality = u8;
