import base64
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import socket
import threading
import unittest

from trinity_memory.bridge import BridgeClient, BridgeError, BridgeServer, SDKMemoryBackend
from trinity_memory.container import encode_file
from trinity_memory.tensorpack import Tensor, encode_tensors


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.server = BridgeServer().start()
        self.addCleanup(self.server.close)
        self.client = BridgeClient(self.server.url)

    def raw(self, body, headers=None):
        connection = HTTPConnection("127.0.0.1", int(self.server.url.split(":")[-1].strip("/")), timeout=2)
        try:
            connection.request("POST", "/", body, headers or {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def assert_rpc_error(self, code, action):
        with self.assertRaises(BridgeError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_real_http_upload_read_chunks_info_and_delete(self):
        caps = self.client.capabilities()
        self.assertEqual(caps["backend"], "emulator")
        self.assertIs(caps["hardware"], False)
        data = encode_file([-1, 0, 1, 1, -1, 0])
        handle = self.client.upload(data)
        self.assertEqual(self.client.read(handle), data)
        self.assertEqual(self.client.read(handle, 3, 5), data[3:8])
        self.assertEqual(self.client.read(handle, len(data)), b"")
        info = self.client.info(handle)
        self.assertEqual((info["format"], info["count"], info["backend"]), ("TMEM", 6, "emulator"))
        self.assertEqual(info["tensors"][0]["name"], "weights")
        self.assertTrue(self.client.delete(handle)["deleted"])
        self.assert_rpc_error(-32004, lambda: self.client.read(handle))
        self.assert_rpc_error(-32004, lambda: self.client.delete(handle))

    def test_exact_dot_signed_extremes_and_row_scales(self):
        tensor = Tensor("matrix", (2, 3), (1, -1, 0, -1, 1, 1),
                        scales=(0.5, 2.0), scale_axis=0)
        handle = self.client.upload(encode_tensors([tensor]))
        result = self.client.dot(handle, "matrix", [-128, 127, -128])
        self.assertEqual(result["accumulators"], [-255, 127])
        self.assertEqual(result["scales"], [0.5, 2.0])
        self.assertIs(result["scale_applied"], False)
        self.assertEqual(result["backend"], "emulator")
        self.assertEqual(self.client.info(handle)["format"], "TensorPack")
        self.assert_rpc_error(-32004, lambda: self.client.dot(handle, "unknown", [0, 0, 0]))
        self.assert_rpc_error(-32602, lambda: self.client.dot(handle, "matrix", [0, 0]))
        for value in (True, 1.0, "1", -129, 128):
            self.assert_rpc_error(-32602, lambda: self.client.dot(handle, "matrix", [0, 0, value]))

    def test_scalar_scale_vector_and_unbounded_python_integer_accumulator(self):
        # Deliberately exceeds signed int16; emulator promises exact integers.
        handle = self.client.upload(encode_file([1] * 300))
        result = self.client.dot(handle, "weights", [127] * 300)
        self.assertEqual(result["accumulators"], [38100])
        self.assertEqual(result["scales"], [1.0])
        handle = self.client.upload(encode_tensors([
            Tensor("vector", (3,), (-1, 0, 1), scales=(0.25,)),
            Tensor("matrix", (2, 3), (1, 1, 1, -1, -1, -1), scales=(2.0,)),
        ]))
        self.assertEqual(self.client.dot(handle, "vector", [2, 3, 4])["accumulators"], [2])
        self.assertEqual(self.client.dot(handle, "matrix", [2, 3, 4])["scales"], [2.0, 2.0])

    def test_dot_rejects_scale_layouts_that_cannot_factor_out(self):
        tensors = [Tensor("column_scales", (2, 3), (1,) * 6,
                          scales=(1.0, 2.0, 3.0), scale_axis=1),
                   Tensor("rank3", (1, 1, 3), (1, 0, -1)),
                   Tensor("scalar", (), (1,))]
        handle = self.client.upload(encode_tensors(tensors))
        for name in ("column_scales", "rank3", "scalar"):
            self.assert_rpc_error(-32602, lambda: self.client.dot(handle, name, [1, 2, 3]))

    def test_empty_tmem_dot_is_zero(self):
        handle = self.client.upload(encode_file([]))
        self.assertEqual(self.client.dot(handle, "weights", [])["accumulators"], [0])

    def test_strict_json_duplicate_keys_nonfinite_and_malformed(self):
        for body in (b'{"jsonrpc":"2.0","id":1,"id":2,"method":"trinity.capabilities"}',
                     b'{"jsonrpc":"2.0","id":1,"method":"x","params":{"n":NaN}}',
                     b'{"jsonrpc":"2.0","id":1,"method":"x","params":{"n":Infinity}}',
                     b'{"jsonrpc":"2.0","id":1,"method":"x","params":{"n":1e999}}',
                     b"\xff", b"{broken", b"[" * 1100):
            status, result = self.raw(body)
            self.assertEqual((status, result["error"]["code"]), (400, -32700))
        # A malformed request never kills the server worker.
        self.assertEqual(self.client.capabilities()["version"], 1)

    def test_rpc_shape_unknown_fields_and_unknown_methods(self):
        for request in ([], {}, {"jsonrpc": "2.0", "method": "trinity.capabilities"},
                        {"jsonrpc": "2.0", "id": True, "method": "trinity.capabilities"},
                        {"jsonrpc": "2.0", "id": 1, "method": [], "extra": 2}):
            _, result = self.raw(json.dumps(request).encode())
            self.assertEqual(result["error"]["code"], -32600)
        self.assert_rpc_error(-32601, lambda: self.client.call("trinity_proveInference"))
        self.assert_rpc_error(-32602, lambda: self.client.call("trinity.capabilities", {"extra": 1}))
        self.assert_rpc_error(-32602, lambda: self.client.call("memory.read", []))
        self.assert_rpc_error(-32602, lambda: self.client.info("../../etc/passwd"))

    def test_base64_crc_and_read_bounds_rejected_without_storage_mutation(self):
        for value in ("!", "AB==", True):
            self.assert_rpc_error(-32602, lambda: self.client.call("memory.upload", {"data": value}))
        data = encode_file([1, 0, -1])
        corrupted = data[:-1] + bytes([data[-1] ^ 1])
        self.assert_rpc_error(-32602, lambda: self.client.upload(corrupted))
        self.assert_rpc_error(-32602, lambda: self.client.upload(b"hello"))
        self.assertEqual(self.server.stored_bytes, 0)
        handle = self.client.upload(data)
        for offset, length in ((-1, 2), (False, 2), (0, -1), (len(data) + 1, 0), (1, len(data))):
            self.assert_rpc_error(-32602, lambda: self.client.read(handle, offset, length))

    def test_origin_host_and_content_type_restrictions(self):
        body = b'{"jsonrpc":"2.0","id":1,"method":"trinity.capabilities"}'
        for headers, expected in (({"Content-Type": "application/json", "Origin": "http://example.org"}, 403),
                                  ({"Content-Type": "application/json", "Host": "evil.example"}, 403),
                                  ({"Content-Type": "text/plain"}, 415)):
            status, _ = self.raw(body, headers)
            self.assertEqual(status, expected)

    def test_storage_count_limits_and_delete_reclaims_capacity(self):
        data = encode_file([0] * 10)
        with BridgeServer(max_objects=1, max_storage_bytes=len(data), max_object_bytes=len(data)) as server:
            client = BridgeClient(server.url)
            handle = client.upload(data)
            self.assert_rpc_error(-32010, lambda: client.upload(data))
            client.delete(handle)
            self.assertEqual(client.read(client.upload(data)), data)
            self.assert_rpc_error(-32010, lambda: client.upload(encode_file([0] * 20)))
        with BridgeServer(max_storage_bytes=len(data) - 1) as server:
            self.assert_rpc_error(-32010, lambda: BridgeClient(server.url).upload(data))

    def test_request_response_and_decoded_trit_limits(self):
        with BridgeServer(max_request_bytes=100) as server:
            self.assert_rpc_error(-32010, lambda: BridgeClient(server.url).upload(encode_file([1])))
        client = BridgeClient(self.server.url, max_response_bytes=50)
        self.assert_rpc_error(-32010, client.capabilities)
        with BridgeServer(max_trits=2) as server:
            client = BridgeClient(server.url)
            self.assert_rpc_error(-32010, lambda: client.upload(encode_file([0, 0, 0])))
            with self.assertRaises(BridgeError):
                client.upload(encode_tensors([Tensor("three", (3,), (0, 0, 0))]))
            self.assertEqual(server.stored_bytes, 0)

    def test_identity_aliases_are_synthetic_and_unsupported_sdk_operations_fail(self):
        sdk = self.client.call("trinity_chipInfo")
        node = self.client.call("chip_info")
        self.assertEqual(sdk["phi_id"], node["phi"])
        self.assertEqual(len(bytes.fromhex(sdk["phi_id"])), 16)
        self.assertEqual(sdk["anchor"], int(node["anchor"], 16))
        self.assertFalse(sdk["hardware"])
        self.assertEqual(sdk["identity_kind"], "synthetic-public-16-byte")
        backend = SDKMemoryBackend(self.client)
        with self.assertRaises(NotImplementedError):
            backend.prove_inference(model="anything", input_data=b"test")
        with self.assertRaises(NotImplementedError):
            backend.submit_to_bittensor(subnet=11, validator_uid=1)

    def test_loopback_lifecycle_and_url_validation(self):
        for host in ("0.0.0.0", "::", "example.org", "192.168.0.1"):
            with self.assertRaises(ValueError):
                BridgeServer(host=host)
        for url in ("http://example.org:9933", "https://127.0.0.1:9933", "http://127.0.0.1",
                    "http://user:pass@127.0.0.1:9933", "http://127.0.0.1:9933/path"):
            with self.assertRaises(ValueError):
                BridgeClient(url)
        server = BridgeServer()
        with self.assertRaises(RuntimeError):
            _ = server.url
        with server:
            client = BridgeClient(server.url)
            handle = client.upload(encode_file([0]))
            with self.assertRaises(RuntimeError):
                server.start()
        self.assertEqual(server.stored_bytes, 0)
        with server:
            self.assert_rpc_error(-32004, lambda: BridgeClient(server.url).read(handle))

    def test_client_does_not_follow_redirects(self):
        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", "http://example.invalid/")
                self.end_headers()

            def log_message(self, *_):
                pass

        server = HTTPServer(("127.0.0.1", 0), Redirect)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        try:
            client = BridgeClient(f"http://127.0.0.1:{server.server_port}/")
            self.assert_rpc_error(-32000, client.capabilities)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_incomplete_body_times_out_and_worker_survives(self):
        with BridgeServer(timeout=0.05) as server:
            port = int(server.url.split(":")[-1].strip("/"))
            with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
                request = (f"POST / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                           "Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{").encode()
                connection.sendall(request)
                response = b""
                while chunk := connection.recv(4096):
                    response += chunk
                self.assertIn(b"400", response.split(b"\r\n", 1)[0])
            self.assertEqual(BridgeClient(server.url).capabilities()["backend"], "emulator")


if __name__ == "__main__":
    unittest.main()
