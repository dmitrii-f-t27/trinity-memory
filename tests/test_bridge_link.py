"""Host link of the Bridge's fpga backend (issue #64) without a board.

(a) Differential: the t27 frame encoder, CRC-32, line check, response-word fields, stream
    decoder, words-per-row and result checksum of t27/fpga_link.t27 (through the native library)
    against tools/uart_loader_protocol.py (the loader's protocol, frames L R S B, lines H A N C)
    and tools/bridge_link_protocol.py (the matvec extension, frames X M, lines Y Z), on random
    inputs and on random byte streams with corrupted lines, read-back frames with bad CRCs and
    bad lengths, stray A5 bytes, garbage and a stream cut off at the end.
(c) The serial OS hooks of native/platform.c on a pseudo-terminal: refusals (no path, not a
    terminal, unsupported rate), the rates this host can set, the exclusive open (a second open
    of the same device fails on Linux; macOS pseudo-terminals ignore TIOCEXCL, so there it is not
    asserted), a read that times out (0 after the timeout, also when a signal arrives meanwhile),
    partial reads, a write larger than the pty's buffer against a slow reader (every byte
    arrives, in order), a write that cannot finish in time (-1), hang-up (-1), drain and close.
    The WebAssembly stubs of the same hooks compile for wasm32 and return -1 (when zig or a wasm
    clang, a wasm linker and node are available).
"""
from __future__ import annotations

import ctypes as C
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tty
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import bridge_link_protocol as link  # noqa: E402
import uart_loader_protocol as proto  # noqa: E402

try:
    from trinity_memory import _native as n
    n.library()
except Exception:  # noqa: BLE001
    n = None

U32, U64, I64, I32 = C.c_uint32, C.c_uint64, C.c_int64, C.c_int32


class TLEvent(C.Structure):
    _fields_ = [("kind", C.c_uint32), ("tag", C.c_uint32), ("a", C.c_uint32), ("v", C.c_uint32), ("check_ok", C.c_bool),
                ("seq", C.c_uint32), ("addr", C.c_uint32), ("data_offset", C.c_size_t), ("data_size", C.c_size_t),
                ("crc_ok", C.c_bool)]


def t27_frame(cmd, seq, addr, length, payload=b""):
    out = n.buffer(len(payload) + 64)
    size = n.call("tl_frame", I64, [U32, U32, U32, U32, n.U8, n.SZ, n.U8, n.SZ],
                  cmd, seq, addr, length, n.octets(payload), len(payload), out, len(payload) + 64)
    assert size >= 0
    return bytes(out[:size])


def t27_events(stream: bytes, tags: int):
    """Events of the t27 decoder over a whole stream, garbage bytes grouped into runs, the bytes left
    at the end (an incomplete line or frame) as garbage: what StreamDecoder.feed + finish report."""
    buffer = n.octets(stream)
    out, garbage, pos = [], bytearray(), 0
    event = TLEvent()
    decode = n.function("tl_decode", I64, (n.U8, n.SZ, U32, C.POINTER(TLEvent)))
    base = C.addressof(buffer)
    while pos < len(stream):
        used = decode(C.cast(base + pos, n.U8), len(stream) - pos, tags, C.byref(event))
        if used == 0:
            garbage += stream[pos:]
            break
        if event.kind == 3:
            garbage += stream[pos:pos + used]
        else:
            if garbage:
                out.append(("garbage", bytes(garbage)))
                garbage = bytearray()
            if event.kind == 1:
                out.append(("line", chr(event.tag), event.a, event.v, event.check_ok))
            else:
                start = pos + event.data_offset
                out.append(("readback", event.seq, event.addr, stream[start:start + event.data_size], event.crc_ok))
        pos += used
    if garbage:
        out.append(("garbage", bytes(garbage)))
    return out


def python_events(stream: bytes, decoder):
    out = []
    for e in decoder.feed(stream) + decoder.finish():
        if e.kind == "garbage":
            if out and out[-1][0] == "garbage":
                out[-1] = ("garbage", out[-1][1] + e.raw)
            else:
                out.append(("garbage", e.raw))
        elif e.kind == "line":
            out.append(("line", e.tag, e.a, e.v, e.check_ok))
        else:
            out.append(("readback", e.seq, e.addr, e.data, e.crc_ok))
    return out


