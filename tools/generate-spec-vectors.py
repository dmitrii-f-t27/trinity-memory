#!/usr/bin/env python3
"""Generate or check conformance/memory_types.json from the spec definitions.

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


def render(document):
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed file differs")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    text = render(build())
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if current != text:
            print(f"{args.output} is stale: regenerate with tools/generate-spec-vectors.py", file=sys.stderr)
            return 1
        print(f"{args.output}: up to date ({text.count(chr(10))} lines)")
        return 0
    args.output.write_text(text, encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
