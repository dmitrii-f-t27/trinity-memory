"""Request/response vectors for the TriOS mesh UART front end (docs/CONTRACT.md).

Each step is (name, request bytes, expected response bytes). The expected bytes are
literals taken from the real crate's documented behaviour at gHashTag/trios
70882158c9ff07c1157ff8a1a50ce39ee172fb3c (routing.rs/packet.rs and their unit tests) and from
the independent acceptance vectors in outputs/CLAUDE-FPGA-ACCEPTANCE.md. This module
only builds bytes and compares; it contains no routing or packet logic, so a passing
run means the soft CPU computed the answers.
"""
import hashlib
import struct

BOOT = b"TRIOS-MESH 70882158 RV32I\n"
SYNC = 0xA5


def crc8(data, crc=0):
    """CRC-8, polynomial 0x07, init 0 (t27/trios_uart_guard.t27 crc8_byte; check value 0xF4)."""
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


assert crc8(b"123456789") == 0xF4


def frame(payload):
    """Request frame: SYNC, len, payload, CRC-8 over len and payload."""
    return bytes([SYNC, len(payload)]) + payload + bytes([crc8(bytes([len(payload)]) + payload)])


class Raw(bytes):
    """Bytes sent on the wire exactly as given (damaged frames, bursts)."""


def ID(x):
    return bytes([x]) * 16


def le32(v):
    return struct.pack("<I", v)


def pkt_announce(dest, sender, hops):
    return b"\x01" + dest + sender + bytes([hops])


def req_p(raw):
    return b"P" + bytes([len(raw)]) + raw


def req_a(raw, quality, now):
    return b"A" + bytes([len(raw)]) + raw + bytes([quality]) + le32(now)


def rsp_a(status, count, cost):
    return b"A" + bytes([status, count]) + struct.pack("<H", cost)


def rsp_q_found(next_hop, hops, quality, last_seen, count, cost):
    return b"Q\x01" + next_hop + bytes([hops, quality]) + le32(last_seen) + bytes([count]) + struct.pack("<H", cost)


def rsp_q_none(count):
    return b"Q\x00" + bytes(16) + bytes([0, 0]) + le32(0) + bytes([count]) + b"\xff\xff"


def rsp_p_ok(hops, ser):
    return b"P\x01" + bytes([hops]) + ser


RSP_P_NONE = b"P\x00\x00" + bytes(34)


