"""UART capture shared by the AX7203 host tools (fpga-capture.py, fpga-bram-capture.py).

Both designs print fixed 20-byte report lines, start a run after configuration
and again on a falling edge of RX, and end a run with a `D` line.
"""
from __future__ import annotations

import time


def read_port(port, baud, timeout, trigger, settle=0.5, quiet=0.5, trigger_byte=0xFF):
    """Trigger one run and return its bytes, from its `H` line through its `D` line.

    Opening the port can glitch the device's RX line and start a run, so wait for
    the settle time and then until the line has been quiet before triggering.
    The current trace player and the block-RAM bench accept a start only while
    idle; players built before 2026-09-22 (f07a667 and earlier) queued one more
    start for a trigger byte with several falling edges, so a byte such as "r"
    (three edges) gave two runs there. 0xFF has exactly one falling edge (the
    start bit) and gives one run on both, so it stays the default.
    """
    import serial  # pyserial

    with serial.Serial(port, baud, timeout=0.1) as link:
        time.sleep(settle)
        drain_deadline = time.monotonic() + max(timeout, 15.0)
        last_byte = time.monotonic()
        while time.monotonic() < drain_deadline and time.monotonic() - last_byte < quiet:
            if link.read(4096):
                last_byte = time.monotonic()
        link.reset_input_buffer()
        if trigger:
            link.write(bytes([trigger_byte]))
        deadline = time.monotonic() + timeout
        data = bytearray()
        while time.monotonic() < deadline:
            chunk = link.read(4096)
            if chunk:
                data += chunk
                # Stop at the first complete D line after this capture's own H line.
                start = data.find(b"H")
                if start >= 0:
                    done = data.find(b"\nD", start)
                    if done >= 0 and data.find(b"\n", done + 1) >= 0:
                        break
        return bytes(data)
