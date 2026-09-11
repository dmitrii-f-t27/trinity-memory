"""Replay conformance/memory_bridge.json over real TCP against the native Bridge.

Every vector opens a BridgeServer (the native loopback runtime) with the
vector's limits, sends the request bytes over HTTP and checks the JSON-RPC
response exactly as tests/spec_bridge_replay.py specifies. Raw HTTP framing
vectors use a plain socket. The identity constants and the SDK adapter are
checked against the spec's derivation rule as well.
"""
import contextlib
import hashlib
import json
import socket
import sys
import unittest
from http.client import HTTPConnection
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import spec_bridge_replay as replay  # noqa: E402

from trinity_memory.bridge import BridgeClient, BridgeError, BridgeServer, SDKMemoryBackend  # noqa: E402

DOCUMENT = replay.load_document()


class TcpSession:
    def __init__(self, server):
        self.server = server
        self.port = int(server.url.split(":")[-1].strip("/"))

    def send(self, body, headers=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("POST", "/", body, headers or {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def raw(self, text):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as connection:
            connection.sendall(text.replace("{port}", str(self.port)).encode("latin-1"))
            data = b""
            while chunk := connection.recv(65536):
                data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1])
        return status, json.loads(body)


@contextlib.contextmanager
def tcp_session(limits):
    with BridgeServer(**limits) as server:
        yield TcpSession(server)


class SpecBridgeTcpReplay(unittest.TestCase):
    def test_document_identity(self):
        self.assertEqual(DOCUMENT["module"], "TrinityMemoryBridgeSpec")
        self.assertTrue((replay.ROOT / DOCUMENT["spec_path"]).is_file())
        self.assertEqual(len({vector["id"] for vector in DOCUMENT["vectors"]}), len(DOCUMENT["vectors"]))

    def test_every_vector_over_tcp(self):
        run, steps, skipped = replay.replay(DOCUMENT, tcp_session, transport=True)
        self.assertEqual(skipped, [])
        self.assertEqual(run, len(DOCUMENT["vectors"]))
        self.assertGreaterEqual(steps, 40)

    def test_identity_follows_the_documented_derivation(self):
        expected = DOCUMENT["constants"]["identity"]
        for part in ("phi", "euler", "gamma"):
            digest = hashlib.sha256(f"trinity-memory-emulator-v1:{part}".encode()).digest()[:16].hex()
            self.assertEqual(expected[f"{part}_id"], digest)
        self.assertEqual(int(expected["anchor_hex"], 16), expected["anchor"])
        with BridgeServer() as server:
            client = BridgeClient(server.url)
            sdk = client.call("trinity_chipInfo")
            node = client.call("chip_info")
            for part in ("phi", "euler", "gamma"):
                self.assertEqual(sdk[f"{part}_id"], expected[f"{part}_id"])
                self.assertEqual(node[part], expected[f"{part}_id"])
            self.assertEqual((sdk["anchor"], node["anchor"]), (expected["anchor"], expected["anchor_hex"]))

    def test_sdk_adapter_requires_the_emulator_label_and_refuses_unsupported_calls(self):
        adapter = DOCUMENT["constants"]["sdk_adapter"]
        with BridgeServer() as server:
            backend = SDKMemoryBackend(BridgeClient(server.url))
            for name in adapter["unsupported"]:
                with self.assertRaises(NotImplementedError):
                    getattr(backend, name)()
            capabilities = backend.client.capabilities()
            for key, value in adapter["chip_info_requires"].items():
                self.assertEqual(capabilities[key], value)

    def test_client_reports_transport_failures_with_the_spec_code(self):
        errors = DOCUMENT["constants"]["errors"]
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            client = BridgeClient(f"http://127.0.0.1:{listener.getsockname()[1]}/", timeout=0.2)
            with self.assertRaises(BridgeError) as caught:
                client.capabilities()
        self.assertEqual(caught.exception.code, errors["transport"])


if __name__ == "__main__":
    unittest.main()
