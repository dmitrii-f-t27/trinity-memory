"""Replay conformance/memory_bridge.json against a Bridge implementation.

A runner supplies a session per vector: ``session(limits)`` is a context manager
yielding an object with ``send(body: bytes) -> (http_status, parsed_json)`` and,
for raw HTTP framing vectors, ``raw(text: str) -> (http_status, parsed_json)``.
The TCP runner in tests/test_spec_bridge.py drives the native loopback server;
tests/native/test_spec_bridge_vectors.py drives the generated bridge module in
process. Expectations are checked identically in both.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT_PATH = ROOT / "conformance" / "memory_bridge.json"
HANDLE = re.compile(r"^[0-9a-f]{32}$")


def load_document():
    return json.loads(DOCUMENT_PATH.read_text(encoding="utf-8"))


class VectorFailure(AssertionError):
    pass


def substitute(value, captures):
    if isinstance(value, str) and value.startswith("$"):
        name = value[1:]
        if name not in captures:
            raise VectorFailure(f"capture ${name} is not defined")
        return captures[name]
    if isinstance(value, dict):
        return {key: substitute(item, captures) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute(item, captures) for item in value]
    return value


def subset(expected, actual, path):
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise VectorFailure(f"{path}: expected an object, got {actual!r}")
        for key, value in expected.items():
            if key not in actual:
                raise VectorFailure(f"{path}.{key}: missing (have {sorted(actual)})")
            subset(value, actual[key], f"{path}.{key}")
        return
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            raise VectorFailure(f"{path}: expected {expected!r}, got {actual!r}")
        for index, (item, other) in enumerate(zip(expected, actual)):
            subset(item, other, f"{path}[{index}]")
        return
    if isinstance(expected, bool) or isinstance(actual, bool):
        if type(expected) is not type(actual) or expected != actual:
            raise VectorFailure(f"{path}: expected {expected!r}, got {actual!r}")
        return
    if expected != actual:
        raise VectorFailure(f"{path}: expected {expected!r}, got {actual!r}")


def check_format(kind, value, path):
    if kind == "uuid4-hex32":
        if not (isinstance(value, str) and HANDLE.match(value) and value[12] == "4" and value[16] in "89ab"):
            raise VectorFailure(f"{path}: {value!r} is not a version-4 UUID in 32 lowercase hex characters")
    else:
        raise VectorFailure(f"{path}: unknown format {kind!r}")


def check(name, status, body, expect, request=None):
    wanted_status = expect.get("http_status", 200)
    if status != wanted_status:
        raise VectorFailure(f"{name}: HTTP {status}, expected {wanted_status}: {body!r}")
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        raise VectorFailure(f"{name}: response is not a JSON-RPC 2.0 object: {body!r}")
    if "id" in expect:
        if body.get("id") != expect["id"]:
            raise VectorFailure(f"{name}: id {body.get('id')!r}, expected {expect['id']!r}")
    elif isinstance(request, dict) and "id" in request and body.get("id") != request["id"]:
        raise VectorFailure(f"{name}: id {body.get('id')!r} was not echoed from {request['id']!r}")
    code = expect.get("error_code")
    if code is not None:
        error = body.get("error")
        if "result" in body or not isinstance(error, dict) or error.get("code") != code:
            raise VectorFailure(f"{name}: expected error {code}, got {body!r}")
        if not isinstance(error.get("message"), str) or not error["message"]:
            raise VectorFailure(f"{name}: error message must be a nonempty string: {error!r}")
        return {}
    if "error" in body or "result" not in body:
        raise VectorFailure(f"{name}: expected a result, got {body!r}")
    result = body["result"]
    if "result" in expect:
        subset(expect["result"], result, f"{name}.result")
    for key in expect.get("result_keys", []):
        if not isinstance(result, dict) or key not in result:
            raise VectorFailure(f"{name}.result.{key}: missing")
    for key, kind in expect.get("result_format", {}).items():
        check_format(kind, result.get(key) if isinstance(result, dict) else None, f"{name}.result.{key}")
    return result


def replay(document, session_factory, *, transport=False, device=False, only=None):
    """Run every vector; return (vectors_run, steps_run, skipped_ids).

    Device vectors (a "device" evidence block) need a harness that can construct
    the fpga backend; the TCP replay has no such server and skips them.
    """
    run, steps, skipped = 0, 0, []
    for vector in document["vectors"]:
        identifier = vector["id"]
        if only and identifier not in only:
            continue
        kind = vector["kind"]
        if kind == "transport" and not transport:
            skipped.append(identifier)
            continue
        if vector.get("device") and not device:
            skipped.append(identifier)
            continue
        limits = dict(vector.get("limits", {}))
        with session_factory(limits, vector.get("device")) as session:
            if kind == "rpc":
                request = vector.get("request")
                body = json.dumps(request).encode() if request is not None else vector["request_text"].encode()
                status, response = session.send(body)
                check(identifier, status, response, vector["expect"], request)
                run += 1
            elif kind == "transport":
                if "raw" in vector:
                    status, response = session.raw(vector["raw"])
                else:
                    status, response = session.send(vector["body"].encode(), vector.get("headers"))
                check(identifier, status, response, vector["expect"])
                run += 1
            elif kind == "sequence":
                captures = {}
                for item in vector["steps"]:
                    name = f"{identifier}/{item['name']}"
                    request = substitute(item["request"], captures)
                    status, response = session.send(json.dumps(request).encode())
                    result = check(name, status, response, item["expect"], request)
                    for variable, key in item.get("capture", {}).items():
                        if not isinstance(result, dict) or key not in result:
                            raise VectorFailure(f"{name}: cannot capture {key!r} from {result!r}")
                        captures[variable] = result[key]
                    steps += 1
                run += 1
            else:
                raise VectorFailure(f"{identifier}: unknown kind {kind!r}")
    return run, steps, skipped
