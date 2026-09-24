"""The Bridge's fpga backend end to end, without a board (issue #64, part (b) of the host tests).

A BridgeServer with backend="fpga" talks over a pseudo-terminal to tests/fake_fpga_device.py, a
fake AX7203 that speaks the UART loader's protocol plus the matvec extension byte for byte
(tools/bridge_link_protocol.MatvecDevice) and computes its Y lines with t27/matvec.t27. Checked:

- capabilities (backend, hardware, transport, configured evidence; nothing read from the device),
  chip_info (checks the device first; its capture sha256 equals the sha256 of the bytes the fake
  device sent during the call), SDKMemoryBackend's acceptance rule, configuration refusals;
- compute.dot on TensorPack matrices and a vector in both device formats: accumulators, scales and
  shapes equal an emulator Bridge's for the same container, the reference count is 0, the image
  and read-back sizes, the upload cache (a second dot uploads nothing) and its invalidation by a
  device reset;
- the real chunk (q_proj rows 0-319) through the fake device in both formats: every accumulator
  equals the first 320 of t27/matvec.t27's q_proj product (the committed report). The fake device
  computes with t27/matvec.t27 itself, so this checks the host path (image, upload, read-back,
  activations, Y lines, assembly), not device arithmetic; the device arithmetic is
  tests/test_ddr3_matvec.py's;
- faults: a corrupted load chunk (crc nak), a dropped byte (timeout nak), a lost ack (the
  retransmission is answered duplicate), a corrupted Y line (the run is repeated with a new seq),
  a lost matvec ack (its Y lines are taken), garbage before a line, a device whose result is wrong
  in one row (reported under reference, not hidden), a device with another build id or protocol, a
  port that does not exist, a codec the device does not decode and an empty row.
"""
from __future__ import annotations

import hashlib
import os
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))

try:
    from trinity_memory import _native as n
    n.library()
except Exception:  # noqa: BLE001
    n = None

REQUIRE_CACHED = os.environ.get("TRINITY_REQUIRE_CACHED") == "1"
BITSTREAM = "bcf6e804" + "0" * 56
EVIDENCE = dict(bitstream_sha256=BITSTREAM, idcode="0x13636093", dna="0x00389c0c2d85e85c", build_id="1d474000")


def device(path, **extra):
    from trinity_memory.bridge import FpgaDevice
    options = dict(EVIDENCE, reply_timeout=1.0, quiet=0.08)
    options.update(extra)
    return FpgaDevice(port=path, **options)


def matrix(rng, rows, cols, codec="dense5", scales=None, name="w"):
    from trinity_memory.tensorpack import Tensor
    values = tuple(rng.choice((-1, 0, 1)) for _ in range(rows * cols))
    if scales:
        return Tensor(name, (rows, cols), values, codec=codec, scales=tuple(scales), scale_axis=0), values
    return Tensor(name, (rows, cols), values, codec=codec), values


