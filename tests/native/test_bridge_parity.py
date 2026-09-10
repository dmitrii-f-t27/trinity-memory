#!/usr/bin/env python3
"""Differential migration evidence for native Bridge domain implementation.

Uses the unchanged Python reference as an oracle and a native C allocation/test
harness; request processing itself comes from generated bridge.t27.
"""
from __future__ import annotations
import argparse
import base64
import ctypes as C
import json
from pathlib import Path
import random
import sys
import uuid


def run(library: Path, source_root: Path) -> dict:
    sys.path.insert(0, str(source_root / "tests/reference"))
    from trinity_memory_reference.bridge import BridgeServer, _strict_json
    from trinity_memory_reference.container import encode_file
    from trinity_memory_reference.tensorpack import Tensor, encode_tensors
    from trinity_memory_reference.codecs import CODECS

    lib = C.CDLL(str(library.resolve()))
    lib.bridge_test_new.argtypes = [C.c_size_t] * 5
    lib.bridge_test_new.restype = C.c_void_p
    lib.bridge_test_free.argtypes = [C.c_void_p]
    lib.bridge_test_call.argtypes = [C.c_void_p, C.c_void_p, C.c_size_t, C.c_void_p, C.c_size_t, C.POINTER(C.c_int32)]
    lib.bridge_test_call.restype = C.c_int64
    for name in ("bridge_test_count", "bridge_test_bytes"):
        getattr(lib, name).argtypes = [C.c_void_p]
        getattr(lib, name).restype = C.c_size_t
    checked = 0
    rng = random.Random(27)

    class Pair:
        def __init__(self, **limits):
            self.reference = BridgeServer(**limits)
            config = self.reference.limits
            self.native = lib.bridge_test_new(*(config[name] for name in (
                "max_request_bytes", "max_object_bytes", "max_storage_bytes", "max_objects", "max_trits")))
            assert self.native, config
            self.handles = {}
            self.ids = 0

        def close(self):
            lib.bridge_test_free(self.native)
            self.reference.close()

        def raw(self, body, capacity=4 * 1024 * 1024):
            nonlocal checked
            output = C.create_string_buffer(capacity)
            status = C.c_int32()
            size = lib.bridge_test_call(self.native, body, len(body), output, capacity, C.byref(status))
            checked += 1
            if size < 0:
                return None, status.value
            return json.loads(output.raw[:size]), status.value

        def request(self, request):
            reference_request = json.loads(json.dumps(request))
            params = reference_request.get("params", {}) if isinstance(reference_request, dict) else None
            if isinstance(params, dict) and isinstance(params.get("handle"), str):
                params["handle"] = self.handles.get(params["handle"], params["handle"])
            expected = self.reference._request(reference_request)
            actual, status = self.raw(json.dumps(request, ensure_ascii=True, allow_nan=False).encode())
            assert status == 200, (request, actual, status)
            assert actual is not None
            if "error" in expected:
                # Numeric codes and echoed ids are the stable failure contract.
                assert (actual.get("error", {}).get("code"), actual["id"]) == (expected["error"]["code"], expected["id"]), (request, expected, actual)
            else:
                assert "result" in actual, (request, expected, actual)
                if request["method"] == "memory.upload":
                    handle = actual["result"]["handle"]
                    parsed = uuid.UUID(hex=handle)
                    assert parsed.version == 4 and parsed.hex == handle and handle not in self.handles
                    self.handles[handle] = expected["result"]["handle"]
                compare = json.loads(json.dumps(actual))
                if isinstance(compare["result"], dict) and "handle" in compare["result"]:
                    compare["result"]["handle"] = self.handles[compare["result"]["handle"]]
                assert compare == expected, (request, expected, compare)
            assert lib.bridge_test_count(self.native) == len(self.reference._objects)
            assert lib.bridge_test_bytes(self.native) == self.reference._stored_bytes
            return actual

        def rpc(self, method, params=None, request_id=None):
            self.ids += 1
            return self.request({"jsonrpc": "2.0", "id": self.ids if request_id is None else request_id,
                                 "method": method, "params": {} if params is None else params})

        def upload(self, data):
            return self.rpc("memory.upload", {"data": base64.b64encode(data).decode()})["result"]["handle"]

    pair = Pair()
    try:
        for method in ("trinity.capabilities", "chip_info", "trinity_chipInfo", "trinity_proveInference", "submit_to_bittensor", "unknown"):
            pair.rpc(method)
        for request in (None, [], {}, {"jsonrpc": "2.0", "method": "chip_info"},
                        {"jsonrpc": "2.0", "id": 0, "method": "chip_info", "unknown": 1}):
            pair.request(request)
        for candidate in (None, True, False, 0.0, 2**53, -(2**53), [], {}, "x" * 129):
            pair.request({"jsonrpc": "2.0", "id": candidate, "method": "chip_info"})
        for candidate in (0, -1, 2**53 - 1, -(2**53 - 1), "", "x" * 128, "☃" * 128, "a\x00b", "quote\"\\end"):
            pair.rpc("chip_info", request_id=candidate)
        pair.request({"jsonrpc": "1.0", "id": "keep-me", "method": "chip_info"})
        pair.request({"jsonrpc": "2.0", "id": 7, "method": 0})
        for params in (None, [], 0, False, {"extra": 1}):
            pair.request({"jsonrpc": "2.0", "id": 1, "method": "trinity.capabilities", "params": params})
        for data in (None, 1, [], "", "!", "AA=", "AB==", "AAB=", "AAAA=", "YQ==", "AA==AA=="):
            pair.rpc("memory.upload", {"data": data})
        malformed = [b'{', b'[]x', b'{"id":1,"id":2}', b'{"a":NaN}', b'{"a":Infinity}',
                     b'{"a":1e400}', b'"\xff"', b'{"a":01}', b'{"a":"\\ud800"}', b'{"a":true,}']
        for body in malformed:
            actual, status = pair.raw(body)
            assert status == 400 and actual["error"]["code"] == -32700, (body, actual, status)
        for codec in CODECS.values():
            for count in (0, 1, 2, 3, 4, 5, 6, 17, 22, 31, 64, 129):
                values = [rng.choice((-1, 0, 1)) for _ in range(count)]
                if codec.max_nonzero is not None:
                    for start in range(0, count, codec.group_size):
                        chosen = set(rng.sample(range(start, min(start + codec.group_size, count)),
                                                min(codec.max_nonzero, count - start)))
                        for i in range(start, min(start + codec.group_size, count)):
                            if i not in chosen:
                                values[i] = 0
                data = encode_file(values, codec.name)
                handle = pair.upload(data)
                pair.rpc("memory.read", {"handle": handle})
                pair.rpc("memory.read", {"handle": handle, "offset": 3, "length": 5})
                pair.rpc("memory.read", {"handle": handle, "offset": len(data)})
                pair.rpc("memory.info", {"handle": handle})
                acts = [rng.randint(-128, 127) for _ in values]
                if acts:
                    acts[0] = -128
                    acts[-1] = 127
                pair.rpc("compute.dot", {"handle": handle, "tensor_name": "weights", "activations": acts})
                pair.rpc("memory.delete", {"handle": handle})
                pair.rpc("memory.read", {"handle": handle})
        tensors = [Tensor("matrix", (2, 3), (1, -1, 0, -1, 1, 1), scales=(0.5, 2.0), scale_axis=0, axes=("rows", "cols")),
                   Tensor("vector ☃", (3,), (1, -1, 1), scales=(1e-8,)),
                   Tensor("cube", (1, 1, 3), (0, 1, 0)),
                   Tensor("input_scale", (2, 3), (0, 1, 0, 1, 0, 1), scales=(1., 2., 3.), scale_axis=1),
                   Tensor("scalar", (), (1,)),
                   Tensor("vector_scale", (3,), (1, 0, 1), scales=(1., 2., 3.), scale_axis=0)]
        handle = pair.upload(encode_tensors(tensors))
        pair.rpc("memory.info", {"handle": handle})
        for name in ("matrix", "vector ☃", "cube", "input_scale", "scalar", "vector_scale", "missing", "", False):
            pair.rpc("compute.dot", {"handle": handle, "tensor_name": name, "activations": [-128, 127, -128]})
        for acts in ([], [1, 2], [1, 2, 128], [1, 2, -129], [1, True, 1], [1, 0.0, 1], "123", None):
            pair.rpc("compute.dot", {"handle": handle, "tensor_name": "matrix", "activations": acts})
        for offset, length in ((-1, 1), (True, 1), (0.0, 1), (0, -1), (0, True), (0, 1.0), (10**6, 0), (0, 10**6)):
            pair.rpc("memory.read", {"handle": handle, "offset": offset, "length": length})
        for bad in ("../file", "A" * 32, "f" * 32, 0, None):
            pair.rpc("memory.info", {"handle": bad})
        pair.rpc("memory.delete", {"handle": handle})
        for size in range(24, 50):
            damaged = bytearray(encode_file([1] * 129))
            damaged[size % len(damaged)] ^= 1
            pair.rpc("memory.upload", {"data": base64.b64encode(damaged).decode()})
        empty = pair.upload(encode_tensors([]))
        pair.rpc("memory.info", {"handle": empty})
        pair.rpc("compute.dot", {"handle": empty, "tensor_name": "weights", "activations": []})
        pair.rpc("memory.delete", {"handle": empty})
    finally:
        pair.close()

    data = encode_file([0] * 10)
    for limits in ({"max_objects": 1, "max_storage_bytes": len(data), "max_object_bytes": len(data)},
                   {"max_storage_bytes": len(data) - 1}, {"max_trits": 2}):
        pair = Pair(**limits)
        try:
            encoded = {"data": base64.b64encode(data).decode()}
            first = pair.rpc("memory.upload", encoded)
            pair.rpc("memory.upload", encoded)
            if "result" in first:
                pair.rpc("memory.delete", {"handle": first["result"]["handle"]})
                pair.rpc("memory.upload", encoded)
            pair.rpc("memory.upload", {"data": base64.b64encode(encode_tensors([Tensor("many", (10,), tuple([0] * 10))])).decode()})
        finally:
            pair.close()
    pair = Pair(max_request_bytes=100)
    try:
        result, status = pair.raw(b" " * 101)
        assert status == 413 and result["error"]["code"] == -32010
    finally:
        pair.close()
    return {"evidence": "native-t27-bridge-domain-differential", "rpc_checks": checked,
            "reference": "trinity_memory_reference.bridge.BridgeServer", "network_tested": False,
            "all_codecs": list(CODECS), "atomic_upload_accounting": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(args.library, args.source_root)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, sort_keys=True))
