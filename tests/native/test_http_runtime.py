"""Actual sockets across native/reference boundaries, including timeout recovery."""
import ctypes as C
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import socket
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/reference"))
from trinity_memory_reference.bridge import BridgeClient, BridgeServer
from trinity_memory_reference.container import encode_file
from trinity_memory_reference.tensorpack import Tensor, encode_tensors

LIBRARY = ROOT / 'build/t27' / ('libtrinity_memory_t27.dylib' if sys.platform == 'darwin' else 'libtrinity_memory_t27.so')
lib = C.CDLL(str(LIBRARY))
lib.tm_runtime_new.argtypes = [C.c_int32] + [C.c_size_t] * 5 + [C.c_double]
lib.tm_runtime_new.restype = C.c_void_p
for name in ('start', 'port'):
    getattr(lib, 'tm_runtime_' + name).argtypes = [C.c_void_p]
    getattr(lib, 'tm_runtime_' + name).restype = C.c_int32
lib.tm_runtime_close.argtypes = [C.c_void_p]
lib.tm_runtime_http.argtypes = [C.c_int32, C.c_double, C.c_void_p, C.c_size_t, C.c_void_p, C.c_size_t]
lib.tm_runtime_http.restype = C.c_int64


class NativeNetwork(unittest.TestCase):
    def server(self, timeout=1, request=2097152):
        server = lib.tm_runtime_new(0, request, 524288, 8388608, 16, 1000000, timeout)
        self.assertTrue(server)
        self.addCleanup(lib.tm_runtime_close, server)
        self.assertEqual(lib.tm_runtime_start(server), 0)
        self.assertEqual(lib.tm_runtime_start(server), -1)
        return lib.tm_runtime_port(server)

    def test_reference_client_to_native_server(self):
        port = self.server()
        client = BridgeClient(f'http://127.0.0.1:{port}/')
        self.assertEqual(client.capabilities()['backend'], 'emulator')
        for codec in ('baseline2', 'dense5', 'dense17', 'dense22', 'sparse41', 'sparse82'):
            data = encode_file([-1, 0, 0, 0, 0, 0, 0, 1], codec)
            handle = client.upload(data)
            self.assertEqual(client.read(handle), data)
            self.assertEqual(client.read(handle, 2, 3), data[2:5])
            self.assertEqual(client.dot(handle, 'weights', [-128] * 8)['accumulators'], [0])
            self.assertTrue(client.delete(handle)['deleted'])
        tensor = Tensor('unicode-λ', (2, 3), (1, -1, 0, -1, 1, 1), scales=(0.5, 2.0), scale_axis=0)
        handle = client.upload(encode_tensors([tensor]))
        result = client.dot(handle, tensor.name, [-128, 127, -128])
        self.assertEqual(result['accumulators'], [-255, 127])
        self.assertEqual(result['scales'], [0.5, 2.0])
        self.assertEqual(client.info(handle)['tensors'][0]['name'], tensor.name)

    def test_http_rejections_and_timeout_recovery(self):
        port = self.server(timeout=0.05)
        body = b'{"jsonrpc":"2.0","id":1,"method":"trinity.capabilities"}'
        for extra, expected in ((b'Origin: null\r\n', 403), (b'Host: localhost:1\r\n', 403),
                                (b'Content-Length: 0\r\n', 400), (b'Transfer-Encoding: chunked\r\n', 400)):
            with socket.create_connection(('127.0.0.1', port), timeout=2) as connection:
                head = f'POST / HTTP/1.1\r\nHost: localhost:{port}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n'.encode()
                connection.sendall(head + extra + b'\r\n' + body)
                response = b''
                while part := connection.recv(4096): response += part
                self.assertEqual(int(response.split(b' ', 2)[1]), expected)
        with socket.create_connection(('127.0.0.1', port), timeout=2) as connection:
            connection.sendall(f'POST / HTTP/1.1\r\nHost: localhost:{port}\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{{'.encode())
            response = b''
            while part := connection.recv(4096): response += part
            self.assertEqual(int(response.split(b' ', 2)[1]), 400)
        self.assertEqual(BridgeClient(f'http://localhost:{port}').capabilities()['version'], 1)

    def native_call(self, port, body, capacity=4194304):
        output = C.create_string_buffer(capacity)
        size = lib.tm_runtime_http(port, 2.0, body, len(body), output, capacity)
        return size, output.raw[:max(size, 0)]

    def test_native_transport_to_reference_server_and_limits(self):
        body = b'{"jsonrpc":"2.0","id":"native","method":"trinity.capabilities"}'
        with BridgeServer() as server:
            size, data = self.native_call(server._http.server_port, body)
            self.assertGreater(size, 0)
            self.assertEqual(json.loads(data)['id'], 'native')
            self.assertEqual(json.loads(data)['result']['backend'], 'emulator')
            self.assertEqual(self.native_call(server._http.server_port, body, 50)[0], -32010)
        port = self.server(request=20)
        size, data = self.native_call(port, body)
        self.assertGreater(size, 0)
        self.assertEqual(json.loads(data)['error']['code'], -32010)

    def test_native_client_chunked_close_delimited_and_redirect_rejection(self):
        for mode in ('chunked', 'close', 'redirect', 'truncated'):
            class Handler(BaseHTTPRequestHandler):
                def do_POST(self):
                    self.rfile.read(int(self.headers['Content-Length']))
                    if mode == 'redirect':
                        self.send_response(302)
                        self.send_header('Location', 'http://example.invalid/')
                        self.end_headers()
                        return
                    self.send_response(200)
                    if mode == 'chunked': self.send_header('Transfer-Encoding', 'chunked')
                    if mode == 'truncated': self.send_header('Content-Length', '100')
                    self.end_headers()
                    self.wfile.write(b'1\r\n{\r\n1\r\n}\r\n0\r\n\r\n' if mode == 'chunked' else b'{}')
                def log_message(self, *_): pass
            server = HTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
            thread.start()
            try:
                size, data = self.native_call(server.server_port, b'{}')
                if mode in ('chunked', 'close'): self.assertEqual((size, data), (2, b'{}'))
                else: self.assertEqual(size, -32000)
            finally:
                server.shutdown(); server.server_close(); thread.join()

    def test_invalid_allocation_configuration_rejected(self):
        for args in ((-1, 1, 1, 1, 1, 1, 1), (0, 1, 1, 1, 1, 1, 0),
                     (0, 1, 1, 1, 0, 1, 1), (0, 1, (1 << 64) - 1, 1, 2, 1, 1)):
            self.assertFalse(lib.tm_runtime_new(*args))


if __name__ == '__main__':
    unittest.main()
