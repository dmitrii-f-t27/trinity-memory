#!/usr/bin/env python3
"""Generate or check the conformance vectors derived from specs/memory/*.t27
and specs/formats/*.t27.

The expectations here are computed from the contract stated in
specs/memory/types.t27 with plain Python arithmetic and zlib.crc32. They do not
call the native implementation; tests/test_spec_types.py compares the committed
vectors with the executable stack, so the spec, this generator and the
implementation must all agree before CI passes. The specs/formats section
restates, in Python, the upstream loops that those specs cite, as a test oracle.
"""
import argparse
import hashlib
import itertools
import json
import re
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "conformance" / "memory_types.json"
BRIDGE_OUTPUT = ROOT / "conformance" / "memory_bridge.json"
TENSORPACK_OUTPUT = ROOT / "conformance" / "memory_tensorpack.json"
STREAM_OUTPUT = ROOT / "conformance" / "memory_stream_compute.json"
LAB_OUTPUT = ROOT / "conformance" / "memory_conformance.json"
EDGE_OUTPUT = ROOT / "conformance" / "memory_edge_demo.json"

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


FPGA_EVIDENCE = {"bitstream_sha256": "bcf6e804" + "0" * 56, "idcode": "0x13636093",
                 "dna": "0x00389c0c2d85e85c", "build_id": "1d474000"}
# quiet (the host's silence before a retransmission) is longer than the double's inter-byte
# timeout (byte_timeout below), as on the board, so a partial frame is dropped before the resend.
FPGA_DEVICE = {"evidence": FPGA_EVIDENCE, "baud": 115200, "reply_timeout": 1.0, "quiet": 0.3, "attempts": 8}
FPGA_CONSTANTS = {
    "backends": {"emulator": {"id": 0, "transport": "in-process", "hardware": False},
                 "fpga": {"id": 1, "transport": "uart", "hardware": True}},
    "evidence_fields": {"bitstream_sha256": "64 lowercase hex", "capture_sha256": "64 lowercase hex",
                        "idcode": "0x and 8 lowercase hex", "dna": "0x and 16 lowercase hex", "build_id": "8 lowercase hex"},
    "device": {"word_bytes": 16, "lanes": {"baseline2": 64, "dense5": 80}, "max_rows_per_run": 1024,
               "max_words_per_row": 1024, "accumulator_bits": 32, "codecs": ["baseline2", "dense5"],
               "row_layout": "row-padded: every row starts at a 16-byte word; padding lanes are zero trits"},
    "wire": {"protocol": 4, "frames": {"X": {"cmd": 88, "index": 5, "addr": "first 80-byte activation block",
                                             "len": "multiple of 8, at most 51 * 80 = 4080"},
                                       "M": {"cmd": 77, "index": 6, "addr": "region byte address, multiple of 16",
                                             "len": 12, "payload": "rows u32, cols u32, format u32 (0 baseline2, 1 dense5)"}},
             "lines": {"Y": "a = seq << 24 | row, v = y (32-bit two's complement)",
                       "Z": "a = seq << 24 | 11 << 16 | index, v = counter"},
             "z": ["status", "rows", "words_per_row", "words", "cycles", "idle_clocks", "latency",
                   "invalid_codes", "stray_words", "consumer_stalls", "checksum"],
             "z_status": {"0": "ran", "1": "refused (configuration, or a region past the store)",
                          "2": "ended early (bus words stopped, or aborted)"}},
}


def fpga_backend(**double):
    return {"name": "fpga", "device": FPGA_DEVICE,
            "double": {"build_id": "1d474000", "protocol": 4, "byte_timeout": 0.2, **double}}


def exact_rows(values, rows, cols, x):
    return [sum(values[r * cols + c] * x[c] for c in range(cols)) for r in range(rows)]


def fpga_vectors():
    """Vectors of the fpga backend, replayed against the device double (docs/bridge.md)."""
    import random
    rng = random.Random(64)
    a_rows, a_cols, b_rows, b_cols = 3, 100, 2, 70
    a_values = [rng.choice((-1, 0, 1)) for _ in range(a_rows * a_cols)]
    b_values = [rng.choice((-1, 0, 1)) for _ in range(b_rows * b_cols)]
    tensors = [tensor("a", (a_rows, a_cols), a_values, "dense5", scales=(0.5, 1.0, 2.0), scale_axis=0),
               tensor("b", (b_rows, b_cols), b_values, "baseline2"),
               tensor("d17", (2, 17), [1] * 34, "dense17")]
    pack = ttpk(tensors)
    xa = [rng.randint(-128, 127) for _ in range(a_cols)]
    xa2 = [rng.randint(-128, 127) for _ in range(a_cols)]
    xb = [rng.randint(-128, 127) for _ in range(b_cols)]
    evidence_format = {"evidence.capture_sha256": "sha256-hex64"}
    wide_values = [rng.choice((-1, 0, 1)) for _ in range(4 * 1100)]
    wide = ttpk([tensor("w", (4, 1100), wide_values, "dense5")])
    xw = [rng.randint(-128, 127) for _ in range(1100)]
    ids = {f"{part}_id": identity(part) for part in IDENTITY_PARTS}
    return [
        dict(rpc("fpga_capabilities_name_the_device", "trinity.capabilities",
                 result={"backend": "fpga", "hardware": True, "transport": "uart", "version": 1,
                         "device": {"baud": 115200, "protocol_min": 4, "region": 0, "max_rows_per_run": 1024,
                                    "max_words_per_row": 1024, "codecs": ["baseline2", "dense5"], "evidence": FPGA_EVIDENCE}},
                 description="Backend fpga: hardware true, transport uart and the configured evidence; the device is not asked"),
             backend=fpga_backend()),
        dict(rpc("fpga_chip_info_checks_the_device", "trinity_chipInfo",
                 result=dict(ids, anchor=18368, backend="fpga", hardware=True, identity_kind="synthetic-public-16-byte",
                             status="memory device (fpga)", evidence=dict(FPGA_EVIDENCE, capture_bytes=460)),
                 result_format=evidence_format,
                 description="The identity asks the device for its 23 status lines (460 bytes) first; the IDs stay the public "
                             "synthetic constants and the evidence block identifies the device"),
             backend=fpga_backend()),
        dict(rpc("fpga_chip_info_refuses_another_build", "chip_info", error_code=-32000,
                 error_message_contains="build id",
                 description="The device reports a build id other than the configured evidence"),
             backend=fpga_backend(build_id="12345678")),
        dict(rpc("fpga_chip_info_refuses_protocol_3", "trinity_chipInfo", error_code=-32000,
                 error_message_contains="protocol",
                 description="A loader without the matvec extension (protocol 3, the DDR3 loader) is not an fpga "
                             "backend device"),
             backend=fpga_backend(protocol=3)),
        dict(rpc("fpga_chip_info_without_a_device", "trinity_chipInfo", error_code=-32000,
                 error_message_contains="cannot open",
                 description="The configured serial port does not exist"),
             backend={"name": "fpga", "device": FPGA_DEVICE, "double": {"absent": True}}),
        {
            "id": "fpga_dot_lifecycle", "kind": "sequence", "backend": fpga_backend(),
            "description": "upload a TensorPack, dot a dense5 and a baseline2 matrix on the device: exact accumulators, "
                           "scales unapplied, the image uploaded once and read back, the emulator's sums as reference",
            "steps": [
                step("upload", "memory.upload", {"data": b64(pack)}, capture={"handle": "handle"},
                     result={"bytes": len(pack), "backend": "fpga"}, result_format={"handle": "uuid4-hex32"}),
                step("dot_dense5", "compute.dot", {"handle": "$handle", "tensor_name": "a", "activations": xa},
                     result={"accumulators": exact_rows(a_values, a_rows, a_cols, xa), "scales": [0.5, 1.0, 2.0],
                             "backend": "fpga", "hardware": True, "arithmetic": "exact-integer", "tensor_name": "a",
                             "input_shape": [a_rows, a_cols], "output_shape": [a_rows], "scale_applied": False,
                             "reference": {"backend": "emulator", "mismatches": 0, "first_mismatch": -1},
                             "evidence": FPGA_EVIDENCE,
                             "transfer": {"codec": "dense5", "image_bytes": a_rows * 2 * 16, "uploaded": True,
                                          "readback_bytes": a_rows * 2 * 16, "matvec_runs": 1, "store_reloads": 0,
                                          "device_counters": {"words": a_rows * 2, "consumer_stalls": 0}}},
                     result_at_least={"transfer.matvec_attempts": 1},
                     result_format=evidence_format,
                     description="Retransmissions and repeated runs depend on timing (a stalled host process can "
                                 "cause one), so they are not fixed here; tests/test_bridge_fpga.py checks them"),
                step("dot_dense5_again", "compute.dot", {"handle": "$handle", "tensor_name": "a", "activations": xa2},
                     result={"accumulators": exact_rows(a_values, a_rows, a_cols, xa2),
                             "transfer": {"uploaded": False, "readback_bytes": 0}},
                     description="The same image is on the device: only the activations and the run go out"),
                step("dot_baseline2", "compute.dot", {"handle": "$handle", "tensor_name": "b", "activations": xb},
                     result={"accumulators": exact_rows(b_values, b_rows, b_cols, xb), "scales": [1.0, 1.0],
                             "transfer": {"codec": "baseline2", "image_bytes": b_rows * 2 * 16, "uploaded": True}}),
                step("dot_dense17_refused", "compute.dot", {"handle": "$handle", "tensor_name": "d17", "activations": [1] * 17},
                     error_code=-32602),
                step("info", "memory.info", {"handle": "$handle"}, result={"format": "TensorPack", "backend": "fpga"}),
                step("delete", "memory.delete", {"handle": "$handle"}, result={"deleted": True, "backend": "fpga"}),
            ],
        },
        {
            "id": "fpga_dot_retransmits_and_reports", "kind": "sequence",
            "backend": fpga_backend(faults=[{"kind": "corrupt", "cmd": "L", "nth": 1, "offset": 100},
                                            {"kind": "drop_line", "tag": "A", "index": 5, "nth": 1},
                                            {"kind": "corrupt_line", "tag": "Y", "nth": 2}],
                                    result_faults={"1": 5}),
            "description": "a corrupted chunk (crc nak), a lost activation ack (the retransmission is answered duplicate), "
                           "a corrupted Y line (the run is "
                           "repeated) and a device whose row 1 is 5 too high: the retransmissions are counted and the "
                           "wrong row is reported under reference, not hidden",
            "steps": [
                step("upload", "memory.upload", {"data": b64(wide)}, capture={"handle": "handle"}, result={"backend": "fpga"}),
                step("dot", "compute.dot", {"handle": "$handle", "tensor_name": "w", "activations": xw},
                     result={"accumulators": [y + (5 if r == 1 else 0) for r, y in enumerate(exact_rows(wide_values, 4, 1100, xw))],
                             "reference": {"backend": "emulator", "mismatches": 1, "first_mismatch": 1},
                             "transfer": {"image_bytes": 4 * 14 * 16, "matvec_runs": 1}},
                     result_at_least={"transfer.matvec_attempts": 2, "transfer.retransmits": 3,
                                      "transfer.timeouts": 1,
                                      "transfer.naks+transfer.late_replies": 1,
                                      "transfer.bad_lines+transfer.late_replies": 1},
                     result_format=evidence_format,
                     description="Each fault forces at least one retransmission or repeated run, and a stalled "
                                 "host process can add more, never fewer: retransmits, matvec_attempts and "
                                 "timeouts (the lost ack never arrives, so at least one attempt of that frame "
                                 "ends at its deadline) are lower bounds. A stall longer than the reply timeout can make "
                                 "the host read the nak or the corrupted Y line while it waits out the "
                                 "retransmission, where it counts as a late reply, so naks and bad_lines are "
                                 "bounded only together with late_replies"),
            ],
        },
        {
            "id": "fpga_dot_without_a_device", "kind": "sequence",
            "backend": {"name": "fpga", "device": FPGA_DEVICE, "double": {"absent": True}},
            "description": "uploads are held in process memory; the dot fails with a transport error",
            "steps": [
                step("upload", "memory.upload", {"data": b64(pack)}, capture={"handle": "handle"}, result={"backend": "fpga"}),
                step("dot", "compute.dot", {"handle": "$handle", "tensor_name": "a", "activations": xa}, error_code=-32000,
                     error_message_contains="cannot open"),
            ],
        },
    ]


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
    vectors += fpga_vectors()
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
        "fpga": FPGA_CONSTANTS,
        "sdk_adapter": {"backend_class": "trinity_memory.bridge.SDKMemoryBackend", "chip_info_requires": {"backend": "emulator", "hardware": False},
                        "fpga_chip_info_requires": {"backend": "fpga", "hardware": True,
                                                    "evidence": "all five fields present and well formed"},
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
        {"id": "evidence_mask_names_five_fields", "condition": "COMPLETE == bitstream|capture|idcode|dna|build_id == 31"},
        {"id": "a_device_word_holds_64_baseline2_or_80_dense5_lanes", "condition": "64 * 2 == 16 * 8; 80 == 16 * 5"},
        {"id": "an_activation_frame_fits_a_loader_chunk", "condition": "51 * 80 <= 4096 < 52 * 80"},
        {"id": "device_accumulators_hold_the_widest_row", "condition": "1024 * 80 * 128 <= 2^31 - 1"},
        {"id": "down_proj_needs_21_signed_bits", "condition": "2^19 < 6912 * 128 < 2^20"},
        {"id": "the_stage_one_chunk_fits_the_default_trit_limit", "condition": "320 * 2560 == 819200 <= 1000000"},
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
                   "backend": "a vector with `backend` runs on that backend; for fpga the replay starts the device "
                              "double of tests/fake_fpga_device.py (tools/bridge_link_protocol.MatvecDevice) on a "
                              "pseudo-terminal with the vector's `double` options, or a port that does not exist "
                              "when `double` is {\"absent\": true}",
                   "result_format": "keys may be dotted paths into the result",
                   "result_at_least": "dotted paths to integer counters, or sums of such paths joined by "
                                      "\"+\", and their lower bounds: counters a stalled process can raise "
                                      "but not lower, such as retransmissions; a sum where a stall can move "
                                      "a count from one counter to another (a nak or bad line read as a "
                                      "late reply)",
                   "error_message_contains": "with error_code: a substring the error message must contain (which "
                                             "refusal it was, when several share one code)",
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
        stall = bool(s.get("out_stall", False))
        if s["load_en"] and self.load_ready(s) and s["load_addr"] < self.words:
            self.memory[s["load_addr"]] = s["load_code"]
        was_active, was_busy, addr = self.active, self.busy(), self.read_addr
        if s["rst"]:
            self.read_addr, self.active, self.valid_q, self.last_q = 0, False, False, False
        elif self.valid_q and stall:
            pass  # the presented word, its flags and the address are held (issue #15)
        else:
            if was_active:
                if self.read_addr not in self.memory:
                    raise RuntimeError("scenario reads an unloaded word")
                self.read_code = self.memory[self.read_addr]
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

    def cycle(self, rst=False, start=False, load_en=False, load_addr=0, load_code=0, out_stall=False):
        stimulus = {"rst": rst, "start": start, "load_en": load_en, "load_addr": load_addr, "load_code": load_code,
                    "out_stall": out_stall}
        self.cycles.append({"stimulus": stimulus, "expect": self.model.clock(stimulus)})

    def code(self, trits):
        return dense_code(trits) if self.dense else baseline_code(trits)

    def load(self, address, code):
        self.cycle(load_en=True, load_addr=address, load_code=code)

    def document(self):
        return {"id": self.identifier, "kind": "storage_trace", "dense": self.dense, "trit_count": self.trit_count,
                "words": self.model.words, "tail_mask": self.model.tail_mask, "description": self.description,
                "cycle_count": len(self.cycles), "cycles": self.cycles}


class JoinModel:
    """The joined path (issue #15): ready-capable storage, the combinational join and the
    dot pipeline, clocked together exactly as rtl/t27/stream_dot.v wires them."""

    def __init__(self, dense, trit_count, acc_width):
        self.storage = StorageModel(dense, trit_count)
        self.dot = DotModel(dense, acc_width)
        self.dot.minimum, self.dot.maximum = -(1 << (acc_width - 1)), (1 << (acc_width - 1)) - 1

    def join(self, s):
        """Combinational join values from the current state and the stimulus."""
        word_valid = self.storage.valid_q
        word_last = self.storage.valid_q and self.storage.last_q
        code = self.storage.read_code if word_valid else 0
        mask = self.storage.tail_mask if word_last else 31
        dot_stim = {"reset": s["rst"], "in_valid": word_valid and s["act_valid"], "in_code": code,
                    "in_activations": list(s["act_data"]), "in_mask": mask, "in_last": word_last,
                    "out_ready": s["out_ready"]}
        dot_ready = self.dot.comb(dot_stim)["in_ready"]
        return {"dot_stim": dot_stim, "fire": word_valid and s["act_valid"] and dot_ready,
                "word_ready": s["act_valid"] and dot_ready, "act_ready": word_valid and dot_ready}

    def clock(self, s):
        pre = self.join(s)
        storage_stim = {"rst": s["rst"], "start": s["start"], "load_en": s["load_en"], "load_addr": s["load_addr"],
                        "load_code": s["load_code"], "out_stall": not pre["word_ready"]}
        storage_out = self.storage.clock(storage_stim)
        minimum, maximum = self.dot.minimum, self.dot.maximum
        accepted, dot_out = self.dot.clock(pre["dot_stim"])
        self.dot.minimum, self.dot.maximum = minimum, maximum
        if accepted != pre["fire"]:
            raise RuntimeError("join model disagrees with the pipeline about the accepted beat")
        post = self.join(s)
        return pre["fire"], {"out_result": dot_out["out_result"], "out_error": dot_out["out_error"],
                             "out_valid": dot_out["out_valid"], "act_ready": post["act_ready"],
                             "load_ready": storage_out["load_ready"], "busy": storage_out["busy"],
                             "beat": post["fire"]}


class JoinScenario:
    def __init__(self, identifier, description, dense=True, trit_count=12, acc_width=32):
        self.identifier, self.description = identifier, description
        self.dense, self.trit_count, self.acc_width = dense, trit_count, acc_width
        self.model = JoinModel(dense, trit_count, acc_width)
        self.cycles = []

    def cycle(self, rst=False, start=False, load_en=False, load_addr=0, load_code=0, act_valid=False,
              acts=(0, 0, 0, 0, 0), out_ready=True):
        stimulus = {"rst": rst, "start": start, "load_en": load_en, "load_addr": load_addr, "load_code": load_code,
                    "act_valid": act_valid, "act_data": list(acts), "out_ready": out_ready}
        fired, expect = self.model.clock(stimulus)
        self.cycles.append({"stimulus": stimulus, "expect": expect})
        return fired

    def code(self, trits):
        return dense_code(trits) if self.dense else baseline_code(trits)

    def load(self, address, code):
        self.cycle(load_en=True, load_addr=address, load_code=code)

    def start(self, out_ready=True):
        self.cycle(start=True, out_ready=out_ready)

    def feed(self, acts, out_ready=True, limit=16):
        """Present one activation beat until the join fires."""
        acts = list(acts) + [0] * (5 - len(acts))
        for _ in range(limit):
            if self.cycle(act_valid=True, acts=acts, out_ready=out_ready):
                return
        raise RuntimeError(f"{self.identifier}: activation beat never accepted")

    def idle(self, cycles=1, out_ready=True):
        for _ in range(cycles):
            self.cycle(out_ready=out_ready)

    def results(self):
        """Results in consumption order: a result is consumed at the edge where it was
        valid before the edge and out_ready is high (its value is stable until then)."""
        consumed = []
        for previous, step in zip(self.cycles, self.cycles[1:]):
            if previous["expect"]["out_valid"] and step["stimulus"]["out_ready"] and not step["stimulus"]["rst"]:
                consumed.append((previous["expect"]["out_result"], previous["expect"]["out_error"]))
        return consumed

    def document(self):
        return {"id": self.identifier, "kind": "join_trace", "dense": self.dense, "trit_count": self.trit_count,
                "acc_width": self.acc_width, "words": self.model.storage.words, "tail_mask": self.model.storage.tail_mask,
                "description": self.description, "cycle_count": len(self.cycles), "cycles": self.cycles}


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

    s = storage("hold_word_under_backpressure", "out_ready low holds the presented word, its mask and the sequencer; the stream resumes where it stopped")
    for address, trits in enumerate(words):
        s.load(address, s.code(trits))
    s.cycle(start=True)
    s.cycle()
    s.cycle(out_stall=True)
    s.cycle(out_stall=True)
    s.cycle()
    s.cycle(out_stall=True)
    s.cycle()
    s.cycle(out_stall=True)
    s.cycle(out_stall=True)
    for _ in range(3):
        s.cycle()
    s = storage("stall_without_a_word_changes_nothing", "out_ready low while idle, during loads and during start has no effect")
    s.cycle(out_stall=True)
    for address, trits in enumerate(words):
        s.cycle(load_en=True, load_addr=address, load_code=s.code(trits), out_stall=True)
    s.cycle(start=True, out_stall=True)
    for _ in range(5):
        s.cycle()
    s = storage("reset_during_hold", "Reset while a word is held aborts the read; the next start replays from word 0")
    for address, trits in enumerate(words):
        s.load(address, s.code(trits))
    s.cycle(start=True)
    s.cycle()
    s.cycle(out_stall=True)
    s.cycle(rst=True, out_stall=True)
    s.cycle()
    s.cycle(start=True)
    for _ in range(5):
        s.cycle()

    joins = []

    def join(identifier, description, dense=True, trit_count=12, acc_width=32):
        scenario = JoinScenario(identifier, description, dense, trit_count, acc_width)
        scenario.cycle(rst=True)
        scenario.cycle(rst=True)
        joins.append(scenario)
        return scenario

    def check_frame(scenario, weights, activations, index=-1):
        expect = frame_expectation(weights, activations, scenario.acc_width)
        got = scenario.results()
        if not got or got[index] != (expect["result"], expect["error"]):
            raise RuntimeError(f"{scenario.identifier}: joined results {got} differ from the frame reference {expect}")

    weights12 = [1, -1, 0, 1, 0, 0, 0, 0, 0, 0, 1, 1]
    acts12 = [-128, 127, 5, 3, 9, 1, 2, 3, 4, 5, 10, -10]
    stored12 = [weights12[0:5], weights12[5:10], weights12[10:12]]
    groups12 = [acts12[0:5], acts12[5:10], acts12[10:12]]
    s = join("join_three_words_dense", "Three stored words meet three activation beats back to back; the result equals the frame reference")
    for address, trits in enumerate(stored12):
        s.load(address, s.code(trits))
    s.start()
    for acts in groups12:
        s.feed(acts)
    s.idle(4)
    check_frame(s, weights12, acts12)
    weights7 = [1, -1, 0, 1, 0, 1, 1]
    acts7 = [-128, 127, 5, 3, 9, 10, -10]
    s = join("join_baseline_two_words", "Five-lane two-bit words with a two-lane tail through the joined path", dense=False, trit_count=7)
    s.load(0, s.code(weights7[0:5]))
    s.load(1, s.code(weights7[5:7]))
    s.start()
    s.feed(acts7[0:5])
    s.feed(acts7[5:7])
    s.idle(4)
    check_frame(s, weights7, acts7)
    s = join("join_activation_bubbles", "Gaps in the activation stream hold the stored word (word_ready low); nothing is lost")
    for address, trits in enumerate(stored12):
        s.load(address, s.code(trits))
    s.start()
    s.idle(2)
    s.feed(groups12[0])
    s.idle(3)
    s.feed(groups12[1])
    s.idle(1)
    s.feed(groups12[2])
    s.idle(4)
    check_frame(s, weights12, acts12)
    acts12b = [7, -7, 1, 0, 100, -3, 3, 0, 0, 0, -50, 50]
    s = join("join_output_backpressure", "A held result stalls the pipeline; the next frame's second word waits in the sequencer until the result is consumed")
    for address, trits in enumerate(stored12):
        s.load(address, s.code(trits))
    s.start(out_ready=False)
    for acts in groups12:
        s.feed(acts, out_ready=False)
    s.idle(2, out_ready=False)
    s.start(out_ready=False)
    s.feed(acts12b[0:5], out_ready=False)
    for _ in range(3):
        if s.cycle(act_valid=True, acts=acts12b[5:10], out_ready=False):
            raise RuntimeError("join_output_backpressure: a beat fired while the result was held")
    s.feed(acts12b[5:10])
    s.feed(acts12b[10:12])
    s.idle(4)
    check_frame(s, weights12, acts12, index=0)
    check_frame(s, weights12, acts12b, index=1)
    s = join("join_reset_mid_stream", "Reset after an accepted beat drops the frame and the read; a new start replays the stored words")
    for address, trits in enumerate(stored12):
        s.load(address, s.code(trits))
    s.start()
    s.feed(groups12[0])
    s.cycle(rst=True, out_ready=False)
    s.idle(1)
    s.start()
    for acts in groups12:
        s.feed(acts)
    s.idle(4)
    check_frame(s, weights12, acts12)
    weights20 = [1] * 20
    acts20 = [127] * 20
    s = join("join_acc12_overflow", "Four full groups of 635 overflow a twelve-bit accumulator through the joined path: error result", trit_count=20, acc_width=12)
    for address in range(4):
        s.load(address, s.code(weights20[5 * address:5 * address + 5]))
    s.start()
    for group in range(4):
        s.feed(acts20[5 * group:5 * group + 5])
    s.idle(4)
    check_frame(s, weights20, acts20)
    s = join("join_start_and_loads_ignored_while_streaming", "A second start and a write during the joined read do nothing; the result uses the original words")
    for address, trits in enumerate(stored12):
        s.load(address, s.code(trits))
    s.start()
    if s.cycle(start=True, act_valid=True, acts=groups12[0]):
        raise RuntimeError("join_start_and_loads_ignored_while_streaming: a beat fired before the first word")
    if not s.cycle(load_en=True, load_addr=1, load_code=s.code([1, 1, 1, 1, 1]), act_valid=True, acts=groups12[0]):
        raise RuntimeError("join_start_and_loads_ignored_while_streaming: the first word did not fire")
    s.feed(groups12[1])
    s.feed(groups12[2])
    s.idle(4)
    check_frame(s, weights12, acts12)

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
        "storage": {"module": "ternary_stream_adapter_t27 (out_ready exposed); ternary_dense5_stream_t27 / ternary_baseline5_stream_t27 tie out_ready high",
                    "capacity_groups": 64, "max_trits": 320,
                    "code_widths": {"dense5": 8, "baseline5": 10}, "element_bits": 16,
                    "signals": ["rst", "start", "busy", "load_en", "load_addr", "load_code", "out_ready", "load_ready", "out_valid", "out_code_valid", "out_last", "out_lane_mask", "out_trits"],
                    "timing": {"S": "start accepted; busy=1", "S+1+k": "word k visible (while out_ready)", "S+WORDS": "last word, out_last=1", "S+WORDS+1": "idle",
                               "hold": "out_ready low while out_valid holds the word, its flags and the address; the timing resumes where it stopped"},
                    "load_ready": "!rst && !busy && !start && WORDS in 1..64", "reset": "aborts the read, keeps RAM contents",
                    "backpressure": True, "join_to_dot_stream": "closed: ready-capable sequencer -> TrinityStreamJoinT27 -> trinity_dot_stream_t27 in rtl/t27/stream_dot.v, no buffer",
                    "specialized_capacities_simulated": [1, 65, 820, 4096]},
        "join": {"module": "ternary_stream_dot_t27", "source": "t27/rtl/stream_join.t27 (combinational) wired by rtl/t27/stream_dot.v",
                 "ports": {"inputs": ["clk", "rst", "start", "load_en", "load_addr", "load_code", "act_valid", "act_data[39:0]", "out_ready"],
                           "outputs": ["busy", "load_ready", "act_ready", "beat", "out_valid", "out_result[ACC_WIDTH-1:0]", "out_error"],
                           "parameters": {"DENSE5": [0, 1], "TRIT_COUNT": [1, 320], "ACC_WIDTH": [2, 32]}},
                 "fire": "word_valid && act_valid && in_ready", "word_ready": "act_valid && in_ready", "act_ready": "word_valid && in_ready",
                 "in_valid": "word_valid && act_valid", "in_mask": "11111, or the tail mask on the last word", "in_code": "the stored word",
                 "packed_join_word": "[0] fire, [1] word_ready, [2] act_ready, [3] in_valid, [4] in_last, [12:5] mask",
                 "activations": "int8 lanes supplied by the activation source; lanes outside the mask must be zero"},
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
        {"id": "the_storage_join_is_closed", "condition": "backpressure through out_ready; no buffer between storage and the pipeline"},
        {"id": "join_bits_are_distinct_and_below_the_mask", "condition": "1, 2, 4, 8, 16, shift 5, full mask 31, 40 activation bits"},
        {"id": "view_packing_uses_the_upper_bits", "condition": "valid bit 2^15, mask shift 10"},
    ]
    return {
        "module": "TrinityMemoryStreamComputeSpec",
        "spec_path": "specs/memory/stream_compute.t27",
        "schema_version": 2,
        "format_family": "Conformance",
        "vector_name": "Trinity Stream Compute traces and frames",
        "description": "Cycle-exact traces of the framed dot pipeline, the storage sequencer with view (including backpressure) "
                       "and the joined read -> decode -> dot path, plus frame-level dot products, computed by the cycle models in "
                       "tools/generate-spec-vectors.py from the contract in the spec. Replayed in Icarus Verilog by "
                       "tests/spec_stream_replay.py; evidence label rtl-simulation.",
        "created_at": "2026-09-11T00:00:00Z",
        "generator": "tools/generate-spec-vectors.py",
        "replay": {"traces": "tests/spec_stream_replay.py (tests/tb_spec_dot_trace.v, tests/tb_spec_storage_trace.v, tests/tb_spec_join_trace.v)",
                   "frames": "tests/test_spec_stream_compute.py through trinity_memory.rtl_compute"},
        "constants": constants,
        "invariants": invariants,
        "vectors": [item.document() for item in traces] + [item.document() for item in storages]
                   + [item.document() for item in joins] + frames,
    }


