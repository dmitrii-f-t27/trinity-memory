#!/usr/bin/env python3
"""Generate or check the conformance vectors derived from specs/memory/*.t27.

The expectations here are computed from the contract stated in
specs/memory/types.t27 with plain Python arithmetic and zlib.crc32. They do not
call the native implementation; tests/test_spec_types.py compares the committed
vectors with the executable stack, so the spec, this generator and the
implementation must all agree before CI passes.
"""
import argparse
import itertools
import json
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "conformance" / "memory_types.json"
BRIDGE_OUTPUT = ROOT / "conformance" / "memory_bridge.json"
TENSORPACK_OUTPUT = ROOT / "conformance" / "memory_tensorpack.json"
STREAM_OUTPUT = ROOT / "conformance" / "memory_stream_compute.json"

CODECS = {
    "baseline2": {"id": 0, "group_trits": 4, "group_bits": 8, "max_nonzero": None},
    "dense5": {"id": 1, "group_trits": 5, "group_bits": 8, "max_nonzero": None},
    "dense17": {"id": 2, "group_trits": 17, "group_bits": 27, "max_nonzero": None},
    "dense22": {"id": 3, "group_trits": 22, "group_bits": 35, "max_nonzero": None},
    "sparse41": {"id": 4, "group_trits": 4, "group_bits": 4, "max_nonzero": 1},
    "sparse82": {"id": 5, "group_trits": 8, "group_bits": 8, "max_nonzero": 2},
}
LANE = {0: 0, 1: 1, -1: 2}
HEADER_BYTES = 24


def valid_word_count(name):
    c = CODECS[name]
    n, k = c["group_trits"], c["max_nonzero"]
    if k is None:
        return 3 ** n
    return sum(__import__("math").comb(n, i) * 2 ** i for i in range(k + 1))


def sparse_word(group, n):
    nonzero = [(i, t) for i, t in enumerate(group) if t]
    if not nonzero:
        return 0
    if len(nonzero) == 1:
        position, trit = nonzero[0]
        return 1 + 2 * position + (1 if trit == 1 else 0)
    (first, a), (second, b) = nonzero
    pair_index = list(itertools.combinations(range(n), 2)).index((first, second))
    signs = (1 if a == 1 else 0) | ((1 if b == 1 else 0) << 1)
    return 1 + 2 * n + pair_index * 4 + signs


def group_word(name, group):
    c = CODECS[name]
    if name == "baseline2":
        return sum(LANE[t] << (2 * i) for i, t in enumerate(group))
    if c["max_nonzero"] is not None:
        return sparse_word(group, c["group_trits"])
    return sum((t + 1) * 3 ** i for i, t in enumerate(group))


def encode(name, trits):
    """Bitstream packing exactly as the spec states: whole groups, LE bit order."""
    c = CODECS[name]
    n, bits = c["group_trits"], c["group_bits"]
    if c["max_nonzero"] is not None:
        for start in range(0, len(trits), n):
            if sum(1 for t in trits[start:start + n] if t) > c["max_nonzero"]:
                raise ValueError("sparsity constraint violated")
    buffered, buffered_bits, out = 0, 0, bytearray()
    for start in range(0, len(trits), n):
        group = list(trits[start:start + n]) + [0] * (n - len(trits[start:start + n]))
        buffered |= group_word(name, group) << buffered_bits
        buffered_bits += bits
        while buffered_bits >= 8:
            out.append(buffered & 255)
            buffered >>= 8
            buffered_bits -= 8
    if buffered_bits:
        out.append(buffered)
    return bytes(out)


