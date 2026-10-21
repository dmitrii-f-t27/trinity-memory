//! φ-Node Identity: Ed25519 keypair → SHA-256[0:127] destination hash.
//!
//! Mirrors `mru_forward` `self_addr` port in RTL.

use sha2::{Digest, Sha256};

/// Trinity φ-node identity derived from a 32-byte Ed25519 public key.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub struct NodeIdentity {
    /// Raw Ed25519 public key bytes.
    pub pubkey: [u8; 32],
    /// RNS destination hash = SHA-256(pubkey)[0..16].
    pub dest_hash: crate::DestHash,
}

impl NodeIdentity {
    /// Derive identity from an existing Ed25519 public key slice.
    pub fn from_pubkey(pubkey: &[u8; 32]) -> Self {
        let hash = Sha256::digest(pubkey);
        let mut dest_hash = [0u8; 16];
        dest_hash.copy_from_slice(&hash[..16]);
        Self {
            pubkey: *pubkey,
            dest_hash,
        }
    }

    /// GF16-nibble representation of the first 4 bytes (debug / logging).
    pub fn gf16_prefix(&self) -> [u8; 8] {
        let mut out = [0u8; 8];
        for (i, byte) in self.dest_hash[..4].iter().enumerate() {
            out[2 * i]     = byte >> 4;    // high nibble
            out[2 * i + 1] = byte & 0x0F;  // low nibble  (GF16 element)
        }
        out
    }
}
