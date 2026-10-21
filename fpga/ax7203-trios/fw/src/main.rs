//! UART front end for the real `trios-mesh` crate on a PicoRV32 (RV32I) soft CPU.
//!
//! Every request is executed here, on the FPGA CPU, by the unmodified crate
//! (AnnounceHeader::parse/serialise, RoutingTable, NodeIdentity). The host only sends
//! request bytes and reads response bytes. Protocol: docs/CONTRACT.md.
#![no_std]
#![no_main]

use core::arch::global_asm;
use core::panic::PanicInfo;
use core::ptr::{read_volatile, write_volatile};

use trios_mesh::identity::NodeIdentity;
use trios_mesh::packet::AnnounceHeader;
use trios_mesh::routing::{route_cost, RoutingTable};
use trios_mesh::transport::Transport;
use trios_mesh::DestHash;

global_asm!(
    r#"
    .section .text.init,"ax"
    .globl _start
_start:
    la sp, _stack_top
    la t0, _sbss
    la t1, _ebss
1:  bgeu t0, t1, 2f
    sw zero, 0(t0)
    addi t0, t0, 4
    j 1b
2:  call trios_main
3:  j 3b
"#
);

const UART_DATA: *mut u32 = 0x1000_0000 as *mut u32;
const UART_STATUS: *mut u32 = 0x1000_0004 as *mut u32;
const LED: *mut u32 = 0x1000_0008 as *mut u32;

const ST_TX_BUSY: u32 = 1;
const ST_RX_READY: u32 = 2;
const ST_OVERRUN: u32 = 4; // sticky: the queue was full and a byte was dropped
const ST_QUIET: u32 = 8; // no activity for 64 character times
const ST_LINE_ERR: u32 = 16; // sticky: a stop bit was low

const SYNC: u8 = 0xA5;
const MAX_PAYLOAD: usize = 80;

/// Frame-level failures. Each one drops the frame, drains the line until it is quiet,
/// clears the sticky flags and answers `E 04 <reason>`; nothing from a damaged frame is executed.
#[derive(Copy, Clone)]
enum FrameErr {
    Overrun = 1,
    Timeout = 2,
    BadLen = 3,
    BadCrc = 4,
    LineErr = 5,
}

/// CRC-8, polynomial 0x07, init 0 (t27/trios_uart_guard.t27 `crc8_byte`).
fn crc8(mut crc: u8, b: u8) -> u8 {
    crc ^= b;
    for _ in 0..8 {
        crc = if crc & 0x80 != 0 { (crc << 1) ^ 0x07 } else { crc << 1 };
    }
    crc
}

fn status() -> u32 {
    unsafe { read_volatile(UART_STATUS) }
}

/// The soft CPU's UART as a `trios_mesh` transport.
struct Uart;

impl Transport for Uart {
    type Error = ();

    fn send(&mut self, data: &[u8]) -> Result<(), ()> {
        for &b in data {
            unsafe {
                while read_volatile(UART_STATUS) & ST_TX_BUSY != 0 {}
                write_volatile(UART_DATA, b as u32);
            }
        }
        Ok(())
    }

    fn recv(&mut self, buf: &mut [u8]) -> Result<usize, ()> {
        let mut n = 0;
        while n < buf.len() && self.poll_recv() {
            buf[n] = unsafe { read_volatile(UART_DATA) } as u8;
            n += 1;
        }
        Ok(n)
    }

    fn poll_recv(&self) -> bool {
        status() & ST_RX_READY != 0
    }
}

impl Uart {
    /// Next received byte. A sticky overrun or line error aborts at once (bytes were lost);
    /// inside a frame a quiet line with an empty queue is a timeout.
    fn next(&mut self, in_frame: bool) -> Result<u8, FrameErr> {
        loop {
            let st = status();
            if st & ST_OVERRUN != 0 {
                return Err(FrameErr::Overrun);
            }
            if st & ST_LINE_ERR != 0 {
                return Err(FrameErr::LineErr);
            }
            if st & ST_RX_READY != 0 {
                let mut b = [0u8; 1];
                if let Ok(1) = self.recv(&mut b) {
                    return Ok(b[0]);
                }
            } else if in_frame && st & ST_QUIET != 0 {
                return Err(FrameErr::Timeout);
            }
        }
    }

    /// Discard input until the line has been quiet, then clear the sticky flags.
    fn resync(&mut self) {
        loop {
            let st = status();
            if st & ST_RX_READY != 0 {
                unsafe { read_volatile(UART_DATA) };
            } else if st & ST_QUIET != 0 {
                break;
            }
        }
        unsafe { write_volatile(UART_STATUS, ST_OVERRUN | ST_LINE_ERR) };
    }
}

/// One request frame: SYNC, len, len payload bytes, CRC-8 over len and payload.
fn read_frame(u: &mut Uart, buf: &mut [u8; MAX_PAYLOAD]) -> Result<usize, FrameErr> {
    while u.next(false)? != SYNC {}
    let len = u.next(true)? as usize;
    if len == 0 || len > MAX_PAYLOAD {
        return Err(FrameErr::BadLen);
    }
    let mut crc = crc8(0, len as u8);
    for slot in buf.iter_mut().take(len) {
        let b = u.next(true)?;
        *slot = b;
        crc = crc8(crc, b);
    }
    if u.next(true)? != crc {
        return Err(FrameErr::BadCrc);
    }
    Ok(len)
}

