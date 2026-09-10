"""Cross-boundary conformance runner; software and RTL evidence stay separate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import random

from .bridge import BridgeClient, BridgeError, BridgeServer
from .codecs import pack, validate_trits
from .container import encode_file
from .tensorpack import Tensor, decode_tensors, encode_tensors


def run_conformance(vectors: Path, *, rtl: bool = False, seed: int = 27) -> dict:
    source = vectors.read_bytes()
    from .cli import _read_json
    document = _read_json(vectors)
    if type(document) is not dict or set(document) != {"schema", "vectors"} or document["schema"] != "trinity.conformance.v1":
        raise ValueError("unsupported conformance fixture schema")
    if type(document["vectors"]) is not list or not 1 <= len(document["vectors"]) <= 256:
        raise ValueError("conformance vectors must be a nonempty array of at most 256 entries")
    cases = list(document["vectors"])
    names = set()
    for case in cases:
        required = {"name", "weights", "activations", "dot"}
        optional = {"dense5_hex", "baseline2_hex", "dense17_hex", "dense22_hex"}
        if type(case) is not dict or not required <= case.keys() or not case.keys() <= required | optional:
            raise ValueError("invalid conformance vector fields")
        if type(case["name"]) is not str or not case["name"] or case["name"] in names:
            raise ValueError("conformance vector names must be nonempty and unique")
        names.add(case["name"])
        weights, activations = case["weights"], case["activations"]
        if type(weights) is not list or not 1 <= len(weights) <= 4096:
            raise ValueError("conformance weight array length must be 1..4096")
        validate_trits(weights)
        if type(activations) is not list or len(activations) != len(weights) or any(type(x) is not int or not -128 <= x <= 127 for x in activations):
            raise ValueError("conformance activations must be a matching signed int8 array")
        if type(case["dot"]) is not int or case["dot"] != sum(w * x for w, x in zip(weights, activations)):
            raise ValueError("golden dot must be the exact integer sum")
        for field in optional & case.keys():
            if type(case[field]) is not str:
                raise ValueError("golden payload must be a hexadecimal string")
            try:
                bytes.fromhex(case[field])
            except ValueError:
                raise ValueError("invalid golden payload hex") from None
    rng = random.Random(seed)
    for count in (1, 4, 5, 6, 12, 31, 65):
        weights = [rng.choice((-1, 0, 1)) for _ in range(count)]
        activations = [rng.randint(-128, 127) for _ in range(count)]
        cases.append({"name": f"random-{count}", "weights": weights, "activations": activations,
                      "dot": sum(w * x for w, x in zip(weights, activations))})
    checks = []
    with BridgeServer() as server:
        client = BridgeClient(server.url)
        for case in cases:
            for codec in ("dense5", "baseline2", "dense17", "dense22"):
                weights = case["weights"]
                expected_hex = case.get(f"{codec}_hex")
                if expected_hex is not None and pack(weights, codec).hex() != expected_hex:
                    raise AssertionError(f"golden byte mismatch: {case['name']}/{codec}")
                blob = encode_tensors([Tensor("weights", (len(weights),), tuple(weights), codec)])
                handle = client.upload(blob)
                restored = client.read(handle)
                retrieved_weights = list(decode_tensors(restored)[0].values)
                if restored != blob or retrieved_weights != weights:
                    raise AssertionError("TensorPack network round trip mismatch")
                response = client.dot(handle, "weights", case["activations"])
                if response["accumulators"] != [case["dot"]]:
                    raise AssertionError("network compute differs from integer reference")
                witness = None
                if rtl and codec in ("dense5", "baseline2"):
                    from .rtl_compute import run_rtl_dot
                    witness = run_rtl_dot(retrieved_weights, case["activations"],
                                          codec="dense5" if codec == "dense5" else "baseline5", seed=seed)
                    if witness["result"] != case["dot"]:
                        raise AssertionError("RTL result mismatch")
                checks.append({"name": case["name"], "codec": codec, "passed": True,
                               "expected_dot": case["dot"], "rtl": witness})
                client.delete(handle)
        # Each sparse fixture obeys the codec's actual local constraint.
        for codec, weights in (("sparse41", [1, 0, 0, 0, 0, -1, 0, 0]),
                               ("sparse82", [1, 0, 0, 0, 0, -1, 0, 0])):
            blob = encode_file(weights, codec)
            handle = client.upload(blob)
            if client.read(handle) != blob:
                raise AssertionError("TMEM sparse transfer mismatch")
            client.delete(handle)
            checks.append({"name": "sparse-transfer", "codec": codec, "passed": True, "rtl": None})
        corrupt_checks = 0
        valid = encode_tensors([Tensor("w", (5,), (1, 0, -1, 0, 1))])
        for index in sorted({0, 4, 12, 31, len(valid) // 2, len(valid) - 1}):
            damaged = bytearray(valid)
            damaged[index] ^= 1
            try:
                client.upload(bytes(damaged))
            except BridgeError as error:
                if error.code != -32602:
                    raise AssertionError(f"expected invalid-container rejection, got RPC {error.code}") from error
                corrupt_checks += 1
            else:
                raise AssertionError(f"corruption was accepted at byte {index}")
    return {"schema": "trinity.conformance-report.v1", "passed": True,
            "seed": seed, "fixture_sha256": hashlib.sha256(source).hexdigest(),
            "checks": checks, "positive_checks": len(checks), "corrupt_rejections": corrupt_checks,
            "rtl_checks": sum(check["rtl"] is not None for check in checks),
            "physical_device_tested": False,
            "evidence": ["software-loopback-http"] + (["rtl-simulation"] if rtl else [])}