# ---------------------------------------------------------------------------
# specs/memory/conformance.t27 -- the lab manifest


def fixture_vector(name, weights, activations):
    return {"name": name, "weights": list(weights), "activations": list(activations),
            "dot": sum(w * a for w, a in zip(weights, activations)),
            "dense5_hex": encode("dense5", weights).hex(), "baseline2_hex": encode("baseline2", weights).hex(),
            "dense17_hex": encode("dense17", weights).hex(), "dense22_hex": encode("dense22", weights).hex()}


def build_conformance():
    base = json.loads((ROOT / "examples" / "conformance.json").read_text(encoding="utf-8"))
    fixture = [fixture_vector(item["name"], item["weights"], item["activations"]) for item in base["vectors"]]
    for original, extended in zip(base["vectors"], fixture):
        for key in ("dot", "dense5_hex", "baseline2_hex"):
            if original[key] != extended[key]:
                raise RuntimeError(f"examples/conformance.json disagrees with the pure encoders on {original['name']}.{key}")
    document = {"schema": "trinity.conformance.v1", "vectors": fixture}
    fixture_text = json.dumps(document, indent=2) + "\n"
    containers = []
    for item in fixture:
        for codec in ("dense5", "baseline2"):
            containers.append({"vector": item["name"], "codec": codec,
                               "ttpk_hex": ttpk([tensor("weights", [len(item["weights"])], item["weights"], codec)]).hex()})
    corruption_blob = ttpk([tensor("w", [5], [1, 0, -1, 0, 1], "dense5")])
    size = len(corruption_blob)
    positions = [0, 4, 12, 31, size // 2, size - 1]
    corruption = [{"index": index, "position": position, "ttpk_hex": (corruption_blob[:position] + bytes([corruption_blob[position] ^ 1]) + corruption_blob[position + 1:]).hex()}
                  for index, position in enumerate(positions)]
    good = fixture[0]
    invalid = [
        {"id": "missing_schema", "rejected_at": "validation", "document": {"vectors": fixture}},
        {"id": "wrong_schema", "rejected_at": "validation", "document": {"schema": "trinity.conformance.v2", "vectors": fixture}},
        {"id": "extra_root_member", "rejected_at": "validation", "document": {"schema": "trinity.conformance.v1", "vectors": fixture, "extra": 1}},
        {"id": "zero_vectors", "rejected_at": "validation", "document": {"schema": "trinity.conformance.v1", "vectors": []}},
        {"id": "too_many_vectors", "rejected_at": "validation",
         "recipe": {"vectors": 257, "vector": {"weights": [1], "activations": [1], "dot": 1}}},
        {"id": "vector_missing_dot", "rejected_at": "validation", "document": {"schema": "trinity.conformance.v1", "vectors": [{k: v for k, v in good.items() if k != "dot"}]}},
        {"id": "vector_extra_field", "rejected_at": "validation", "document": {"schema": "trinity.conformance.v1", "vectors": [dict(good, note="x")]}},
        {"id": "dot_mismatch", "rejected_at": "validation", "document": {"schema": "trinity.conformance.v1", "vectors": [dict(good, dot=good["dot"] + 1)]}},
        {"id": "duplicate_names", "rejected_at": "validation", "document": {"schema": "trinity.conformance.v1", "vectors": [good, dict(good)]}},
        {"id": "weight_out_of_range", "rejected_at": "validation", "document": {"schema": "trinity.conformance.v1", "vectors": [dict(good, weights=[2] + good["weights"][1:], dot=good["dot"] + good["activations"][0])]}},
        {"id": "activation_out_of_range", "rejected_at": "validation", "document": {"schema": "trinity.conformance.v1", "vectors": [dict(good, activations=[128] + good["activations"][1:], dot=good["dot"] + 1)]}},
        {"id": "count_mismatch", "rejected_at": "validation", "document": {"schema": "trinity.conformance.v1", "vectors": [dict(good, activations=good["activations"] + [1])]}},
        {"id": "empty_weights", "rejected_at": "validation", "document": {"schema": "trinity.conformance.v1", "vectors": [dict(good, weights=[], activations=[], dot=0)]}},
        {"id": "empty_name", "rejected_at": "validation", "document": {"schema": "trinity.conformance.v1", "vectors": [dict(good, name="")]}},
        {"id": "golden_not_a_string", "rejected_at": "validation", "document": {"schema": "trinity.conformance.v1", "vectors": [dict(good, dense5_hex=1)]}},
        {"id": "golden_hex_mismatch", "rejected_at": "checks", "document": {"schema": "trinity.conformance.v1", "vectors": [dict(good, dense5_hex="00")]}},
        {"id": "too_many_weights", "rejected_at": "validation",
         "recipe": {"weights": 4097, "weight": 1, "activation": 1}},
    ]
    sections = {
        "fixture": {"consumer": "native runtime (t27/experiments.t27) through trinity_memory.conformance", "input": "fixture_document",
                    "evidence": ["software-loopback-http", "rtl-simulation"],
                    "expected": {"positive_checks": 4 * (len(fixture) + 7) + 2, "corrupt_rejections": 6, "rtl_checks": 2 * (len(fixture) + 7),
                                 "checks_without_rtl": 4 * (len(fixture) + 7) + 2 + 6, "physical_device_tested": False}},
        "codecs": {"file": "memory_types.json", "kinds": ["group", "container", "invalid_word", "invalid_sparsity"],
                   "consumers": ["python adapter", "wasm (five-trit groups)"], "evidence": ["software", "wasm"]},
        "containers": {"file": "memory_tensorpack.json", "kinds": ["pack", "invalid_pack"],
                       "consumers": ["python adapter", "native CLI when built"], "evidence": ["software"]},
        "bridge": {"file": "memory_bridge.json", "kinds": ["rpc", "transport", "sequence"], "consumers": ["native loopback server over TCP"],
                   "evidence": ["emulator", "software-loopback-http"],
                   "lab_checks": ["client_disconnect_after_upload", "server_restart_clears_objects"]},
        "stream": {"file": "memory_stream_compute.json", "kinds": ["dot_trace", "storage_trace"], "consumers": ["Icarus Verilog trace replay"],
                   "evidence": ["rtl-simulation"]},
        "frames": {"file": "memory_stream_compute.json", "kinds": ["frame"], "consumers": ["native RTL runner"], "evidence": ["rtl-simulation"]},
    }
    catalogue = {
        "corruption": {
            "header_bit_flip": ["memory_tensorpack.json#wrong_magic", "memory_tensorpack.json#version_2", "memory_tensorpack.json#flags_set", "fixture_corruption#0", "fixture_corruption#1"],
            "metadata_bit_flip": ["fixture_corruption#2", "memory_tensorpack.json#metadata_crc_mismatch"],
            "payload_bit_flip": ["fixture_corruption#3", "fixture_corruption#4", "fixture_corruption#5", "memory_tensorpack.json#payload_crc_mismatch", "memory_bridge.json#upload_corrupted_crc"],
            "resealed_crc": ["memory_tensorpack.json#nested_nonzero_padding", "memory_tensorpack.json#nested_reserved_code", "memory_tensorpack.json#nested_count_mismatch", "memory_types.json#dense5_nonzero_padding"],
            "truncated_body": ["memory_bridge.json#truncated_body_times_out", "memory_tensorpack.json#truncated_payload", "memory_tensorpack.json#truncated_header"],
            "wrong_content_type": ["memory_bridge.json#text_plain_unsupported"],
            "oversized_request": ["memory_bridge.json#oversized_request", "memory_bridge.json#content_length_over_limit"],
            "reserved_code": ["memory_types.json#dense5_reserved_code_243", "memory_types.json#sparse41_reserved_code_9", "memory_tensorpack.json#nested_reserved_code"],
            "nonzero_padding": ["memory_types.json#dense5_nonzero_padding", "memory_tensorpack.json#nested_nonzero_padding", "memory_tensorpack.json#nested_unused_high_bits"],
        },
        "interruption": {
            "truncated_body_timeout": ["memory_bridge.json#truncated_body_times_out"],
            "client_disconnect_after_upload": ["memory_bridge.json#tmem_lifecycle", "lab#client_disconnect_after_upload"],
            "server_restart_clears_objects": ["lab#server_restart_clears_objects"],
        },
        "reset": {
            "rtl_reset_mid_frame": ["memory_stream_compute.json#reset_mid_frame"],
            "rtl_reset_with_pending_output": ["memory_stream_compute.json#reset_with_pending_output"],
            "storage_reset_mid_stream": ["memory_stream_compute.json#reset_mid_stream_preserves_contents"],
        },
    }
    report = {
        "schema": "trinity.conformance-lab.v1",
        "native_report_schema": "trinity.conformance-report.v1",
        "sections": list(sections),
        "timing_keys": ["timing"],
        "determinism": "two runs from one clone are equal after removing the timing keys",
        "tool": "tools/conformance-lab.py",
        "fields": ["schema", "passed", "seed", "sections", "catalogue", "sources", "tools", "timing", "physical_device_tested"],
    }
    return {
        "module": "TrinityMemoryConformanceLabSpec",
        "spec_path": "specs/memory/conformance.t27",
        "schema_version": 2,
        "format_family": "Conformance",
        "vector_name": "Trinity Conformance Lab manifest",
        "description": "The fixture the native conformance experiment consumes (with golden bytes for all four dense codecs), the "
                       "containers it uploads, its six corruption blobs, the fixtures it must reject, the map from lab sections to "
                       "the sibling conformance files and their consumers, and the catalogue of corruption, interruption and reset cases.",
        "created_at": "2026-09-11T00:00:00Z",
        "generator": "tools/generate-spec-vectors.py",
        "constants": {
            "fixture_schema": {"name": "trinity.conformance.v1", "root": ["schema", "vectors"], "vectors": [1, 256], "weights": [1, 4096],
                               "required": ["name", "weights", "activations", "dot"], "optional": ["dense5_hex", "baseline2_hex", "dense17_hex", "dense22_hex"],
                               "rules": ["unique nonempty names", "weights in {-1,0,1}", "int8 activations", "dot == sum(weights*activations)", "fixture <= 16 MiB"]},
            "plan": {"codec_order": ["dense5", "baseline2", "dense17", "dense22"], "rtl_codecs": ["dense5", "baseline2"],
                     "random_case_counts": [1, 4, 5, 6, 12, 31, 65], "sparse_transfer_codecs": ["sparse41", "sparse82"],
                     "sparse_pattern": [1, 0, 0, 0, 0, -1, 0, 0], "corruption_positions": "0, 4, 12, 31, size/2, size-1",
                     "corruption_rpc_error": -32602, "seed_default": 27, "rtl_seed_range": [0, 4294967295],
                     "server": {"max_request_bytes": 131072, "max_object_bytes": 32768, "max_storage_bytes": 131072, "max_objects": 4, "max_trits": 16384, "timeout_seconds": 10.0},
                     "positive_checks": "4 * (vectors + 7) + 2", "rtl_checks": "2 * (vectors + 7) when rtl", "corrupt_rejections": 6},
            "status": {"check": -90, "resource": -91, "rtl": -82},
            "report": report,
        },
        "invariants": [
            {"id": "required_fields_are_the_first_four_bits", "condition": "name|weights|activations|dot == 15; allowed == 255"},
            {"id": "fixture_bounds_are_the_documented_limits", "condition": "256 vectors, 4096 weights, 16 MiB"},
            {"id": "the_plan_counts_add_up", "condition": "4 codecs, 2 RTL codecs, 7 random cases, 2 sparse transfers, 6 flips"},
            {"id": "corruption_is_rejected_as_invalid_params", "condition": "-32602"},
            {"id": "the_experiment_server_fits_its_vectors", "condition": "4 * 32768 == 131072; 16384 >= 4096"},
            {"id": "the_report_has_six_sections_and_no_device", "condition": "6 sections; physical_device_tested == false"},
        ],
        "fixture_document": document,
        "fixture_text": fixture_text,
        "fixture_containers": containers,
        "fixture_corruption": {"blob_ttpk_hex": corruption_blob.hex(), "size": size, "flips": corruption},
        "invalid_fixtures": invalid,
        "sections": sections,
        "catalogue": catalogue,
        "vectors": [{"id": f"fixture_{item['name']}", "kind": "fixture_vector", **item} for item in fixture]
                   + [{"id": f"corruption_{flip['index']}", "kind": "fixture_corruption", **flip} for flip in corruption]
                   + [{"id": item["id"], "kind": "invalid_fixture", "rejected_at": item["rejected_at"]} for item in invalid],
    }


# ---------------------------------------------------------------------------
# specs/memory/edge_demo.t27


EDGE_LABELS = ["rising", "falling", "alternating"]
EDGE_LIMITATIONS = [
    "Illustrative synthetic fixtures; no measured generalization accuracy.",
    "HTTP wall times include native C, JSON and scheduling overhead.",
    "RTL cycles are simulation evidence, not FPGA throughput or power.",
    "No physical board, DDR controller, trained checkpoint or ZK proof.",
]


def edge_weight(row, column):
    if row == 2:
        return -1 if column % 2 == 0 else 1
    if row == 0:
        return -1 if column < 6 else 1
    return 1 if column < 6 else -1


def edge_argmax(scores):
    top = max(scores)
    if scores.count(top) > 1:
        return None
    return scores.index(top)


def build_edge_demo():
    templates = [[edge_weight(row, column) for column in range(12)] for row in range(3)]
    weights = [value for row in templates for value in row]
    fixtures = [
        ("step_up", [40 * edge_weight(0, c) for c in range(12)]),
        ("step_down", [40 * edge_weight(1, c) for c in range(12)]),
        ("alternating", [30 * edge_weight(2, c) for c in range(12)]),
        ("offset_step_up", [-8, -7, -9, -8, -6, -8, 33, 34, 35, 34, 32, 35]),
        ("offset_step_down", [36, 35, 33, 35, 34, 36, -8, -9, -7, -8, -8, -6]),
        ("noisy_alternating", [-25, 29, -27, 31, -28, 30, -26, 28, -29, 32, -25, 31]),
    ]
    cases = []
    for index, (name, samples) in enumerate(fixtures):
        accumulators = [sum(edge_weight(row, c) * samples[c] for c in range(12)) for row in range(3)]
        label = edge_argmax([float(a) for a in accumulators])
        if label != index % 3:
            raise RuntimeError(f"fixture {name} does not score its own template")
        cases.append({"name": name, "samples": samples, "expected": EDGE_LABELS[index % 3], "accumulators": accumulators,
                      "scores": [float(a) for a in accumulators], "label": EDGE_LABELS[label], "ambiguous": False})
    models = {}
    for codec in ("dense5", "baseline2"):
        container = ttpk([{"name": "signal_templates", "shape": [3, 12], "values": weights, "codec": codec,
                           "scales": [1.0], "scale_axis": None, "axes": ["class", "sample"]}])
        models[codec] = {"ttpk_hex": container.hex(), "container_bytes": len(container), "container_sha256": sha256_hex(container),
                         "raw_weight_payload_bytes": payload_bytes(codec, 36)}
    predictions = [{"codec": codec, "fixture": case["name"], "label": case["label"], "accumulators": case["accumulators"]}
                   for codec in ("dense5", "baseline2") for case in cases]
    rtl_rows = [{"codec": codec, "fixture": case["name"], "row": row, "seed": 27 + row, "result": case["accumulators"][row],
                 "error": False, "weight_count": 12, "groups": 3, "encoded_weight_bits": 3 * (8 if codec == "dense5" else 10)}
                for codec in ("dense5", "baseline2") for case in cases for row in range(3)]
    scoring = [
        {"id": "constant_input_is_ambiguous", "samples": [17] * 12, "scales": [1.0], "accumulators": [0, 0, 0], "label": None, "ambiguous": True},
        {"id": "per_row_scales_reorder_the_winner", "model": {"shape": [3, 1], "values": [1, 1, -1], "scales": [1.0, 2.0, 0.5], "scale_axis": 0},
         "samples": [3], "accumulators": [3, 3, -3], "scores": [3.0, 6.0, -1.5], "label": "falling", "ambiguous": False},
        {"id": "overflowing_scale_is_rejected", "model": {"shape": [3, 1], "values": [1, 1, -1], "scales": [1e308, 1e308, 1.0], "scale_axis": 0},
         "samples": [3], "error": "finite"},
        {"id": "two_way_tie_is_ambiguous", "scores": [5.0, 5.0, 1.0], "label": None},
        {"id": "strict_maximum_wins", "scores": [1.0, 3.0, 2.0], "label": "falling"},
    ]
    vectors = ([{"id": f"model_{codec}", "kind": "model", "codec": codec, **model} for codec, model in models.items()]
               + [{"id": f"fixture_{case['name']}", "kind": "fixture", **case} for case in cases]
               + [{"id": f"prediction_{p['codec']}_{p['fixture']}", "kind": "prediction", **p} for p in predictions]
               + [{"id": f"rtl_{r['codec']}_{r['fixture']}_{r['row']}", "kind": "rtl_row", **r} for r in rtl_rows]
               + [{"id": item["id"], "kind": "scoring", **{k: v for k, v in item.items() if k != "id"}} for item in scoring])
    return {
        "module": "TrinityMemoryEdgeDemoSpec",
        "spec_path": "specs/memory/edge_demo.t27",
        "schema_version": 2,
        "format_family": "Conformance",
        "vector_name": "Trinity Edge Demo",
        "description": "The template classifier model in both encodings, six fixtures with exact accumulators and labels, "
                       "twelve predictions, thirty-six RTL rows with their seeds, scoring rules (ties, per-row scales, finiteness) "
                       "and the report contract, computed from the spec by tools/generate-spec-vectors.py.",
        "created_at": "2026-09-11T00:00:00Z",
        "generator": "tools/generate-spec-vectors.py",
        "constants": {
            "model": {"name": "signal_templates", "shape": [3, 12], "scales": [1.0], "scale_axis": None, "axes": ["class", "sample"],
                      "templates": templates, "labels": EDGE_LABELS, "description": "hand-authored ternary template classifier, shape [3,12]"},
            "fixtures": {"count": 6, "expected_label": "index mod 3", "step_amplitude": 40, "alternating_amplitude": 30},
            "scoring": {"score": "accumulator * scale (one scale for all rows or one per row)", "label": "strict maximum",
                        "tie": "ambiguous, label null", "scales": "positive finite; a non-finite score is rejected"},
            "experiment": {"modes": ["dense5", "baseline2"], "predictions": 12, "rtl_rows": 36, "rtl_seed": "seed + row", "seed_default": 27,
                           "rtl_seed_max": 4294967293, "server": {"max_request_bytes": 131072, "max_object_bytes": 32768, "max_storage_bytes": 131072,
                                                                  "max_objects": 4, "max_trits": 16384, "timeout_seconds": 10.0}},
            "report": {"schema": "trinity.edge-report.v1", "evidence": ["software-loopback-http", "rtl-simulation"], "runtime": "native-t27",
                       "physical_device_tested": False, "fixture_count": 6,
                       "mode_fields": ["codec", "capabilities", "container_bytes", "raw_weight_payload_bytes", "container_sha256", "roundtrip_exact", "upload_read_wall_ns", "cases"],
                       "case_fields": ["name", "samples", "expected", "label", "ambiguous", "accumulators", "scores", "backend", "reference", "rpc_wall_ns", "rtl", "passed"],
                       "timing_fields": ["upload_read_wall_ns", "rpc_wall_ns"], "limitations": EDGE_LIMITATIONS},
            "evidence": {"emulator": "loopback HTTP transfer and exact dot", "rtl-simulation": "row replays in Icarus", "fpga": "not produced by this demo"},
        },
        "invariants": [
            {"id": "the_model_is_three_by_twelve", "condition": "3 * 12 == 36"},
            {"id": "raw_payload_bytes_follow_the_codec_geometry", "condition": "dense5 8 bytes, baseline2 9 bytes for 36 trits"},
            {"id": "synthetic_accumulators_are_amplitude_times_samples", "condition": "480 == 40 * 12, 360 == 30 * 12"},
            {"id": "predictions_and_rtl_rows_count_every_fixture_in_every_mode", "condition": "12 == 6 * 2, 36 == 6 * 2 * 3"},
            {"id": "the_rtl_seed_leaves_room_for_three_rows", "condition": "seed + 2 <= 2^32 - 1"},
        ],
        "vectors": vectors,
    }


# ---------------------------------------------------------------------------
# specs/formats/*.t27 -- external ternary weight-packing formats
#
# Test oracle only: the byte layouts below are restated from the upstream
# loops each spec restates (the same commits and lines), independently of
# t27/formats.t27 and of the specs' reference functions. The product readers
# and writers are the generated t27; tests/native_spec_formats.c,
# tests/spec_formats_wasm_replay.mjs and tests/test_spec_formats.py check that
# the spec, the implementation (C and WASM) and these vectors agree.

FORMATS_OUTPUTS = {family: ROOT / "conformance" / f"formats_{family}.json"
                   for family in ("llama_cpp", "prismml", "bitnet_cpp", "hf_bitnet", "mlx", "onnx")}
FORMATS_CREATED = "2026-09-23T00:00:00Z"
UPSTREAM_LOCK = ROOT / "specs" / "formats" / "upstream.lock.json"

FORMAT_ERRORS = {"format": -50, "length": -51, "capacity": -52, "code": -53, "padding": -54,
                 "truncated": -55, "container": -56, "not_found": -57, "scale_nonfinite": -58,
                 "type_ambiguous": -59, "layout_unsupported": -60, "misaligned": -61, "extent": -62}
FORMAT_FLAGS = {"outside_ternary": 0, "noncanonical_base3": 1, "scale_negative": 2, "scale_zero": 3,
                "trailer_nonzero": 4, "padding_nonzero": 5, "affine_not_ternary": 6}
FORMAT_SILENT = {
    "group_size_mismatch": "group-128 bytes read as group-64 Q2_0 (synthetic); every byte is a valid code",
    "i2s_layout_arm": "I2_S bytes in the ARM layout of bitnet.cpp's src/ggml-bitnet-mad.cpp (not compiled at the "
                      "pin) read as the x86 ACT_PARALLEL layout",
    "i2s_layout_1x4": "I2_S bytes in the four-row layout of bitnet.cpp's src/ggml-bitnet-mad.cpp (x86 without "
                      "ACT_PARALLEL, not compiled at the pin) read as ACT_PARALLEL",
    "i2s_layout_consecutive": "I2_S bytes of the llama.cpp submodule's quantize_i2_s (four consecutive weights per "
                              "byte) read as ACT_PARALLEL",
    "q1_0_payload": "a corrupted Q1_0 byte; every bit pattern is a valid pair of +-1 values",
    "payload_corrupted": "a corrupted code byte that still holds valid codes decodes to other weights",
    "scale_corrupted": "a corrupted scale that stays finite and positive rescales its block",
}
# "Silent output" view of a negative vector: what each upstream reader does
# with the same bytes. behaviour: decodes (accepts them; values_hex, when
# present, is its output before scaling), rejects, not_applicable (the input
# cannot reach that upstream code, e.g. codes an encoder never receives) or
# unknown (the reading code was not found at the pin: cite holds "UNKNOWN").
# Every cite is "path:line" or "path:first-last" in a file pinned for that
# upstream in specs/formats/upstream.lock.json.
BEHAVIOURS = ("decodes", "rejects", "not_applicable", "unknown")
CITE = re.compile(r"^[A-Za-z0-9_./-]+:[0-9]+(-[0-9]+)?$")
FORMAT_IDS = {"TQ1_0": 1, "TQ2_0": 2, "Q2_0": 3, "Q1_0": 4, "PQ2_0": 5, "PTQ1_0": 6,
              "I2_S": 7, "HF_PACKED": 8, "LINEAR2": 9, "ONNX2": 10}
BLOCKS = {  # weights, bytes, scale offset, ggml type, bits per weight
    "TQ1_0": (256, 54, 52, 34, "27/16"), "TQ2_0": (256, 66, 64, 35, "33/16"),
    "Q2_0": (64, 18, 0, 42, "9/4"), "Q1_0": (128, 18, 0, 41, "9/8"),
    "PQ2_0": (128, 34, 0, 142, "17/8"), "PTQ1_0": (128, 28, 26, 143, "7/4"),
}
KIND_BITS = {"F16": 1, "BF16": 2, "F32": 3}

# Real bytes cut from the fixture cache (fixtures/manifest.lock.json ranges;
# begin/end are file offsets). The first 128 weights of Ternary Bonsai 2 27B
# blk.0.ffn_down agree in all four Bonsai files.
BONSAI_GGUF = "prism-ml/Ternary-Bonsai-2-27B-gguf@6ed5e12bf84b7a63069882c91dd9e9218647d17b"
BONSAI_DEV = "prism-ml/Ternary-Bonsai-2-27B-gguf-dev@2a263ef827a2e215f3ddd14c9871a5bd1800fcbc"
BONSAI_MLX = "prism-ml/Ternary-Bonsai-2-27B-mlx-2bit@fcba37d2117a7077eac6b613b2668d14d9779edd"
BITNET_GGUF = "microsoft/bitnet-b1.58-2B-4T-gguf@a1f2f1c765812aa8af3f6eda4a313707064bba15"
BITNET_PACKED = "microsoft/bitnet-b1.58-2B-4T@04c3b9ad9361b824064a1f25ea60a8be9599b127"
REAL = {
    "bonsai_q2_0": (BONSAI_DEV, "Ternary-Bonsai-2-27B-Q2_0-prism-fork-required.gguf", "blk.0.ffn_down.weight", 749916512,
                    "e521525069454a1a06061486260248a01016e5218a092240a25951a48a98054545502902"),
    "bonsai_pq2_0": (BONSAI_GGUF, "Ternary-Bonsai-2-27B-PQ2_0.gguf", "blk.0.ffn_down.weight", 708874592,
                     "e521525069454a1a06061486260248a010168a092240a25951a48a980545455029027d224508a529642690186182010588aa040560aa11a5258565166829089a20414501"),
    "bonsai_ptq1_0": (BONSAI_GGUF, "Ternary-Bonsai-2-27B-PTQ1_0.gguf", "blk.0.ffn_down.weight", 585748832,
                      "e64b5f784d45796fa4d1c356a8750457df18c8dc59de99a6a2abe5215f794e8355d0421c6059c9e464f2ca03728cf926a08c33b35f727d22"),
    "bonsai_mlx_weight": (BONSAI_MLX, "model.safetensors", "language_model.model.layers.0.mlp.down_proj.weight", 7016633766,
                          "525069454a1a06061486260248a010168a092240a25951a48a98054545502902"),
    "bonsai_mlx_scale": (BONSAI_MLX, "model.safetensors", "language_model.model.layers.0.mlp.down_proj.scales", 2424018438, "e521"),
    "bonsai_mlx_bias": (BONSAI_MLX, "model.safetensors", "language_model.model.layers.0.mlp.down_proj.biases", 7862603622, "e5a1"),
    "bitnet_i2s_block": (BITNET_GGUF, "ggml-model-i2_s.gguf", "blk.0.ffn_down.weight", 665032320,
                         "2569a1164022a956416819a89212519660141580501a450a0998050966166a64"),
    "bitnet_i2s_tail": (BITNET_GGUF, "ggml-model-i2_s.gguf", "blk.0.ffn_down.weight", 665032320 + 6912 * 2560 // 4,
                        "3c710a404db9d63905b8a53f6fbedc340d3920b9b8b9753d1ab099ace9b98b3f"),
    "bitnet_hf_row0": (BITNET_PACKED, "model.safetensors", "model.layers.0.self_attn.q_proj.weight", 672931828,
                       "5541555055545545414055416a455551"),
}


def real_bytes(key):
    return bytes.fromhex(REAL[key][4])


def real_source(key):
    repo_rev, file, tensor, begin, data = REAL[key]
    repo, revision = repo_rev.split("@")
    raw = bytes.fromhex(data)
    return {"repo": repo, "revision": revision, "file": file, "tensor": tensor,
            "begin": begin, "end": begin + len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


class Lcg:
    """The harnesses' generator: state = state * 1664525 + 1013904223, value = state >> 8."""

    def __init__(self, seed):
        self.state = seed

    def next(self):
        self.state = (self.state * 1664525 + 1013904223) & 0xFFFFFFFF
        return self.state >> 8

    def trits(self, count, low=-1, high=1):
        return [low + self.next() % (high - low + 1) for _ in range(count)]

    def octets(self, count):
        return bytes(self.next() & 255 for _ in range(count))


def i8_hex(values):
    return bytes(v & 255 for v in values).hex()


def scale_class(word, kind):
    """0 positive, 1 zero, 2 negative, or the scale_nonfinite status."""
    if kind == "F32":
        exponent, top, magnitude, sign = (word >> 23) & 255, 255, word & 0x7FFFFFFF, word >> 31
    elif kind == "BF16":
        exponent, top, magnitude, sign = (word >> 7) & 255, 255, word & 0x7FFF, (word >> 15) & 1
    else:
        exponent, top, magnitude, sign = (word >> 10) & 31, 31, word & 0x7FFF, (word >> 15) & 1
    if exponent == top:
        return FORMAT_ERRORS["scale_nonfinite"]
    return 1 if magnitude == 0 else (2 if sign else 0)


def view(upstream, behaviour, cite, note, **values):
    assert behaviour in BEHAVIOURS, behaviour
    entry = {"upstream": upstream, "behaviour": behaviour, "cite": [cite] if isinstance(cite, str) else list(cite),
             "note": note}
    entry.update(values)
    return entry


def rejected(vector, *views):
    """A strict reader refuses the input: kind reject, the reader's own kind in `reader`."""
    assert "error_class" in vector and views, vector["id"]
    vector["reader"] = vector.pop("kind")
    vector["kind"] = "reject"
    vector["silent_output"] = list(views)
    return vector


def viewed(vector, *views):
    """A flagged or silent vector: the upstream readers' output of the same bytes."""
    assert ("flag_class" in vector or "silent_class" in vector) and views, vector["id"]
    vector["silent_output"] = list(views)
    return vector


def check_views(vectors, upstream_keys, lock):
    for vector in vectors:
        negative = any(key in vector for key in ("error_class", "flag_class", "silent_class"))
        assert ("error_class" in vector) == (vector["kind"] == "reject"), vector["id"]
        assert negative == ("silent_output" in vector), vector["id"]
        for entry in vector.get("silent_output", []):
            assert entry["upstream"] in upstream_keys, (vector["id"], entry["upstream"])
            for cite in entry["cite"]:
                if cite == "UNKNOWN":
                    continue
                assert CITE.match(cite), (vector["id"], cite)
                assert cite.split(":")[0] in lock[entry["upstream"]]["files"], (vector["id"], cite)
            assert (entry["behaviour"] == "unknown") == ("UNKNOWN" in entry["cite"]), vector["id"]


def empty_flags():
    return {token: 0 for token in FORMAT_FLAGS}


def add_scale_flags(flags, words, kind):
    for word in words:
        cls = scale_class(word, kind)
        flags["scale_zero"] += cls == 1
        flags["scale_negative"] += cls == 2


# ---- llama.cpp and PrismML blocks (upstream quantizer and dequantizer loops)

def b3_group(digits_values, shift=False):
    q = 0
    for value in digits_values:
        q = q * 3 + value + 1
    if shift:
        q *= 3
    return (q * 256 + 242) // 243


def b3_trit(byte, n):
    q = (byte * 3 ** n) & 255
    return ((q * 3) >> 8) - 1


def b3_stages(qs_bytes):
    """Byte runs of five-digit groups: 32 then 16 (TQ1_0); 16 then 8 (PTQ1_0)."""
    runs, j = [], 0
    for c in (32, 16, 8):
        while j + c <= qs_bytes:
            runs.append((j, c))
            j += c
    return runs


def encode_block(fmt, values, scale_word):
    per, size, scale_at = BLOCKS[fmt][:3]
    out = bytearray(size)
    out[scale_at:scale_at + 2] = scale_word.to_bytes(2, "little")
    if fmt in ("TQ1_0", "PTQ1_0"):
        qs, qh = (48, 4) if fmt == "TQ1_0" else (24, 2)
        x = 0
        for j, c in b3_stages(qs):
            for m in range(c):
                out[j + m] = b3_group([values[x + m + n * c] for n in range(5)])
            x += 5 * c
        for j in range(qh):
            out[qs + j] = b3_group([values[x + j + m * qh] for m in range(4)], shift=True)
    elif fmt == "TQ2_0":
        for j in range(0, 64, 32):
            for m in range(32):
                out[j + m] = sum(((values[4 * j + m + n * 32] + 1) & 3) << (2 * n) for n in range(4))
    elif fmt == "Q1_0":
        for j in range(128):
            if values[j] == 1:
                out[2 + j // 8] |= 1 << (j % 8)
    else:  # Q2_0, PQ2_0
        for j in range(per):
            out[2 + j // 4] |= (values[j] + 1) << (2 * (j % 4))
    return bytes(out)


def decode_block(fmt, block):
    if fmt in ("TQ1_0", "PTQ1_0"):
        qs, qh = (48, 4) if fmt == "TQ1_0" else (24, 2)
        y = []
        for j, c in b3_stages(qs):
            for n in range(5):
                y += [b3_trit(block[j + m], n) for m in range(c)]
        for n in range(4):
            y += [b3_trit(block[qs + j], n) for j in range(qh)]
        return y
    if fmt == "TQ2_0":
        return [((block[j + m] >> (2 * l)) & 3) - 1 for j in (0, 32) for l in range(4) for m in range(32)]
    if fmt == "Q1_0":
        return [1 if (block[2 + j // 8] >> (j % 8)) & 1 else -1 for j in range(128)]
    per = BLOCKS[fmt][0]
    return [((block[2 + j // 4] >> (2 * (j % 4))) & 3) - 1 for j in range(per)]


CANONICAL_QS = {(q * 256 + 242) // 243 for q in range(243)}
CANONICAL_QH = {(3 * q * 256 + 242) // 243 for q in range(81)}


def blocks_expect(fmt, data, count):
    per, size, scale_at = BLOCKS[fmt][:3]
    if count % per or len(data) != count // per * size:
        return {"status": FORMAT_ERRORS["length"]}
    words = [int.from_bytes(data[b * size + scale_at:b * size + scale_at + 2], "little") for b in range(count // per)]
    if any(scale_class(w, "F16") < 0 for w in words):
        return {"status": FORMAT_ERRORS["scale_nonfinite"]}
    if fmt in ("TQ1_0", "PTQ1_0"):
        qs, qh = (48, 4) if fmt == "TQ1_0" else (24, 2)
        if any(b3_trit(data[b * size + qs + j], 4) != -1 for b in range(count // per) for j in range(qh)):
            return {"status": FORMAT_ERRORS["padding"]}
    values = []
    flags = empty_flags()
    for b in range(count // per):
        block = data[b * size:(b + 1) * size]
        values += decode_block(fmt, block)
        if fmt in ("TQ1_0", "PTQ1_0"):
            qs, qh = (48, 4) if fmt == "TQ1_0" else (24, 2)
            flags["noncanonical_base3"] += sum(byte not in CANONICAL_QS for byte in block[:qs])
            flags["noncanonical_base3"] += sum(byte not in CANONICAL_QH for byte in block[qs:qs + qh])
    flags["outside_ternary"] = sum(v > 1 for v in values)
    add_scale_flags(flags, words, "F16")
    return {"status": flags["outside_ternary"], "values_hex": i8_hex(values), "scale_words": words, "flags": flags}


def check_classes(vector, *statuses):
    """The declared class must be what the expectation shows."""
    expect = vector["expect"]
    if "error_class" in vector:
        assert FORMAT_ERRORS[vector["error_class"]] in statuses, vector["id"]
    elif "flag_class" in vector:
        assert expect["status"] >= 0 and expect["flags"][vector["flag_class"]] > 0, vector["id"]
    else:
        assert all(status >= 0 for status in statuses), vector["id"]
    return vector


def corruption_intended(fmt, original, count, source=None):
    """The bytes before corruption (with their provenance) and what they decode to."""
    expect = blocks_expect(fmt, original, count)
    intended = {"format": fmt, "count": count, "data_hex": original.hex(), "values_hex": expect["values_hex"],
                "scale_words": expect["scale_words"]}
    if source:
        intended["source"] = source
    return intended


def block_vector(identifier, fmt, data, count, description, encode=False, source=None, **extra):
    vector = {"id": identifier, "kind": "block", "format": fmt, "count": count, "data_hex": data.hex(),
              "expect": blocks_expect(fmt, data, count), "encode": encode, "description": description}
    if source:
        vector["source"] = source
    vector.update(extra)
    return check_classes(vector, vector["expect"]["status"])


def encode_blocks(fmt, values, words):
    per = BLOCKS[fmt][0]
    return b"".join(encode_block(fmt, values[b * per:(b + 1) * per], words[b]) for b in range(len(words)))


# Upstream code for the block formats: (lock key, dequantizer, quantizer,
# ggml_validate_row_data case). The fork's Q2_0 has its own lines.
BLOCK_UPSTREAM = {
    "TQ1_0": ("llama.cpp", "ggml/src/ggml-quants.c:2428-2465", "ggml/src/ggml-quants.c:2316-2380",
              "ggml/src/ggml-quants.c:5570-5573"),
    "TQ2_0": ("llama.cpp", "ggml/src/ggml-quants.c:2467-2484", "ggml/src/ggml-quants.c:2382-2412",
              "ggml/src/ggml-quants.c:5574-5577"),
    "Q2_0": ("llama.cpp", "ggml/src/ggml-quants.c:439-457", "ggml/src/ggml-quants.c:74-110",
             "ggml/src/ggml-quants.c:5507-5510"),
    "Q1_0": ("llama.cpp", "ggml/src/ggml-quants.c:419-437", "ggml/src/ggml-quants.c:40-72",
             "ggml/src/ggml-quants.c:5503-5506"),
    "PQ2_0": ("prismml", "ggml/src/ggml-quants.c:494-512", "ggml/src/ggml-quants.c:113-145",
              "ggml/src/ggml-quants.c:5711-5714"),
    "PTQ1_0": ("prismml", "ggml/src/ggml-quants.c:2255-2285", "ggml/src/ggml-quants.c:2205-2253",
               "ggml/src/ggml-quants.c:5715-5718"),
    "prism Q2_0": ("prismml", "ggml/src/ggml-quants.c:474-492", "ggml/src/ggml-quants.c:74-110",
                   "ggml/src/ggml-quants.c:5707-5710"),
}
# qh padding digit: the quantizer multiplies by 3 once more; the dequantizer reads digits 0..3.
B3_PADDING = {"TQ1_0": ("ggml/src/ggml-quants.c:2372-2373", "ggml/src/ggml-quants.c:2457-2462"),
              "PTQ1_0": ("ggml/src/ggml-quants.c:2246-2248", "ggml/src/ggml-quants.c:2277-2283")}
# gguf.cpp and the model loader: type id range, row length in whole blocks,
# offsets equal to the padded end of the previous tensor, data within the file.
GGUF_UPSTREAM = {
    "llama.cpp": ("ggml/src/gguf.cpp:722-727", "ggml/src/gguf.cpp:732-738", "ggml/src/gguf.cpp:787-793",
                  "src/llama-model-loader.h:46-48"),
    "prismml": ("ggml/src/gguf.cpp:714-719", "ggml/src/gguf.cpp:724-730", "ggml/src/gguf.cpp:781-787",
                "src/llama-model-loader.h:46-48"),
    "bitnet.cpp-llama.cpp": ("ggml/src/gguf.cpp:701-706", "ggml/src/gguf.cpp:711-717", "ggml/src/gguf.cpp:766-772",
                             "src/llama-model-loader.h:46-48"),
}


def block_upstream(fmt, family):
    return BLOCK_UPSTREAM["prism Q2_0" if (fmt, family) == ("Q2_0", "prismml") else fmt]


def block_view(fmt, family, note, **values):
    key, dequant = block_upstream(fmt, family)[:2]
    return view(key, "decodes", dequant, note, **values)


def silent_values(fmt, data, count):
    """Trits of the upstream dequantizer loop (padding digits and scales ignored) and the raw scale words."""
    per, size, scale_at = BLOCKS[fmt][:3]
    words, values = [], []
    for b in range(count // per):
        block = data[b * size:(b + 1) * size]
        words.append(int.from_bytes(block[scale_at:scale_at + 2], "little"))
        values += decode_block(fmt, block)
    return {"values_hex": i8_hex(values), "scale_words": words}


def block_family_vectors(formats, seed, family):
    rng = Lcg(seed)
    vectors = []
    for fmt in formats:
        per, size = BLOCKS[fmt][:2]
        key, dequant, quant, validate = block_upstream(fmt, family)
        gguf_type, gguf_row, gguf_offset, loader = GGUF_UPSTREAM[key]
        low = -1
        high = 1
        values = rng.trits(2 * per, low, high)
        if fmt == "Q1_0":
            values = [1 if v >= 0 else -1 for v in values]
        words = [0x3C00, 0x2E66]
        data = encode_blocks(fmt, values, words)
        vectors.append(block_vector(f"{fmt.lower()}_random_two_blocks", fmt, data, 2 * per,
                                    f"Two {fmt} blocks of deterministic ternary codes; the encoder must reproduce the bytes",
                                    encode=True))
        # Every code a byte position can hold, in the upstream order.
        if fmt in ("TQ1_0", "PTQ1_0"):
            groups = list(range(243))
            qs = 48 if fmt == "TQ1_0" else 24
            blocks = (243 + qs - 1) // qs
            data = bytearray()
            for b in range(blocks):
                block = bytearray(size)
                for j in range(qs):
                    block[j] = (groups[(b * qs + j) % 243] * 256 + 242) // 243
                for j in range(qs, size - 2):
                    block[j] = (3 * ((b * qs + j) % 81) * 256 + 242) // 243
                block[size - 2:] = (0x3800).to_bytes(2, "little")
                data += block
            vectors.append(block_vector(f"{fmt.lower()}_all_base3_groups", fmt, bytes(data), blocks * per,
                                        f"All 243 canonical qs bytes and canonical qh bytes of {fmt}; the encoder reproduces them",
                                        encode=True))
            bad = bytearray(encode_blocks(fmt, [0] * per, [0x3C00]))
            bad[0], bad[1], bad[qs] = 1, 237, 1
            vectors.append(viewed(block_vector(f"{fmt.lower()}_noncanonical_bytes", fmt, bytes(bad), per,
                                               "Bytes the ceiling rule never writes (qs 1 and 237, qh 1, whose padding "
                                               "digit is 0) decode silently and are flagged",
                                               flag_class="noncanonical_base3"),
                                  block_view(fmt, family, "decoded like the canonical byte of the same digits")))
            # A qh byte whose fifth (padding) digit is 1: the dequantizer
            # reads digits 0..3 only, so it yields the intended weights.
            padded = bytearray(encode_blocks(fmt, values[:per], [0x3C00]))
            w = 0
            for n in range(4):
                w = w * 3 + values[per - 4 * (qs // 12) + n * (qs // 12)] + 1
            assert padded[qs] == (3 * w * 256 + 242) // 243
            padded[qs] = ((3 * w + 1) * 256 + 242) // 243
            quant_line, dequant_line = B3_PADDING[fmt]
            vector = block_vector(f"{fmt.lower()}_qh_padding_digit", fmt, bytes(padded), per,
                                  "qh byte 0 carries a nonzero fifth digit, which the quantizer always writes as 0 "
                                  "and the dequantizer never reads", error_class="padding")
            vectors.append(rejected(vector, view(key, "decodes", [dequant_line, quant_line],
                                                 "digits 0..3 decode; the padding digit is ignored",
                                                 **silent_values(fmt, bytes(padded), per))))
            assert vector["silent_output"][0]["values_hex"] == i8_hex(values[:per])
        if fmt in ("TQ2_0", "Q2_0", "PQ2_0"):
            plus = [0] * per
            plus[0], plus[per - 1] = 2, 2
            data = encode_blocks(fmt, plus, [0x3C00])
            vectors.append(viewed(block_vector(f"{fmt.lower()}_code3_is_plus2", fmt, data, per,
                                               "Code 3 decodes to +2 upstream; the reader keeps the value and flags it",
                                               encode=True, flag_class="outside_ternary"),
                                  block_view(fmt, family, "code 3 is (3 - 1) * d = +2d")))
        data = encode_blocks(fmt, values[:per], [0xB800])
        vectors.append(viewed(block_vector(f"{fmt.lower()}_negative_scale", fmt, data, per,
                                           "A negative scale is accepted upstream (signs flip); flagged", encode=True,
                                           flag_class="scale_negative"),
                              block_view(fmt, family, "every weight is its trit times the negative d")))
        data = encode_blocks(fmt, values[:per], [0x8000])
        vectors.append(viewed(block_vector(f"{fmt.lower()}_zero_scale", fmt, data, per,
                                           "A zero scale (here -0) makes every weight 0 upstream; flagged", encode=True,
                                           flag_class="scale_zero"),
                              block_view(fmt, family, "every weight is its trit times 0")))
        for word, name in ((0x7E00, "nan"), (0x7C00, "inf"), (0xFC00, "negative_inf")):
            data = encode_blocks(fmt, values, [0x3C00, word])
            vector = block_vector(f"{fmt.lower()}_scale_{name}", fmt, data, 2 * per,
                                  f"Block 2 scale 0x{word:04x}: ggml_validate_row_data rejects it with --check-tensors; decoding without the check gives non-finite weights",
                                  error_class="scale_nonfinite")
            load_check = [validate] + (["src/llama-model-loader.cpp:1486-1488"] if key == "llama.cpp" else [])
            vectors.append(rejected(vector,
                                    block_view(fmt, family, "the default load path multiplies the trits by the "
                                               "non-finite d", **silent_values(fmt, data, 2 * per)),
                                    view(key, "rejects", load_check, "only when the model is loaded with --check-tensors")))
        data = encode_blocks(fmt, values, words)
        vectors.append(rejected(block_vector(f"{fmt.lower()}_truncated", fmt, data[:-1], 2 * per,
                                             "One byte short of two blocks", error_class="length"),
                                view(key, "rejects", loader, "a tensor whose bytes pass the end of the file is refused")))
        vectors.append(rejected(block_vector(f"{fmt.lower()}_partial_block", fmt, data, 2 * per - 1,
                                             "A weight count that is not whole blocks", error_class="length"),
                                view(key, "rejects", gguf_row, "a row that is not whole blocks is refused")))
        bad_value = 2 if fmt in ("TQ1_0", "PTQ1_0") else (0 if fmt == "Q1_0" else 3)
        codes = list(values[:per])
        codes[per // 2] = bad_value
        vectors.append(rejected({"id": f"{fmt.lower()}_encode_rejects_{'zero' if bad_value == 0 else 'value_' + str(bad_value)}",
                                 "kind": "block_encode", "format": fmt, "count": per, "values_hex": i8_hex(codes),
                                 "scale_words": [0x3C00], "error_class": "code", "expect": {"status": FORMAT_ERRORS["code"]},
                                 "description": f"{fmt} cannot store the value {bad_value}; nothing is written"},
                                view(key, "not_applicable", quant,
                                     "the quantizer derives codes from float weights and never produces this value")))
    return vectors


# ---- GGUF headers for the type-id and offset rules

def gguf_bytes(arch, prism, tensors, alignment=None):
    """tensors: (name, ne list, ggml type, offset)."""
    def text(value):
        raw = value.encode()
        return struct.pack("<Q", len(raw)) + raw
    kv = [(b"general.architecture", 8, text(arch))]
    if alignment is not None:
        kv.append((b"general.alignment", 4, struct.pack("<I", alignment)))
    if prism:
        kv.append((b"prism.hadamard.block_size", 4, struct.pack("<I", 1024)))
    out = bytearray(struct.pack("<IIQQ", 0x46554747, 3, len(tensors), len(kv)))
    for key, vtype, value in kv:
        out += struct.pack("<Q", len(key)) + key + struct.pack("<I", vtype) + value
    for name, ne, ggml_type, offset in tensors:
        out += text(name) + struct.pack("<I", len(ne)) + b"".join(struct.pack("<Q", d) for d in ne)
        out += struct.pack("<IQ", ggml_type, offset)
    return bytes(out)


def gguf_format(ggml_type, prism, bitnet):
    if ggml_type in (34, 35, 41):
        return {34: "TQ1_0", 35: "TQ2_0", 41: "Q1_0"}[ggml_type]
    if ggml_type in (36, 38, 42, 142, 143) and prism and bitnet:
        return "type_ambiguous"
    if ggml_type == 36:
        return "I2_S" if bitnet else None
    if ggml_type in (38, 42) and bitnet:
        return "layout_unsupported"
    if ggml_type == 42:
        return "Q2_0"
    if prism and ggml_type in (142, 143):
        return {142: "PQ2_0", 143: "PTQ1_0"}[ggml_type]
    return None


INT64_MAX = (1 << 63) - 1


def gguf_elements(ne):
    """gguf.cpp:684-711: every ne a non-negative int64 and a nonzero product below INT64_MAX; None if refused."""
    ne = list(ne) + [1] * (4 - len(ne))
    if any(d > INT64_MAX for d in ne):
        return None
    if 0 in ne:
        return 0
    if (INT64_MAX // ne[1] <= ne[0] or INT64_MAX // ne[2] <= ne[0] * ne[1]
            or INT64_MAX // ne[3] <= ne[0] * ne[1] * ne[2]):
        return None
    return ne[0] * ne[1] * ne[2] * ne[3]


def gguf_layout_bytes(fmt, ne0, count):
    if fmt == "I2_S":
        return count // 4 + 32
    per, block = BLOCKS[fmt][:2]
    return 0 if ne0 % per else count // per * block


def gguf_record_bytes(ggml_type, ne, prism, bitnet):
    """Bytes of a record the reader knows: ternary layouts, F32 (0), F16 (1), BF16 (30); 0 otherwise."""
    count = gguf_elements(ne)
    if not count:
        return 0
    if ggml_type == 0:
        return count * 4
    if ggml_type in (1, 30):
        return count * 2
    fmt = gguf_format(ggml_type, prism, bitnet)
    if fmt is None or fmt in FORMAT_ERRORS:
        return 0
    return gguf_layout_bytes(fmt, ne[0], count)


def gguf_vector(identifier, arch, prism, tensors, target, description, alignment=None, data_bytes=None, views=()):
    """The file is data_start + data_bytes long; data_bytes defaults to the highest offset + 1024."""
    data = gguf_bytes(arch, prism, tensors, alignment)
    align = alignment or 32
    data_start = -(-len(data) // align) * align
    if data_bytes is None:
        data_bytes = max(t[3] for t in tensors) + 1024
    file_size = data_start + data_bytes
    bitnet = arch == "bitnet-b1.58" or any(t[2] in (36, 38) for t in tensors)
    index = next(i for i, t in enumerate(tensors) if t[0] == target)
    name, ne, ggml_type, offset = tensors[index]
    # Other records that hold at least one weight bound the extent: from above
    # the least offset at or above this one, from below the greatest end of a
    # record of known size that begins below it.
    holding = [t for i, t in enumerate(tensors) if i != index and gguf_elements(t[1]) != 0]
    others = [t[3] for t in holding if t[3] >= offset]
    has_next = bool(others)
    next_offset = min(others) if others else 0
    ends = [t[3] + gguf_record_bytes(t[2], t[1], prism, bitnet) for t in holding
            if t[3] < offset and gguf_record_bytes(t[2], t[1], prism, bitnet)]
    has_prev = bool(ends)
    prev_end = min(max(ends), (1 << 64) - 1) if ends else 0
    resolved = gguf_format(ggml_type, prism, bitnet)
    count = gguf_elements(ne)
    expect = {"find": 0, "ggml_type": ggml_type, "prism": prism, "bitnet": bitnet, "offset": offset,
              "alignment": align, "data_start": data_start, "has_next": has_next, "next_offset": next_offset,
              "has_prev": has_prev, "prev_end": prev_end}
    if resolved in FORMAT_ERRORS:
        expect["check"], error_class = FORMAT_ERRORS[resolved], resolved
    elif offset % align:
        expect["check"], error_class = FORMAT_ERRORS["misaligned"], "misaligned"
    elif resolved is None:
        expect["check"], error_class = 0, None
    elif count is None:
        expect["check"], error_class = FORMAT_ERRORS["length"], "length"
    else:
        size = gguf_layout_bytes(resolved, ne[0], count)
        expect["tensor_bytes"] = size
        if size == 0:
            expect["check"], error_class = FORMAT_ERRORS["length"], "length"
        elif ((has_next and offset + size > next_offset) or (has_prev and prev_end > offset)
              or data_start + offset + size > file_size):
            expect["check"], error_class = FORMAT_ERRORS["extent"], "extent"
        else:
            expect["check"], error_class = FORMAT_IDS[resolved], None
    vector = {"id": identifier, "kind": "gguf", "gguf_hex": data.hex(), "tensor": target, "file_size": file_size,
              "expect": expect, "description": description}
    if error_class:
        vector["error_class"] = error_class
        return rejected(vector, *views)
    assert not views, identifier
    return vector


def gguf_vectors():
    q2 = 256 // 64 * 18 * 2
    org_type, org_row, org_offset, org_loader = GGUF_UPSTREAM["llama.cpp"]
    bit_type, bit_row, bit_offset, bit_loader = GGUF_UPSTREAM["bitnet.cpp-llama.cpp"]
    pr_type = GGUF_UPSTREAM["prismml"][0]
    contiguous = view("llama.cpp", "rejects", org_offset, "every offset must equal the padded end of the previous tensor")
    tl2 = view("bitnet.cpp-llama.cpp", "decodes", ["ggml/include/ggml.h:433", "ggml/src/ggml.c:1314-1321"],
               "bitnet.cpp reads id 42 as TL2, a lookup-table layout of other size")
    q2_org = view("llama.cpp", "decodes", ["ggml/include/ggml.h:432", "ggml/src/ggml-quants.c:439-457"],
                  "ggml-org reads id 42 as group-64 Q2_0")
    return [
        gguf_vector("gguf_q2_0_plain", "llama", False, [("a.weight", [256, 2], 42, 0), ("b.weight", [256], 0, 160)],
                    "a.weight", "Id 42 without namespace markers is ggml-org Q2_0 (group 64)"),
        gguf_vector("gguf_prism_q2_0_stays_group_64", "qwen35", True, [("a.weight", [256, 2], 42, 0)], "a.weight",
                    "The PrismML fork keeps id 42 as group-64 Q2_0"),
        gguf_vector("gguf_prism_pq2_0", "qwen35", True, [("a.weight", [256, 2], 142, 0)], "a.weight",
                    "Id 142 is PQ2_0 in a file with prism.* keys"),
        gguf_vector("gguf_prism_ptq1_0", "qwen35", True, [("a.weight", [256, 2], 143, 0)], "a.weight",
                    "Id 143 is PTQ1_0 in a file with prism.* keys"),
        gguf_vector("gguf_143_without_prism_keys", "llama", False, [("a.weight", [256, 2], 143, 0)], "a.weight",
                    "Id 143 without prism.* keys is not a ternary layout of that namespace"),
        gguf_vector("gguf_bitnet_i2_s", "bitnet-b1.58", False, [("a.weight", [256, 2], 36, 0)], "a.weight",
                    "Id 36 is I2_S in bitnet.cpp files; its bytes are n/4 + 32"),
        gguf_vector("gguf_bitnet_42_is_tl2", "bitnet-b1.58", False, [("a.weight", [256, 2], 42, 0)], "a.weight",
                    "In bitnet.cpp id 42 is TL2, a lookup-table layout without a storage contract",
                    views=(tl2, q2_org)),
        gguf_vector("gguf_42_marked_by_an_i2_s_tensor", "llama", False,
                    [("a.weight", [256, 2], 42, 0), ("b.weight", [256], 36, 160)], "a.weight",
                    "A type-36 tensor marks the file as bitnet.cpp, so id 42 is TL2",
                    views=(tl2, view("llama.cpp", "rejects", [org_row, "ggml/src/ggml.c:928-945"],
                                     "ids 36 to 38 are removed in ggml-org (block size 0), so the file is refused"))),
        gguf_vector("gguf_tl1", "bitnet-b1.58", False, [("a.weight", [256, 2], 38, 0)], "a.weight",
                    "Id 38 is TL1 in bitnet.cpp",
                    views=(view("bitnet.cpp-llama.cpp", "decodes", ["ggml/include/ggml.h:428", "ggml/src/ggml.c:1314-1321"],
                                "bitnet.cpp reads id 38 as TL1 with its generated kernels"),
                           view("llama.cpp", "rejects", [org_row, "ggml/src/ggml.c:928-945"],
                                "a removed id of block size 0 is refused"))),
        gguf_vector("gguf_42_with_both_markers", "bitnet-b1.58", True, [("a.weight", [256, 2], 42, 0)], "a.weight",
                    "prism.* keys and the bitnet-b1.58 architecture give id 42 two meanings",
                    views=(tl2, view("prismml", "decodes", ["ggml/include/ggml.h:432", "ggml/src/ggml-quants.c:474-492"],
                                     "the PrismML fork reads id 42 as group-64 Q2_0"))),
        gguf_vector("gguf_143_with_both_markers", "bitnet-b1.58", True, [("a.weight", [256, 2], 143, 0)], "a.weight",
                    "prism.* keys and the bitnet-b1.58 architecture give id 143 two meanings",
                    views=(view("prismml", "decodes", ["ggml/include/ggml.h:436", "ggml/src/ggml-quants.c:2255-2285"],
                                "the PrismML fork reads id 143 as PTQ1_0"),
                           view("bitnet.cpp-llama.cpp", "rejects", bit_type, "ids from 43 up are outside its type range"))),
        gguf_vector("gguf_tq2_0_in_any_namespace", "bitnet-b1.58", True, [("a.weight", [256, 2], 35, 0)], "a.weight",
                    "Ids 34, 35 and 41 mean the same layout in every namespace"),
        gguf_vector("gguf_offset_shifted_by_one", "llama", False,
                    [("a.weight", [256, 2], 42, 0), ("b.weight", [256], 42, 161)], "b.weight",
                    "A tensor offset one byte past its aligned position", views=(contiguous,)),
        gguf_vector("gguf_offset_alignment_64", "llama", False,
                    [("a.weight", [256, 2], 42, 0), ("b.weight", [256], 42, 160)], "b.weight",
                    "general.alignment 64: offset 160 is not aligned", alignment=64, views=(contiguous,)),
        gguf_vector("gguf_group_128_bytes_labelled_42", "llama", False,
                    [("a.weight", [1152], 42, 0), ("b.weight", [256], 0, 320)], "a.weight",
                    "1152 weights stored as group-128 PQ2_0 (306 bytes, next tensor at 320) but labelled id 42: "
                    "Q2_0 needs 324 bytes, so the tensor would run into the next one (synthetic)",
                    views=(contiguous,)),
        gguf_vector("gguf_overlapping_offsets", "llama", False,
                    [("a.weight", [256, 2], 42, 0), ("b.weight", [256, 2], 42, 0)], "b.weight",
                    "Two tensor records share offset 0: the tensors overlap", views=(contiguous,)),
        gguf_vector("gguf_offset_shifted_back_into_previous", "llama", False,
                    [("a.weight", [256, 2], 42, 0), ("b.weight", [256], 42, 96)], "a.weight",
                    "b.weight starts at 96, inside a.weight's 144 bytes", views=(contiguous,)),
        gguf_vector("gguf_shifted_record_read_itself", "llama", False,
                    [("a.weight", [256, 2], 42, 0), ("b.weight", [256], 42, 96)], "b.weight",
                    "The shifted record itself: b.weight at 96 begins inside a.weight's 144 bytes, and no record "
                    "follows it", views=(contiguous,)),
        gguf_vector("gguf_begins_inside_f32_record", "llama", False,
                    [("norm.weight", [64], 0, 0), ("a.weight", [256, 2], 42, 128)], "a.weight",
                    "a.weight at 128 begins inside the 256 bytes of the F32 record at 0", views=(contiguous,)),
        gguf_vector("gguf_zero_weight_record_shares_offset", "llama", False,
                    [("z.weight", [0], 0, 0), ("a.weight", [256, 2], 42, 0)], "a.weight",
                    "A zero-weight record holds no bytes (ggml_nbytes is 0), so the next record starts at the same "
                    "offset, as gguf.cpp's writer places it"),
        gguf_vector("gguf_zero_weight_record_between", "llama", False,
                    [("a.weight", [256, 2], 42, 0), ("z.weight", [0, 4], 42, 160), ("b.weight", [256, 2], 42, 160)],
                    "a.weight", "a.weight is bounded by b.weight at 160, not by the zero-weight record there"),
        gguf_vector("gguf_zero_weight_record_before", "llama", False,
                    [("a.weight", [256, 2], 42, 0), ("z.weight", [0, 4], 42, 160), ("b.weight", [256, 2], 42, 160)],
                    "b.weight", "b.weight shares its offset with a zero-weight record and follows a.weight's padded end"),
        gguf_vector("gguf_weight_count_not_representable", "llama", False,
                    [("a.weight", [64, (1 << 58) + 1, 64], 42, 0)], "a.weight",
                    "64 x (2^58 + 1) x 64 weights: the product does not fit in 64 bits and wraps to 4096 "
                    "(1152 bytes, which the file holds)", data_bytes=1152,
                    views=(view("llama.cpp", "rejects", "ggml/src/gguf.cpp:699-711",
                                "the total number of elements must be representable (below INT64_MAX)"),)),
        gguf_vector("gguf_weight_count_at_int64_max", "llama", False,
                    [("a.weight", [64, 1 << 57], 42, 0)], "a.weight",
                    "64 x 2^57 = 2^63 weights fit in a u64 but not below INT64_MAX", data_bytes=1152,
                    views=(view("llama.cpp", "rejects", "ggml/src/gguf.cpp:699-711",
                                "INT64_MAX / ne[1] <= ne[0]: the count is not representable"),)),
        gguf_vector("gguf_last_tensor_within_file", "llama", False,
                    [("a.weight", [1152], 42, 0)], "a.weight",
                    "The last tensor ends exactly at the end of the file", data_bytes=324),
        gguf_vector("gguf_last_tensor_past_end_of_file", "llama", False,
                    [("a.weight", [1152], 42, 0)], "a.weight",
                    "A truncated file: the last tensor needs 324 bytes, 323 remain",
                    data_bytes=323,
                    views=(view("llama.cpp", "rejects", org_loader, "tensor data not within the file bounds"),)),
        gguf_vector("gguf_last_tensor_shifted_by_alignment", "llama", False,
                    [("a.weight", [256, 2], 42, 0), ("b.weight", [256, 2], 42, 192)], "b.weight",
                    "b.weight shifted by one alignment unit (160 to 192) in a file that ends at its intended end",
                    data_bytes=160 + 144,
                    views=(contiguous, view("llama.cpp", "rejects", org_loader, "tensor data not within the file bounds"))),
        gguf_vector("gguf_row_not_whole_blocks", "llama", False, [("a.weight", [100, 2], 42, 0)], "a.weight",
                    "ne[0] = 100 is not a whole number of 64-weight blocks",
                    views=(view("llama.cpp", "rejects", org_row, "a row that is not whole blocks is refused"),)),
        gguf_vector("gguf_exact_fit", "llama", False, [("a.weight", [256, 2], 42, 0), ("b.weight", [256], 0, q2)],
                    "a.weight", "The next tensor starts exactly where this one ends"),
    ]


def formats_document(family, module, name, description, constants, invariants, vectors, upstream_keys):
    lock = json.loads(UPSTREAM_LOCK.read_text(encoding="utf-8"))["upstreams"]
    ids = [vector["id"] for vector in vectors]
    assert len(ids) == len(set(ids)), family
    check_views(vectors, upstream_keys, lock)
    base = {"errors": FORMAT_ERRORS, "flags": FORMAT_FLAGS, "silent": FORMAT_SILENT, "format_ids": FORMAT_IDS}
    base.update(constants)
    return {
        "module": module,
        "spec_path": f"specs/formats/{family}.t27",
        "schema_version": 2,
        "format_family": "Conformance",
        "vector_name": name,
        "description": description + " Expectations come from the upstream loops restated in "
                                     "tools/generate-spec-vectors.py (a test oracle), not from the native implementation.",
        "created_at": FORMATS_CREATED,
        "generator": "tools/generate-spec-vectors.py",
        "upstream": {key: {"repo": lock[key]["repo"], "commit": lock[key]["commit"],
                           "files": sorted(lock[key]["files"])} for key in upstream_keys},
        "constants": base,
        "invariants": invariants,
        "vectors": vectors,
    }


def block_constants(formats):
    return {"formats": {fmt: {"id": FORMAT_IDS[fmt], "ggml_type": BLOCKS[fmt][3], "block_weights": BLOCKS[fmt][0],
                              "block_bytes": BLOCKS[fmt][1], "scale_offset": BLOCKS[fmt][2], "scale": "F16",
                              "bits_per_weight": BLOCKS[fmt][4]} for fmt in formats}}


def build_formats_llama_cpp():
    vectors = block_family_vectors(("TQ1_0", "TQ2_0", "Q2_0", "Q1_0"), 2709, "llama_cpp")
    # Worked examples of the upstream quantizer: codes x0=1, x1=0.5, x2=-0.5,
    # x64=-1, x96=1 with d = 1.0.
    codes = [0] * 256
    codes[0], codes[1], codes[2], codes[64], codes[96] = 1, 1, -1, -1, 1
    for fmt, words in (("TQ1_0", [0x3C00]), ("TQ2_0", [0x3C00]), ("Q2_0", [0x3C00] * 4)):
        vectors.append(block_vector(f"{fmt.lower()}_quantizer_example", fmt, encode_blocks(fmt, codes, words), 256,
                                    "Codes of the upstream quantizer for x0=1, x1=0.5, x2=-0.5, x64=-1, x96=1 and d=1",
                                    encode=True))
    q1 = bytes([0x00, 0x34, 0xA5, 0x03] + [0] * 14)
    vectors.append(block_vector("q1_0_worked_example", "Q1_0", q1, 128,
                                "Q1_0 bits are read low bit first; a set bit is +d, a clear bit -d", encode=True))
    real = real_bytes("bonsai_q2_0")
    vectors.append(block_vector("q2_0_real_bonsai_blk0_ffn_down", "Q2_0", real, 128,
                                "The first two group-64 blocks of Ternary Bonsai 2 27B blk.0.ffn_down.weight (PrismML dev file)",
                                encode=True, source=real_source("bonsai_q2_0")))
    corrupt = bytearray(q1)
    corrupt[5] ^= 0xFF
    vector = block_vector("q1_0_corrupted_payload_decodes", "Q1_0", bytes(corrupt), 128,
                          "One Q1_0 byte inverted: eight weights change sign and nothing can detect it",
                          silent_class="q1_0_payload")
    vector["intended"] = corruption_intended("Q1_0", q1, 128)
    vectors.append(viewed(vector, block_view("Q1_0", "llama_cpp", "every bit is a sign; there is no invalid pattern")))
    # Corrupted real bytes that stay valid: a code byte and a scale.
    real = real_bytes("bonsai_q2_0")
    corrupt = bytearray(real)
    first = corrupt[2] & 3
    assert first < 3
    corrupt[2] = (corrupt[2] & 0xFC) | ((first + 1) % 3)
    vector = block_vector("q2_0_real_corrupted_code_byte", "Q2_0", bytes(corrupt), 128,
                          "Real Bonsai Q2_0 bytes with the first code changed to another ternary code: weight 0 "
                          "decodes to another trit and nothing can detect it", silent_class="payload_corrupted")
    vector["intended"] = corruption_intended("Q2_0", real, 128, real_source("bonsai_q2_0"))
    vectors.append(viewed(vector, block_view("Q2_0", "llama_cpp", "decodes the changed code")))
    corrupt = bytearray(real)
    corrupt[1] ^= 0x10
    vector = block_vector("q2_0_real_corrupted_scale", "Q2_0", bytes(corrupt), 128,
                          "Real Bonsai Q2_0 bytes with bit 12 of block 0's fp16 scale flipped: the scale stays finite "
                          "and positive, the trits are unchanged, block 0 is rescaled by 2^4 and nothing can detect it",
                          silent_class="scale_corrupted")
    vector["intended"] = corruption_intended("Q2_0", real, 128, real_source("bonsai_q2_0"))
    vectors.append(viewed(vector, block_view("Q2_0", "llama_cpp", "multiplies the trits by the changed d")))
    vectors += gguf_vectors()
    constants = block_constants(("TQ1_0", "TQ2_0", "Q2_0", "Q1_0"))
    constants["base3"] = {"qs_canonical": 243, "qh_canonical": 81,
                          "qs_noncanonical": sorted(set(range(256)) - CANONICAL_QS),
                          "qh_padding_nonzero": sum(b3_trit(b, 4) != -1 for b in range(256)),
                          "qh_noncanonical_flagged": sorted(b for b in set(range(256)) - CANONICAL_QH
                                                            if b3_trit(b, 4) == -1)}
    constants["gguf"] = {"magic": "GGUF", "version": 3, "default_alignment": 32, "max_dims": 4,
                         "info_fixed_bytes": 24, "info_bytes_per_dim": 8,
                         "namespaces": {"prism": "a key beginning prism.",
                                        "bitnet": "general.architecture bitnet-b1.58, or a tensor of type 36 or 38"}}
    invariants = [
        {"id": "llama_tq1_0_bits_per_weight", "condition": "54 * 8 * 16 == 27 * 256"},
        {"id": "llama_tq2_0_bits_per_weight", "condition": "66 * 8 * 16 == 33 * 256"},
        {"id": "llama_q2_0_bits_per_weight", "condition": "18 * 8 * 4 == 9 * 64"},
        {"id": "llama_q1_0_bits_per_weight", "condition": "18 * 8 * 8 == 9 * 128"},
        {"id": "llama_tq1_0_digits_cover_the_block", "condition": "5 * 48 + 4 * 4 == 256"},
        {"id": "llama_base3_code_counts", "condition": "243 == 3^5, 81 == 3^4"},
        {"id": "llama_gguf_alignment_is_a_power_of_two", "condition": "32 & 31 == 0, 24 == 8 + 4 + 4 + 8"},
    ]
    return formats_document("llama_cpp", "TrinityFormatsLlamaCppSpec", "llama.cpp ternary blocks and GGUF rules",
                            "TQ1_0, TQ2_0, Q2_0 (group 64) and Q1_0 blocks, their flags and rejections, and GGUF "
                            "type-id, alignment and extent rules.",
                            constants, invariants, vectors, ["llama.cpp", "bitnet.cpp-llama.cpp", "prismml"])


def build_formats_prismml():
    vectors = block_family_vectors(("PQ2_0", "PTQ1_0"), 142, "prismml")
    p = [0] * 128
    p[0], p[1], p[2], p[3] = 1, 0, -1, 1
    vectors.append(block_vector("pq2_0_worked_example", "PQ2_0", encode_blocks("PQ2_0", p, [0x3800]), 128,
                                "PQ2_0 codes low bits first after the fp16 scale", encode=True))
    p = [0] * 128
    for index, value in ((0, 1), (16, 0), (32, -1), (48, 1), (64, 0), (120, -1), (122, 1), (124, 0), (126, 1)):
        p[index] = value
    vectors.append(block_vector("ptq1_0_worked_example", "PTQ1_0", encode_blocks("PTQ1_0", p, [0x3800]), 128,
                                "PTQ1_0 stages of 16 and 8 five-digit bytes, then 2 qh bytes", encode=True))
    same = None
    for key, fmt in (("bonsai_pq2_0", "PQ2_0"), ("bonsai_ptq1_0", "PTQ1_0")):
        vector = block_vector(f"{fmt.lower()}_real_bonsai_blk0_ffn_down", fmt, real_bytes(key), 256,
                              f"The first two {fmt} blocks of Ternary Bonsai 2 27B blk.0.ffn_down.weight",
                              encode=(fmt == "PQ2_0"), source=real_source(key))
        values = vector["expect"]["values_hex"]
        assert same is None or same == values, "PQ2_0 and PTQ1_0 hold the same trits"
        same = values
        vectors.append(vector)
    q2 = block_vector("q2_0_fork_group_64", "Q2_0", real_bytes("bonsai_q2_0"), 128,
                      "The fork's id-42 Q2_0 is group 64; its two blocks hold the same trits as PQ2_0 block 0",
                      encode=True, source=real_source("bonsai_q2_0"))
    assert q2["expect"]["values_hex"] == same[:2 * 128]
    vectors.append(q2)
    # Group-128 bytes read as group 64: 306 bytes are 9 PQ2_0 blocks or 17 Q2_0 blocks.
    seed = 306
    while True:
        intended = Lcg(seed).trits(1152)
        words = [0x2000 + 37 * b for b in range(9)]
        data = encode_blocks("PQ2_0", intended, words)
        if blocks_expect("Q2_0", data, 1088)["status"] >= 0:
            break
        seed += 1
    vector = block_vector("group_128_bytes_read_as_group_64", "Q2_0", data, 1088,
                          "Synthetic: nine PQ2_0 blocks declared as Q2_0 decode as 17 blocks without error; "
                          "scales are read from code bytes and PQ2_0 scale bytes become codes",
                          silent_class="group_size_mismatch")
    vector["intended"] = {"format": "PQ2_0", "count": 1152, "values_hex": i8_hex(intended), "scale_words": words}
    vectors.append(viewed(vector, block_view("Q2_0", "prismml",
                                             "a group-64 reader decodes the bytes as 17 blocks; this repository's "
                                             "synthetic case, not a published file (the published Q2_0 file holds "
                                             "valid group-64 blocks)")))
    constants = block_constants(("PQ2_0", "PTQ1_0", "Q2_0"))
    constants["common_byte_run"] = {"bytes": 306, "pq2_0_blocks": 9, "q2_0_blocks": 17}
    constants["ptq1_0_stages"] = [16, 8]
    invariants = [
        {"id": "prism_bits_per_weight", "condition": "34 * 8 * 8 == 17 * 128, 28 * 8 * 4 == 7 * 128, 18 * 8 * 4 == 9 * 64"},
        {"id": "prism_ptq1_0_stages_cover_qs", "condition": "16 + 8 == 24, 5 * 16 + 5 * 8 + 4 * 2 == 128"},
        {"id": "prism_common_byte_run", "condition": "306 == 9 * 34 == 17 * 18"},
    ]
    return formats_document("prismml", "TrinityFormatsPrismmlSpec", "PrismML fork PQ2_0, PTQ1_0 and Q2_0",
                            "PQ2_0 and PTQ1_0 (group 128) and the fork's group-64 Q2_0, with real Bonsai blocks and "
                            "the synthetic group-128-as-64 case.",
                            constants, invariants, vectors, ["prismml"])


# ---- bitnet.cpp I2_S

def i2s_encode(values, rows, cols, scale_word, layout="x86", trailer=b"\0" * 28):
    count = rows * cols
    out = bytearray(count // 4 + 32)
    for e, value in enumerate(values):
        code = value + 1
        if layout == "x86":      # ggml-bitnet-mad.cpp:78-85
            at, shift = (e // 128) * 32 + (e % 128) % 32, 6 - 2 * ((e % 128) // 32)
        elif layout == "arm":    # :174-181
            at, shift = (e // 64) * 16 + (e % 64) % 16, 6 - 2 * ((e % 64) // 16)
        elif layout == "1x4":    # :122-138, four rows per byte
            r, c = divmod(e, cols)
            at, shift = (r // 4) * cols + c, 6 - 2 * (r % 4)
        else:                    # submodule ggml-cpu/quants.c:1378-1380, four consecutive weights
            at, shift = e // 4, 6 - 2 * (e % 4)
        out[at] |= code << shift
    out[count // 4:count // 4 + 4] = scale_word.to_bytes(4, "little")
    out[count // 4 + 4:] = trailer
    return bytes(out)


def i2s_expect(data, count):
    if count == 0 or count % 128 or len(data) != count // 4 + 32:
        return {"status": FORMAT_ERRORS["length"]}
    word = int.from_bytes(data[count // 4:count // 4 + 4], "little")
    if scale_class(word, "F32") < 0:
        return {"status": FORMAT_ERRORS["scale_nonfinite"]}
    values = [((data[(e // 128) * 32 + (e % 128) % 32] >> (6 - 2 * ((e % 128) // 32))) & 3) - 1 for e in range(count)]
    flags = empty_flags()
    flags["outside_ternary"] = sum(v == 2 for v in values)
    flags["trailer_nonzero"] = sum(b != 0 for b in data[count // 4 + 4:])
    add_scale_flags(flags, [word], "F32")
    return {"status": flags["outside_ternary"], "values_hex": i8_hex(values), "scale_word": word, "flags": flags}


def i2s_vector(identifier, data, count, description, **extra):
    vector = {"id": identifier, "kind": "i2s", "count": count, "data_hex": data.hex(),
              "expect": i2s_expect(data, count), "description": description}
    vector.update(extra)
    return check_classes(vector, vector["expect"]["status"])


def build_formats_bitnet_cpp():
    rng = Lcg(36)
    vectors = []
    t = [0] * 128
    t[0], t[32], t[64], t[96] = 1, 0, -1, 1
    vectors.append(i2s_vector("i2s_worked_example", i2s_encode(t, 1, 128, 0x3F800000), 128,
                              "Weights 0, 32, 64, 96 share byte 0 at bits 6, 4, 2, 0", encode=True))
    values = rng.trits(512)
    data = i2s_encode(values, 4, 128, 0x3D4CCCCD)
    vectors.append(i2s_vector("i2s_random_four_blocks", data, 512,
                              "Four blocks of deterministic codes with a zero trailer (the Python converter's output)",
                              encode=True))
    real = real_bytes("bitnet_i2s_block") + real_bytes("bitnet_i2s_tail")
    source = {"block": real_source("bitnet_i2s_block"), "tail": real_source("bitnet_i2s_tail")}
    vectors.append(viewed(i2s_vector("i2s_real_block_scale_and_trailer", real, 128,
                                     "Assembled from real bytes of BitNet b1.58 2B4T blk.0.ffn_down.weight: its first 32 "
                                     "bytes, then the tensor's f32 scale and the 28 trailer bytes that llama-quantize "
                                     "left there", source=source, flag_class="trailer_nonzero"),
                          view("bitnet.cpp-llama.cpp", "decodes",
                               ["ggml/src/ggml-cpu/quants.c:1335-1356", "ggml/src/ggml-cpu/ops.cpp:4783-4829",
                                "ggml/src/ggml-cpu/ggml-cpu.c:1217-1252"],
                               "the readers take the n/4 code bytes and the f32 scale at byte n/4; no pinned code "
                               "reads the 28 bytes after the scale, which keep what llama-quantize's reused output "
                               "buffer held")))
    threes = bytearray(i2s_encode(values[:128], 1, 128, 0x3D4CCCCD))
    threes[0] |= 0xC0
    threes[31] = 0xFF
    # bitnet.cpp's llama.cpp submodule: the to_float/get_rows reader and the
    # mul_mat path (ggml/src/ggml-cpu/*, the files its build compiles).
    dequant = ["ggml/src/ggml-cpu/quants.c:1335-1356", "ggml/src/ggml.c:936", "ggml/src/ggml-cpu/ops.cpp:4783-4829"]
    mul_mat = ["ggml/src/ggml-cpu/ggml-cpu.c:1217-1252", "ggml/src/ggml-cpu/ggml-cpu.c:1491-1545",
               "ggml/src/ggml-cpu/ggml-cpu-i2s.c:38-120"]
    code3 = i2s_expect(bytes(threes), 128)["values_hex"]
    vectors.append(viewed(i2s_vector("i2s_code3", bytes(threes), 128,
                                     "Code 3 is never written; bitnet.cpp reads it as 0 through dequantize_row_i2_s and "
                                     "as +2 through mul_mat; reported as +2 and flagged",
                                     flag_class="outside_ternary"),
                          view("bitnet.cpp-llama.cpp", "decodes", dequant,
                               "dequantize_row_i2_s maps codes through {-1, 0, +1, 0}: code 3 is 0",
                               values_hex=bytes(0 if v == 2 else v & 255 for v in bytes.fromhex(code3)).hex(),
                               code3_value=0),
                          view("bitnet.cpp-llama.cpp", "decodes", mul_mat,
                               "mul_mat multiplies the raw codes 0..3 and subtracts the activation sum: code 3 is +2",
                               values_hex=code3, code3_value=2)))
    for word, name, flag, effect in ((0xBF800000, "negative", "scale_negative", "flips the sign of every weight"),
                                     (0, "zero", "scale_zero", "makes every weight 0")):
        vector = i2s_vector(f"i2s_{name}_scale", i2s_encode(values[:128], 1, 128, word), 128,
                            f"A {name} f32 scale; flagged", encode=True, flag_class=flag)
        vectors.append(viewed(vector,
                              view("bitnet.cpp-llama.cpp", "decodes", dequant,
                                   f"y = scale * trit with no check of the scale: it {effect}"),
                              view("bitnet.cpp-llama.cpp", "decodes", mul_mat,
                                   f"the dot product is multiplied by the scale with no check: it {effect}")))
    for word, name, effect in ((0x7FC00000, "nan", "every weight is NaN"),
                               (0x7F800000, "inf", "+1 and -1 become +inf and -inf, 0 becomes NaN (0 * inf)")):
        data = i2s_encode(values[:128], 1, 128, word)
        vector = i2s_vector(f"i2s_scale_{name}", data, 128, f"f32 scale 0x{word:08x}", error_class="scale_nonfinite")
        trits = i2s_expect(i2s_encode(values[:128], 1, 128, 0x3F800000), 128)["values_hex"]
        vectors.append(rejected(vector,
                                view("bitnet.cpp-llama.cpp", "decodes", dequant,
                                     f"no check of the scale; these trits times the scale: {effect}",
                                     values_hex=trits, scale_word=word),
                                view("bitnet.cpp-llama.cpp", "decodes", mul_mat,
                                     "no check of the scale; every dot product is multiplied by it")))
    data = i2s_encode(values[:128], 1, 128, 0x3D4CCCCD)
    vectors.append(rejected(i2s_vector("i2s_trailer_missing", data[:-1], 128, "n/4 + 31 bytes", error_class="length"),
                            view("bitnet.cpp-llama.cpp", "rejects",
                                 ["ggml/src/ggml.c:1314-1321", "src/llama-model-loader.h:46-48"],
                                 "the tensor is n/4 + 32 bytes; data past the end of the file is refused")))
    vectors.append(rejected(i2s_vector("i2s_count_not_whole_blocks", data[:64 // 4 + 32], 64,
                                       "64 weights are not a whole 128-weight block of the x86 layout",
                                       error_class="length"),
                            view("bitnet.cpp-llama.cpp", "decodes", ["ggml/src/ggml.c:931-937"] + dequant[:1],
                                 "I2_S has block size 1, so gguf.cpp accepts any row length; dequantize_row_i2_s reads "
                                 "a 32-byte group for 64 weights, so bytes 16..31 (the scale and trailer at n/4) "
                                 "decode as weights 16..31 and 48..63")))
    intended = rng.trits(256)
    for layout, token, rows, cols in (("arm", "i2s_layout_arm", 2, 128), ("1x4", "i2s_layout_1x4", 4, 64),
                                      ("consecutive", "i2s_layout_consecutive", 2, 128)):
        data = i2s_encode(intended, rows, cols, 0x3F800000, layout)
        vector = i2s_vector(f"i2s_{layout}_bytes_read_as_act_parallel", data, 256,
                            f"bitnet.cpp's {layout} layout writes the same codes to other bytes; read as ACT_PARALLEL "
                            "they decode without error to other weights", silent_class=token)
        vector["intended"] = {"layout": layout, "rows": rows, "cols": cols, "values_hex": i8_hex(intended)}
        if layout == "consecutive":
            vectors.append(viewed(vector, view("bitnet.cpp-llama.cpp", "decodes",
                                               ["ggml/src/ggml-cpu/quants.c:1358-1387", dequant[0], "ggml/src/ggml.c:7787"],
                                               "the submodule's own quantize_i2_s writes these bytes and its own "
                                               "dequantize_row_i2_s reads them as other weights")))
            continue
        writer = "src/ggml-bitnet-mad.cpp:151-194" if layout == "arm" else "src/ggml-bitnet-mad.cpp:97-149"
        vectors.append(viewed(vector,
                              view("bitnet.cpp-llama.cpp", "decodes", [dequant[0], mul_mat[2]],
                                   "the pinned build's readers use the ACT_PARALLEL positions and read these bytes "
                                   "as other weights"),
                              view("bitnet.cpp", "not_applicable", [writer, "src/CMakeLists.txt:2-3"],
                                   f"the {layout} layout is written by src/ggml-bitnet-mad.cpp, which the pinned build "
                                   "does not compile (src/CMakeLists.txt only names it in a variable it overwrites)")))
    bad = list(values[:128])
    bad[5] = 2
    vectors.append(rejected({"id": "i2s_encode_rejects_value_2", "kind": "i2s_encode", "count": 128,
                             "values_hex": i8_hex(bad), "scale_word": 0x3F800000, "error_class": "code",
                             "expect": {"status": FORMAT_ERRORS["code"]},
                             "description": "I2_S stores -1, 0, +1 only; nothing is written"},
                            view("bitnet.cpp-llama.cpp", "not_applicable", "ggml/src/ggml-cpu/quants.c:1358-1387",
                                 "the compiled quantize_i2_s takes float weights and writes code 0, 1 or 2 from the "
                                 "sign of each one only")))
    constants = {"i2s": {"id": 7, "ggml_type": 36, "block_weights": 128, "group_bytes": 32, "tail_bytes": 32,
                         "scale": "F32", "scale_offset": "n/4", "trailer_bytes": 28, "bits_per_weight": "2 + 256/n",
                         "layout": "x86 ACT_PARALLEL"},
                 "other_layouts": {"arm": {"block_weights": 64, "group_bytes": 16},
                                   "1x4": {"rows_per_byte": 4, "byte": "(r/4)*cols + c", "shift": "6 - 2(r%4)"},
                                   "consecutive": {"byte": "e/4", "shift": "6 - 2(e%4)"}},
                 "type_ids": {"I2_S": 36, "TL1": 38, "TL2": 42}}
    invariants = [
        {"id": "bitnet_block_is_32_bytes", "condition": "128 * 2 == 32 * 8, 64 * 2 == 16 * 8"},
        {"id": "bitnet_tail_is_scale_and_trailer", "condition": "4 + 28 == 32"},
    ]
    return formats_document("bitnet_cpp", "TrinityFormatsBitnetCppSpec", "bitnet.cpp I2_S",
                            "I2_S in the x86 ACT_PARALLEL layout with real BitNet bytes, flags, rejections, and the ARM, "
                            "four-row and four-consecutive layouts read as ACT_PARALLEL.",
                            constants, invariants, vectors, ["bitnet.cpp", "bitnet.cpp-llama.cpp"])


# ---- transformers BitNet packed weights

def hf_encode(values, rows, cols):
    stride = rows // 4
    out = bytearray(stride * cols)
    for i in range(4):  # integrations/bitnet.py:47-50
        for r in range(stride):
            for k in range(cols):
                out[r * cols + k] |= (values[(i * stride + r) * cols + k] + 1) << (2 * i)
    return bytes(out)


def hf_expect(data, rows, cols, scale_word):
    if rows == 0 or cols == 0 or rows % 4 or len(data) != rows // 4 * cols:
        return {"status": FORMAT_ERRORS["length"]}
    stride = rows // 4
    values = [0] * (rows * cols)
    for i in range(4):  # unpack_weights :114-121
        for r in range(stride):
            for k in range(cols):
                values[(i * stride + r) * cols + k] = ((data[r * cols + k] >> (2 * i)) & 3) - 1
    flags = empty_flags()
    flags["outside_ternary"] = sum(v == 2 for v in values)
    scale_status = scale_class(scale_word, "BF16")
    if scale_status >= 0:
        add_scale_flags(flags, [scale_word], "BF16")
    return {"status": flags["outside_ternary"], "values_hex": i8_hex(values), "flags": flags,
            "scale_status": min(scale_status, 0)}


def hf_vector(identifier, data, rows, cols, scale_word, description, **extra):
    vector = {"id": identifier, "kind": "hf_packed", "rows": rows, "cols": cols, "data_hex": data.hex(),
              "scale_word": scale_word, "scale_kind": "BF16", "expect": hf_expect(data, rows, cols, scale_word),
              "description": description}
    vector.update(extra)
    return check_classes(vector, vector["expect"]["status"], vector["expect"].get("scale_status", 0))


def build_formats_hf_bitnet():
    rng = Lcg(8)
    vectors = [hf_vector("hf_docstring_example", bytes([161, 24, 144, 10]), 8, 2, 0x3F80,
                         "The unpack_weights docstring example: packed [[161, 24], [144, 10]] is an 8 x 2 tensor",
                         encode=True)]
    values = rng.trits(12 * 5)
    vectors.append(hf_vector("hf_random_12x5", hf_encode(values, 12, 5), 12, 5, 0x3F9C,
                             "Deterministic codes, 12 rows (stride 3) and 5 columns", encode=True))
    vectors.append(hf_vector("hf_real_q_proj_rows_0_640_1280_1920", real_bytes("bitnet_hf_row0"), 4, 16, 0x3F9C,
                             "Packed row 0, columns 0..15 of BitNet b1.58 2B4T layers.0.self_attn.q_proj.weight "
                             "(U8 [640, 2560]) holds rows 0, 640, 1280 and 1920; read as a 4 x 16 tensor with the "
                             "tensor's bf16 weight_scale 1.21875", encode=True, source=real_source("bitnet_hf_row0")))
    three = bytearray(hf_encode(values, 12, 5))
    three[0] |= 0xC0
    scaled = view("transformers", "decodes", "src/transformers/integrations/bitnet.py:291-292",
                  "AutoBitLinear (offline) multiplies the output by weight_scale")
    vectors.append(viewed(hf_vector("hf_code3", bytes(three), 12, 5, 0x3F9C, "Code 3 unpacks to +2; flagged",
                                    flag_class="outside_ternary"),
                          view("transformers", "decodes", "src/transformers/integrations/bitnet.py:114-121", "unpack_weights returns code - 1: +2")))
    vectors.append(viewed(hf_vector("hf_negative_scale", hf_encode(values, 12, 5), 12, 5, 0xBF9C,
                                    "A negative weight_scale; flagged", encode=True, flag_class="scale_negative"),
                          scaled))
    vectors.append(rejected(hf_vector("hf_scale_nan", hf_encode(values, 12, 5), 12, 5, 0x7FC0,
                                      "A NaN weight_scale", error_class="scale_nonfinite"), scaled))
    vectors.append(rejected(hf_vector("hf_scale_inf", hf_encode(values, 12, 5), 12, 5, 0xFF80,
                                      "A negative infinite weight_scale", error_class="scale_nonfinite"), scaled))
    vectors.append(rejected(hf_vector("hf_rows_not_multiple_of_4", hf_encode(values, 12, 5), 10, 5, 0x3F9C,
                                      "rows % 4 != 0 has no storage in the packed tensor", error_class="length"),
                            view("transformers", "not_applicable", "src/transformers/integrations/bitnet.py:104-111",
                                 "a packed [rows/4, cols] tensor cannot state rows % 4; unpack_weights returns "
                                 "4 * packed rows")))
    vectors.append(rejected(hf_vector("hf_size_mismatch", hf_encode(values, 12, 5)[:-1], 12, 5, 0x3F9C,
                                      "One packed byte missing", error_class="length"),
                            view("safetensors", "rejects", "safetensors/src/tensor.rs:642-661",
                                 "a byte range that is not numel * dtype width is refused")))
    bad = list(values)
    bad[7] = -2
    # pack_weights (bitnet.py:17-53) takes codes, not floats, and checks none:
    # (value + 1) cast to uint8 (-1 becomes 255), shifted in uint8 and ORed in.
    packed = bytearray(3 * 5)
    for i in range(4):
        for r in range(3):
            for k in range(5):
                packed[r * 5 + k] |= (((bad[(i * 3 + r) * 5 + k] + 1) & 0xFF) << (2 * i)) & 0xFF
    clobbered = [row for row in range(12) if row % 3 == 1 and (packed[7] >> (2 * (row // 3))) & 3 == 3]
    vectors.append(rejected({"id": "hf_encode_rejects_minus_2", "kind": "hf_encode", "rows": 12, "cols": 5,
                             "values_hex": i8_hex(bad), "error_class": "code",
                             "expect": {"status": FORMAT_ERRORS["code"]},
                             "description": "The packed format stores -1, 0, +1 only; nothing is written"},
                            view("transformers", "decodes", "src/transformers/integrations/bitnet.py:17-53",
                                 "pack_weights takes the codes directly and does not check them: -2 + 1 cast to uint8 "
                                 f"is 255, so packed byte 7 becomes 0x{packed[7]:02x} (packed tensor "
                                 f"{bytes(packed).hex()}) and rows {', '.join(map(str, clobbered))} of column 2 "
                                 "unpack as +2")))
    vectors += safetensors_vectors()
    constants = {"hf_packed": {"id": 8, "values_per_byte": 4, "code": "T + 1 at bits 2*(i / (rows/4))",
                               "packed_shape": "[rows/4, cols] uint8", "weight_scale": "[1] in the model dtype (BF16)",
                               "value": "trit * weight_scale (AutoBitLinear, offline)", "bits_per_weight": "2 + 16/n"}}
    invariants = [{"id": "hf_four_codes_per_byte", "condition": "4 * 2 == 8"}]
    return formats_document("hf_bitnet", "TrinityFormatsHfBitnetSpec", "transformers BitNet packed weights",
                            "Packed uint8 BitNet weights and their bf16 weight_scale, with a real row of BitNet b1.58 2B4T.",
                            constants, invariants, vectors, ["transformers", "safetensors"])


# ---- safetensors extents (huggingface/safetensors tensor.rs)

SAFETENSORS_BITS = {"F4": 4, "F6_E3M2": 6, "F6_E2M3": 6, "BOOL": 8, "U8": 8, "I8": 8, "F8_E5M2": 8, "F8_E4M3": 8,
                    "F8_E8M0": 8, "F8_E4M3FNUZ": 8, "F8_E5M2FNUZ": 8, "I16": 16, "U16": 16, "F16": 16, "BF16": 16,
                    "I32": 32, "U32": 32, "F32": 32, "I64": 64, "U64": 64, "F64": 64, "C64": 64}
TENSOR_RS = "safetensors/src/tensor.rs"


def safetensors_vector(identifier, entries, target, description, data_bytes=None, views=()):
    """entries: (name, dtype, shape, begin, end) relative to the data section."""
    header = {"__metadata__": {"format": "pt"}}
    for name, dtype, shape, begin, end in entries:
        header[name] = {"dtype": dtype, "shape": shape, "data_offsets": [begin, end]}
    text = json.dumps(header, separators=(",", ":")).encode()
    blob = struct.pack("<Q", len(text)) + text
    base = len(blob)
    if data_bytes is None:
        data_bytes = max(e[4] for e in entries)
    file_size = base + data_bytes
    name, dtype, shape, begin, end = next(e for e in entries if e[0] == target)
    later = [e[3] for e in entries if e[0] != target and e[4] > e[3] and e[3] >= begin]
    earlier = [e[4] for e in entries if e[0] != target and e[4] > e[3] and e[3] < begin]
    bits = SAFETENSORS_BITS.get(dtype, 0)
    numel = 1
    for d in shape:
        numel *= d
    # Absolute offsets base + data_offsets must fit in 64 bits for every entry.
    wraps = any(base + e[4] >= 1 << 64 for e in entries)
    if wraps:
        check, error_class = FORMAT_ERRORS["extent"], "extent"
    elif bits == 0:
        check, error_class = FORMAT_ERRORS["container"], "container"
    elif bits * numel >= 1 << 64 or bits * numel % 8:
        check, error_class = FORMAT_ERRORS["length"], "length"
    elif (end - begin != bits * numel // 8 or base + end > file_size
          # tensor.rs:627-661, :419-421: the tensors tile the data section, so
          # this one begins where the one below it ends (at 0 when none does)
          # and ends where the next begins, or at the end of the file.
          or begin != (max(earlier) if earlier else 0)
          or (later and end != min(later)) or (not later and base + end != file_size)):
        check, error_class = FORMAT_ERRORS["extent"], "extent"
    else:
        check, error_class = 0, None
    expect = {"find": 0, "dtype": dtype, "shape": shape, "begin": base + begin, "end": base + end,
              "dtype_bits": bits, "has_next": bool(later), "next_begin": base + min(later) if later else 0,
              "has_prev": bool(earlier), "prev_end": base + max(earlier) if earlier else base, "check": check}
    if wraps:
        # tf_safetensors_find refuses the header; the range fields stay 0.
        expect.update({"find": check, "begin": 0, "end": 0, "has_next": False, "next_begin": 0,
                       "has_prev": False, "prev_end": 0})
    vector = {"id": identifier, "kind": "safetensors", "safetensors_hex": blob.hex(), "tensor": target,
              "file_size": file_size, "description": description, "expect": expect,
              "data_offsets": [[e[3], e[4]] for e in entries]}
    if error_class:
        vector["error_class"] = error_class
        return rejected(vector, *views)
    assert not views, identifier
    return vector


def safetensors_vectors():
    extent = view("safetensors", "rejects", f"{TENSOR_RS}:659-661", "TensorInvalidInfo: end - begin != numel * width")
    tiling = view("safetensors", "rejects", f"{TENSOR_RS}:632-638",
                  "InvalidOffset: each tensor must begin where the previous one ended")
    covered = view("safetensors", "rejects", f"{TENSOR_RS}:420-422",
                   "MetadataIncompleteBuffer: the data section must end at the end of the file")
    packed = ("model.layers.0.self_attn.q_proj.weight", "U8", [2, 3], 0, 6)
    scale = ("model.layers.0.self_attn.q_proj.weight_scale", "BF16", [1], 6, 8)
    return [
        safetensors_vector("st_packed_weight_and_scale", [packed, scale], packed[0],
                           "A packed U8 [2, 3] weight followed by its BF16 weight_scale; the file ends after the scale"),
        safetensors_vector("st_scale_last", [packed, scale], scale[0], "The BF16 [1] weight_scale ends the file"),
        safetensors_vector("st_zero_size_neighbour", [packed, ("empty", "F32", [0], 0, 0), scale], packed[0],
                           "A zero-size tensor at the same begin holds no bytes and bounds nothing"),
        safetensors_vector("st_extent_not_numel_times_width", [("w", "U8", [2, 3], 0, 5), ("s", "BF16", [1], 5, 7)],
                           "w", "U8 [2, 3] needs 6 bytes; data_offsets give 5", views=(extent,)),
        safetensors_vector("st_overlapping_tensors", [("w", "U8", [2, 3], 0, 6), ("s", "BF16", [1], 4, 6)], "w",
                           "s begins at 4, inside w's 6 bytes", views=(tiling,)),
        safetensors_vector("st_overlapping_tensor_read_itself", [("w", "U8", [2, 3], 0, 6), ("s", "BF16", [1], 4, 6)],
                           "s", "The overlapping tensor itself: s at [4, 6) begins inside w at [0, 6)", views=(tiling,)),
        safetensors_vector("st_offsets_wrap_around", [("a", "U8", [2], (1 << 64) - 16, (1 << 64) - 14)], "a",
                           "data_offsets near 2^64: 8 + header + offset wraps to bytes inside the JSON header",
                           data_bytes=8, views=(tiling,)),
        safetensors_vector("st_other_offsets_wrap_around",
                           [("a", "U8", [4], 0, 4), ("b", "U8", [2], (1 << 64) - 16, (1 << 64) - 14)], "a",
                           "Another entry's data_offsets wrap around; the header is refused whichever tensor is read",
                           data_bytes=8, views=(tiling,)),
        safetensors_vector("st_gap_after_tensor", [("w", "U8", [2, 3], 0, 6), ("s", "BF16", [1], 8, 10)], "w",
                           "A 2-byte gap between w at [0, 6) and s at [8, 10)", views=(tiling,)),
        safetensors_vector("st_gap_before_tensor", [("w", "U8", [2, 3], 0, 6), ("s", "BF16", [1], 8, 10)], "s",
                           "The tensor after the gap: s at [8, 10) does not begin where w ends", views=(tiling,)),
        safetensors_vector("st_first_tensor_not_at_zero", [("w", "U8", [2, 3], 2, 8)], "w",
                           "The only tensor begins 2 bytes into the data section", views=(tiling,)),
        safetensors_vector("st_bytes_after_last_tensor", [packed, scale], scale[0],
                           "Two bytes follow the last tensor, weight_scale at [6, 8)", data_bytes=10,
                           views=(covered,)),
        safetensors_vector("st_past_end_of_file", [packed, scale], scale[0],
                           "A truncated file: weight_scale needs bytes 6..8 of the data section, 7 remain",
                           data_bytes=7, views=(covered,)),
        safetensors_vector("st_unknown_dtype", [("w", "Q2", [2, 3], 0, 6)], "w",
                           "Q2 is not a safetensors dtype",
                           views=(view("safetensors", "rejects", [f"{TENSOR_RS}:416-418", f"{TENSOR_RS}:867-891"],
                                       "the header does not deserialize: Q2 is no Dtype"),)),
        safetensors_vector("st_shape_overflows", [("w", "U8", [4294967296, 4294967296], 0, 8)], "w",
                           "2^64 elements: the bit count does not fit in 64 bits (length, as a GGUF weight "
                           "count that gguf.cpp refuses)",
                           views=(view("safetensors", "rejects", f"{TENSOR_RS}:642-650",
                                       "ValidationOverflow: numel * bitsize does not fit"),)),
        safetensors_vector("st_sub_byte_not_whole_bytes", [("w", "F4", [3], 0, 2)], "w",
                           "Three F4 elements are 12 bits, not whole bytes",
                           views=(view("safetensors", "rejects", f"{TENSOR_RS}:652-654", "MisalignedSlice"),)),
    ]


# ---- MLX 2-bit affine

def mlx_encode(values, rows, cols):
    out = bytearray(rows * cols // 4)
    for r in range(rows):
        for w in range(cols // 16):  # ops.cpp:4975, shifts 2^(0, 2, ..., 30)
            word = sum((values[r * cols + 16 * w + k] + 1) << (2 * k) for k in range(16))
            out[4 * (r * cols // 16 + w):4 * (r * cols // 16 + w) + 4] = word.to_bytes(4, "little")
    return bytes(out)


def mlx_ternary(scale, bias):
    return ((scale & 0x7FFF) == 0 and (bias & 0x7FFF) == 0) or bias == scale ^ 0x8000


def mlx_expect(data, rows, cols, group, scales, biases, kind):
    if rows == 0 or cols == 0 or group not in (32, 64, 128) or cols % group or len(data) != rows * cols // 4:
        return {"status": FORMAT_ERRORS["length"]}
    values = []
    for r in range(rows):
        for k in range(cols):
            word = int.from_bytes(data[4 * (r * cols // 16 + k // 16):4 * (r * cols // 16 + k // 16) + 4], "little")
            values.append(((word >> (2 * (k % 16))) & 3) - 1)
    flags = empty_flags()
    flags["outside_ternary"] = sum(v == 2 for v in values)
    if any(scale_class(w, kind) < 0 for w in scales + biases):
        affine = FORMAT_ERRORS["scale_nonfinite"]
    else:
        affine = 0
        add_scale_flags(flags, scales, kind)
        flags["affine_not_ternary"] = sum(not mlx_ternary(s, b) for s, b in zip(scales, biases))
    return {"status": flags["outside_ternary"], "values_hex": i8_hex(values), "flags": flags, "affine_status": affine}


def mlx_vector(identifier, data, rows, cols, group, scales, biases, description, kind="F16", **extra):
    vector = {"id": identifier, "kind": "mlx", "rows": rows, "cols": cols, "group": group, "data_hex": data.hex(),
              "scale_kind": kind, "scale_words": scales, "bias_words": biases,
              "expect": mlx_expect(data, rows, cols, group, scales, biases, kind), "description": description}
    vector.update(extra)
    return check_classes(vector, vector["expect"]["status"], vector["expect"].get("affine_status", 0))


def build_formats_mlx():
    rng = Lcg(16)
    vectors = []
    t = [0] * 32
    t[0], t[1], t[15], t[16] = 1, -1, 1, 1
    vectors.append(mlx_vector("mlx_worked_example", mlx_encode(t, 1, 32), 1, 32, 32, [0x3C00], [0xBC00],
                              "Sixteen codes per little-endian uint32 word, low bits first", encode=True))
    values = rng.trits(2 * 128)
    scales = [0x2E66, 0x2A00, 0x3000, 0x2C00]
    biases = [s ^ 0x8000 for s in scales]
    vectors.append(mlx_vector("mlx_random_group_64", mlx_encode(values, 2, 128), 2, 128, 64, scales, biases,
                              "Two rows of 128 weights, groups of 64, bias = -scale", encode=True))
    vectors.append(mlx_vector("mlx_real_bonsai_down_proj_row0_group0", real_bytes("bonsai_mlx_weight"), 1, 128, 128,
                              [int.from_bytes(real_bytes("bonsai_mlx_scale"), "little")],
                              [int.from_bytes(real_bytes("bonsai_mlx_bias"), "little")],
                              "Row 0, group 0 of Ternary Bonsai 2 27B layers.0.mlp.down_proj (U32 words, F16 scale and "
                              "bias); the trits equal the first PTQ1_0 block of the same tensor", encode=True,
                              source={"weight": real_source("bonsai_mlx_weight"), "scale": real_source("bonsai_mlx_scale"),
                                      "bias": real_source("bonsai_mlx_bias")}))
    three = list(values)
    three[5] = 2
    affine = view("mlx", "decodes", "mlx/ops.cpp:5297-5298", "w = q * scale + bias per group, without checks")
    shapes = view("mlx", "rejects", "mlx/ops.cpp:5249-5256",
                  "affine_dequantize refuses words, scales and biases whose shapes disagree for the group size")
    vectors.append(viewed(mlx_vector("mlx_code3", mlx_encode(three, 2, 128), 2, 128, 64, scales, biases,
                                     "Code 3 with bias = -scale is +2; flagged", encode=True, flag_class="outside_ternary"),
                          affine))
    off = list(biases)
    off[2] = 0x0000
    vectors.append(viewed(mlx_vector("mlx_group_not_ternary", mlx_encode(values, 2, 128), 2, 128, 64, scales, off,
                                     "A group whose bias is not -scale holds affine, not ternary, values; flagged",
                                     encode=True, flag_class="affine_not_ternary"), affine))
    neg = [scales[0] ^ 0x8000] + scales[1:]
    vectors.append(viewed(mlx_vector("mlx_negative_scale", mlx_encode(values, 2, 128), 2, 128, 64, neg,
                                     [s ^ 0x8000 for s in neg], "A negative scale with bias = -scale; flagged",
                                     encode=True, flag_class="scale_negative"), affine))
    vectors.append(rejected(mlx_vector("mlx_scale_nan", mlx_encode(values, 2, 128), 2, 128, 64, [0x7E00] + scales[1:],
                                       biases, "A NaN scale", error_class="scale_nonfinite"), affine))
    vectors.append(rejected(mlx_vector("mlx_bias_inf", mlx_encode(values, 2, 128), 2, 128, 64, scales,
                                       biases[:3] + [0xFC00], "An infinite bias", error_class="scale_nonfinite"), affine))
    vectors.append(rejected(mlx_vector("mlx_group_48", mlx_encode(values, 2, 128), 2, 128, 48, scales, biases,
                                       "Group sizes are 32, 64 or 128", error_class="length"),
                            view("mlx", "rejects", ["mlx/ops.cpp:5007-5012", "mlx/ops.cpp:5249-5256"],
                                 "the quantizer offers groups of 32, 64 and 128 only; the dequantizer refuses scales "
                                 "that do not cover the row in groups of 48")))
    vectors.append(rejected(mlx_vector("mlx_cols_not_divisible", mlx_encode(values, 2, 128)[:2 * 24], 2, 96, 64, scales,
                                       biases, "96 columns are not whole groups of 64", error_class="length"),
                            view("mlx", "rejects", ["mlx/ops.cpp:5207-5214", "mlx/ops.cpp:5249-5256"],
                                 "the last dimension must be whole groups")))
    vectors.append(rejected(mlx_vector("mlx_size_mismatch", mlx_encode(values, 2, 128)[:-4], 2, 128, 64, scales, biases,
                                       "One word missing", error_class="length"), shapes))
    bad = list(values)
    bad[3] = 3
    vectors.append(rejected({"id": "mlx_encode_rejects_value_3", "kind": "mlx_encode", "rows": 2, "cols": 128,
                             "group": 64, "values_hex": i8_hex(bad), "error_class": "code",
                             "expect": {"status": FORMAT_ERRORS["code"]},
                             "description": "A 2-bit code holds q = 0..3, values -1..2; nothing is written"},
                            view("mlx", "not_applicable", "mlx/ops.cpp:4957-5002",
                                 "pack_and_quantize clips codes to 0..3 from float weights")))
    constants = {"mlx": {"id": 9, "bits": 2, "codes_per_word": 16, "groups": [32, 64, 128], "zero_point": 1,
                         "value": "scale * q + bias; ternary iff bias == -scale",
                         "bits_per_weight": {"32": "3", "64": "5/2", "128": "9/4"}}}
    invariants = [{"id": "mlx_bits_per_weight_group_128", "condition": "4 * (128 * 2 + 16 + 16) == 9 * 128"}]
    return formats_document("mlx", "TrinityFormatsMlxSpec", "MLX 2-bit affine",
                            "MLX 2-bit codes with per-group scales and biases, with a real Bonsai group.",
                            constants, invariants, vectors, ["mlx"])


# ---- ONNX Runtime MatMulNBits bits=2

def onnx_geometry(n, k, bs):
    return n > 0 and k > 0 and bs >= 16 and bs & (bs - 1) == 0


def onnx_zero_point(zp, n, b, zp_row):
    return 2 if not zp else (zp[n * zp_row + b // 4] >> (2 * (b % 4))) & 3


def onnx_encode(values, n, k, bs, zp=b"", padding=0):
    blocks = -(-k // bs)
    row, zp_row = blocks * bs // 4, -(-blocks // 4)
    out = bytearray(n * row)
    for r in range(n):
        for c in range(blocks * bs):
            code = values[r * k + c] + onnx_zero_point(zp, r, c // bs, zp_row) if c < k else padding
            out[r * row + c // 4] |= code << (2 * (c % 4))
    return bytes(out)


def onnx_expect(data, n, k, bs, zp, scales, kind):
    blocks = -(-k // bs) if bs else 0
    if not onnx_geometry(n, k, bs) or len(data) != n * blocks * bs // 4 or (zp and len(zp) != n * -(-blocks // 4)):
        return {"status": FORMAT_ERRORS["length"]}
    row, zp_row = blocks * bs // 4, -(-blocks // 4)
    values = []
    padding = 0
    for r in range(n):
        for c in range(blocks * bs):
            code = (data[r * row + c // 4] >> (2 * (c % 4))) & 3
            if c < k:
                values.append(code - onnx_zero_point(zp, r, c // bs, zp_row))
            else:
                padding += code != 0
    flags = empty_flags()
    flags["outside_ternary"] = sum(not -1 <= v <= 1 for v in values)
    flags["padding_nonzero"] = padding
    scale_status = 0
    if any(scale_class(w, kind) < 0 for w in scales):
        scale_status = FORMAT_ERRORS["scale_nonfinite"]
    else:
        add_scale_flags(flags, scales, kind)
    return {"status": flags["outside_ternary"], "values_hex": i8_hex(values), "flags": flags,
            "scale_status": scale_status}


def onnx_vector(identifier, data, n, k, bs, zp, scales, description, kind="F16", **extra):
    vector = {"id": identifier, "kind": "onnx", "n": n, "k": k, "block_size": bs, "data_hex": data.hex(),
              "zero_points_hex": zp.hex(), "scale_kind": kind, "scale_words": scales,
              "expect": onnx_expect(data, n, k, bs, zp, scales, kind), "description": description}
    vector.update(extra)
    return check_classes(vector, vector["expect"]["status"], vector["expect"].get("scale_status", 0))


def build_formats_onnx():
    rng = Lcg(2)
    vectors = []
    vectors.append(onnx_vector("onnx_default_zero_point", onnx_encode([1, 0, -1, 1], 1, 4, 16), 1, 4, 16, b"",
                               [0x3C00], "Default zero point 2: +1, 0, -1, +1 are codes 3, 2, 1, 3", encode=True))
    values = rng.trits(3 * 70)
    scales = [0x2E66] * 9
    vectors.append(onnx_vector("onnx_padded_rows", onnx_encode(values, 3, 70, 32), 3, 70, 32, b"", scales,
                               "K = 70 with block 32: three blocks per row, the last one padded with zero codes",
                               encode=True))
    zp = bytes([0x1B, 0x26, 0x39])
    blocks = 3
    shifted = []
    for r in range(3):
        for c in range(70):
            z = (zp[r] >> (2 * (c // 32))) & 3
            shifted.append(rng.next() % 4 - z)
    dequant = view("onnxruntime", "decodes", "onnxruntime/core/mlas/lib/q4_dq.cpp:619-637", "v = (code - zero point) * scale, without checks")
    vectors.append(viewed(onnx_vector("onnx_packed_zero_points", onnx_encode(shifted, 3, 70, 32, zp), 3, 70, 32, zp,
                                      scales, "Packed per-block zero points 3, 2, 1 / 2, 1, 2 / 1, 2, 3; every code "
                                      "0..3 occurs, so values outside +-1 are flagged", encode=True,
                                      flag_class="outside_ternary"), dequant))
    zeros = onnx_encode([-2] * 16, 1, 16, 16)
    vectors.append(viewed(onnx_vector("onnx_code0_is_minus_2", zeros, 1, 16, 16, b"", [0x3C00],
                                      "Under the default zero point code 0 is -2, which a ternary tensor never uses; "
                                      "flagged", encode=True, flag_class="outside_ternary"), dequant))
    vectors.append(viewed(onnx_vector("onnx_padding_nonzero", onnx_encode(values, 3, 70, 32, padding=1), 3, 70, 32, b"",
                                      scales, "Padding codes (k >= K) are never read; nonzero ones are flagged",
                                      flag_class="padding_nonzero"),
                          view("onnxruntime", "decodes", ["onnxruntime/core/mlas/lib/q4_dq.cpp:619-637", "onnxruntime/core/mlas/lib/q4_dq.cpp:493-494", "onnxruntime/core/mlas/lib/q4_dq.cpp:551-567"],
                               "the dequantizer reads k < K only; the quantizer itself leaves nonzero codes in a "
                               "partial last byte (vi keeps the previous row's codes)")))
    vectors.append(viewed(onnx_vector("onnx_negative_scale", onnx_encode(values, 3, 70, 32), 3, 70, 32, b"",
                                      [0xAE66] + scales[1:], "A negative block scale; flagged", encode=True,
                                      flag_class="scale_negative"), dequant))
    vectors.append(onnx_vector("onnx_f32_scales", onnx_encode(values, 3, 70, 32), 3, 70, 32, b"",
                               [0x3D4CCCCD] * 9, "f32 scales (the scales share the type of input A)", kind="F32",
                               encode=True))
    vectors.append(rejected(onnx_vector("onnx_scale_nan", onnx_encode(values, 3, 70, 32), 3, 70, 32, b"",
                                        scales[:4] + [0x7E00] + scales[5:], "A NaN block scale",
                                        error_class="scale_nonfinite"), dequant))
    block_sizes = view("onnxruntime", "rejects", "onnxruntime/contrib_ops/cpu/quantization/matmul_nbits.cc:146-149", "block_size must be 16, 32, 64, 128 or 256")
    vectors.append(rejected(onnx_vector("onnx_block_size_24", onnx_encode(values, 3, 70, 32), 3, 70, 24, b"", scales,
                                        "block_size must be a power of two, at least 16", error_class="length"),
                            block_sizes))
    vectors.append(rejected(onnx_vector("onnx_block_size_8", onnx_encode(values[:8], 1, 8, 16), 1, 8, 8, b"", [0x3C00],
                                        "block_size 8 is below the minimum of 16", error_class="length"), block_sizes))
    vectors.append(rejected(onnx_vector("onnx_zero_points_size", onnx_encode(shifted, 3, 70, 32, zp), 3, 70, 32, zp[:2],
                                        scales, "zero_points must be N * ceil(k_blocks / 4) bytes",
                                        error_class="length"),
                            view("onnxruntime", "rejects", "onnxruntime/contrib_ops/cpu/quantization/matmul_nbits.cc:323-327",
                                 "a packed zero_points shape other than N * ceil(k_blocks / 4) is refused")))
    vectors.append(rejected(onnx_vector("onnx_size_mismatch", onnx_encode(values, 3, 70, 32)[:-1], 3, 70, 32, b"",
                                        scales, "One byte short of three rows", error_class="length"),
                            view("onnxruntime", "rejects", "onnxruntime/contrib_ops/cpu/quantization/matmul_nbits.cc:340-344",
                                 "B must have shape (N, k_blocks, block_size / 4)")))
    bad = list(shifted)
    bad[66] = -2
    vectors.append(rejected({"id": "onnx_encode_rejects_code_below_0", "kind": "onnx_encode", "n": 3, "k": 70,
                             "block_size": 32, "zero_points_hex": zp.hex(), "values_hex": i8_hex(bad),
                             "error_class": "code", "expect": {"status": FORMAT_ERRORS["code"]},
                             "description": "Value -2 in row 0, block 2 (zero point 1) would be code -1; nothing is "
                                            "written"},
                            view("onnxruntime", "not_applicable", "onnxruntime/core/mlas/lib/q4_dq.cpp:551-558",
                                 "the quantizer clamps every code to 0..3")))
    assert blocks == 3
    constants = {"onnx": {"id": 10, "bits": 2, "default_zero_point": 2, "min_block_size": 16,
                          "b_shape": "(N, ceil(K / block_size), block_size / 4) uint8",
                          "zero_points": "(N, ceil(k_blocks / 4)) uint8, 2 bits per block, low bits first",
                          "value": "(code - zero_point) * scale",
                          "bits_per_weight": "2 + scale_bits / block_size (+ zero points)"}}
    invariants = [{"id": "onnx_example_is_17_8_bits_per_weight", "condition": "(4096 * 2 + 32 * 16) * 8 == 17 * 4096"}]
    return formats_document("onnx", "TrinityFormatsOnnxSpec", "ONNX Runtime MatMulNBits bits=2",
                            "MatMulNBits 2-bit weights with default and packed zero points, padding and scales.",
                            constants, invariants, vectors, ["onnxruntime"])


FORMATS_BUILDERS = {"llama_cpp": build_formats_llama_cpp, "prismml": build_formats_prismml,
                    "bitnet_cpp": build_formats_bitnet_cpp, "hf_bitnet": build_formats_hf_bitnet,
                    "mlx": build_formats_mlx, "onnx": build_formats_onnx}


def render(document):
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if a committed file differs")
    args = parser.parse_args()
    documents = ((OUTPUT, render(build())), (BRIDGE_OUTPUT, render(build_bridge())),
                 (TENSORPACK_OUTPUT, render(build_tensorpack())), (STREAM_OUTPUT, render(build_stream_compute())),
                 (LAB_OUTPUT, render(build_conformance())), (EDGE_OUTPUT, render(build_edge_demo())))
    documents += tuple((FORMATS_OUTPUTS[family], render(builder())) for family, builder in FORMATS_BUILDERS.items())
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
