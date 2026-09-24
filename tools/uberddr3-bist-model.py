#!/usr/bin/env python3
"""Address coverage and address-line blindness of UberDDR3's built-in self-test.

A model of the calibration self-test of UberDDR3 79d8fd3e
(rtl/ddr3_controller.v, pinned in fpga/ax7203/ddr3/uberddr3.lock), with the
parameters of fpga/ax7203/ddr3/tms_ddr3_ax7203.v: ROW 15, COL 10, BA 3,
ECC_ENABLE 0 (so the Wishbone address is {row, bank, col[9:3]}, L1381-1388),
MICRON_SIM 0, SECOND_WISHBONE 0. The Wishbone address counts BL8 bursts:
25 bits, 2^25 bursts, 16 bytes each in x16 (512 MiB, chip U6), 32 in x32.

The self-test runs once, inside calibration, in three phases on two counters
(write and read) that are zeroed only in IDLE:

- burst write, then burst read (L3181-3252): the address is the counter;
- random write, then random read (L3254-3306): row <= counter[ROW-1:0],
  {bank, col} <= counter[top:ROW] (the fields are swapped so that each step
  opens a new row);
- alternating write/read (L3308-3325): the address is the counter, each
  address is read right after it is written, and the phase leaves on the cycle
  that writes the last address, which is therefore never read.

With BIST_MODE 1 the phases share one sweep of the counter: burst
[0, 2^23), random [2^23, 3 * 2^23), alternating [3 * 2^23, 2^25). With
BIST_MODE 2 each phase sweeps the whole counter (the counters are zeroed at the
end of every phase). The data written and expected is a function of
counter[7:0] only (calib_data_randomized L3553-3561, correct_data L3594-3602).

Two results:

- coverage(): which burst addresses are written and read back. BIST_MODE 1
  reads 25,165,823 of the 33,554,432 bursts; rows 8192-24575 of banks 0, 1, 6
  and 7 are never written or read.
- blind_bits(): the logical address bits whose loss (stuck at 0 or 1, or not
  decoded, as on a 2 Gb part without A14) cannot produce a wrong read, because
  the two addresses that collide always carry the same data. For the parameters
  above: bank[1], bank[2] and row[8]..row[14] (Wishbone bits 8, 9 and 18-24).
  This is the address map only: a lost bank bit also confuses the controller's
  open-row bookkeeping, which is a protocol error the model does not cover.

simulate() runs the three phases operation by operation with an injected
address fault and counts wrong reads; the tests use it on a reduced geometry to
check coverage() and blind_bits() against it. At full size it takes minutes
per fault in pure Python, so the command line runs the fast forms only.

  python3 tools/uberddr3-bist-model.py              JSON for BIST_MODE 1 and 2 at full size
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Geometry:
    row_bits: int = 15
    col_bits: int = 10
    ba_bits: int = 3
    data_bits: int = 8      # the test data depends on counter[data_bits-1:0]
    burst_log2: int = 3     # log2(serdes_ratio * 2) = log2(8): the column bits a burst covers

    @property
    def addr_bits(self) -> int:
        return self.row_bits + self.col_bits + self.ba_bits - self.burst_log2

    @property
    def low_bits(self) -> int:
        """Bank plus column bits of the Wishbone address (below the row)."""
        return self.addr_bits - self.row_bits


def _phase_ranges(g: Geometry, mode: int):
    n = g.addr_bits
    quarter = 1 << (n - 2)
    if mode == 1:
        return (0, quarter), (quarter, 3 * quarter), (3 * quarter, 1 << n)
    if mode == 2:
        return (0, 1 << n), (0, 1 << n), (0, 1 << n)
    raise ValueError("BIST_MODE must be 1 or 2")


def random_address(g: Geometry, counter: int) -> int:
    """The address of the random phase: row <= counter[ROW-1:0], {bank, col} <= counter[top:ROW]."""
    row = counter & ((1 << g.row_bits) - 1)
    low = counter >> g.row_bits
    return (row << g.low_bits) | low


def data_of(g: Geometry, counter: int) -> int:
    return counter & ((1 << g.data_bits) - 1)


def operations(g: Geometry, mode: int):
    """Every (write, address, counter) of the self-test in order; write False is a checked read."""
    burst, rand, alt = _phase_ranges(g, mode)
    for c in range(*burst):
        yield True, c, c
    for c in range(*burst):
        yield False, c, c
    for c in range(*rand):
        yield True, random_address(g, c), c
    for c in range(*rand):
        yield False, random_address(g, c), c
    last = alt[1] - 1
    for c in range(*alt):
        yield True, c, c
        if c != last:
            yield False, c, c


def simulate(g: Geometry, mode: int, fault=None):
    """Run the self-test on a memory whose cell of address a is fault(a) (identity when None).

    Returns (wrong_reads, written, read): the count of reads whose data differs
    from correct_data, and the sets of addresses the controller wrote and read."""
    cell = fault or (lambda a: a)
    memory = {}
    wrong, written, read = 0, set(), set()
    for write, address, counter in operations(g, mode):
        if write:
            memory[cell(address)] = data_of(g, counter)
            written.add(address)
        else:
            read.add(address)
            if memory.get(cell(address)) != data_of(g, counter):
                wrong += 1
    return wrong, written, read


def coverage(g: Geometry = Geometry(), mode: int = 1) -> dict:
    """How often each burst address is read back, computed with slices (full size in about a second)."""
    n = g.addr_bits
    size = 1 << n
    inc = bytes(min(i + 1, 255) for i in range(256))
    burst, rand, alt = _phase_ranges(g, mode)

    def sweep(alt_end):
        hits = bytearray(size)
        hits[burst[0]:burst[1]] = hits[burst[0]:burst[1]].translate(inc)
        # Random phase: counter c gives low part c >> ROW and row c & rowmask; a counter
        # range aligned to 2^ROW covers every row of each low part in it.
        step = 1 << g.row_bits
        assert rand[0] % step == 0 and rand[1] % step == 0
        stride = 1 << g.low_bits
        for low in range(rand[0] >> g.row_bits, rand[1] >> g.row_bits):
            hits[low::stride] = hits[low::stride].translate(inc)
        hits[alt[0]:alt_end] = hits[alt[0]:alt_end].translate(inc)
        return hits

    writes = sweep(alt[1])
    reads = sweep(alt[1] - 1)              # the last write of the alternating phase is not read
    never = reads.count(0)
    result = {
        "bist_mode": mode,
        "burst_addresses": size,
        "written": size - writes.count(0),
        "read_back": size - never,
        "never_read": never,
        "never_written": writes.count(0),
        "written_never_read": _written_never_read(writes, reads),
        "read_more_than_once": size - never - reads.count(1),
        "fraction_read": (size - never) / size,
    }
    # The rows of each bank of which no burst address is read back.
    by_bank = {}
    bank_shift = g.col_bits - g.burst_log2
    for bank in range(1 << g.ba_bits):
        rows = []
        for row in range(1 << g.row_bits):
            base = (row << g.low_bits) | (bank << bank_shift)
            if reads[base:base + (1 << bank_shift)].count(0) == 1 << bank_shift:
                rows.append(row)
        if rows:
            by_bank[bank] = _ranges(rows)
    result["banks_rows_never_read"] = by_bank
    return result


def _written_never_read(writes, reads, limit=8):
    size = len(writes)
    written = int.from_bytes(writes.translate(bytes([0] + [1] * 255)), "big")
    unread = int.from_bytes(reads.translate(bytes([1] + [0] * 255)), "big")
    both = (written & unread).to_bytes(size, "big")
    out, at = [], both.find(1)
    while at >= 0 and len(out) < limit:
        out.append(at)
        at = both.find(1, at + 1)
    return out


def _ranges(values):
    out, start, prev = [], None, None
    for v in values:
        if start is None:
            start = prev = v
        elif v == prev + 1:
            prev = v
        else:
            out.append([start, prev])
            start = prev = v
    if start is not None:
        out.append([start, prev])
    return out


def data_bits_by_phase(g: Geometry = Geometry()) -> dict:
    """The Wishbone address bits that feed counter[data_bits-1:0], per phase."""
    direct = set(range(g.data_bits))                                    # counter = address
    rand = {g.low_bits + i for i in range(min(g.data_bits, g.row_bits))}  # counter[k] = row[k]
    return {"burst": sorted(direct), "random": sorted(rand), "alternating": []}


def blind_bits(g: Geometry = Geometry()) -> list:
    """Address bits whose loss never changes the data at a collision.

    Two addresses that differ only in bit b meet in one cell when b is lost. In
    the burst and random phases every write precedes every read, so a read gets
    the last write to its cell; that write carries the same data unless b feeds
    counter[data_bits-1:0] in that phase. The alternating phase reads each
    address right after writing it and sees no collision at all."""
    seen = set()
    for bits in data_bits_by_phase(g).values():
        seen |= set(bits)
    return [b for b in range(g.addr_bits) if b not in seen]


def name_of(g: Geometry, bit: int) -> str:
    cols = g.col_bits - g.burst_log2
    if bit < cols:
        return f"col[{bit + g.burst_log2}]"
    if bit < g.low_bits:
        return f"bank[{bit - cols}]"
    return f"row[{bit - g.low_bits}]"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.parse_args()
    g = Geometry()
    blind = blind_bits(g)
    print(json.dumps({
        "model": "UberDDR3 79d8fd3e self-test, ROW 15 COL 10 BA 3, ECC_ENABLE 0",
        "wishbone_address_bits": g.addr_bits,
        "coverage": [coverage(g, 1), coverage(g, 2)],
        "data_bits_by_phase": {k: [name_of(g, b) for b in v] for k, v in data_bits_by_phase(g).items()},
        "blind_address_bits": [name_of(g, b) for b in blind],
    }, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