fn cost_of(t: &RoutingTable, dest: &DestHash) -> u16 {
    t.route_cost_to(dest).unwrap_or(0xFFFF)
}

fn err(u: &mut Uart, code: u8, cmd: u8) {
    let _ = u.send(&[b'E', code, cmd]);
}

fn dest16(p: &[u8]) -> DestHash {
    let mut d = [0u8; 16];
    d.copy_from_slice(&p[..16]);
    d
}

fn le32(p: &[u8]) -> u32 {
    u32::from_le_bytes([p[0], p[1], p[2], p[3]])
}

/// Execute one validated frame payload with the real trios-mesh crate.
fn handle(u: &mut Uart, table: &mut RoutingTable, p: &[u8]) {
    let cmd = p[0];
    let args = &p[1..];
    match cmd {
        b'V' if args.is_empty() => {
            let _ = u.send(b"VTRIOS-MESH 70882158 RV32I\n");
        }
        b'N' if args.len() == 16 => {
            *table = RoutingTable::new(dest16(args));
            let _ = u.send(&[b'N', 0]);
        }
        b'P' if !args.is_empty() && args.len() == 1 + args[0] as usize => {
            let raw = &args[1..];
            if raw.len() > 64 {
                return err(u, 3, cmd);
            }
            let mut out = [0u8; 3 + AnnounceHeader::MIN_WIRE];
            out[0] = b'P';
            if let Some(h) = AnnounceHeader::parse(raw) {
                out[1] = 1;
                out[2] = h.hops;
                let mut ser = [0u8; AnnounceHeader::MIN_WIRE];
                h.serialise(&mut ser);
                out[3..].copy_from_slice(&ser);
            }
            let _ = u.send(&out);
        }
        b'A' if !args.is_empty() && args.len() == 1 + args[0] as usize + 5 => {
            let l = args[0] as usize;
            if l > 64 {
                return err(u, 3, cmd);
            }
            let raw = &args[1..1 + l];
            let quality = args[1 + l];
            let now = le32(&args[2 + l..]);
            match AnnounceHeader::parse(raw) {
                None => {
                    let _ = u.send(&[b'A', 0, table.len() as u8, 0xFF, 0xFF]);
                }
                Some(h) => {
                    let changed = table.process_announce(h.dest, h.sender, h.hops, quality, now);
                    let c = cost_of(table, &h.dest).to_le_bytes();
                    let st = if changed { 1 } else { 2 };
                    let _ = u.send(&[b'A', st, table.len() as u8, c[0], c[1]]);
                }
            }
        }
        b'Q' if args.len() == 16 => {
            let dest = dest16(args);
            let mut out = [0u8; 27];
            out[0] = b'Q';
            // next_hop() is the authority for the route (None for this node itself and unknown).
            if let Some(nh) = table.next_hop(&dest) {
                for e in table.iter() {
                    if e.dest == dest {
                        out[1] = 1;
                        out[2..18].copy_from_slice(&nh);
                        out[18] = e.hops;
                        out[19] = e.quality;
                        out[20..24].copy_from_slice(&e.last_seen.to_le_bytes());
                        break;
                    }
                }
            }
            out[24] = table.len() as u8;
            let c = if out[1] == 1 { cost_of(table, &dest) } else { 0xFFFF };
            out[25..27].copy_from_slice(&c.to_le_bytes());
            let _ = u.send(&out);
        }
        b'T' if args.len() == 8 => {
            table.expire(le32(&args[..4]), le32(&args[4..]));
            let _ = u.send(&[b'T', table.len() as u8]);
        }
        b'C' if args.len() == 2 => {
            let c = route_cost(args[0], args[1]).to_le_bytes();
            let _ = u.send(&[b'C', c[0], c[1]]);
        }
        b'H' if args.len() == 32 => {
            let mut pk = [0u8; 32];
            pk.copy_from_slice(args);
            let id = NodeIdentity::from_pubkey(&pk);
            let mut out = [0u8; 17];
            out[0] = b'H';
            out[1..].copy_from_slice(&id.dest_hash);
            let _ = u.send(&out);
        }
        b'V' | b'N' | b'P' | b'A' | b'Q' | b'T' | b'C' | b'H' => err(u, 5, cmd),
        _ => err(u, 2, cmd),
    }
}

#[no_mangle]
pub extern "C" fn trios_main() -> ! {
    let mut u = Uart;
    let mut table = RoutingTable::new([0u8; 16]);
    let mut buf = [0u8; MAX_PAYLOAD];
    unsafe { write_volatile(LED, 1) };
    // Boot line: tells the host which build runs.
    let _ = u.send(b"TRIOS-MESH 70882158 RV32I\n");
    loop {
        match read_frame(&mut u, &mut buf) {
            Ok(n) => {
                unsafe { write_volatile(LED, 3) };
                handle(&mut u, &mut table, &buf[..n]);
                unsafe { write_volatile(LED, 1) };
            }
            Err(e) => {
                u.resync();
                let _ = u.send(&[b'E', 4, e as u8]);
            }
        }
    }
}

#[panic_handler]
fn panic(_: &PanicInfo) -> ! {
    let mut u = Uart;
    loop {
        let _ = u.send(b"!");
    }
}
