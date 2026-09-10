"""Bounded localhost JSON-RPC memory emulator; no device or proof claims."""

from __future__ import annotations

import base64
import binascii
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import re
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
import uuid

from .codecs import CODECS, CodecError
from .container import PREFIX, decode_file, inspect_file


class BridgeError(RuntimeError):
    """A JSON-RPC failure with a stable numeric code."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise BridgeError(-32000, "RPC redirects are not supported")


def _strict_json(data: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("nonfinite JSON number")

    def number(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("nonfinite JSON number")
        return result

    return json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                      parse_constant=constant, parse_float=number)


def _integer(value, name, minimum=0, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise BridgeError(-32602, f"invalid {name}")
    return value


def _fields(params, required=(), optional=()):
    if type(params) is not dict or not set(required) <= params.keys() or not params.keys() <= set(required) | set(optional):
        raise BridgeError(-32602, "invalid method parameters")


def _identity():
    # Synthetic, public identifiers. They are not a PUF or an attestation.
    return {f"{part}_id": hashlib.sha256(
        f"trinity-memory-emulator-v1:{part}".encode()).digest()[:16].hex()
            for part in ("phi", "euler", "gamma")}


class BridgeServer:
    """Single-worker in-memory HTTP server, restricted to IPv4 localhost.

    Use as a context manager. ``port=0`` selects an unused port. Storage is
    discarded at close. Limits bound containers, decoded trits and requests.
    """

    def __init__(self, host="127.0.0.1", port=0, *, max_request_bytes=2 * 1024 * 1024,
                 max_object_bytes=512 * 1024, max_storage_bytes=8 * 1024 * 1024,
                 max_objects=16, max_trits=1_000_000, timeout=5.0):
        if host not in ("127.0.0.1", "localhost"):
            raise ValueError("Bridge supports only 127.0.0.1 or localhost until authentication is designed")
        _integer(port, "port", maximum=65535)
        for name, value in (("max_request_bytes", max_request_bytes),
                            ("max_object_bytes", max_object_bytes),
                            ("max_storage_bytes", max_storage_bytes),
                            ("max_objects", max_objects), ("max_trits", max_trits)):
            _integer(value, name, minimum=1)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self.host, self.port, self.timeout = "127.0.0.1", port, timeout
        self.limits = dict(max_request_bytes=max_request_bytes, max_object_bytes=max_object_bytes,
                           max_storage_bytes=max_storage_bytes, max_objects=max_objects,
                           max_trits=max_trits)
        self._objects = {}
        self._stored_bytes = 0
        self._http = None
        self._thread = None

    @property
    def url(self):
        if self._http is None:
            raise RuntimeError("BridgeServer is not running")
        return f"http://127.0.0.1:{self._http.server_port}/"

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()

    def start(self):
        if self._http is not None:
            raise RuntimeError("BridgeServer is already running")
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "TrinityMemoryBridge/1"
            sys_version = ""

            def setup(self):
                super().setup()
                self.connection.settimeout(owner.timeout)

            def log_message(self, *_):
                pass

            def respond(self, payload, status=200):
                body = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.close_connection = True
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    pass

            def fail(self, code, message, status=200):
                self.respond({"jsonrpc": "2.0", "id": None,
                              "error": {"code": code, "message": message}}, status)

            def do_POST(self):
                hosts = (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")
                if self.path != "/" or len(self.headers.get_all("Host", [])) != 1 or self.headers.get("Host") not in hosts or "Origin" in self.headers:
                    self.fail(-32600, "only local non-browser RPC requests are accepted", 403)
                    return
                lengths = self.headers.get_all("Content-Length", [])
                if "Transfer-Encoding" in self.headers or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
                    self.fail(-32600, "one Content-Length is required", 400)
                    return
                size = int(lengths[0])
                if size > owner.limits["max_request_bytes"]:
                    self.fail(-32010, "request limit exceeded", 413)
                    return
                if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                    self.fail(-32600, "Content-Type must be application/json", 415)
                    return
                try:
                    deadline = time.monotonic() + owner.timeout
                    chunks, remaining = [], size
                    while remaining:
                        timeout = deadline - time.monotonic()
                        if timeout <= 0:
                            raise TimeoutError
                        self.connection.settimeout(timeout)
                        chunk = self.rfile.read1(min(remaining, 65536))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    body = b"".join(chunks)
                    if len(body) != size:
                        self.fail(-32700, "truncated JSON body", 400)
                        return
                    request = _strict_json(body)
                except (ValueError, UnicodeError, RecursionError, TimeoutError):
                    self.fail(-32700, "invalid JSON", 400)
                    return
                self.respond(owner._request(request))

        self._http = HTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._http.serve_forever,
                                        kwargs={"poll_interval": 0.02}, daemon=True)
        self._thread.start()
        return self

    def close(self):
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
            self._thread.join()
            self._http = self._thread = None
        self._objects.clear()
        self._stored_bytes = 0

    def _request(self, request):
        request_id = None
        try:
            if type(request) is not dict or not {"jsonrpc", "id", "method"} <= request.keys() or not request.keys() <= {"jsonrpc", "id", "method", "params"}:
                raise BridgeError(-32600, "one JSON-RPC request with an id is required")
            candidate = request["id"]
            if not ((type(candidate) is int and abs(candidate) <= 2**53 - 1) or
                    (type(candidate) is str and len(candidate) <= 128)):
                raise BridgeError(-32600, "invalid request id")
            request_id = candidate
            if request["jsonrpc"] != "2.0" or type(request["method"]) is not str:
                raise BridgeError(-32600, "invalid JSON-RPC envelope")
            result = self._dispatch(request["method"], request.get("params", {}))
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except (BridgeError, CodecError) as error:
            code = error.code if isinstance(error, BridgeError) else -32602
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": code, "message": str(error)}}
        except Exception:
            # Keep implementation traces and local paths out of RPC responses.
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32603, "message": "internal RPC error"}}

    def _object(self, handle):
        if type(handle) is not str or not re.fullmatch(r"[0-9a-f]{32}", handle):
            raise BridgeError(-32602, "invalid handle")
        if handle not in self._objects:
            raise BridgeError(-32004, "handle not found")
        return self._objects[handle]

    def _inspect(self, data):
        if data.startswith(b"TMEM"):
            if len(data) >= PREFIX.size and PREFIX.unpack_from(data)[5] > self.limits["max_trits"]:
                raise BridgeError(-32010, "decoded trit limit exceeded")
            meta = inspect_file(data)
            meta["tensors"] = [{"name": "weights", "shape": [meta["count"]],
                                 "scales": [1.0], "scale_axis": None}]
            return meta
        if data.startswith(b"TTPK"):
            from .tensorpack import inspect_tensorpack
            meta = inspect_tensorpack(data, max_total_trits=self.limits["max_trits"])
            if meta["total_count"] > self.limits["max_trits"]:
                raise BridgeError(-32010, "decoded trit limit exceeded")
            return meta
        raise BridgeError(-32602, "upload must contain a valid TMEM or TensorPack container")

    def _dispatch(self, method, params):
        if method == "trinity.capabilities":
            _fields(params)
            return {"protocol": "trinity-memory-bridge", "version": 1, "backend": "emulator",
                    "hardware": False, "persistence": "process-memory", "authentication": "none-loopback-only",
                    "formats": ["TMEM/1", "TensorPack/1"], "codecs": list(CODECS),
                    "methods": ["trinity.capabilities", "memory.upload", "memory.read", "memory.info",
                                "memory.delete", "compute.dot", "chip_info", "trinity_chipInfo"],
                    "limits": dict(self.limits)}
        if method in ("chip_info", "trinity_chipInfo"):
            _fields(params)
            result = _identity()
            if method == "chip_info":
                result = {key.removesuffix("_id"): value for key, value in result.items()}
                result["anchor"] = "0x47C0"
            else:
                result["anchor"] = 0x47C0
            return {**result, "backend": "emulator", "hardware": False,
                    "identity_kind": "synthetic-public-16-byte", "status": "memory emulator"}
        if method == "memory.upload":
            _fields(params, ("data",))
            encoded = params["data"]
            if type(encoded) is not str:
                raise BridgeError(-32602, "data must be base64 text")
            if len(encoded) > 4 * ((self.limits["max_object_bytes"] + 2) // 3):
                raise BridgeError(-32010, "object limit exceeded")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise BridgeError(-32602, "invalid base64 data") from None
            if base64.b64encode(data).decode() != encoded:
                raise BridgeError(-32602, "noncanonical base64 data")
            if len(data) > self.limits["max_object_bytes"]:
                raise BridgeError(-32010, "object limit exceeded")
            if len(self._objects) >= self.limits["max_objects"] or self._stored_bytes + len(data) > self.limits["max_storage_bytes"]:
                raise BridgeError(-32010, "storage limit exceeded")
            self._inspect(data)
            handle = uuid.uuid4().hex
            self._objects[handle] = data
            self._stored_bytes += len(data)
            return {"handle": handle, "bytes": len(data), "backend": "emulator"}
        if method in ("memory.read", "memory.info", "memory.delete"):
            _fields(params, ("handle",), ("offset", "length") if method == "memory.read" else ())
            data = self._object(params["handle"])
            if method == "memory.read":
                offset = _integer(params.get("offset", 0), "offset", maximum=len(data))
                length = _integer(params.get("length", len(data) - offset), "length", maximum=len(data) - offset)
                return {"data": base64.b64encode(data[offset:offset + length]).decode(),
                        "offset": offset, "length": length, "total_bytes": len(data), "backend": "emulator"}
            if method == "memory.info":
                return {**self._inspect(data), "handle": params["handle"],
                        "sha256": hashlib.sha256(data).hexdigest(), "backend": "emulator"}
            del self._objects[params["handle"]]
            self._stored_bytes -= len(data)
            return {"deleted": True, "backend": "emulator"}
        if method == "compute.dot":
            _fields(params, ("handle", "tensor_name", "activations"))
            return self._dot(self._object(params["handle"]), params["tensor_name"], params["activations"])
        raise BridgeError(-32601, "method not found")

    def _dot(self, data, tensor_name, activations):
        if type(tensor_name) is not str or not tensor_name or len(tensor_name) > 1024:
            raise BridgeError(-32602, "invalid tensor_name")
        if type(activations) is not list or len(activations) > self.limits["max_trits"]:
            raise BridgeError(-32602, "activations must be a bounded int8 array")
        for value in activations:
            _integer(value, "signed int8 activation", -128, 127)
        if data.startswith(b"TMEM"):
            if tensor_name != "weights":
                raise BridgeError(-32004, "tensor not found")
            values = decode_file(data)
            shape, scales, axis = (len(values),), (1.0,), None
        else:
            from .tensorpack import decode_tensors
            tensor = next((tensor for tensor in decode_tensors(data) if tensor.name == tensor_name), None)
            if tensor is None:
                raise BridgeError(-32004, "tensor not found")
            values, shape, scales, axis = tensor.values, tensor.shape, tensor.scales, tensor.scale_axis
        if len(shape) not in (1, 2):
            raise BridgeError(-32602, "dot supports vectors and row-major matrices")
        if axis is not None and not (len(shape) == 2 and axis == 0):
            raise BridgeError(-32602, "dot requires a scalar scale or per-row matrix scales")
        width = shape[-1]
        if len(activations) != width:
            raise BridgeError(-32602, "activation length must equal the last tensor dimension")
        rows = shape[0] if len(shape) == 2 else 1
        accumulators = [sum(values[row * width + col] * activations[col] for col in range(width))
                        for row in range(rows)]
        return {"accumulators": accumulators, "scales": list(scales) if axis == 0 else [scales[0]] * rows,
                "backend": "emulator", "arithmetic": "exact-integer", "tensor_name": tensor_name,
                "input_shape": list(shape), "output_shape": [rows], "scale_applied": False}


class BridgeClient:
    """Synchronous stdlib HTTP client. No environment proxies or redirects."""

    def __init__(self, url, *, timeout=5.0, max_response_bytes=4 * 1024 * 1024):
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise ValueError("URL must be a plain loopback HTTP endpoint")
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("invalid RPC port") from None
        if port is None or not 1 <= port <= 65535:
            raise ValueError("an explicit RPC port is required")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        _integer(max_response_bytes, "max_response_bytes", minimum=1)
        self.url = f"http://127.0.0.1:{port}/"
        self.timeout, self.max_response_bytes = timeout, max_response_bytes
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def call(self, method, params=None):
        request_id = uuid.uuid4().hex
        try:
            payload = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method,
                                  "params": {} if params is None else params}, allow_nan=False).encode()
        except (ValueError, TypeError):
            raise BridgeError(-32602, "request is not valid JSON") from None
        request = Request(self.url, payload, {"Content-Type": "application/json"}, method="POST")
        try:
            try:
                response = self._opener.open(request, timeout=self.timeout)
            except HTTPError as error:
                response = error
            with response:
                if response.geturl() != self.url:
                    raise BridgeError(-32000, "RPC redirects are not supported")
                body = response.read(self.max_response_bytes + 1)
            if len(body) > self.max_response_bytes:
                raise BridgeError(-32010, "response limit exceeded")
            result = _strict_json(body)
        except (URLError, OSError) as error:
            raise BridgeError(-32000, f"RPC transport failed: {type(error).__name__}") from None
        except (ValueError, UnicodeError, RecursionError):
            raise BridgeError(-32000, "invalid JSON-RPC response") from None
        if type(result) is not dict or result.get("jsonrpc") != "2.0" or (("error" in result) == ("result" in result)):
            raise BridgeError(-32000, "invalid JSON-RPC response envelope")
        if result.get("id") != request_id and not ("error" in result and result.get("id") is None):
            raise BridgeError(-32000, "JSON-RPC response id mismatch")
        if "error" in result:
            error = result["error"]
            if type(error) is not dict or type(error.get("code")) is not int or type(error.get("message")) is not str:
                raise BridgeError(-32000, "invalid JSON-RPC error")
            raise BridgeError(error["code"], error["message"])
        if result.get("id") != request_id:
            raise BridgeError(-32000, "JSON-RPC response id mismatch")
        return result["result"]

    def capabilities(self):
        return self.call("trinity.capabilities")

    def upload(self, data: bytes) -> str:
        if type(data) is not bytes:
            raise TypeError("upload requires bytes")
        return self.call("memory.upload", {"data": base64.b64encode(data).decode()})["handle"]

    def read(self, handle, offset=0, length=None) -> bytes:
        params = {"handle": handle, "offset": offset}
        if length is not None:
            params["length"] = length
        return base64.b64decode(self.call("memory.read", params)["data"], validate=True)

    def info(self, handle):
        return self.call("memory.info", {"handle": handle})

    def dot(self, handle, tensor_name, activations):
        return self.call("compute.dot", {"handle": handle, "tensor_name": tensor_name, "activations": activations})

    def delete(self, handle):
        return self.call("memory.delete", {"handle": handle})


class SDKMemoryBackend:
    """Inject into upstream ``TrinityChip``; identity is explicitly simulated.

    Install the separate Trinity SDK to use ``get_chip_info``. Memory RPCs are
    exposed by ``client``. No inference, attestation or network receipts exist.
    """

    def __init__(self, client: BridgeClient):
        self.client = client

    def get_chip_info(self):
        from trinity.types import ChipInfo
        info = self.client.call("trinity_chipInfo")
        if info.get("backend") != "emulator" or info.get("hardware") is not False:
            raise BridgeError(-32000, "expected an explicit memory emulator identity")
        return ChipInfo(phi_id=bytes.fromhex(info["phi_id"]),
                        euler_id=bytes.fromhex(info["euler_id"]),
                        gamma_id=bytes.fromhex(info["gamma_id"]), anchor=info["anchor"])

    def prove_inference(self, **_):
        raise NotImplementedError("Memory emulator does not run model inference or produce ZK proofs")

    def submit_to_bittensor(self, **_):
        raise NotImplementedError("Memory emulator does not submit to Bittensor")