def random_stream(rng: random.Random, tags: str) -> bytes:
    parts = []
    for _ in range(rng.randint(1, 40)):
        kind = rng.random()
        if kind < 0.35:
            tag = rng.choice(tags + "Q")
            line = bytearray(proto.format_line(tag, rng.getrandbits(32), rng.getrandbits(32)))
            if rng.random() < 0.15:
                line[rng.randrange(1, 19)] = ord(rng.choice("0123456789abcdefgA "))
            if rng.random() < 0.05:
                line[19] = ord("x")
            parts.append(bytes(line))
        elif kind < 0.6:
            data = bytes(rng.randrange(256) for _ in range(rng.choice((1, 2, 5, 16, 100))))
            frame = bytearray(proto.readback_frame(rng.randrange(256), rng.getrandbits(32), data))
            if rng.random() < 0.3:
                frame[rng.randrange(2, len(frame))] ^= 1 << rng.randrange(8)
            if rng.random() < 0.05:
                frame[8], frame[9] = 0, 0x20              # length 8192: not a frame
            parts.append(bytes(frame))
        elif kind < 0.75:
            parts.append(bytes([0xA5]) + bytes(rng.randrange(256) for _ in range(rng.randint(0, 12))))
        else:
            parts.append(bytes(rng.randrange(256) for _ in range(rng.randint(1, 30))))
    stream = b"".join(parts)
    if rng.random() < 0.3:
        stream = stream[:-rng.randint(1, 15)]
    return stream


@unittest.skipUnless(n, "native library required (tools/build-t27.sh)")
class Differential(unittest.TestCase):
    def test_crc32_is_zlibs(self):
        rng = random.Random(1)
        for size in (0, 1, 2, 7, 64, 4096, 4110):
            data = bytes(rng.randrange(256) for _ in range(size))
            got = n.call("tl_crc32", U32, [n.U8, n.SZ, n.SZ], n.octets(b"xx" + data), 2, size)
            self.assertEqual(got, zlib.crc32(data))

    def test_frames_are_the_protocols(self):
        rng = random.Random(2)
        for _ in range(400):
            seq, addr = rng.randrange(256), rng.getrandbits(32)
            payload = bytes(rng.randrange(256) for _ in range(rng.choice((1, 3, 80, 4080, 4096))))
            self.assertEqual(t27_frame(proto.CMD_LOAD, seq, addr, len(payload), payload), proto.load_frame(seq, addr, payload))
            self.assertEqual(t27_frame(link.CMD_ACT, seq, addr & 1023, len(payload), payload),
                             link.act_frame(seq, addr & 1023, payload))
            length = rng.randrange(1, 4097)
            self.assertEqual(t27_frame(proto.CMD_READ, seq, addr, length), proto.read_frame(seq, addr, length))
            self.assertEqual(t27_frame(proto.CMD_STATUS, seq, 0, 0), proto.status_frame(seq))
            self.assertEqual(t27_frame(proto.CMD_BAUD, seq, addr & 0xFFFF, 0), proto.baud_frame(seq, addr & 0xFFFF))
            rows, cols, fmt = rng.randrange(1, 1025), rng.randrange(1, 81921), rng.randrange(2)
            self.assertEqual(t27_frame(link.CMD_MATVEC, seq, addr & ~15, 12, link.matvec_payload(rows, cols, fmt)),
                             link.matvec_frame(seq, addr & ~15 & proto.M32, rows, cols, fmt))

    def test_line_check_and_response_fields(self):
        rng = random.Random(3)
        for _ in range(3000):
            tag, a, v = rng.choice(b"HANCYZ"), rng.getrandbits(32), rng.getrandbits(32)
            self.assertEqual(n.call("tl_line_check", U32, [U32, U32, U32], tag, a, v), proto.line_check(tag, a, v))
            fields = proto.decode_resp_word(a)
            self.assertEqual(n.call("tl_resp_seq", U32, [U32], a), fields["seq"])
            self.assertEqual(n.call("tl_resp_reason", U32, [U32], a), fields["reason"])
            self.assertEqual(n.call("tl_resp_len", U32, [U32], a), fields["len"])
            self.assertEqual(n.call("tl_resp_index", U32, [U32], a), (a >> 17) & 7)
        for cmd, index in link.CMD_INDEX.items():
            word = link.resp_word(9, True, 1, cmd, 12)
            self.assertEqual(n.call("tl_resp_index", U32, [U32], word), index)

    def test_decoder_is_the_loaders_and_the_extensions(self):
        rng = random.Random(4)
        for trial in range(600):
            for tags, mask, decoder in (("HANC", 15, proto.StreamDecoder()), ("HANCYZ", 63, link.LinkDecoder())):
                stream = random_stream(rng, tags)
                self.assertEqual(t27_events(stream, mask), python_events(stream, decoder), (trial, tags, stream.hex()))
        # Y and Z lines are garbage to the loader's decoder, lines to the link's.
        line = proto.format_line("Y", link.y_word(3, 7), 12345)
        self.assertEqual(t27_events(line, 15), [("garbage", line)])
        self.assertEqual(t27_events(line, 63)[0][:4], ("line", "Y", link.y_word(3, 7), 12345))

    def test_row_arithmetic_and_checksum(self):
        rng = random.Random(5)
        for _ in range(500):
            cols, fmt = rng.randrange(1, 100000), rng.randrange(2)
            self.assertEqual(n.call("tl_words_per_row", U64, [U64, U32], cols, fmt), link.words_per_row(cols, fmt))
            values = [rng.randint(-884736, 884736) for _ in range(rng.randrange(1, 50))]
            array = (C.c_int64 * len(values))(*values)
            self.assertEqual(n.call("tl_result_checksum", U32, [n.I64, n.SZ], array, len(values)), link.result_checksum(values))
            v = rng.getrandbits(32)
            self.assertEqual(n.call("tl_signed32", I64, [U32], v), link.signed32(v))


