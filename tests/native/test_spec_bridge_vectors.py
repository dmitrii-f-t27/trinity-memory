#!/usr/bin/env python3
"""Replay conformance/memory_bridge.json through the generated bridge module.

Uses the bridge test harness library built by tools/test-t27.sh
(bridge_test_new / bridge_test_call from tests/native_bridge.c), so every
request runs in process through tm_bridge_request without HTTP framing.
Transport vectors are skipped here; tests/test_spec_bridge.py covers them.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes as C
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
import spec_bridge_replay as replay  # noqa: E402

HARNESS_DEFAULTS = {"max_request_bytes": 65536, "max_object_bytes": 65536, "max_storage_bytes": 1 << 20,
                    "max_objects": 16, "max_trits": 65536}


class NativeSession:
    def __init__(self, lib, limits, device=None):
        config = dict(HARNESS_DEFAULTS, **{k: v for k, v in limits.items() if k != "timeout"})
        self.lib = lib
        if device:
            bitstream = bytes.fromhex(device["bitstream_sha256"])
            capture = bytes.fromhex(device["capture_sha256"])
            if len(bitstream) != 32 or len(capture) != 32:
                raise RuntimeError("device evidence hashes must be 32 bytes of hex")
            self.handle = self._new_device(config, device, bitstream, capture)
        else:
            self.handle = lib.bridge_test_new(config["max_request_bytes"], config["max_object_bytes"],
                                              config["max_storage_bytes"], config["max_objects"], config["max_trits"])
        if not self.handle:
            raise RuntimeError(f"harness refused limits {config}")
        self.capacity = 16384 + 2 * config["max_object_bytes"] + 40 * config["max_trits"]
        self.output = C.create_string_buffer(self.capacity)

    def _new_device(self, config, device, bitstream, capture):
        dna = int(device["dna"], 16)
        if not 0 <= dna < 1 << 64:
            raise RuntimeError("device dna must be a 64-bit hex value")
        self.lib.bridge_test_new_device.argtypes = [C.c_size_t] * 5 + [C.c_uint32, C.c_uint64, C.c_void_p, C.c_void_p]
        self.lib.bridge_test_new_device.restype = C.c_void_p
        bitstream_buffer = C.create_string_buffer(bitstream, 32)
        capture_buffer = C.create_string_buffer(capture, 32)
        return self.lib.bridge_test_new_device(config["max_request_bytes"], config["max_object_bytes"],
                                               config["max_storage_bytes"], config["max_objects"], config["max_trits"],
                                               C.c_uint32(device["idcode"]), C.c_uint64(dna),
                                               bitstream_buffer, capture_buffer)

    def send(self, body, headers=None):
        status = C.c_int32(0)
        request = C.create_string_buffer(body, len(body))
        written = self.lib.bridge_test_call(self.handle, request, len(body), self.output, self.capacity, C.byref(status))
        if written < 0:
            raise replay.VectorFailure(f"bridge_test_call failed ({written})")
        return status.value, json.loads(self.output.raw[:written])

    def close(self):
        self.lib.bridge_test_free(self.handle)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    lib = C.CDLL(str(args.library.resolve()))
    lib.bridge_test_new.argtypes = [C.c_size_t] * 5
    lib.bridge_test_new.restype = C.c_void_p
    lib.bridge_test_free.argtypes = [C.c_void_p]
    lib.bridge_test_call.argtypes = [C.c_void_p, C.c_void_p, C.c_size_t, C.c_void_p, C.c_size_t, C.POINTER(C.c_int32)]
    lib.bridge_test_call.restype = C.c_int64

    @contextlib.contextmanager
    def session(limits, device=None):
        item = NativeSession(lib, limits, device)
        try:
            yield item
        finally:
            item.close()

    document = replay.load_document()
    run, steps, skipped = replay.replay(document, session, transport=False, device=True)
    expected = sum(1 for vector in document["vectors"] if vector["kind"] != "transport")
    if run != expected:
        raise SystemExit(f"replayed {run} of {expected} in-process vectors")
    summary = {"library": str(args.library), "vectors": run, "steps": steps, "skipped_transport": skipped}
    if args.output:
        args.output.write_text(json.dumps(summary, indent=1) + "\n")
    print(f"PASS native bridge vector replay: {run} vectors, {steps} sequence steps, "
          f"{len(skipped)} transport vectors left to the TCP replay")


if __name__ == "__main__":
    main()