def steps():
    s = []
    pk = bytes(range(32))
    add = lambda n, rq, rs: s.append((n, bytes(rq) if isinstance(rq, Raw) else frame(rq), rs))
    add("V1 probe", b"V", b"V" + BOOT)

    # Acceptance vector 1: packet positive (hops byte a7 -> 7 after the 0x0f mask)
    d16 = bytes(range(0x00, 0x10))
    s16 = bytes(range(0x10, 0x20))
    p1 = pkt_announce(d16, s16, 0xA7)
    assert len(p1) == 34
    add("AV1 packet positive: parse and reserialise", req_p(p1), rsp_p_ok(7, p1[:33] + b"\x07"))
    # Acceptance vector 3: packet negative (33 bytes, hops byte missing)
    add("AV3 packet negative: 33 bytes -> None", req_p(p1[:33]), RSP_P_NONE)
    add("N1 packet negative: wrong type byte -> None", req_p(b"\x02" + p1[1:]), RSP_P_NONE)

    # Acceptance vector 2: route positive
    add("AV2 new table, self ID(00)", b"N" + ID(0x00), b"N\x00")
    add("AV2 announce ID(aa) via ID(10) hops1 q10 t100 -> true, cost 21",
        req_a(pkt_announce(ID(0xAA), ID(0x10), 1), 10, 100), rsp_a(1, 1, 21))
    add("AV2 announce ID(aa) via ID(20) hops5 q0 t101 -> true, cost 5",
        req_a(pkt_announce(ID(0xAA), ID(0x20), 5), 0, 101), rsp_a(1, 1, 5))
    add("AV2 lookup: next hop ID(20), hops 5, q 0, last_seen 101, count 1",
        b"Q" + ID(0xAA), rsp_q_found(ID(0x20), 5, 0, 101, 1, 5))

    # Acceptance vector 4: route negative (equal cost does not replace, time not refreshed)
    add("AV4 new table", b"N" + ID(0x00), b"N\x00")
    add("AV4 announce ID(bb) via ID(10) hops2 q2 t100 -> true, cost 6",
        req_a(pkt_announce(ID(0xBB), ID(0x10), 2), 2, 100), rsp_a(1, 1, 6))
    add("AV4 announce ID(bb) via ID(20) hops4 q1 t110 -> false, cost stays 6",
        req_a(pkt_announce(ID(0xBB), ID(0x20), 4), 1, 110), rsp_a(2, 1, 6))
    add("AV4 lookup: next hop ID(10), hops 2, q 2, last_seen 100, count 1",
        b"Q" + ID(0xBB), rsp_q_found(ID(0x10), 2, 2, 100, 1, 6))
    add("AV4 expire now=120 ttl=20 removes the route (rejected announce did not refresh)",
        b"T" + le32(120) + le32(20), b"T\x00")
    add("AV4 lookup after expiry: none", b"Q" + ID(0xBB), rsp_q_none(0))

    # Extra negatives and boundaries from the crate's own tests
    add("X1 new table, self ID(ab)", b"N" + ID(0xAB), b"N\x00")
    add("X1 local delivery: next_hop(self) is None", b"Q" + ID(0xAB), rsp_q_none(0))
    add("X2 unknown destination: none", b"Q" + ID(0xCC), rsp_q_none(0))
    add("X3 announce with a short buffer is rejected, table unchanged",
        req_a(pkt_announce(ID(1), ID(2), 1)[:20], 0, 0), rsp_a(0, 0, 0xFFFF))
    add("X4 route cost formula: hops 3 q 1 -> 5", b"C\x03\x01", b"C\x05\x00")
    add("X4 route cost GF16 clamp: hops ff q ff -> 45", b"C\xff\xff", b"C\x2d\x00")
    add("X5 quality overrides hop count (crate test): new table", b"N" + ID(0x00), b"N\x00")
    add("X5 path A hops2 q15 t100 -> cost 32",
        req_a(pkt_announce(ID(0xAA), ID(0x10), 2), 15, 100), rsp_a(1, 1, 32))
    add("X5 path B hops3 q0 t101 -> cost 3, replaces",
        req_a(pkt_announce(ID(0xAA), ID(0x20), 3), 0, 101), rsp_a(1, 1, 3))
    add("X6 table full: new table", b"N" + ID(0x00), b"N\x00")
    for i in range(1, 17):
        add("X6 insert destination %d" % i,
            req_a(pkt_announce(ID(0x40 + i), ID(0x10), 1), 0, i), rsp_a(1, i, 1))
    add("X6 17th destination is rejected, count stays 16",
        req_a(pkt_announce(ID(0x60), ID(0x10), 1), 0, 17), rsp_a(2, 16, 0xFFFF))
    # Identity hash (sha2 on the soft CPU) against hashlib, an independent oracle
    add("H1 identity: SHA-256(pubkey)[0:16]", b"H" + pk, b"H" + hashlib.sha256(pk).digest()[:16])
    add("E1 unknown command", b"Z", b"E\x02Z")
    add("E2 oversize packet length is refused", b"P" + bytes([70]) + bytes(70), b"E\x03P")
    add("E3 payload length does not match the command", b"N" + bytes(5), b"E\x05N")

    # Framing, overflow and recovery. Table state survives every error path.
    add("S1 new table", b"N" + ID(0x00), b"N\x00")
    add("S1 announce ID(77) via ID(10) hops1 q0 t5 -> cost 1",
        req_a(pkt_announce(ID(0x77), ID(0x10), 1), 0, 5), rsp_a(1, 1, 1))
    burst = frame(b"H" + pk) + frame(b"V") * 24
    add("B1 overflow: a request, then 96 more bytes while the CPU computes and answers; the lost bytes "
        "are reported, none of the buffered frames runs",
        Raw(burst), b"H" + hashlib.sha256(pk).digest()[:16] + b"E\x04\x01")
    add("B1 after the overflow the line works and the table is intact",
        b"Q" + ID(0x77), rsp_q_found(ID(0x10), 1, 0, 5, 1, 1))
    good = frame(b"V")
    add("B2 truncated frame (length 32, 5 bytes sent) times out", Raw(bytes([SYNC, 32]) + bytes(5)), b"E\x04\x02")
    add("B3 wrong CRC is refused, nothing executed", Raw(good[:-1] + bytes([good[-1] ^ 1])), b"E\x04\x04")
    add("B4 length byte 0xff is refused", Raw(bytes([SYNC, 0xFF])), b"E\x04\x03")
    add("B4 length byte 0 is refused", Raw(bytes([SYNC, 0x00])), b"E\x04\x03")
    add("B5 garbage before the sync byte is skipped", Raw(b"\x00\x11\x7f" + good), b"V" + BOOT)
    add("B6 recovery: the announced route is still there", b"Q" + ID(0x77), rsp_q_found(ID(0x10), 1, 0, 5, 1, 1))
    add("V2 still alive after the error paths", b"V", b"V" + BOOT)
    return s


def totals():
    ss = steps()
    return sum(len(r) for _, r, _ in ss), sum(len(e) for _, _, e in ss)


if __name__ == "__main__":
    ss = steps()
    print(len(ss), "steps", totals())
