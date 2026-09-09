#!/usr/bin/env python3
"""Exercise the actual, separately checked-out Trinity SDK against our Bridge.

Run with PYTHONPATH=build/upstream-sdk python scripts/test_sdk_bridge.py.
The SDK itself remains unmodified; its default jsonrpc backend is not replaced.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trinity import TrinityChip
from trinity.types import ChipInfo
from trinity_memory.bridge import BridgeClient, BridgeServer, SDKMemoryBackend
from trinity_memory.edge import signal_model


with BridgeServer() as server:
    backend = SDKMemoryBackend(BridgeClient(server.url))
    chip = TrinityChip(backend)
    info = chip.chip_info()
    assert isinstance(info, ChipInfo)
    assert all(len(value) == 16 for value in (info.phi_id, info.euler_id, info.gamma_id))
    assert chip.verify_anchor()  # Checks a software constant only, not a physical device.
    assert backend.client.capabilities()["backend"] == "emulator"
    data = signal_model()
    handle = backend.client.upload(data)
    assert backend.client.read(handle) == data
    response = backend.client.dot(handle, "signal_templates", [-40] * 6 + [40] * 6)
    assert response["accumulators"] == [480, -480, 0]
    for method in (lambda: chip.prove_inference("model", b"input"),
                   lambda: chip.submit_to_bittensor(1, 1)):
        try:
            method()
        except NotImplementedError:
            pass
        else:
            raise AssertionError("unsupported proof/submission must not report success")
    backend.client.delete(handle)
print("PASS: upstream TrinityChip + SDKMemoryBackend + real HTTP memory/dot; emulator only")