def pty_pair():
    master, slave = os.openpty()
    tty.setraw(slave)
    return master, slave, os.ttyname(slave)


def open_port(path: str, baud: int = 115200) -> int:
    data = path.encode()
    return n.call("tm_os_serial_open", I32, [n.U8, n.SZ, U32], n.octets(data), len(data), baud)


def read_port(fd: int, capacity: int, timeout_ms: int):
    out = n.buffer(capacity)
    got = n.call("tm_os_serial_read", I64, [I32, n.U8, n.SZ, U32], fd, out, capacity, timeout_ms)
    return got, bytes(out[:max(got, 0)])


def write_port(fd: int, data: bytes, timeout_ms: int) -> int:
    return n.call("tm_os_serial_write", I64, [I32, n.U8, n.SZ, U32], fd, n.octets(data), len(data), timeout_ms)


def has_cap_sys_admin() -> bool:
    """Whether this process holds CAP_SYS_ADMIN (bit 21 of CapEff in /proc/self/status, Linux)."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("CapEff:"):
                return bool(int(line.split()[1], 16) >> 21 & 1)
    except OSError:
        pass
    return False


@unittest.skipUnless(n, "native library required (tools/build-t27.sh)")
class SerialHooks(unittest.TestCase):
    def test_refusals(self):
        self.assertEqual(open_port("/nonexistent/tty"), -1)
        self.assertEqual(open_port("/dev/null"), -1)              # not a terminal
        master, slave, path = pty_pair()
        try:
            self.assertEqual(open_port(path, 12345), -1)           # not a standard rate
            self.assertEqual(n.call("tm_os_serial_open", I32, [n.U8, n.SZ, U32], n.octets(b"a\0b"), 3, 115200), -1)
            self.assertEqual(read_port(-1, 8, 10)[0], -1)
            self.assertEqual(write_port(-1, b"x", 10), -1)
        finally:
            os.close(master)
            os.close(slave)

    def test_rates_and_exclusive_open(self):
        rate_ok = [n.call("tm_os_serial_rate_ok", I32, [U32], b) for b in (9600, 19200, 38400, 57600, 115200, 230400)]
        self.assertEqual(rate_ok, [1] * 6)
        self.assertEqual(n.call("tm_os_serial_rate_ok", I32, [U32], 12345), 0)
        high = [n.call("tm_os_serial_rate_ok", I32, [U32], b) for b in (460800, 921600)]
        if sys.platform == "darwin":
            self.assertEqual(high, [0, 0])                     # <sys/termios.h> stops at B230400
        elif sys.platform.startswith("linux"):
            self.assertEqual(high, [1, 1])
        master, slave, path = pty_pair()
        try:
            for baud, ok in zip((460800, 921600), high):
                fd = open_port(path, baud)
                self.assertEqual(fd >= 0, bool(ok), baud)
                if fd >= 0:
                    n.call("tm_os_serial_close", I32, [I32], fd)
            first = open_port(path)
            self.assertGreaterEqual(first, 0)
            second = open_port(path)
            if sys.platform.startswith("linux"):
                # TIOCEXCL: EBUSY for the second opener, unless it has CAP_SYS_ADMIN (the kernel's
                # tty_reopen lets such a process in; root in a container usually has it).
                self.assertEqual(second >= 0, has_cap_sys_admin(), second)
            if second >= 0:
                n.call("tm_os_serial_close", I32, [I32], second)
            self.assertEqual(n.call("tm_os_serial_close", I32, [I32], first), 0)
            third = open_port(path)                            # closing released the exclusive open
            self.assertGreaterEqual(third, 0)
            n.call("tm_os_serial_close", I32, [I32], third)
        finally:
            os.close(master)
            os.close(slave)

    def test_reads_time_out_and_return_partial_data(self):
        master, slave, path = pty_pair()
        fd = open_port(path)
        self.assertGreaterEqual(fd, 0)
        try:
            start = time.monotonic()
            got, _ = read_port(fd, 64, 150)
            elapsed = time.monotonic() - start
            self.assertEqual(got, 0)
            self.assertGreaterEqual(elapsed, 0.14)
            self.assertLess(elapsed, 1.0)
            os.write(master, b"hello")
            self.assertEqual(read_port(fd, 64, 500), (5, b"hello"))
            os.write(master, bytes(range(100)))
            time.sleep(0.05)
            first = read_port(fd, 10, 500)
            self.assertEqual(first, (10, bytes(range(10))))
            rest = b""
            while len(rest) < 90:
                got, data = read_port(fd, 1000, 500)
                self.assertGreater(got, 0)
                rest += data
            self.assertEqual(rest, bytes(range(10, 100)))
            # Bytes that arrive during the wait end it at once.
            threading.Timer(0.1, lambda: os.write(master, b"late")).start()
            start = time.monotonic()
            self.assertEqual(read_port(fd, 64, 2000), (4, b"late"))
            self.assertLess(time.monotonic() - start, 1.0)
        finally:
            n.call("tm_os_serial_close", I32, [I32], fd)
            os.close(master)
            os.close(slave)

    def test_a_signal_does_not_end_the_wait(self):
        master, slave, path = pty_pair()
        fd = open_port(path)
        fired = []
        previous = signal.signal(signal.SIGALRM, lambda *_: fired.append(time.monotonic()))
        try:
            signal.setitimer(signal.ITIMER_REAL, 0.05, 0.05)
            start = time.monotonic()
            got, _ = read_port(fd, 16, 300)
            elapsed = time.monotonic() - start
            signal.setitimer(signal.ITIMER_REAL, 0)
            self.assertEqual(got, 0)
            self.assertGreaterEqual(elapsed, 0.29)
            self.assertGreaterEqual(len(fired), 1)      # Python runs the handler after the call returns
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
            n.call("tm_os_serial_close", I32, [I32], fd)
            os.close(master)
            os.close(slave)

    def test_large_writes_resume_after_partial_writes(self):
        master, slave, path = pty_pair()
        fd = open_port(path)
        data = bytes(random.Random(6).randrange(256) for _ in range(256 * 1024))
        received = bytearray()

        def reader():
            while len(received) < len(data):
                try:
                    chunk = os.read(master, 1000)
                except OSError:
                    return
                received.extend(chunk)
                time.sleep(0.001)

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            self.assertEqual(write_port(fd, data, 20000), len(data))
            self.assertEqual(n.call("tm_os_serial_drain", I32, [I32], fd), 0)
            thread.join(10)
            self.assertEqual(bytes(received), data)
            # Nobody reads now: a write larger than the buffers cannot finish in 200 ms.
            start = time.monotonic()
            self.assertEqual(write_port(fd, bytes(4 * 1024 * 1024), 200), -1)
            self.assertLess(time.monotonic() - start, 2.0)
        finally:
            n.call("tm_os_serial_close", I32, [I32], fd)
            os.close(master)
            os.close(slave)
            thread.join(2)

    def test_hang_up_and_close(self):
        master, slave, path = pty_pair()
        fd = open_port(path)
        os.close(slave)
        os.close(master)                                 # the device side goes away
        got, _ = read_port(fd, 16, 500)
        self.assertEqual(got, -1)
        self.assertEqual(n.call("tm_os_serial_close", I32, [I32], fd), 0)
        self.assertEqual(n.call("tm_os_serial_close", I32, [I32], fd), -1)
        self.assertEqual(n.call("tm_os_serial_close", I32, [I32], -1), -1)
        a = n.call("tm_os_monotonic_ns", U64, [])
        time.sleep(0.01)
        self.assertGreater(n.call("tm_os_monotonic_ns", U64, []), a)


def wasm_compiler():
    if shutil.which("zig"):
        return ["zig", "cc", "-target", "wasm32-freestanding"]
    clang = shutil.which("clang")
    if clang and "wasm32" in subprocess.run([clang, "--print-targets"], capture_output=True, text=True).stdout:
        return [clang, "--target=wasm32"]
    return None


def wasm_linker():
    """zig's wasm-ld, WASM_LD, wasm-ld, or a versioned wasm-ld-NN (Ubuntu's lld-NN packages
    install only /usr/bin/wasm-ld-NN; /usr/bin/wasm-ld comes with the unversioned lld)."""
    if shutil.which("zig"):
        return ["zig", "wasm-ld"]
    for name in (os.environ.get("WASM_LD"), "wasm-ld"):
        if name and shutil.which(name):
            return [shutil.which(name)]
    versioned = sorted({Path(d) / f for d in os.environ.get("PATH", "").split(os.pathsep) if d and Path(d).is_dir()
                        for f in os.listdir(d) if f.startswith("wasm-ld-") and f[8:].isdigit()},
                       key=lambda p: int(p.name[8:]), reverse=True)
    return [str(versioned[0])] if versioned else None


@unittest.skipUnless(wasm_compiler() and wasm_linker() and shutil.which("node"),
                     "zig or a wasm clang, a wasm linker, and node, required")
class WasmStubs(unittest.TestCase):
    def test_serial_hooks_fail_cleanly_in_wasm(self):
        with tempfile.TemporaryDirectory() as work:
            obj, wasm = Path(work) / "platform.o", Path(work) / "platform.wasm"
            subprocess.run([*wasm_compiler(), "-std=c11", "-O2", "-ffreestanding", "-nostdlib", "-c",
                            str(ROOT / "native/platform.c"), "-o", str(obj)], check=True, capture_output=True)
            exports = ["tm_os_serial_rate_ok", "tm_os_serial_open", "tm_os_serial_read", "tm_os_serial_write",
                       "tm_os_serial_drain", "tm_os_serial_close", "tm_os_monotonic_ns"]
            subprocess.run([*wasm_linker(), "--no-entry", *[f"--export={name}" for name in exports], str(obj), "-o", str(wasm)],
                           check=True, capture_output=True)
            script = ("const m=new WebAssembly.Instance(new WebAssembly.Module(require('fs').readFileSync(process.argv[1]))).exports;"
                      "console.log(JSON.stringify([m.tm_os_serial_rate_ok(115200),m.tm_os_serial_open(0,0,115200),Number(m.tm_os_serial_read(0,0,1,1)),"
                      "Number(m.tm_os_serial_write(0,0,1,1)),m.tm_os_serial_drain(0),m.tm_os_serial_close(0),"
                      "Number(m.tm_os_monotonic_ns())]))")
            result = subprocess.run(["node", "-e", script, str(wasm)], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), "[0,-1,-1,-1,-1,-1,0]")


if __name__ == "__main__":
    unittest.main()