def payload_bytes(name, count):
    c = CODECS[name]
    groups = -(-count // c["group_trits"])
    return -(-(groups * c["group_bits"]) // 8)


def tmem(name, trits):
    payload = encode(name, trits)
    prefix = struct.pack("<4sBBBBQI", b"TMEM", 1, CODECS[name]["id"], 0, 0, len(trits), len(payload))
    crc = zlib.crc32(prefix + payload)
    return prefix + struct.pack("<I", crc) + payload


def word_hex(name, word):
    """A single encoded word packed into the minimum whole bytes."""
    c = CODECS[name]
    return word.to_bytes(-(-c["group_bits"] // 8), "little").hex()


def build():
    groups = {
        "baseline2": [1, -1, 0, 1],
        "dense5": [0, 0, 0, 0, 0],
        "dense17": [1, -1, 0, 1, 1, 0, -1, -1, 0, 1, 0, 0, 1, -1, 1, 0, -1],
        "dense22": [-1, 0, 1, 1, 0, -1, 0, 1, 1, -1, 0, 0, 1, 0, -1, 1, 0, 0, -1, 1, 1, 0],
        "sparse41": [0, -1, 0, 0],
        "sparse82": [1, 0, 0, 0, 0, 0, -1, 0],
    }
    vectors = []
    for name, group in groups.items():
        vectors.append({
            "id": f"{name}_group", "kind": "group", "codec": name, "trits": group,
            "payload_hex": encode(name, group).hex(),
            "description": f"One complete {name} group; payload is the encoded word in little-endian bit order",
        })
    vectors.append({
        "id": "dense5_zero_group_is_code_121", "kind": "group", "codec": "dense5", "trits": [0] * 5,
        "payload_hex": "79", "description": "All-zero dense5 group uses code 121 (0x79), not 0",
    })
    vectors.append({
        "id": "dense5_partial_group_padding", "kind": "group", "codec": "dense5", "trits": [1, -1, 0, 1, 0, -1, 1],
        "payload_hex": encode("dense5", [1, -1, 0, 1, 0, -1, 1]).hex(),
        "description": "Seven trits: the second group pads lanes 2..4 with logical zero",
    })
    for name, group in groups.items():
        vectors.append({
            "id": f"tmem_{name}_group", "kind": "container", "codec": name, "trits": group,
            "tmem_hex": tmem(name, group).hex(),
            "description": "TMEM v1: 24-byte header, CRC32 over header[0:20] + payload",
        })
    seven = [1, -1, 0, 1, 0, -1, 1]
    vectors.append({
        "id": "tmem_dense5_seven_trits", "kind": "container", "codec": "dense5", "trits": seven,
        "tmem_hex": tmem("dense5", seven).hex(), "description": "Container with a padded final group",
    })
    invalid = [
        ("dense5_reserved_code_243", "dense5", 5, word_hex("dense5", 243), "First reserved dense5 code"),
        ("dense5_reserved_code_255", "dense5", 5, word_hex("dense5", 255), "Last reserved dense5 code"),
        ("dense5_nonzero_padding", "dense5", 4, word_hex("dense5", 121 + 81), "Lane 4 holds +1 beyond count 4"),
        ("baseline2_lane_11", "baseline2", 4, word_hex("baseline2", 0xc0), "Lane 3 equals the invalid code 11"),
        ("baseline2_lane_11_low", "baseline2", 4, word_hex("baseline2", 0x03), "Lane 0 equals the invalid code 11"),
        ("dense17_reserved_code", "dense17", 17, word_hex("dense17", 3 ** 17), "3^17 is the first reserved dense17 word"),
        ("dense22_reserved_code", "dense22", 22, word_hex("dense22", 3 ** 22), "3^22 is the first reserved dense22 word"),
        ("sparse41_reserved_code_9", "sparse41", 4, word_hex("sparse41", 9), "First reserved sparse41 code"),
        ("sparse82_reserved_code_129", "sparse82", 8, word_hex("sparse82", 129), "First reserved sparse82 code"),
    ]
    for identifier, codec, count, payload_hex, description in invalid:
        vectors.append({"id": identifier, "kind": "invalid_word", "codec": codec, "count": count,
                        "payload_hex": payload_hex, "description": description})
    vectors.append({
        "id": "sparse41_two_nonzero_rejected", "kind": "invalid_sparsity", "codec": "sparse41",
        "trits": [1, 1, 0, 0], "description": "Two nonzero trits in a block of four violate sparse41",
    })
    vectors.append({
        "id": "sparse82_three_nonzero_rejected", "kind": "invalid_sparsity", "codec": "sparse82",
        "trits": [1, 0, -1, 0, 0, 1, 0, 0], "description": "Three nonzero trits in a block of eight violate sparse82",
    })
    constants = {
        "lanes": {"bits": 2, "zero": 0, "pos": 1, "neg": 2, "invalid": 3},
        "codecs": {name: dict(c, valid_words=valid_word_count(name)) for name, c in CODECS.items()},
        "dense5": {"zero_group_code": 121, "code_limit": 243, "reserved_first": 243, "reserved_last": 255},
        "sparse41": {"code_limit": 9, "reserved_first": 9, "reserved_last": 15},
        "sparse82": {"code_limit": 129, "reserved_first": 129, "reserved_last": 255},
        "tmem": {"magic": "TMEM", "version": 1, "header_bytes": HEADER_BYTES, "version_offset": 4,
                 "codec_offset": 5, "reserved_offset": 6, "count_offset": 8, "payload_length_offset": 16,
                 "crc_offset": 20, "crc_covers": "header[0:20] + payload", "max_payload_bytes": 4294967295},
        "crc32": {"poly": "0xedb88320", "init": "0xffffffff", "xor_out": "0xffffffff", "check_value": "0xcbf43926"},
        "status": {"ok": 0, "err_codec": -1, "err_capacity": -2, "err_trit": -3, "err_sparsity": -4,
                   "err_length": -5, "err_code": -6, "err_padding": -7, "err_header": -8,
                   "err_checksum": -9, "err_limit": -10},
        "evidence": {"emulator": 0, "software": 1, "rtl_simulation": 2, "fpga": 3},
        "payload_bytes_65536": {name: payload_bytes(name, 65536) for name in CODECS},
    }
    invariants = [
        {"id": "lane_codes_fit_two_bits", "condition": "TMS_LANE_INVALID < 4 && TMS_LANE_BITS == 2"},
        {"id": "dense5_fits_one_byte", "condition": "TMS_DENSE5_CODE_LIMIT <= 256"},
        {"id": "dense17_fits_27_bits", "condition": "TMS_DENSE17_CODE_LIMIT <= 2^27"},
        {"id": "dense22_fits_35_bits", "condition": "TMS_DENSE22_CODE_LIMIT <= 2^35"},
        {"id": "sparse41_state_count", "condition": "TMS_SPARSE41_CODE_LIMIT == 1 + 4*2"},
        {"id": "sparse82_state_count", "condition": "TMS_SPARSE82_CODE_LIMIT == 1 + 8*2 + 28*4"},
        {"id": "dense5_zero_group_is_all_middle_digits", "condition": "TMS_DENSE5_ZERO_GROUP_CODE == 1+3+9+27+81"},
        {"id": "tmem_header_is_contiguous", "condition": "4+1+1+2+8+4+4 == 24"},
        {"id": "tmem_crc_covers_the_header_prefix", "condition": "TMS_TMEM_CRC_COVERED_HEADER_BYTES == TMS_TMEM_CRC_OFFSET"},
        {"id": "status_codes_are_distinct_and_negative", "condition": "TMS_ERR_LIMIT < ... < TMS_ERR_CODEC < TMS_OK"},
    ]
    return {
        "module": "TrinityMemoryTypes",
        "spec_path": "specs/memory/types.t27",
        "schema_version": 2,
        "format_family": "Conformance",
        "vector_name": "Trinity Memory shared types",
        "description": "Lane codes, codec geometry, valid-code limits, TMEM v1 framing, CRC32 and status codes. "
                       "Expectations are computed from the spec by tools/generate-spec-vectors.py without the native implementation.",
        "created_at": "2026-09-11T00:00:00Z",
        "generator": "tools/generate-spec-vectors.py",
        "constants": constants,
        "invariants": invariants,
        "vectors": vectors,
    }


# ---------------------------------------------------------------------------
# specs/memory/bridge.t27


def ttpk(tensors):
    """TensorPack v1 container from the format stated in docs/tensorpack.md."""
    descriptors, payload = [], bytearray()
    for tensor in tensors:
        codec = tensor.get("codec", "dense5")
        file = tmem(codec, tensor["values"])
        descriptors.append({
            "name": tensor["name"], "shape": list(tensor["shape"]), "axes": list(tensor.get("axes", [])),
            "codec": codec, "scales": [float(x) for x in tensor.get("scales", [1.0])],
            "scale_axis": tensor.get("scale_axis"), "offset": len(payload), "length": len(file),
        })
        payload += file
    metadata = json.dumps({"order": "C", "tensors": descriptors}, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":"), sort_keys=True).encode("utf-8")
    prefix = struct.pack("<4sBBHIIQ", b"TTPK", 1, 0, 0, len(metadata), len(descriptors), len(payload))
    header = prefix + struct.pack("<II", zlib.crc32(prefix + metadata), zlib.crc32(payload))
    return bytes(header + metadata + payload)


def b64(data):
    import base64
    return base64.b64encode(data).decode("ascii")


def sha256_hex(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def tmem_info(name, trits, container):
    """The memory.info result for a TMEM container, from the documented fields."""
    count, payload = len(trits), len(container) - HEADER_BYTES
    codec = CODECS[name]
    crc = int.from_bytes(container[20:24], "little")
    return {
        "format": "TMEM", "version": 1, "codec": name, "count": count, "payload_bytes": payload,
        "header_bytes": HEADER_BYTES, "container_bytes": len(container),
        "payload_bpw": (payload * 8.0 / count) if count else None,
        "container_bpw": (len(container) * 8.0 / count) if count else None,
        "crc32": f"{crc:08x}", "validated": True,
        "layout": {"id": codec["id"], "name": name, "group_size": codec["group_trits"],
                   "group_bits": codec["group_bits"], "max_nonzero": codec["max_nonzero"]},
        "tensors": [{"name": "weights", "shape": [count], "scales": [1.0], "scale_axis": None}],
        "sha256": sha256_hex(container), "backend": "emulator",
    }


def ttpk_info(tensors, container):
    metadata_bytes = int.from_bytes(container[8:12], "little")
    payload_bytes = int.from_bytes(container[16:24], "little")
    items, offset, total = [], 0, 0
    for tensor in tensors:
        codec = tensor.get("codec", "dense5")
        count = 1
        for dim in tensor["shape"]:
            count *= dim
        length = HEADER_BYTES + payload_bytes_for(codec, count)
        items.append({"name": tensor["name"], "shape": list(tensor["shape"]), "codec": codec,
                      "scales": [float(x) for x in tensor.get("scales", [1.0])],
                      "scale_axis": tensor.get("scale_axis"), "axes": list(tensor.get("axes", [])),
                      "offset": offset, "length": length, "count": count, "payload_bytes": length - HEADER_BYTES})
        offset += length
        total += count
    return {"format": "TensorPack", "version": 1, "order": "C", "tensor_count": len(tensors), "total_count": total,
            "header_bytes": 32, "metadata_bytes": metadata_bytes, "payload_bytes": payload_bytes,
            "container_bytes": len(container), "validated": True, "tensors": items,
            "sha256": sha256_hex(container), "backend": "emulator"}


def payload_bytes_for(name, count):
    return payload_bytes(name, count)


IDENTITY_PARTS = ("phi", "euler", "gamma")


def identity(part):
    import hashlib
    return hashlib.sha256(f"trinity-memory-emulator-v1:{part}".encode()).digest()[:16].hex()


DEFAULT_LIMITS = {"max_request_bytes": 2097152, "max_object_bytes": 524288, "max_storage_bytes": 8388608,
                  "max_objects": 16, "max_trits": 1000000}
METHODS = ["trinity.capabilities", "memory.upload", "memory.read", "memory.info", "memory.delete",
           "compute.dot", "chip_info", "trinity_chipInfo"]


def rpc(identifier, method, params=None, *, request_id=1, limits=None, description="", **expect):
    request = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        request["params"] = params
    vector = {"id": identifier, "kind": "rpc", "request": request, "expect": expect, "description": description}
    if limits:
        vector["limits"] = limits
    return vector


def raw_rpc(identifier, text, *, limits=None, description="", **expect):
    vector = {"id": identifier, "kind": "rpc", "request_text": text, "expect": expect, "description": description}
    if limits:
        vector["limits"] = limits
    return vector


def transport(identifier, *, limits=None, description="", **fields):
    expect = fields.pop("expect")
    vector = {"id": identifier, "kind": "transport", "expect": expect, "description": description, **fields}
    if limits:
        vector["limits"] = limits
    return vector


def step(name, method, params=None, *, request_id=1, capture=None, description="", **expect):
    request = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        request["params"] = params
    item = {"name": name, "request": request, "expect": expect}
    if capture:
        item["capture"] = capture
    if description:
        item["description"] = description
    return item


def build_bridge():
    six = [-1, 0, 1, 1, -1, 0]
    tmem6 = tmem("dense5", six)
    tmem3 = tmem("dense5", [1, -1, 0])
    tmem_empty = tmem("dense5", [])
    tmem10 = tmem("dense5", [0] * 10)
    tmem20 = tmem("dense5", [0] * 20)
    rows = [{"name": "matrix", "shape": [2, 3], "values": [1, -1, 0, -1, 1, 1], "scales": [0.5, 2.0], "scale_axis": 0}]
    ttpk_rows = ttpk(rows)
    layouts = [
        {"name": "column_scales", "shape": [2, 3], "values": [1] * 6, "scales": [1.0, 2.0, 3.0], "scale_axis": 1},
        {"name": "rank3", "shape": [1, 1, 3], "values": [1, 0, -1]},
        {"name": "scalar", "shape": [], "values": [1]},
        {"name": "vector", "shape": [3], "values": [-1, 0, 1], "scales": [0.25]},
        {"name": "matrix2", "shape": [2, 3], "values": [1, 1, 1, -1, -1, -1], "scales": [2.0]},
    ]
    ttpk_layouts = ttpk(layouts)
    ttpk3 = ttpk([{"name": "three", "shape": [3], "values": [0, 0, 0]}])
    corrupted = tmem3[:-1] + bytes([tmem3[-1] ^ 1])
    capabilities = {
        "protocol": "trinity-memory-bridge", "version": 1, "backend": "emulator", "hardware": False,
        "persistence": "process-memory", "authentication": "none-loopback-only",
        "formats": ["TMEM/1", "TensorPack/1"],
        "codecs": ["baseline2", "dense5", "dense17", "dense22", "sparse41", "sparse82"],
        "methods": METHODS, "limits": dict(DEFAULT_LIMITS),
    }
    sdk_identity = {f"{part}_id": identity(part) for part in IDENTITY_PARTS}
    sdk_identity.update({"anchor": 18368, "backend": "emulator", "hardware": False,
                         "identity_kind": "synthetic-public-16-byte", "status": "memory emulator"})
    node_identity = {part: identity(part) for part in IDENTITY_PARTS}
    node_identity.update({"anchor": "0x47C0", "backend": "emulator", "hardware": False,
                          "identity_kind": "synthetic-public-16-byte", "status": "memory emulator"})
    vectors = [
        rpc("capabilities_default_limits", "trinity.capabilities", limits=dict(DEFAULT_LIMITS), result=capabilities,
            description="The capability descriptor names the emulator explicitly and reports the configured limits"),
        rpc("capabilities_with_empty_params", "trinity.capabilities", {}, result={"backend": "emulator", "hardware": False}),
        rpc("capabilities_rejects_unknown_param", "trinity.capabilities", {"extra": 1}, error_code=-32602),
        rpc("capabilities_rejects_array_params", "trinity.capabilities", [], error_code=-32602),
        rpc("trinity_chip_info_is_synthetic", "trinity_chipInfo", request_id="abc", result=sdk_identity,
            description="SDK field names; identities are SHA-256('trinity-memory-emulator-v1:'+part)[0:16]"),
        rpc("chip_info_alias_uses_node_names", "chip_info", result=node_identity),
        rpc("method_not_found", "trinity_proveInference", error_code=-32601),
        raw_rpc("batch_rejected", "[]", error_code=-32600, id=None),
        raw_rpc("empty_object_rejected", "{}", error_code=-32600, id=None),
        rpc("missing_id_rejected", "trinity.capabilities", request_id=None, error_code=-32600, id=None,
            description="A null id is a notification, which the Bridge does not implement"),
        raw_rpc("id_absent_rejected", '{"jsonrpc":"2.0","method":"trinity.capabilities"}', error_code=-32600, id=None),
        rpc("boolean_id_rejected", "trinity.capabilities", request_id=True, error_code=-32600, id=None),
        rpc("id_above_2_53_rejected", "trinity.capabilities", request_id=9007199254740992, error_code=-32600, id=None),
        rpc("id_at_2_53_minus_1_accepted", "trinity.capabilities", request_id=9007199254740991, result={"version": 1}),
        rpc("negative_id_at_bound_accepted", "trinity.capabilities", request_id=-9007199254740991, result={"version": 1}),
        rpc("string_id_128_codepoints_accepted", "trinity.capabilities", request_id="x" * 128, result={"version": 1}),
        rpc("string_id_129_codepoints_rejected", "trinity.capabilities", request_id="x" * 129, error_code=-32600, id=None),
        raw_rpc("unknown_envelope_field_rejected", '{"jsonrpc":"2.0","id":1,"method":"trinity.capabilities","extra":2}',
                error_code=-32600, id=None),
        raw_rpc("jsonrpc_version_1_rejected", '{"jsonrpc":"1.0","id":1,"method":"trinity.capabilities"}',
                error_code=-32600, id=1, description="The envelope is complete, so the id is echoed"),
        raw_rpc("method_not_a_string_rejected", '{"jsonrpc":"2.0","id":1,"method":[]}', error_code=-32600, id=1),
        raw_rpc("duplicate_keys_are_invalid_json", '{"jsonrpc":"2.0","id":1,"id":2,"method":"chip_info"}',
                http_status=400, error_code=-32700, id=None),
        raw_rpc("nan_is_invalid_json", '{"jsonrpc":"2.0","id":1,"method":"x","params":{"n":NaN}}',
                http_status=400, error_code=-32700, id=None),
        raw_rpc("malformed_json", "{broken", http_status=400, error_code=-32700, id=None),
        raw_rpc("nesting_deeper_than_64_is_invalid_json", "[" * 70 + "]" * 70, http_status=400, error_code=-32700, id=None),
        rpc("oversized_request", "memory.upload", {"data": b64(tmem6)}, limits={"max_request_bytes": 100},
            http_status=413, error_code=-32010, id=None,
            description="A body above max_request_bytes is refused before parsing"),
        rpc("read_invalid_handle_format", "memory.read", {"handle": "../../etc/passwd"}, error_code=-32602),
        rpc("read_uppercase_handle_rejected", "memory.read", {"handle": "0123456789ABCDEF0123456789ABCDEF"}, error_code=-32602),
        rpc("read_unknown_handle", "memory.read", {"handle": "0123456789abcdef0123456789abcdef"}, error_code=-32004),
        rpc("read_missing_handle", "memory.read", {}, error_code=-32602),
        rpc("read_unknown_param", "memory.read", {"handle": "0123456789abcdef0123456789abcdef", "extra": 1}, error_code=-32602),
        rpc("upload_missing_params", "memory.upload", error_code=-32602),
        rpc("upload_invalid_base64", "memory.upload", {"data": "!"}, error_code=-32602),
        rpc("upload_noncanonical_base64", "memory.upload", {"data": "AB=="}, error_code=-32602),
        rpc("upload_not_a_container", "memory.upload", {"data": "AAAA"}, error_code=-32602),
        rpc("upload_data_not_a_string", "memory.upload", {"data": True}, error_code=-32602),
        rpc("upload_corrupted_crc", "memory.upload", {"data": b64(corrupted)}, error_code=-32602),
        rpc("upload_tmem_count_over_trit_limit", "memory.upload", {"data": b64(tmem3)}, limits={"max_trits": 2},
            error_code=-32010, description="The TMEM count field is checked against max_trits before decoding"),
        rpc("upload_tensorpack_over_trit_limit", "memory.upload", {"data": b64(ttpk3)}, limits={"max_trits": 2},
            error_code=-32602, description="TensorPack's decoded-trit limit is a container validation error"),
        rpc("upload_over_object_limit", "memory.upload", {"data": b64(tmem20)},
            limits={"max_object_bytes": 26, "max_storage_bytes": 26, "max_objects": 1, "max_trits": 100}, error_code=-32010),
        rpc("upload_over_storage_limit", "memory.upload", {"data": b64(tmem6)},
            limits={"max_storage_bytes": 25, "max_object_bytes": 26, "max_objects": 1, "max_trits": 100}, error_code=-32010),
        transport("origin_header_forbidden", headers={"Content-Type": "application/json", "Origin": "http://example.org"},
                  body='{"jsonrpc":"2.0","id":1,"method":"trinity.capabilities"}', expect={"http_status": 403, "error_code": -32600}),
        transport("foreign_host_forbidden", headers={"Content-Type": "application/json", "Host": "evil.example"},
                  body='{"jsonrpc":"2.0","id":1,"method":"trinity.capabilities"}', expect={"http_status": 403, "error_code": -32600}),
        transport("text_plain_unsupported", headers={"Content-Type": "text/plain"},
                  body='{"jsonrpc":"2.0","id":1,"method":"trinity.capabilities"}', expect={"http_status": 415, "error_code": -32600}),
        transport("get_not_allowed", raw="GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n",
                  expect={"http_status": 405, "error_code": -32600}),
        transport("path_not_root_forbidden",
                  raw="POST /x HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}",
                  expect={"http_status": 403, "error_code": -32600}),
        transport("missing_content_length",
                  raw="POST / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\n\r\n",
                  expect={"http_status": 400, "error_code": -32600}),
        transport("chunked_transfer_rejected",
                  raw="POST / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\nContent-Length: 2\r\n\r\n{}",
                  expect={"http_status": 400, "error_code": -32600}),
        transport("content_length_over_limit", limits={"max_request_bytes": 100},
                  raw="POST / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\nContent-Length: 200\r\n\r\n" + "{" * 200,
                  expect={"http_status": 413, "error_code": -32010}),
        transport("truncated_body_times_out", limits={"timeout": 0.05},
                  raw="POST / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{",
                  expect={"http_status": 400, "error_code": -32700},
                  description="The body never completes; after the inactivity timeout the request is invalid JSON"),
        transport("http_1_0_accepted",
                  raw="POST / HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\nContent-Length: 56\r\n\r\n"
                      '{"jsonrpc":"2.0","id":1,"method":"trinity.capabilities"}',
                  expect={"http_status": 200, "result": {"backend": "emulator"}}),
        {
            "id": "tmem_lifecycle", "kind": "sequence",
            "description": "upload -> read (whole, range, tail, out of range) -> info -> dot -> delete on one TMEM container",
            "steps": [
                step("upload", "memory.upload", {"data": b64(tmem6)}, capture={"handle": "handle"},
                     result={"bytes": len(tmem6), "backend": "emulator"}, result_format={"handle": "uuid4-hex32"}),
                step("read_whole", "memory.read", {"handle": "$handle"},
                     result={"data": b64(tmem6), "offset": 0, "length": len(tmem6), "total_bytes": len(tmem6), "backend": "emulator"}),
                step("read_range", "memory.read", {"handle": "$handle", "offset": 3, "length": 5},
                     result={"data": b64(tmem6[3:8]), "offset": 3, "length": 5, "total_bytes": len(tmem6)}),
                step("read_tail", "memory.read", {"handle": "$handle", "offset": len(tmem6)},
                     result={"data": "", "offset": len(tmem6), "length": 0}),
                step("read_offset_past_end", "memory.read", {"handle": "$handle", "offset": len(tmem6) + 1}, error_code=-32602),
                step("read_length_past_end", "memory.read", {"handle": "$handle", "offset": 1, "length": len(tmem6)}, error_code=-32602),
                step("read_negative_offset", "memory.read", {"handle": "$handle", "offset": -1}, error_code=-32602),
                step("read_boolean_length", "memory.read", {"handle": "$handle", "length": False}, error_code=-32602),
                step("info", "memory.info", {"handle": "$handle"}, result=tmem_info("dense5", six, tmem6)),
                step("dot", "compute.dot", {"handle": "$handle", "tensor_name": "weights", "activations": [-128, 127, -128, 127, -128, 127]},
                     result={"accumulators": [255], "scales": [1.0], "backend": "emulator", "arithmetic": "exact-integer",
                             "tensor_name": "weights", "input_shape": [6], "output_shape": [1], "scale_applied": False}),
                step("dot_unknown_tensor", "compute.dot", {"handle": "$handle", "tensor_name": "other", "activations": [0] * 6}, error_code=-32004),
                step("dot_length_mismatch", "compute.dot", {"handle": "$handle", "tensor_name": "weights", "activations": [0] * 5}, error_code=-32602),
                step("dot_activation_128", "compute.dot", {"handle": "$handle", "tensor_name": "weights", "activations": [0] * 5 + [128]}, error_code=-32602),
                step("dot_activation_minus_129", "compute.dot", {"handle": "$handle", "tensor_name": "weights", "activations": [0] * 5 + [-129]}, error_code=-32602),
                step("dot_activation_float", "compute.dot", {"handle": "$handle", "tensor_name": "weights", "activations": [0] * 5 + [1.0]}, error_code=-32602),
                step("dot_activation_boolean", "compute.dot", {"handle": "$handle", "tensor_name": "weights", "activations": [0] * 5 + [True]}, error_code=-32602),
                step("dot_empty_tensor_name", "compute.dot", {"handle": "$handle", "tensor_name": "", "activations": [0] * 6}, error_code=-32602),
                step("dot_missing_activations", "compute.dot", {"handle": "$handle", "tensor_name": "weights"}, error_code=-32602),
                step("delete", "memory.delete", {"handle": "$handle"}, result={"deleted": True, "backend": "emulator"}),
                step("read_after_delete", "memory.read", {"handle": "$handle"}, error_code=-32004),
                step("delete_after_delete", "memory.delete", {"handle": "$handle"}, error_code=-32004),
            ],
        },
        {
            "id": "tensorpack_row_scales", "kind": "sequence",
            "description": "A 2x3 matrix with per-row scales: exact accumulators, scales returned unapplied",
            "steps": [
                step("upload", "memory.upload", {"data": b64(ttpk_rows)}, capture={"handle": "handle"},
                     result={"bytes": len(ttpk_rows)}, result_format={"handle": "uuid4-hex32"}),
                step("read_whole", "memory.read", {"handle": "$handle"}, result={"data": b64(ttpk_rows), "total_bytes": len(ttpk_rows)}),
                step("info", "memory.info", {"handle": "$handle"}, result=ttpk_info(rows, ttpk_rows)),
                step("dot_rows", "compute.dot", {"handle": "$handle", "tensor_name": "matrix", "activations": [-128, 127, -128]},
                     result={"accumulators": [-255, 127], "scales": [0.5, 2.0], "input_shape": [2, 3], "output_shape": [2],
                             "scale_applied": False, "arithmetic": "exact-integer"}),
                step("dot_unknown_tensor", "compute.dot", {"handle": "$handle", "tensor_name": "weights", "activations": [0, 0, 0]}, error_code=-32004),
                step("delete", "memory.delete", {"handle": "$handle"}, result={"deleted": True}),
            ],
        },
        {
            "id": "tensorpack_dot_layouts", "kind": "sequence",
            "description": "Scales on a contracted axis, rank 3 and scalars are rejected; vectors and scalar-scaled matrices work",
            "steps": [
                step("upload", "memory.upload", {"data": b64(ttpk_layouts)}, capture={"handle": "handle"},
                     result={"bytes": len(ttpk_layouts)}),
                step("column_scales_rejected", "compute.dot", {"handle": "$handle", "tensor_name": "column_scales", "activations": [1, 2, 3]}, error_code=-32602),
                step("rank3_rejected", "compute.dot", {"handle": "$handle", "tensor_name": "rank3", "activations": [1, 2, 3]}, error_code=-32602),
                step("scalar_rejected", "compute.dot", {"handle": "$handle", "tensor_name": "scalar", "activations": [1]}, error_code=-32602),
                step("vector_dot", "compute.dot", {"handle": "$handle", "tensor_name": "vector", "activations": [2, 3, 4]},
                     result={"accumulators": [2], "scales": [0.25], "input_shape": [3], "output_shape": [1]}),
                step("matrix_scalar_scale_repeats_per_row", "compute.dot", {"handle": "$handle", "tensor_name": "matrix2", "activations": [2, 3, 4]},
                     result={"accumulators": [9, -9], "scales": [2.0, 2.0], "output_shape": [2]}),
                step("info_lists_all_tensors", "memory.info", {"handle": "$handle"},
                     result={"format": "TensorPack", "tensor_count": 5, "total_count": 6 + 3 + 1 + 3 + 6}),
            ],
        },
        {
            "id": "empty_tmem_dot", "kind": "sequence",
            "description": "An empty TMEM vector is a valid object; its dot with no activations is [0]",
            "steps": [
                step("upload", "memory.upload", {"data": b64(tmem_empty)}, capture={"handle": "handle"}, result={"bytes": 24}),
                step("dot_empty", "compute.dot", {"handle": "$handle", "tensor_name": "weights", "activations": []},
                     result={"accumulators": [0], "scales": [1.0], "input_shape": [0], "output_shape": [1]}),
                step("info", "memory.info", {"handle": "$handle"}, result={"count": 0, "payload_bpw": None, "container_bpw": None, "payload_bytes": 0}),
            ],
        },
        {
            "id": "storage_limits_and_reclaim", "kind": "sequence",
            "limits": {"max_objects": 1, "max_object_bytes": 26, "max_storage_bytes": 26, "max_trits": 100},
            "description": "Object count and byte limits refuse the second upload; delete reclaims both",
            "steps": [
                step("upload_first", "memory.upload", {"data": b64(tmem10)}, capture={"handle": "handle"}, result={"bytes": 26}),
                step("upload_second_refused", "memory.upload", {"data": b64(tmem10)}, error_code=-32010),
                step("delete", "memory.delete", {"handle": "$handle"}, result={"deleted": True}),
                step("upload_again", "memory.upload", {"data": b64(tmem10)}, capture={"second": "handle"}, result={"bytes": 26}),
                step("first_handle_gone", "memory.read", {"handle": "$handle"}, error_code=-32004),
                step("second_readable", "memory.read", {"handle": "$second"}, result={"data": b64(tmem10)}),
            ],
        },
    ]
    constants = {
        "protocol": {"name": "trinity-memory-bridge", "version": 1, "backend": "emulator", "hardware": False,
                     "persistence": "process-memory", "authentication": "none-loopback-only",
                     "formats": ["TMEM/1", "TensorPack/1"], "methods": METHODS},
        "envelope": {"required": ["jsonrpc", "id", "method"], "optional": ["params"], "jsonrpc": "2.0",
                     "id_integer_range": [-9007199254740991, 9007199254740991], "id_string_max_codepoints": 128,
                     "json_max_depth": 64, "batches": False, "notifications": False},
        "params": {"trinity.capabilities": [], "chip_info": [], "trinity_chipInfo": [], "memory.upload": ["data"],
                   "memory.read": ["handle", "offset?", "length?"], "memory.info": ["handle"], "memory.delete": ["handle"],
                   "compute.dot": ["handle", "tensor_name", "activations"]},
        "errors": {"parse": -32700, "invalid_request": -32600, "method_not_found": -32601, "invalid_params": -32602,
                   "internal": -32603, "not_found": -32004, "limit": -32010, "transport": -32000},
        "http_status": {"ok": 200, "invalid_json_or_framing": 400, "host_origin_or_path": 403, "not_post": 405,
                        "request_too_large": 413, "content_type": 415, "headers_too_large": 431,
                        "rpc_errors_are_200": True},
        "limits_default": dict(DEFAULT_LIMITS, timeout_seconds=5.0, client_max_response_bytes=4194304),
        "handle": {"bytes": 16, "hex_chars": 32, "alphabet": "0-9a-f", "version_char_index": 12, "version_char": "4",
                   "variant_char_index": 16, "variant_chars": "89ab"},
        "dot": {"activation_range": [-128, 127], "ranks": [1, 2], "scale_axis": "none or 0 on a rank-2 tensor",
                "tmem_tensor_name": "weights", "tensor_name_max_codepoints": 1024, "accumulators": "exact integer, never applied to scales",
                "scale_applied": False},
        "identity": {"derivation": "sha256('trinity-memory-emulator-v1:' + part)[0:16] for phi, euler, gamma",
                     **{f"{part}_id": identity(part) for part in IDENTITY_PARTS}, "anchor": 18368, "anchor_hex": "0x47C0",
                     "identity_kind": "synthetic-public-16-byte"},
        "sdk_adapter": {"backend_class": "trinity_memory.bridge.SDKMemoryBackend", "chip_info_requires": {"backend": "emulator", "hardware": False},
                        "unsupported": ["prove_inference", "submit_to_bittensor"],
                        "pins": {"trinity-sdk": "fa8476397ac69438315268342958759e91da9e20", "trinity-node": "4da4d7f7fee7a76c78f91c3b80e97a23b54e344d"}},
    }
    invariants = [
        {"id": "error_codes_are_distinct_and_negative", "condition": "-32700 < -32603 < -32602 < -32601 < -32600 < -32010 < -32004 < -32000 < 0"},
        {"id": "envelope_masks_cover_the_four_fields", "condition": "REQUIRED == jsonrpc|id|method; ALLOWED == REQUIRED|params"},
        {"id": "read_and_dot_masks_are_sums_of_their_fields", "condition": "READ_ALLOWED == handle|offset|length; DOT_REQUIRED == handle|tensor_name|activations"},
        {"id": "request_id_bounds_are_the_double_precision_integer_range", "condition": "|id| <= 2^53 - 1"},
        {"id": "default_storage_holds_exactly_max_objects_of_max_size", "condition": "16 * 524288 == 8388608"},
        {"id": "a_maximal_object_uploads_within_the_default_request_limit", "condition": "4*ceil(524288/3) + 256 <= 2097152"},
        {"id": "default_accumulators_fit_a_signed_32_bit_word", "condition": "1000000 * 128 <= 2^31 - 1"},
        {"id": "handles_serialize_two_hex_chars_per_byte", "condition": "32 == 2 * 16"},
        {"id": "identity_anchor_is_0x47c0", "condition": "18368 == 0x47C0"},
    ]
    return {
        "module": "TrinityMemoryBridgeSpec",
        "spec_path": "specs/memory/bridge.t27",
        "schema_version": 2,
        "format_family": "Conformance",
        "vector_name": "Trinity Bridge protocol",
        "description": "JSON-RPC envelope, error codes and HTTP statuses, limits, handles, upload/read/info/delete/dot "
                       "semantics and the synthetic identity of the emulator. Requests and expected responses are computed "
                       "from the spec by tools/generate-spec-vectors.py; the TMEM/TensorPack containers use the pure-Python encoders.",
        "created_at": "2026-09-11T00:00:00Z",
        "generator": "tools/generate-spec-vectors.py",
        "replay": {"tcp": "tests/test_spec_bridge.py", "native": "tests/native/test_spec_bridge_vectors.py",
                   "kinds": {"rpc": "one request body, default or per-vector limits",
                             "transport": "raw HTTP framing (TCP replay only)",
                             "sequence": "ordered steps on one server; $name substitutes a captured result field"}},
        "constants": constants,
        "invariants": invariants,
        "vectors": vectors,
    }


# ---------------------------------------------------------------------------
# specs/memory/tensorpack.t27


def frame(metadata, payload=b"", tensor_count=None):
    """Independent TTPK framing from arbitrary metadata bytes (for rejection cases)."""
    if isinstance(metadata, dict):
        if tensor_count is None:
            tensor_count = len(metadata["tensors"])
        metadata = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    prefix = struct.pack("<4sBBHIIQ", b"TTPK", 1, 0, 0, len(metadata), tensor_count or 0, len(payload))
    return prefix + struct.pack("<II", zlib.crc32(prefix + metadata), zlib.crc32(payload)) + metadata + payload


def reseal_inner(blob):
    """Recompute the nested TMEM checksum over deliberately malformed content."""
    return blob[:20] + struct.pack("<I", zlib.crc32(blob[:20] + blob[24:])) + blob[24:]


def tensor(name, shape, values, codec="dense5", scales=(1.0,), scale_axis=None, axes=()):
    return {"name": name, "shape": list(shape), "values": list(values), "codec": codec,
            "scales": [float(x) for x in scales], "scale_axis": scale_axis, "axes": list(axes)}


def descriptor_entry(name, values, codec="dense5", **fields):
    blob = tmem(codec, values)
    entry = {"name": name, "shape": [len(values)], "codec": codec, "scales": [1.0],
             "scale_axis": None, "axes": [], "offset": 0, "length": len(blob)}
    entry.update(fields)
    return entry, blob


def tensorpack_inspect(tensors, container):
    document = ttpk_info(tensors, container)
    return {key: document[key] for key in ("format", "version", "order", "tensor_count", "total_count",
                                           "header_bytes", "metadata_bytes", "payload_bytes", "container_bytes",
                                           "validated", "tensors")}


def build_tensorpack():
    import math
    pattern = [-1 if i % 8 == 0 else 0 for i in range(23)]
    packs = [("empty_pack", [], "An empty pack: 32-byte header and 26 bytes of metadata, no payload")]
    for name in CODECS:
        packs.append((f"{name}_23_trits", [tensor(name, [23], pattern, name)],
                      f"One {name} tensor with a partial final group; nested TMEM keeps its canonical padding"))
    packs += [
        ("scalar_baseline2_quarter_scale", [tensor("scalar", [], [-1], "baseline2", [0.25])],
         "A scalar has shape [] and exactly one trit"),
        ("matrix_row_scales_dense22_with_axes",
         [tensor("matrix.α", [2, 3], [-1, 0, 1, 1, 0, -1], "dense22", [0.1, 0.75], 0, ["output", "input"])],
         "Per-row scales on axis 0, labelled axes, a non-ASCII name written as raw UTF-8"),
        ("cube_sparse41_next_after_one",
         [tensor("cube", [1, 2, 2], [0, 0, 0, -1], "sparse41", [math.nextafter(1.0, 2.0)], None, ["batch", "row", "column"])],
         "Rank 3, sparse41 block across the flattened values, a scale printed with 17 significant digits"),
        ("three_tensors_in_one_pack",
         [tensor("matrix.α", [2, 3], [-1, 0, 1, 1, 0, -1], "dense22", [0.1, 0.75], 0, ["output", "input"]),
          tensor("cube", [1, 2, 2], [0, 0, 0, -1], "sparse41", [math.nextafter(1.0, 2.0)], None, ["batch", "row", "column"]),
          tensor("scalar", [], [1])],
         "Descriptors and payload stay in input order; offsets chain without gaps"),
        ("float_presentation_of_scales",
         [tensor("scaled", [4], [1, 0, -1, 0], "dense5", [0.0001, 0.00001, 1e16, 123456789.0], 0)],
         "Fixed notation for exponents -4..15, scientific with a two-digit exponent otherwise"),
        ("example_tensorpack_json", [tensor("weights", [7], [1, -1, 0, 1, 0, -1, 1], "dense5", [1.0], None, ["sample"])],
         "The repository example input examples/tensorpack.json"),
    ]
    vectors = []
    for identifier, tensors, description in packs:
        container = ttpk(tensors)
        metadata = container[32:32 + int.from_bytes(container[8:12], "little")].decode("utf-8")
        vectors.append({"id": identifier, "kind": "pack", "tensors": tensors, "ttpk_hex": container.hex(),
                        "metadata_json": metadata, "inspect": tensorpack_inspect(tensors, container),
                        "description": description})
    # Rejections: every container below must be refused by decode and inspect.
    entry, blob = descriptor_entry("weights", [1, 0, -1])
    valid = {"order": "C", "tensors": [entry]}
    good = frame(valid, blob)
    second, blob2 = descriptor_entry("other", [0, 1, 1])
    second["offset"] = len(blob)
    invalid = []

    def reject(identifier, data, reason, error_class=None):
        item = {"id": identifier, "kind": "invalid_pack", "ttpk_hex": data.hex(), "reason": reason}
        if error_class:
            item["error_class"] = error_class
        invalid.append(item)

    reject("wrong_magic", b"TTPX" + good[4:], "magic must be TTPK", "header")
    reject("version_2", good[:4] + b"\x02" + good[5:], "only version 1 exists", "header")
    reject("flags_set", good[:5] + b"\x01" + good[6:], "flags must be zero", "header")
    reject("reserved_set", good[:6] + b"\x01\x00" + good[8:], "reserved bytes must be zero", "header")
    reject("trailing_byte", good + b"\x00", "size must equal 32 + metadata + payload", "length")
    reject("truncated_payload", good[:-1], "size must equal 32 + metadata + payload", "length")
    reject("truncated_header", good[:31], "a container is at least 32 bytes", "length")
    reject("metadata_crc_mismatch", good[:24] + bytes([good[24] ^ 1]) + good[25:], "CRC32 over header[0:24] + metadata", "checksum")
    reject("payload_crc_mismatch", good[:28] + bytes([good[28] ^ 1]) + good[29:], "CRC32 over the payload", "checksum")
    reject("metadata_limit_in_header", struct.pack("<4sBBHIIQII", b"TTPK", 1, 0, 0, 1048577, 0, 0, 0, 0), "metadata length above 1 MiB", "limit")
    reject("tensor_count_limit_in_header", struct.pack("<4sBBHIIQII", b"TTPK", 1, 0, 0, 0, 1025, 0, 0, 0), "more than 1024 tensors", "limit")
    reject("payload_limit_in_header", struct.pack("<4sBBHIIQII", b"TTPK", 1, 0, 0, 0, 0, 67108865, 0, 0), "payload above 64 MiB", "limit")
    reject("utf8_bom", frame(b"\xef\xbb\xbf" + json.dumps(valid, separators=(",", ":")).encode(), blob, 1), "no byte order mark", "text")
    reject("invalid_utf8_metadata", frame(b"\xff", blob, 1), "metadata must be UTF-8", "text")
    reject("json_null", frame(b"null", blob, 1), "root must be an object", "schema")
    reject("json_array_root", frame(b"[]", blob, 1), "root must be an object", "schema")
    reject("json_empty_object", frame(b"{}", blob, 1), "root needs order and tensors", "schema")
    reject("json_trailing_comma", frame(b'{"order":"C","tensors":[],}', blob, 1), "strict JSON grammar", "json")
    reject("json_duplicate_root_key", frame(b'{"order":"C","order":"C","tensors":[]}', blob, 1), "duplicate keys are rejected", "json")
    canonical = json.dumps(valid, separators=(",", ":")).encode()
    reject("json_duplicate_descriptor_key", frame(canonical.replace(b'"name":"weights"', b'"name":"weights","name":"other"'), blob, 1), "duplicate keys are rejected", "json")
    reject("json_nan_scale", frame(canonical.replace(b"[1.0]", b"[NaN]"), blob, 1), "nonfinite numbers are invalid JSON", "json")
    reject("json_overflowing_real", frame(canonical.replace(b"[1.0]", b"[1e999]"), blob, 1), "nonfinite numbers are invalid JSON", "json")
    reject("json_integer_token_too_long", frame(canonical.replace(b'"offset":0', b'"offset":' + b"1" * 21), blob, 1), "integer tokens have at most 20 characters", "json")
    reject("json_nesting_too_deep", frame(b'{"order":"C","tensors":[' + b"[" * 8 + b"]" * 8 + b"]}", blob, 1), "nesting deeper than 8 levels", "json")
    reject("json_trailing_garbage", frame(canonical + b" extra", blob, 1), "one JSON document only", "json")
    reject("order_f", frame({"order": "F", "tensors": [entry]}, blob), "only C order", "schema")
    reject("root_extra_member", frame({"order": "C", "tensors": [entry], "future": 1}, blob), "exactly two root members", "schema")
    reject("tensors_not_an_array", frame({"order": "C", "tensors": {}}, blob, 1), "tensors must be an array", "schema")
    reject("descriptor_null", frame({"order": "C", "tensors": [None]}, blob), "descriptors are objects", "schema")
    reject("descriptor_missing_name", frame({"order": "C", "tensors": [{k: v for k, v in entry.items() if k != "name"}]}, blob), "exactly eight descriptor members", "schema")
    reject("descriptor_missing_length", frame({"order": "C", "tensors": [{k: v for k, v in entry.items() if k != "length"}]}, blob), "exactly eight descriptor members", "schema")
    reject("descriptor_extra_member", frame({"order": "C", "tensors": [{**entry, "future": 1}]}, blob), "exactly eight descriptor members", "schema")
    reject("tensor_count_header_mismatch", frame(valid, blob, 2), "header count must equal the array length", "schema")
    reject("shape_zero_dimension", frame({"order": "C", "tensors": [{**entry, "shape": [0]}]}, blob), "dimensions are at least 1", "shape")
    reject("shape_negative_dimension", frame({"order": "C", "tensors": [{**entry, "shape": [-3]}]}, blob), "dimensions are unsigned integers", "shape")
    reject("shape_real_dimension", frame({"order": "C", "tensors": [{**entry, "shape": [3.0]}]}, blob), "dimensions are integers", "shape")
    reject("shape_count_mismatch", frame({"order": "C", "tensors": [{**entry, "shape": [4]}]}, blob), "product of the shape must equal the nested count", "descriptor")
    reject("shape_rank_17", frame({"order": "C", "tensors": [{**entry, "shape": [1] * 16 + [3]}]}, blob), "rank at most 16", "shape")
    reject("shape_dimension_over_limit", frame({"order": "C", "tensors": [{**entry, "shape": [2147483648]}]}, blob), "dimension at most 2^31 - 1", "shape")
    reject("shape_product_over_limit", frame({"order": "C", "tensors": [{**entry, "shape": [2048, 2049]}]}, blob), "at most 4194304 trits", "shape")
    reject("axes_count_mismatch", frame({"order": "C", "tensors": [{**entry, "axes": ["x", "y"]}]}, blob), "axes are empty or one label per dimension", "axes")
    reject("axes_duplicate_labels", frame({"order": "C", "tensors": [{**entry, "shape": [3, 1], "axes": ["x", "x"]}]}, blob), "axis labels are unique", "axes")
    reject("axes_empty_label", frame({"order": "C", "tensors": [{**entry, "axes": [""]}]}, blob), "labels are nonempty", "axes")
    reject("axes_label_65_bytes", frame({"order": "C", "tensors": [{**entry, "axes": ["x" * 65]}]}, blob), "labels are at most 64 bytes", "axes")
    reject("name_empty", frame({"order": "C", "tensors": [{**entry, "name": ""}]}, blob), "names are nonempty", "text")
    reject("name_257_bytes", frame({"order": "C", "tensors": [{**entry, "name": "n" * 257}]}, blob), "names are at most 256 bytes", "text")
    reject("name_control_character", frame({"order": "C", "tensors": [{**entry, "name": "a\nb"}]}, blob), "no ASCII control characters", "text")
    reject("scales_integer_token", frame(canonical.replace(b"[1.0]", b"[1]"), blob, 1), "scales are JSON reals", "scale")
    reject("scales_empty", frame({"order": "C", "tensors": [{**entry, "scales": []}]}, blob), "exactly one scale without an axis", "scale")
    reject("scales_zero", frame({"order": "C", "tensors": [{**entry, "scales": [0.0]}]}, blob), "scales are positive", "scale")
    reject("scales_negative", frame({"order": "C", "tensors": [{**entry, "scales": [-1.0]}]}, blob), "scales are positive", "scale")
    reject("scales_two_without_axis", frame({"order": "C", "tensors": [{**entry, "scales": [1.0, 2.0]}]}, blob), "exactly one scale without an axis", "scale")
    reject("scale_axis_out_of_rank", frame({"order": "C", "tensors": [{**entry, "scale_axis": 1}]}, blob), "scale_axis is below the rank", "scale")
    reject("scale_axis_negative", frame({"order": "C", "tensors": [{**entry, "scale_axis": -1}]}, blob), "scale_axis is null or unsigned", "scale")
    reject("scale_axis_16", frame({"order": "C", "tensors": [{**entry, "scale_axis": 16}]}, blob), "scale_axis is at most 15", "scale")
    reject("scale_axis_real", frame({"order": "C", "tensors": [{**entry, "scale_axis": 0.0}]}, blob), "scale_axis is an integer", "scale")
    reject("codec_unknown", frame({"order": "C", "tensors": [{**entry, "codec": "unknown"}]}, blob), "codec is one of the six names", "codec")
    reject("codec_mismatch_with_nested", frame({"order": "C", "tensors": [{**entry, "codec": "baseline2"}]}, blob), "nested codec byte must match", "descriptor")
    reject("offset_not_zero", frame({"order": "C", "tensors": [{**entry, "offset": 1}]}, blob), "first offset is zero", "offset")
    reject("offset_negative", frame({"order": "C", "tensors": [{**entry, "offset": -1}]}, blob), "offsets are unsigned", "schema")
    reject("length_short", frame({"order": "C", "tensors": [{**entry, "length": len(blob) - 1}]}, blob), "length is the whole nested file", "offset")
    reject("length_long", frame({"order": "C", "tensors": [{**entry, "length": len(blob) + 1}]}, blob), "length is the whole nested file", "offset")
    reject("duplicate_names", frame({"order": "C", "tensors": [entry, {**second, "name": "weights"}]}, blob + blob2), "names are unique", "descriptor")
    reject("gap_between_tensors", frame({"order": "C", "tensors": [entry, {**second, "offset": len(blob) + 1}]}, blob + blob2), "no gaps", "offset")
    reject("overlapping_tensors", frame({"order": "C", "tensors": [entry, {**second, "offset": 0}]}, blob + blob2), "no overlaps", "offset")
    reject("reordered_descriptors", frame({"order": "C", "tensors": [second, entry]}, blob + blob2), "descriptors follow payload order", "offset")
    reject("payload_tail", frame(valid, blob + b"\x00"), "no unused payload bytes", "offset")
    reject("payload_without_descriptors", frame({"order": "C", "tensors": []}, blob), "payload must be covered", "offset")
    reject("nested_crc_mismatch", frame(valid, blob[:20] + b"\x00\x00\x00\x00" + blob[24:]), "nested TMEM checksum", "checksum")
    reject("nested_count_mismatch", frame(valid, reseal_inner(blob[:8] + struct.pack("<Q", 1) + blob[16:])), "nested count must match the shape", "descriptor")
    reject("nested_flags_set", frame(valid, reseal_inner(blob[:6] + b"\x01" + blob[7:])), "nested reserved bytes must be zero", "header")
    reject("nested_version_2", frame(valid, reseal_inner(blob[:4] + b"\x02" + blob[5:])), "nested version must be 1", "header")
    padded_entry, padded_blob = descriptor_entry("weights", [0])
    bad_padding = reseal_inner(padded_blob[:24] + encode("dense5", [0, 0, 0, 0, 1]))
    reject("nested_nonzero_padding", frame({"order": "C", "tensors": [padded_entry]}, bad_padding), "padding trits must be zero", "padding")
    reserved = reseal_inner(padded_blob[:24] + bytes([243]))
    reject("nested_reserved_code", frame({"order": "C", "tensors": [padded_entry]}, reserved), "reserved dense5 code 243", "code")
    entry17, blob17 = descriptor_entry("seventeen", [0], "dense17")
    high_bits = reseal_inner(blob17[:-1] + bytes([blob17[-1] | 0x80]))
    reject("nested_unused_high_bits", frame({"order": "C", "tensors": [entry17]}, high_bits), "unused high bits must be zero", "padding")
    sparse_entry, sparse_blob = descriptor_entry("sparse", [0, 0, 0, 0], "sparse41")
    bad_sparse = reseal_inner(sparse_blob[:24] + bytes([9]))
    reject("nested_sparse41_reserved_code", frame({"order": "C", "tensors": [sparse_entry]}, bad_sparse), "reserved sparse41 code 9", "code")
    constants = {
        "header": {"magic": "TTPK", "version": 1, "flags": 0, "reserved": 0, "header_bytes": 32,
                   "metadata_length_offset": 8, "tensor_count_offset": 12, "payload_length_offset": 16,
                   "metadata_crc_offset": 24, "payload_crc_offset": 28, "metadata_crc_covers": "header[0:24] + metadata",
                   "payload_crc_covers": "payload", "crc32": "IEEE, as zlib.crc32", "endianness": "little"},
        "limits": {"tensors": 1024, "metadata_bytes": 1048576, "payload_bytes": 67108864, "total_trits": 4194304,
                   "rank": 16, "dimension": 2147483647, "name_bytes": 256, "axis_bytes": 64, "json_depth": 8,
                   "json_integer_chars": 20, "scale_axis": 15},
        "schema": {"root": ["order", "tensors"], "order": "C",
                   "descriptor": ["axes", "codec", "length", "name", "offset", "scale_axis", "scales", "shape"],
                   "writer": "sorted keys, no insignificant whitespace, raw UTF-8, only quote and backslash escaped",
                   "codecs": list(CODECS), "scales": "JSON reals only; integer tokens are rejected",
                   "scale_axis": "null or an unsigned index below the rank"},
        "nested": {"format": "TMEM v1", "header_bytes": HEADER_BYTES, "length": "24 + payload bytes of the codec",
                   "checks": ["magic", "version", "flags", "codec byte equals descriptor codec", "count equals shape product",
                              "payload length", "CRC32", "valid codes", "zero padding", "zero unused high bits"]},
        "float_presentation": {"fixed_exponent_range": [-4, 15], "scientific_exponent_min_digits": 2,
                               "examples": {"0.0001": "0.0001", "0.00001": "1e-05", "1e16": "1e+16", "123456789.0": "123456789.0"}},
        "errors": {"descriptor": -20, "text": -21, "shape": -22, "scale": -23, "axes": -24, "offset": -25, "schema": -26, "json": -27},
        "empty_pack": {"bytes": 58, "metadata": '{"order":"C","tensors":[]}'},
    }
    invariants = [
        {"id": "header_layout_is_contiguous", "condition": "4+1+1+2+4+4+8+4+4 == 32"},
        {"id": "metadata_crc_covers_the_header_prefix", "condition": "covered bytes == 24 == metadata_crc_offset"},
        {"id": "empty_pack_is_header_plus_empty_metadata", "condition": "58 == 32 + 26"},
        {"id": "limits_are_the_documented_powers_of_two", "condition": "1 MiB metadata, 64 MiB payload, 4 Mi trits, 2^31-1 dimension"},
        {"id": "scale_axis_indexes_a_dimension", "condition": "max scale_axis + 1 == max rank"},
        {"id": "a_maximal_pack_of_any_codec_fits_the_payload_limit", "condition": "4194304 trits at 2 bits or 8/5 bits + 24 <= 64 MiB"},
        {"id": "schema_member_counts_are_fixed", "condition": "2 root members, 8 descriptor members, 6 codec names"},
        {"id": "float_notation_thresholds_match_python", "condition": "fixed for -4 <= exponent <= 15"},
        {"id": "tensorpack_status_codes_are_distinct_and_below_the_shared_codes", "condition": "-27 < ... < -20 < -10"},
    ]
    return {
        "module": "TrinityMemoryTensorPackSpec",
        "spec_path": "specs/memory/tensorpack.t27",
        "schema_version": 2,
        "format_family": "Conformance",
        "vector_name": "TensorPack v1 containers",
        "description": "Golden TTPK containers per codec, scalar, per-axis scales, axis labels, float presentation and an "
                       "empty pack, plus rejected containers for framing, JSON, schema, descriptor, chain and nested-TMEM rules. "
                       "Bytes come from the pure-Python encoders in tools/generate-spec-vectors.py.",
        "created_at": "2026-09-11T00:00:00Z",
        "generator": "tools/generate-spec-vectors.py",
        "constants": constants,
        "invariants": invariants,
        "vectors": vectors + invalid,
    }


# ---------------------------------------------------------------------------
# specs/memory/stream_compute.t27 -- cycle models restated from the contract


def dense_code(trits):
    return sum((t + 1) * 3 ** i for i, t in enumerate(list(trits) + [0] * (5 - len(trits))))


def baseline_code(trits):
    return sum(LANE[t] << (2 * i) for i, t in enumerate(list(trits) + [0] * (5 - len(trits))))


def dot_decode(code, dense):
    if dense:
        if code >= 243:
            return 0
        lanes = 0
        for i in range(5):
            digit = (code // 3 ** i) % 3
            lanes |= (2 if digit == 0 else 0 if digit == 1 else 1) << (2 * i)
        return 1024 | lanes
    if code >= 1024 or any(((code >> (2 * i)) & 3) == 3 for i in range(5)):
        return 0
    return 1024 | code


def dot_lane_trit(lanes, lane):
    code = (lanes >> (2 * lane)) & 3
    return 1 if code == 1 else -1 if code == 2 else 0


def dot_activation(acts, lane):
    byte = (acts >> (8 * lane)) & 255
    return byte - 256 if byte >= 128 else byte


def dot_group_sum(lanes, acts, mask):
    return sum(dot_lane_trit(lanes, lane) * dot_activation(acts, lane) for lane in range(5) if (mask >> lane) & 1)


def dot_mask_valid(mask, last):
    return mask in (0, 1, 3, 7, 15, 31) if last else mask == 31


def dot_group_valid(lanes, acts, mask, last):
    if not lanes & 1024 or not dot_mask_valid(mask, last):
        return False
    return all((mask >> lane) & 1 or (((lanes >> (2 * lane)) & 3) == 0 and ((acts >> (8 * lane)) & 255) == 0)
               for lane in range(5))


def pack_acts(values):
    return sum((value & 255) << (8 * lane) for lane, value in enumerate(values))


class DotModel:
    """The framed dot pipeline exactly as specs/memory/stream_compute.t27 states it."""

    def __init__(self, dense, acc_width):
        self.dense = dense
        self.minimum = -(1 << (acc_width - 1))
        self.maximum = (1 << (acc_width - 1)) - 1
        self.stage_valid = self.stage_error = self.stage_last = self.stage_empty = False
        self.stage_sum = self.accumulator = self.out_result = 0
        self.frame_error = self.frame_active = self.out_valid = self.out_error = False

    def comb(self, s):
        acts = pack_acts(s["in_activations"])
        decoded = dot_decode(s["in_code"], self.dense)
        accumulate_ready = (not self.out_valid) or s["out_ready"]
        next_total = self.accumulator + self.stage_sum
        return {
            "accumulate_ready": accumulate_ready,
            "in_ready": (not s["reset"]) and ((not self.stage_valid) or accumulate_ready),
            "group_sum": dot_group_sum(decoded, acts, s["in_mask"]),
            "group_ok": dot_group_valid(decoded, acts, s["in_mask"], s["in_last"]),
            "next_total": next_total,
            "next_error": self.frame_error or self.stage_error or next_total < self.minimum
                          or next_total > self.maximum or (self.stage_empty and self.frame_active),
        }

    def clock(self, s):
        c = self.comb(s)
        accepted = bool(s["in_valid"] and c["in_ready"])
        if s["reset"]:
            self.__init__(self.dense, 32)
            self.minimum, self.maximum = self.minimum, self.maximum
        else:
            if self.out_valid and s["out_ready"]:
                self.out_valid = False
            if self.stage_valid and c["accumulate_ready"]:
                self.stage_valid = False
                if self.stage_last:
                    self.out_valid = True
                    self.out_error = c["next_error"]
                    self.out_result = 0 if c["next_error"] else c["next_total"]
                    self.accumulator = 0
                    self.frame_error = self.frame_active = False
                else:
                    self.accumulator = 0 if c["next_error"] else c["next_total"]
                    self.frame_error = c["next_error"]
                    self.frame_active = True
            if accepted:
                self.stage_valid = True
                self.stage_sum = c["group_sum"]
                self.stage_error = not c["group_ok"]
                self.stage_last = bool(s["in_last"])
                self.stage_empty = s["in_mask"] == 0
        post = self.comb(s)
        return accepted, {"in_ready": post["in_ready"], "out_valid": self.out_valid,
                          "out_result": self.out_result, "out_error": self.out_error}


class DotScenario:
    def __init__(self, identifier, description, dense=True, acc_width=32):
        self.identifier, self.description, self.dense, self.acc_width = identifier, description, dense, acc_width
        self.model = DotModel(dense, acc_width)
        # A fresh model keeps the configured range across the reset shortcut above.
        self.model.minimum, self.model.maximum = -(1 << (acc_width - 1)), (1 << (acc_width - 1)) - 1
        self.cycles = []

    def cycle(self, reset=False, in_valid=False, code=0, acts=(0, 0, 0, 0, 0), mask=0, last=False, out_ready=True):
        stimulus = {"reset": reset, "in_valid": in_valid, "in_code": code, "in_activations": list(acts),
                    "in_mask": mask, "in_last": last, "out_ready": out_ready}
        minimum, maximum = self.model.minimum, self.model.maximum
        accepted, expect = self.model.clock(stimulus)
        self.model.minimum, self.model.maximum = minimum, maximum
        self.cycles.append({"stimulus": stimulus, "expect": expect})
        return accepted

    def reset(self, cycles=2):
        for _ in range(cycles):
            self.cycle(reset=True, out_ready=False)

    def beat(self, trits, acts, mask=31, last=False, out_ready=True, limit=16):
        code = dense_code(trits) if self.dense else baseline_code(trits)
        return self.beat_code(code, acts, mask, last, out_ready, limit)

    def beat_code(self, code, acts, mask=31, last=False, out_ready=True, limit=16):
        for _ in range(limit):
            if self.cycle(in_valid=True, code=code, acts=acts, mask=mask, last=last, out_ready=out_ready):
                return
        raise RuntimeError(f"{self.identifier}: beat never accepted")

    def idle(self, cycles=1, out_ready=True):
        for _ in range(cycles):
            self.cycle(out_ready=out_ready)

    def document(self):
        results = [step["expect"]["out_result"] for step in self.cycles if step["expect"]["out_valid"]]
        return {"id": self.identifier, "kind": "dot_trace", "dense": self.dense, "acc_width": self.acc_width,
                "description": self.description, "cycle_count": len(self.cycles), "cycles": self.cycles}


class StorageModel:
    """The storage sequencer and view exactly as the contract states them."""

    def __init__(self, dense, trit_count):
        self.dense, self.words = dense, (trit_count + 4) // 5
        self.tail_mask = (1 << (trit_count - 5 * (self.words - 1))) - 1
        self.memory = {}
        self.read_code, self.read_addr = 0, 0
        self.active = self.valid_q = self.last_q = False

    def busy(self):
        return self.active or self.valid_q

    def load_ready(self, s):
        return (not s["rst"]) and (not self.busy()) and (not s["start"])

    def clock(self, s):
        if s["load_en"] and self.load_ready(s) and s["load_addr"] < self.words:
            self.memory[s["load_addr"]] = s["load_code"]
        if not s["rst"] and self.active:
            if self.read_addr not in self.memory:
                raise RuntimeError("scenario reads an unloaded word")
            self.read_code = self.memory[self.read_addr]
        was_active, was_busy, addr = self.active, self.busy(), self.read_addr
        if s["rst"]:
            self.read_addr, self.active, self.valid_q, self.last_q = 0, False, False, False
        else:
            self.valid_q = self.last_q = False
            if s["start"] and not was_busy:
                self.read_addr, self.active = 0, True
            elif was_active:
                self.valid_q = True
                if addr == self.words - 1:
                    self.active, self.last_q = False, True
                else:
                    self.read_addr = addr + 1
        out_valid = self.valid_q
        out_last = self.valid_q and self.last_q
        code = self.read_code if self.valid_q else 0
        packed = 0
        if out_valid:
            mask = self.tail_mask if out_last else 31
            decoded = dot_decode(code, self.dense)
            if decoded & 1024:
                visible = sum(decoded & (3 << (2 * lane)) for lane in range(5) if (mask >> lane) & 1)
                packed = 32768 | (mask << 10) | visible
            else:
                packed = mask << 10
        return {"busy": self.busy(), "load_ready": self.load_ready(s), "out_valid": out_valid, "out_last": out_last,
                "out_code_valid": bool(packed >> 15), "out_lane_mask": (packed >> 10) & 31, "out_trits": packed & 1023}


class StorageScenario:
    def __init__(self, identifier, description, dense=True, trit_count=12):
        self.identifier, self.description, self.dense, self.trit_count = identifier, description, dense, trit_count
        self.model = StorageModel(dense, trit_count)
        self.cycles = []

    def cycle(self, rst=False, start=False, load_en=False, load_addr=0, load_code=0):
        stimulus = {"rst": rst, "start": start, "load_en": load_en, "load_addr": load_addr, "load_code": load_code}
        self.cycles.append({"stimulus": stimulus, "expect": self.model.clock(stimulus)})

    def code(self, trits):
        return dense_code(trits) if self.dense else baseline_code(trits)

    def load(self, address, code):
        self.cycle(load_en=True, load_addr=address, load_code=code)

    def document(self):
        return {"id": self.identifier, "kind": "storage_trace", "dense": self.dense, "trit_count": self.trit_count,
                "words": self.model.words, "tail_mask": self.model.tail_mask, "description": self.description,
                "cycle_count": len(self.cycles), "cycles": self.cycles}


def frame_expectation(weights, activations, acc_width):
    """Exact per-group checked accumulation: sticky error, zero result."""
    minimum, maximum = -(1 << (acc_width - 1)), (1 << (acc_width - 1)) - 1
    total, error = 0, False
    groups = max(1, -(-len(weights) // 5))
    for group in range(groups):
        total += sum(w * a for w, a in zip(weights[5 * group:5 * group + 5], activations[5 * group:5 * group + 5]))
        if total < minimum or total > maximum:
            error = True
    return {"result": 0 if error else total, "error": error}


def build_stream_compute():
    import random
    traces = []

    def dot(identifier, description, dense=True, acc_width=32):
        scenario = DotScenario(identifier, description, dense, acc_width)
        scenario.reset()
        traces.append(scenario)
        return scenario

    s = dot("two_beat_frame_dense", "A full beat then a two-lane final beat; the result is visible one edge after the last beat is accepted")
    s.beat([1, -1, 0, 1, 0], [-128, 127, 5, 3, 9])
    s.beat([1, 1, 0, 0, 0], [10, -10, 0, 0, 0], mask=3, last=True)
    s.idle(4)
    s = dot("two_beat_frame_baseline", "The same frame in five-lane two-bit mode", dense=False)
    s.beat([1, -1, 0, 1, 0], [-128, 127, 5, 3, 9])
    s.beat([1, 1, 0, 0, 0], [10, -10, 0, 0, 0], mask=3, last=True)
    s.idle(4)
    s = dot("empty_frame", "Mask 00000 on a single last beat is an empty frame with result zero")
    s.beat([0, 0, 0, 0, 0], [0, 0, 0, 0, 0], mask=0, last=True)
    s.idle(4)
    s = dot("reserved_code_then_recovery", "A reserved dense code produces an error result; the next frame is unaffected")
    s.beat_code(243, [0, 0, 0, 0, 0], mask=31, last=True)
    s.idle(2)
    s.beat([1, 0, 0, 0, 0], [100, 0, 0, 0, 0], mask=1, last=True)
    s.idle(4)
    s = dot("mask_hole_rejected", "A final mask with a hole (01011) is malformed")
    s.beat([0, 0, 0, 0, 0], [0, 0, 0, 0, 0], mask=11, last=True)
    s.idle(4)
    s = dot("inactive_lane_activation_rejected", "An activation in a masked-out lane is malformed")
    s.beat([0, 0, 0, 0, 0], [0, 5, 0, 0, 0], mask=1, last=True)
    s.idle(4)
    s = dot("inactive_lane_weight_rejected", "A nonzero trit in a masked-out lane is malformed")
    s.beat([0, 1, 0, 0, 0], [0, 0, 0, 0, 0], mask=1, last=True)
    s.idle(4)
    s = dot("partial_mask_before_last_rejected", "Every beat before the last must carry mask 11111")
    s.beat([0, 0, 0, 0, 0], [0, 0, 0, 0, 0], mask=3, last=False)
    s.beat([0, 0, 0, 0, 0], [0, 0, 0, 0, 0], mask=31, last=True)
    s.idle(4)
    s = dot("empty_beat_inside_frame_rejected", "Mask 00000 cannot terminate a nonempty frame")
    s.beat([1, 0, 0, 0, 0], [1, 0, 0, 0, 0], mask=31, last=False)
    s.beat([0, 0, 0, 0, 0], [0, 0, 0, 0, 0], mask=0, last=True)
    s.idle(4)
    s = dot("reset_mid_frame", "Reset after an accepted beat drops the partial frame; in_ready is low during reset")
    s.beat([1, 1, 1, 1, 1], [1, 1, 1, 1, 1], mask=31, last=False)
    s.cycle(reset=True, out_ready=False)
    s.beat([1, 0, 0, 0, 0], [7, 0, 0, 0, 0], mask=1, last=True)
    s.idle(4)
    s = dot("reset_with_pending_output", "A result held under backpressure is discarded by reset")
    s.beat([1, 0, 0, 0, 0], [9, 0, 0, 0, 0], mask=1, last=True, out_ready=False)
    s.idle(2, out_ready=False)
    s.cycle(reset=True, out_ready=False)
    s.idle(2)
    s = dot("backpressure_holds_result_and_stalls_input", "out_ready low holds the result; a buffered beat stalls in_ready until the result is consumed")
    s.beat([1, 0, 0, 0, 0], [3, 0, 0, 0, 0], mask=1, last=True, out_ready=False)
    s.idle(1, out_ready=False)
    s.beat([1, 1, 0, 0, 0], [2, 2, 0, 0, 0], mask=31, last=False, out_ready=False)
    for _ in range(3):
        s.cycle(in_valid=True, code=dense_code([1, 0, 0, 0, 0]), acts=[5, 0, 0, 0, 0], mask=1, last=True, out_ready=False)
    s.beat([1, 0, 0, 0, 0], [5, 0, 0, 0, 0], mask=1, last=True, out_ready=True)
    s.idle(5)
    s = dot("input_bubbles", "Gaps between beats do not change the result")
    s.beat([1, 0, -1, 0, 0], [10, 20, 30, 40, 50], mask=31, last=False)
    s.idle(2)
    s.beat([0, 1, 0, 0, 0], [0, 4, 0, 0, 0], mask=31, last=False)
    s.idle(3)
    s.beat([-1, 0, 0, 0, 0], [6, 0, 0, 0, 0], mask=1, last=True)
    s.idle(4)
    s = dot("acc12_reaches_2047", "Twelve-bit accumulator: three full groups of 635 plus 142 equal the maximum 2047", acc_width=12)
    for _ in range(3):
        s.beat([1, 1, 1, 1, 1], [127, 127, 127, 127, 127], mask=31, last=False)
    s.beat([1, 1, 0, 0, 0], [127, 15, 0, 0, 0], mask=3, last=True)
    s.idle(4)
    s = dot("acc12_reaches_minus_2048", "Twelve-bit accumulator: the minimum -2048 is representable", acc_width=12)
    for _ in range(3):
        s.beat([-1, -1, -1, -1, -1], [127, 127, 127, 127, 127], mask=31, last=False)
    s.beat([-1, -1, 0, 0, 0], [127, 16, 0, 0, 0], mask=3, last=True)
    s.idle(4)
    s = dot("acc12_overflow", "Twelve-bit accumulator: 2048 overflows at the final beat and yields an error result", acc_width=12)
    for _ in range(3):
        s.beat([1, 1, 1, 1, 1], [127, 127, 127, 127, 127], mask=31, last=False)
    s.beat([1, 1, 0, 0, 0], [127, 16, 0, 0, 0], mask=3, last=True)
    s.idle(4)
    s = dot("acc12_negative_overflow", "Twelve-bit accumulator: -2049 overflows", acc_width=12)
    for _ in range(3):
        s.beat([-1, -1, -1, -1, -1], [127, 127, 127, 127, 127], mask=31, last=False)
    s.beat([-1, -1, 0, 0, 0], [127, 17, 0, 0, 0], mask=3, last=True)
    s.idle(4)
    s = dot("acc12_cancellation_after_overflow", "An overflow inside the frame is sticky even when later beats bring the total back", acc_width=12)
    for _ in range(4):
        s.beat([1, 1, 1, 1, 1], [127, 127, 127, 127, 127], mask=31, last=False)
    s.beat([-1, -1, -1, -1, -1], [127, 127, 127, 127, 127], mask=31, last=True)
    s.idle(4)

    storages = []

    def storage(identifier, description, dense=True, trit_count=12):
        scenario = StorageScenario(identifier, description, dense, trit_count)
        scenario.cycle(rst=True)
        scenario.cycle(rst=True)
        storages.append(scenario)
        return scenario

    words = [[1, -1, 0, 1, 0], [0, 0, 0, 0, 0], [1, 1, 0, 0, 0]]
    s = storage("load_three_words_and_read", "Word k is visible after edge S+1+k; the last word carries the tail mask; idle after S+WORDS+1")
    for address, trits in enumerate(words):
        s.load(address, s.code(trits))
    s.cycle(start=True)
    for _ in range(6):
        s.cycle()
    s = storage("start_and_writes_while_busy_are_ignored", "A second start and a write during the read do nothing; the next read shows the original words")
    for address, trits in enumerate(words):
        s.load(address, s.code(trits))
    s.cycle(start=True)
    s.cycle(start=True)
    s.cycle(load_en=True, load_addr=1, load_code=s.code([1, 1, 1, 1, 1]))
    for _ in range(4):
        s.cycle()
    s.cycle(start=True)
    for _ in range(6):
        s.cycle()
    s = storage("reset_mid_stream_preserves_contents", "Reset aborts the read; a new start replays the stored words")
    for address, trits in enumerate(words):
        s.load(address, s.code(trits))
    s.cycle(start=True)
    s.cycle()
    s.cycle(rst=True)
    s.cycle()
    s.cycle(start=True)
    for _ in range(6):
        s.cycle()
    s = storage("invalid_code_word_zeroes_lanes", "A reserved code yields out_valid=1, out_code_valid=0, zero lanes and the normal mask")
    s.load(0, s.code([1, 0, 0, 0, 0]))
    s.load(1, 243)
    s.load(2, s.code([0, -1, 0, 0, 0]))
    s.cycle(start=True)
    for _ in range(6):
        s.cycle()
    s = storage("baseline_two_words", "Five-lane two-bit words with a two-lane tail", dense=False, trit_count=7)
    s.load(0, s.code([1, -1, 0, 1, 0]))
    s.load(1, s.code([1, 1]))
    s.cycle(start=True)
    for _ in range(5):
        s.cycle()
    s = storage("single_word_stream", "TRIT_COUNT=1: one word, mask 00001, idle after S+2", dense=True, trit_count=1)
    s.load(0, s.code([-1]))
    s.cycle(start=True)
    for _ in range(4):
        s.cycle()

    rng = random.Random(27)
    random_weights = [rng.choice((-1, 0, 1)) for _ in range(37)]
    random_acts = [rng.randint(-128, 127) for _ in range(37)]
    frames = []

    def frame(identifier, weights, activations, codec="dense5", acc_width=32, description=""):
        frames.append({"id": f"frame_{identifier}", "kind": "frame", "codec": codec, "acc_width": acc_width,
                       "weights": list(weights), "activations": list(activations),
                       "expect": frame_expectation(weights, activations, acc_width), "description": description})

    frame("random_seed27_dense5", random_weights, random_acts, description="37 random weights and int8 activations")
    frame("random_seed27_baseline2", random_weights, random_acts, "baseline2")
    frame("signed_extremes", [-1] * 5, [-128] * 5, description="(-1)(-128) widened before negation gives 640")
    frame("positive_extremes", [1] * 5, [127] * 5)
    frame("empty_vector", [], [], description="An empty vector is one empty frame with result 0")
    frame("partial_group", [1, -1, 0, 1, 0, -1, 1], [127, -128, 55, 2, -90, 3, -4], description="Seven values: a full group and a two-lane tail")
    frame("acc12_at_max", [1] * 16, [127] * 16, acc_width=12, description="16 x 127 = 2032 fits twelve bits")
    frame("acc12_at_min", [1] * 16, [-128] * 16, acc_width=12, description="16 x -128 = -2048 fits twelve bits")
    frame("acc12_overflow", [1] * 17, [127] * 17, acc_width=12, description="2159 exceeds 2047 at the fourth group")
    frame("acc12_negative_overflow", [1] * 17, [-128] * 17, acc_width=12, description="-2176 is below -2048")
    frame("acc12_cancellation_after_overflow", [1] * 17 + [-1] * 10, [127] * 27, acc_width=12,
          description="The total returns to 889 but the frame stays in error")

    constants = {
        "dot_ports": {"inputs": ["clk", "rst", "in_valid", "in_code[9:0]", "in_activations[39:0]", "in_mask[4:0]", "in_last", "out_ready"],
                      "outputs": ["in_ready", "out_valid", "out_result[ACC_WIDTH-1:0]", "out_error"],
                      "parameters": {"DENSE5": [0, 1], "ACC_WIDTH": [2, 32]}, "module": "trinity_dot_stream_t27"},
        "beat": {"lanes": 5, "lane_encoding": {"00": 0, "01": 1, "10": -1, "11": "invalid"}, "dense_code_limit": 243,
                 "dense_high_bits_must_be_zero": [9, 8], "baseline_code_limit": 1024, "activation_range": [-128, 127],
                 "mask_rule": "11111 before the last beat; 00001/00011/00111/01111/11111 on the last beat; 00000 only as a whole empty frame",
                 "inactive_lanes": "trit code 00 and activation byte 0", "group_sum_range": [-640, 640]},
        "accumulator": {"widths": [2, 32], "runner_widths": [12, 32], "overflow": "checked after every accepted group; error is sticky, result 0, no wrap or saturate",
                        "error_result": 0},
        "pipeline": {"stages": 2, "accept_to_valid_edges": 1, "accept_to_consume_edges": 2,
                     "in_ready": "!reset && (!stage_valid || !out_valid || out_ready)",
                     "stable_output": "out_valid, out_result and out_error hold until out_ready", "reset": "synchronous, flushes stage, accumulator, errors and pending output"},
        "runner_packet": {"code": [9, 0], "activations": [49, 10], "mask": [54, 50], "last": 55, "reset": 56, "reset_pending_output": 57,
                          "expected_result": [31, 0], "expected_error": 32,
                          "status": {"argument": -80, "capacity": -81, "overflow": -82, "output": -83, "mismatch": -84, "process": -85, "resource": -86}},
        "storage": {"module": "ternary_dense5_stream_t27 / ternary_baseline5_stream_t27", "capacity_groups": 64, "max_trits": 320,
                    "code_widths": {"dense5": 8, "baseline5": 10}, "element_bits": 16,
                    "signals": ["rst", "start", "busy", "load_en", "load_addr", "load_code", "load_ready", "out_valid", "out_code_valid", "out_last", "out_lane_mask", "out_trits"],
                    "timing": {"S": "start accepted; busy=1", "S+1+k": "word k visible", "S+WORDS": "last word, out_last=1", "S+WORDS+1": "idle"},
                    "load_ready": "!rst && !busy && !start && WORDS in 1..64", "reset": "aborts the read, keeps RAM contents",
                    "backpressure": False, "join_to_dot_stream": "recorded gap: needs a FIFO of WORDS beats or a ready-capable sequencer",
                    "specialized_capacities_simulated": [1, 65, 820, 4096]},
        "view": {"packed": "[15] code valid, [14:10] lane mask, [9:0] visible lanes", "idle": 0, "invalid_code": "mask << 10"},
        "evidence": {"label": "rtl-simulation", "simulator": "Icarus Verilog", "not_claimed": ["fpga", "timing", "utilization", "throughput"]},
    }
    invariants = [
        {"id": "beat_fields_have_the_documented_widths", "condition": "5*8 == 40, 5*2 == 10, valid bit == 2^10"},
        {"id": "code_limits_are_three_to_the_fifth_and_two_to_the_tenth", "condition": "243 == 3^5 <= 256, 1024 == 2^10"},
        {"id": "group_sum_bound_is_five_times_the_int8_magnitude", "condition": "640 == 5*128"},
        {"id": "accumulator_widths_nest", "condition": "2 < 12 <= 32"},
        {"id": "pipeline_latency_follows_the_two_stages", "condition": "accept -> valid: 1 edge; accept -> consume: 2 edges"},
        {"id": "packet_fields_are_contiguous", "condition": "10 + 40 + 5 + 1 + 1 + 1 bits"},
        {"id": "storage_capacity_and_element_width", "condition": "64*5 == 320; 16 >= 10 > 8"},
        {"id": "the_storage_join_is_a_recorded_gap", "condition": "no backpressure; join needs a buffer"},
        {"id": "view_packing_uses_the_upper_bits", "condition": "valid bit 2^15, mask shift 10"},
    ]
    return {
        "module": "TrinityMemoryStreamComputeSpec",
        "spec_path": "specs/memory/stream_compute.t27",
        "schema_version": 2,
        "format_family": "Conformance",
        "vector_name": "Trinity Stream Compute traces and frames",
        "description": "Cycle-exact traces of the framed dot pipeline and the storage sequencer with view, plus frame-level "
                       "dot products, computed by the cycle models in tools/generate-spec-vectors.py from the contract in the spec. "
                       "Replayed in Icarus Verilog by tests/spec_stream_replay.py; evidence label rtl-simulation.",
        "created_at": "2026-09-11T00:00:00Z",
        "generator": "tools/generate-spec-vectors.py",
        "replay": {"traces": "tests/spec_stream_replay.py (tests/tb_spec_dot_trace.v, tests/tb_spec_storage_trace.v)",
                   "frames": "tests/test_spec_stream_compute.py through trinity_memory.rtl_compute"},
        "constants": constants,
        "invariants": invariants,
        "vectors": [item.document() for item in traces] + [item.document() for item in storages] + frames,
    }


def render(document):
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if a committed file differs")
    args = parser.parse_args()
    documents = ((OUTPUT, render(build())), (BRIDGE_OUTPUT, render(build_bridge())),
                 (TENSORPACK_OUTPUT, render(build_tensorpack())), (STREAM_OUTPUT, render(build_stream_compute())))
    stale = 0
    for output, text in documents:
        if args.check:
            current = output.read_text(encoding="utf-8") if output.exists() else ""
            if current != text:
                print(f"{output} is stale: regenerate with tools/generate-spec-vectors.py", file=sys.stderr)
                stale += 1
            else:
                print(f"{output}: up to date ({text.count(chr(10))} lines)")
        else:
            output.write_text(text, encoding="utf-8")
            print(f"wrote {output}")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
