"""UART acknowledgment boundaries discovered during the matvec board run."""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import uart_loader_protocol as proto

spec = importlib.util.spec_from_file_location("matvec_uart_host", ROOT / "tools/fpga-matvec-run.py")
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


class MatvecUART(unittest.TestCase):
    def test_ack_at_start_and_across_read_boundary(self):
        ack = proto.format_line("A", proto.resp_word(1, True, 0, proto.CMD_LOAD, 16), 1)
        self.assertTrue(host.load_acknowledged(ack))
        self.assertTrue(host.load_acknowledged(b"y000000000000000000\n" + ack))
        self.assertFalse(host.load_acknowledged(ack[:-1]))
        self.assertFalse(host.load_acknowledged(ack, seq=2))
        corrupt = ack[:10] + (b"1" if ack[10:11] != b"1" else b"2") + ack[11:]
        self.assertFalse(host.load_acknowledged(corrupt))
        status_ack = proto.format_line("A", proto.resp_word(1, True, 0, proto.CMD_STATUS, 0), 1)
        self.assertFalse(host.load_acknowledged(status_ack))


if __name__ == "__main__":
    unittest.main()