@unittest.skipUnless(n, "native library required (tools/build-t27.sh)")
class FpgaBackend(unittest.TestCase):
    def setUp(self):
        from fake_fpga_device import FakeDevice
        self.FakeDevice = FakeDevice

    def serve(self, fake, **extra):
        from trinity_memory.bridge import BridgeClient, BridgeServer
        server = BridgeServer(backend="fpga", device=device(fake.path, **extra)).start()
        self.addCleanup(server.close)
        return BridgeClient(server.url, timeout=120)

    def fake(self, **options):
        fake = self.FakeDevice(**options)
        self.addCleanup(fake.close)
        return fake

    def emulator_dot(self, data, name, x):
        from trinity_memory.bridge import BridgeClient, BridgeServer
        with BridgeServer() as server:
            client = BridgeClient(server.url)
            return client.dot(client.upload(data), name, x)

    def test_capabilities_identity_and_sdk_rule(self):
        from trinity_memory.bridge import BridgeError, check_identity, evidence_complete
        fake = self.fake()
        client = self.serve(fake)
        caps = client.capabilities()
        self.assertEqual((caps["backend"], caps["hardware"], caps["transport"]), ("fpga", True, "uart"))
        self.assertEqual(caps["device"]["evidence"], EVIDENCE)
        self.assertEqual(caps["device"]["codecs"], ["baseline2", "dense5"])
        self.assertEqual(len(fake.rx_log), 0)                         # capabilities never touches the device
        before = len(fake.tx_log)
        for method in ("trinity_chipInfo", "chip_info"):
            start = len(fake.tx_log)
            info = client.call(method)
            self.assertEqual((info["backend"], info["hardware"], info["status"]), ("fpga", True, "memory device (fpga)"))
            self.assertEqual(info["identity_kind"], "synthetic-public-16-byte")
            evidence = info["evidence"]
            self.assertTrue(evidence_complete(evidence))
            self.assertEqual(evidence["capture_bytes"], 23 * 20)
            self.assertEqual(evidence["capture_sha256"], hashlib.sha256(bytes(fake.tx_log[start:])).hexdigest())
        self.assertGreater(len(fake.tx_log), before)
        self.assertIs(check_identity(client.call("trinity_chipInfo"))["hardware"], True)
        good = client.call("trinity_chipInfo")
        for key in ("bitstream_sha256", "capture_sha256", "idcode", "dna", "build_id"):
            broken = dict(good, evidence={k: v for k, v in good["evidence"].items() if k != key})
            with self.assertRaises(BridgeError):
                check_identity(broken)
            bad = dict(good, evidence=dict(good["evidence"], **{key: good["evidence"][key].upper() + "0"}))
            with self.assertRaises(BridgeError):
                check_identity(bad)
        for info in (dict(good, hardware=False), dict(good, backend="emulator", hardware=True), dict(good, backend="asic"),
                     dict(good, evidence=None)):
            with self.assertRaises(BridgeError):
                check_identity(info)
        self.assertIs(check_identity({"backend": "emulator", "hardware": False})["hardware"], False)

    def test_configuration_refusals(self):
        from trinity_memory.bridge import BridgeServer, FpgaDevice
        with self.assertRaises(ValueError):
            BridgeServer(backend="fpga")
        with self.assertRaises(ValueError):
            BridgeServer(backend="asic")
        with self.assertRaises(ValueError):
            BridgeServer(device=device("/dev/null"))
        for key, value in (("bitstream_sha256", "AB" * 32), ("bitstream_sha256", "ab" * 31), ("idcode", "0x3636093"),
                           ("dna", "0x389c0c2d85e85c"), ("build_id", "1d47400"), ("baud", 12345), ("region", 8),
                           ("attempts", 0), ("reply_timeout", 0)):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    BridgeServer(backend="fpga", device=device("/dev/null", **{key: value}))
        with BridgeServer() as server:
            from trinity_memory.bridge import BridgeClient
            caps = BridgeClient(server.url).capabilities()
            self.assertEqual((caps["backend"], caps["hardware"]), ("emulator", False))
            self.assertNotIn("transport", caps)
        self.assertTrue(FpgaDevice)

    def test_dot_equals_the_emulator_in_both_formats(self):
        from trinity_memory.tensorpack import Tensor, encode_tensors
        rng = random.Random(64)
        fake = self.fake()
        client = self.serve(fake)
        tensors, cases = [], []
        for index, (rows, cols, codec) in enumerate(((7, 250, "dense5"), (5, 64, "baseline2"), (64, 1000, "dense5"),
                                                     (3, 81, "baseline2"), (2, 6912, "dense5"))):
            tensor, _ = matrix(rng, rows, cols, codec, scales=[0.5 + r for r in range(rows)], name=f"m{index}")
            tensors.append(tensor)
            cases.append((f"m{index}", rows, cols, codec))
        tensors.append(Tensor("v", (300,), tuple(rng.choice((-1, 0, 1)) for _ in range(300)), codec="baseline2", scales=(2.0,)))
        cases.append(("v", 1, 300, "baseline2"))
        data = encode_tensors(tensors)
        handle = client.upload(data)
        for name, rows, cols, codec in cases:
            with self.subTest(name=name):
                x = [rng.randint(-128, 127) for _ in range(cols)]
                got = client.dot(handle, name, x)
                want = self.emulator_dot(data, name, x)
                for key in ("accumulators", "scales", "input_shape", "output_shape", "scale_applied", "arithmetic", "tensor_name"):
                    self.assertEqual(got[key], want[key], key)
                self.assertEqual((got["backend"], got["hardware"]), ("fpga", True))
                self.assertEqual(got["reference"], {"backend": "emulator", "mismatches": 0, "first_mismatch": -1})
                transfer = got["transfer"]
                wpr = -(-cols // (80 if codec == "dense5" else 64))
                self.assertEqual((transfer["codec"], transfer["image_bytes"], transfer["uploaded"]), (codec, rows * wpr * 16, True))
                self.assertEqual(transfer["readback_bytes"], rows * wpr * 16)
                self.assertEqual(transfer["device_counters"]["words"], rows * wpr)
                self.assertEqual((transfer["retransmits"], transfer["naks"], transfer["bad_lines"]), (0, 0, 0))
                # The same tensor again, other activations: nothing is uploaded.
                x2 = [rng.randint(-128, 127) for _ in range(cols)]
                again = client.dot(handle, name, x2)
                self.assertEqual(again["accumulators"], self.emulator_dot(data, name, x2)["accumulators"])
                self.assertEqual((again["transfer"]["uploaded"], again["transfer"]["readback_bytes"]), (False, 0))

    def test_reset_forces_a_new_upload(self):
        from trinity_memory.tensorpack import encode_tensors
        rng = random.Random(7)
        fake = self.fake()
        client = self.serve(fake)
        tensor, _ = matrix(rng, 4, 200)
        data = encode_tensors([tensor])
        handle = client.upload(data)
        x = [rng.randint(-128, 127) for _ in range(200)]
        self.assertTrue(client.dot(handle, "w", x)["transfer"]["uploaded"])
        self.assertFalse(client.dot(handle, "w", x)["transfer"]["uploaded"])
        with fake.lock:
            fake.model.reset()          # the reset button: an H line; the store keeps its bytes
            fake._flush()
        result = client.dot(handle, "w", x)
        self.assertTrue(result["transfer"]["uploaded"])
        self.assertEqual(result["accumulators"], self.emulator_dot(data, "w", x)["accumulators"])

    def test_faults_are_retransmitted_and_recorded(self):
        from trinity_memory.tensorpack import encode_tensors
        rng = random.Random(8)
        faults = [
            {"kind": "corrupt", "cmd": "L", "nth": 2, "offset": 100},        # crc nak
            {"kind": "drop", "cmd": "L", "nth": 4, "offset": 2000},          # timeout nak
            {"kind": "drop_line", "tag": "A", "index": 1, "nth": 4},         # a lost load ack: duplicate
            {"kind": "corrupt", "cmd": "R", "nth": 2, "offset": 5},          # a read request with a bad CRC
            {"kind": "corrupt_line", "tag": "Y", "nth": 3},                  # a run repeated
            {"kind": "drop_line", "tag": "A", "index": 6, "nth": 2},         # the second run's ack lost
            {"kind": "garbage", "before": "Z", "nth": 5, "data": "a5a5ff0042"},
        ]
        fake = self.fake(faults=faults)
        client = self.serve(fake)
        tensor, _ = matrix(rng, 60, 1100)                    # 14 words per row: 13,440 bytes, four chunks
        data = encode_tensors([tensor])
        handle = client.upload(data)
        x = [rng.randint(-128, 127) for _ in range(1100)]
        start = len(fake.tx_log)
        result = client.dot(handle, "w", x)
        self.assertEqual(result["accumulators"], self.emulator_dot(data, "w", x)["accumulators"])
        self.assertTrue(all(f["done"] for f in fake.faults), [f for f in fake.faults if not f["done"]])
        transfer = result["transfer"]
        self.assertEqual(transfer["matvec_attempts"], 2)
        self.assertEqual(transfer["matvec_runs"], 1)
        self.assertGreaterEqual(transfer["naks"], 3)          # crc, timeout, the read request's crc
        self.assertGreaterEqual(transfer["timeouts"], 1)      # the lost ack
        self.assertGreaterEqual(transfer["retransmits"], 5)
        self.assertGreaterEqual(transfer["bad_lines"], 1)
        self.assertGreater(transfer["garbage_bytes"], 0)
        self.assertEqual(transfer["readback_bytes"], 60 * 14 * 16)
        self.assertEqual(fake.model.c["frames_duplicate"], 1)
        self.assertEqual(result["evidence"]["capture_sha256"], hashlib.sha256(bytes(fake.tx_log[start:])).hexdigest())
        self.assertEqual(result["evidence"]["capture_bytes"], len(fake.tx_log) - start)

    def test_a_wrong_device_row_is_reported(self):
        from trinity_memory.tensorpack import encode_tensors
        rng = random.Random(9)
        fake = self.fake(result_faults={3: 1})
        client = self.serve(fake)
        tensor, _ = matrix(rng, 6, 300)
        data = encode_tensors([tensor])
        x = [rng.randint(-128, 127) for _ in range(300)]
        result = client.dot(client.upload(data), "w", x)
        want = self.emulator_dot(data, "w", x)["accumulators"]
        self.assertEqual(result["reference"], {"backend": "emulator", "mismatches": 1, "first_mismatch": 3})
        self.assertEqual(result["accumulators"][3], want[3] + 1)
        self.assertEqual(result["accumulators"][:3] + result["accumulators"][4:], want[:3] + want[4:])

    def test_refusals_and_device_failures(self):
        from trinity_memory.bridge import BridgeError
        from trinity_memory.tensorpack import Tensor, encode_tensors
        rng = random.Random(10)
        tensor, _ = matrix(rng, 3, 100)
        data = encode_tensors([tensor, Tensor("d17", (2, 17), tuple([1] * 34), codec="dense17")])
        x = [1] * 100
        for fake_options, message in (({"build_id": 0x12345678}, "build id"), ({"protocol": 2}, "protocol")):
            with self.subTest(message=message):
                client = self.serve(self.fake(**fake_options))
                with self.assertRaises(BridgeError) as caught:
                    client.dot(client.upload(data), "w", x)
                self.assertEqual(caught.exception.code, -32000)
                self.assertIn(message, str(caught.exception))
                with self.assertRaises(BridgeError):
                    client.call("trinity_chipInfo")
        from trinity_memory.bridge import BridgeClient, BridgeServer
        server = BridgeServer(backend="fpga", device=device("/nonexistent/cu.usbserial-110")).start()
        self.addCleanup(server.close)
        client = BridgeClient(server.url, timeout=30)
        with self.assertRaises(BridgeError) as caught:
            client.dot(client.upload(data), "w", x)
        self.assertEqual((caught.exception.code, str(caught.exception)), (-32000, "cannot open the device serial port"))
        from trinity_memory import container
        client = self.serve(self.fake())
        handle = client.upload(data)
        empty = client.upload(container.encode_file([], "dense5"))      # a TMEM vector of 0 trits
        for target, name, activations in ((handle, "d17", [1] * 17), (empty, "weights", []), (handle, "w", [1] * 99)):
            with self.subTest(name=name):
                with self.assertRaises(BridgeError) as caught:
                    client.dot(target, name, activations)
                self.assertEqual(caught.exception.code, -32602)

    def test_real_chunk_through_the_fake_device(self):
        import stage1_chunk
        from trinity_memory.tensorpack import Tensor, encode_tensors
        try:
            chunk = stage1_chunk.chunk()
        except Exception as error:  # noqa: BLE001 - fixtures.CacheMiss or a missing cache directory
            if REQUIRE_CACHED:
                self.fail(f"TRINITY_REQUIRE_CACHED=1: {error}")
            self.skipTest(f"fixture cache: {error}")
        self.assertEqual(chunk["full_sha256"], chunk["report_sha256"])
        fake = self.fake()
        client = self.serve(fake)
        for codec in ("dense5", "baseline2"):
            with self.subTest(codec=codec):
                data = encode_tensors([Tensor("q_proj_rows_0_319", (320, 2560), tuple(chunk["trits"]), codec=codec)])
                result = client.dot(client.upload(data), "q_proj_rows_0_319", chunk["x"])
                self.assertEqual(result["accumulators"], chunk["y"])
                self.assertEqual(result["accumulators"][:8], chunk["first8"])
                self.assertEqual(result["reference"]["mismatches"], 0)
                self.assertEqual(result["transfer"]["image_bytes"], 163840 if codec == "dense5" else 204800)


if __name__ == "__main__":
    unittest.main()
