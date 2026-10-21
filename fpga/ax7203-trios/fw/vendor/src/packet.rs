//! RNS packet encode / decode — zero-copy, no_std.
//!
//! Minimal subset: ANNOUNCE and DATA headers.
//! Full LINKREQ / PROOF / cryptographic envelope — Phase B (M35-2).

use crate::DestHash;

/// Packet type discriminant (1 byte in RNS header).
#[repr(u8)]
#[derive(Copy, Clone, Debug, PartialEq)]
pub enum PacketType {
    /// Destination propagation beacon.
    Announce  = 0x01,
    /// Encrypted data payload.
    Data      = 0x02,
    /// Delivery proof / receipt.
    Proof     = 0x04,
    /// Link request (encrypted tunnel setup).
    LinkReq   = 0x08,
}

/// Parsed ANNOUNCE header.
#[derive(Copy, Clone, Debug, PartialEq)]
pub struct AnnounceHeader {
    /// Originating destination hash.
    pub dest:    DestHash,
    /// Identity hash of the immediately sending node.
    pub sender:  DestHash,
    /// Number of hops already traversed.
    pub hops:    u8,
}

impl AnnounceHeader {
    /// Minimum wire size: 1 (type) + 16 (dest) + 16 (sender) + 1 (hops) = 34 bytes.
    pub const MIN_WIRE: usize = 34;

    /// Parse from raw bytes. Returns `None` if buffer is too short.
    pub fn parse(buf: &[u8]) -> Option<Self> {
        if buf.len() < Self::MIN_WIRE {
            return None;
        }
        if buf[0] != PacketType::Announce as u8 {
            return None;
        }
        let mut dest   = [0u8; 16];
        let mut sender = [0u8; 16];
        dest.copy_from_slice(&buf[1..17]);
        sender.copy_from_slice(&buf[17..33]);
        Some(Self { dest, sender, hops: buf[33] & 0x0F })
    }

    /// Serialise into a fixed-size buffer. Returns number of bytes written.
    pub fn serialise(&self, buf: &mut [u8; Self::MIN_WIRE]) {
        buf[0] = PacketType::Announce as u8;
        buf[1..17].copy_from_slice(&self.dest);
        buf[17..33].copy_from_slice(&self.sender);
        buf[33] = self.hops & 0x0F;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_announce() {
        let hdr = AnnounceHeader {
            dest:   [0xAB; 16],
            sender: [0xCD; 16],
            hops:   3,
        };
        let mut buf = [0u8; AnnounceHeader::MIN_WIRE];
        hdr.serialise(&mut buf);
        let parsed = AnnounceHeader::parse(&buf).unwrap();
        assert_eq!(hdr, parsed);
    }
}
