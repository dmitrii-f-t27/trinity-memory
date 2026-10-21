//! Transport interface trait — hardware abstraction for Trinity MRU.
//!
//! Implementations: LoRa SPI (SX1276), UART, USB-CDC.
//! The trait is object-safe and no_std compatible.

/// Abstract byte-oriented interface to a physical transport.
///
/// Implemented by the FPGA SPI bridge, LoRa driver, or software loopback.
pub trait Transport {
    /// Error type returned by I/O operations.
    type Error: core::fmt::Debug;

    /// Send `data` bytes. Blocks until TX FIFO accepts the bytes.
    fn send(&mut self, data: &[u8]) -> Result<(), Self::Error>;

    /// Receive up to `buf.len()` bytes. Returns actual bytes read.
    fn recv(&mut self, buf: &mut [u8]) -> Result<usize, Self::Error>;

    /// Returns true if receive buffer has data available.
    fn poll_recv(&self) -> bool;
}

/// Software loopback transport for unit testing.
#[cfg(test)]
pub mod loopback {
    use super::Transport;
    use heapless::Deque;

    /// In-memory loopback with 256-byte ring buffer.
    pub struct Loopback {
        buf: Deque<u8, 256>,
    }

    impl Loopback {
        /// Create an empty loopback transport.
        pub fn new() -> Self {
            Self { buf: Deque::new() }
        }
    }

    impl Transport for Loopback {
        type Error = ();

        fn send(&mut self, data: &[u8]) -> Result<(), ()> {
            for &b in data {
                self.buf.push_back(b).map_err(|_| ())?;
            }
            Ok(())
        }

        fn recv(&mut self, buf: &mut [u8]) -> Result<usize, ()> {
            let n = buf.len().min(self.buf.len());
            for slot in buf[..n].iter_mut() {
                *slot = self.buf.pop_front().unwrap();
            }
            Ok(n)
        }

        fn poll_recv(&self) -> bool {
            !self.buf.is_empty()
        }
    }
}
