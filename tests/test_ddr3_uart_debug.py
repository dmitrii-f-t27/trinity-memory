"""The `uart_tx` of the UART_DEBUG_BIST build (fpga/ax7203/ddr3/uart_tx_t27.v) in Icarus.

UberDDR3's controller drives it with its own handshake (ddr3_controller.v 79d8fd3e,
UART_FSM_SEND_BYTE / UART_FSM_WAIT_SEND): uart_tx_en with the byte while busy is
low, en lowered once busy is seen, the next byte once busy is low again. The bench
reproduces that handshake and checks every frame on the line: start bit, eight data
bits LSB first, a stop bit of a whole bit period, each byte exactly once.

Runs with T27_ROOT set and Icarus installed (tools/test-t27.sh); skipped otherwise.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPILER = Path(os.environ["T27_ROOT"]) / "target/release/t27c" if os.environ.get("T27_ROOT") else None
HAVE_TOOLS = bool(COMPILER and COMPILER.is_file() and shutil.which("iverilog") and shutil.which("vvp"))
DIV = 10          # CLK_HZ / BIT_RATE in the bench
MESSAGE = b"DONE\n\x00\xff\x01A"

BENCH = """
`timescale 1ns/1ps
module tb;
    reg clk = 0, resetn = 0, en = 0;
    reg [7:0] data = 0;
    wire txd, busy;
    uart_tx #(.BIT_RATE(10), .CLK_HZ(%(clk_hz)d)) dut (.clk(clk), .resetn(resetn), .uart_txd(txd),
        .uart_tx_busy(busy), .uart_tx_en(en), .uart_tx_data(data));
    reg [7:0] msg [0:%(last)d];
    integer i = 0, state = 0, t;
    initial begin
%(init)s
    end
    always #1 clk = !clk;
    // UberDDR3's sender: SEND_BYTE raises en while !busy and leaves once busy; WAIT_SEND waits for !busy.
    always @(posedge clk) if (resetn) begin
        case (state)
            0: if (!busy) begin en <= 1; data <= msg[i]; end else begin en <= 0; state <= 1; end
            1: if (!busy) begin if (i == %(last)d) state <= 2; else begin i <= i + 1; state <= 0; end end
            default: en <= 0;
        endcase
    end
    initial begin
        #10 resetn = 1;
        for (t = 0; t < %(clocks)d; t = t + 1) begin @(posedge clk); $write("%%0d", txd); end
        $display("");
        $finish;
    end
endmodule
"""


def frames(line: str, div: int) -> list[int]:
    """Decode 8N1 frames from one sample per clock; every stop bit must be high for a whole period."""
    out, t = [], 0
    while t < len(line):
        if line[t] == "1":
            t += 1
            continue
        mid = t + div // 2
        if mid + 9 * div >= len(line):
            break
        assert line[mid] == "0", f"false start at {t}"
        byte = sum(int(line[mid + (k + 1) * div]) << k for k in range(8))
        stop = line[t + 9 * div:t + 10 * div]
        assert stop == "1" * div, f"short stop bit after byte {len(out)}: {stop!r}"
        out.append(byte)
        t += 10 * div
    return out


@unittest.skipUnless(HAVE_TOOLS, "T27_ROOT with a built t27c and Icarus required")
class UberUartOnT27(unittest.TestCase):
    def test_handshake_sends_every_byte_once(self):
        with tempfile.TemporaryDirectory(prefix="trinity-uart-debug-") as work:
            work = Path(work)
            core = subprocess.run([str(COMPILER), "gen-verilog", str(ROOT / "t27/rtl/fpga_uart_tx.t27")],
                                  capture_output=True, text=True, check=True).stdout
            (work / "fpga_uart_tx.v").write_text(core)
            init = "\n".join(f"        msg[{k}] = 8'h{b:02x};" for k, b in enumerate(MESSAGE))
            bench = BENCH % {"clk_hz": 10 * DIV, "last": len(MESSAGE) - 1, "init": init,
                             "clocks": (len(MESSAGE) + 3) * 10 * DIV}
            (work / "tb.v").write_text(bench)
            subprocess.run(["iverilog", "-g2012", "-s", "tb", "-o", str(work / "tb.vvp"), str(work / "tb.v"),
                            str(work / "fpga_uart_tx.v"), str(ROOT / "fpga/ax7203/ddr3/uart_tx_t27.v")],
                           check=True, capture_output=True, text=True)
            run = subprocess.run(["vvp", "-n", str(work / "tb.vvp")], check=True, capture_output=True, text=True)
            line = next(text for text in run.stdout.splitlines() if set(text) <= {"0", "1"} and len(text) > 100)
            self.assertEqual(bytes(frames(line, DIV)), MESSAGE)


if __name__ == "__main__":
    unittest.main()
