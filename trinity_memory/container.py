"""TMEM v1: a 24-byte header, canonical payload, and CRC32 integrity check."""

from dataclasses import asdict
import struct
import zlib
from typing import Iterable

from .codecs import CODECS_BY_ID, CodecError, get_codec, pack, payload_size, unpack, validate_trits

PREFIX = struct.Struct("<4sBBBBQI")
CRC = struct.Struct("<I")
HEADER_BYTES = PREFIX.size + CRC.size
MAGIC = b"TMEM"
VERSION = 1


def encode_file(values: Iterable[int], codec: str = "dense5") -> bytes:
    c = get_codec(codec)
    trits = validate_trits(values)
    payload = pack(trits, codec)
    if len(payload) > 0xFFFFFFFF:
        raise CodecError("v1 payload exceeds 4 GiB - 1 byte")
    prefix = PREFIX.pack(MAGIC, VERSION, c.id, 0, 0, len(trits), len(payload))
    checksum = zlib.crc32(payload, zlib.crc32(prefix))
    return prefix + CRC.pack(checksum) + payload


def _read(data: bytes) -> tuple[dict, list[int]]:
    if not isinstance(data, (bytes, bytearray)) or len(data) < HEADER_BYTES:
        raise CodecError("truncated TMEM header (requires 24 bytes)")
    magic, version, codec_id, flags, reserved, count, size = PREFIX.unpack_from(data)
    if magic != MAGIC:
        raise CodecError("invalid TMEM magic")
    if version != VERSION:
        raise CodecError(f"unsupported TMEM version {version}")
    if flags or reserved:
        raise CodecError("unsupported flags or nonzero reserved header byte")
    if codec_id not in CODECS_BY_ID:
        raise CodecError(f"unknown codec id {codec_id}")
    c = CODECS_BY_ID[codec_id]
    if size != payload_size(count, c.name) or len(data) != HEADER_BYTES + size:
        raise CodecError("TMEM count, payload size, or file length mismatch")
    checksum = CRC.unpack_from(data, PREFIX.size)[0]
    payload = data[HEADER_BYTES:]
    if zlib.crc32(payload, zlib.crc32(data[:PREFIX.size])) != checksum:
        raise CodecError("TMEM CRC32 mismatch")
    values = unpack(payload, count, c.name)
    return {"format": "TMEM", "version": version, "codec": c.name, "count": count,
            "payload_bytes": size, "header_bytes": HEADER_BYTES, "container_bytes": len(data),
            "payload_bpw": size * 8 / count if count else None,
            "container_bpw": len(data) * 8 / count if count else None,
            "crc32": f"{checksum:08x}", "validated": True, "layout": asdict(c)}, values


def decode_file(data: bytes) -> list[int]:
    return _read(data)[1]


def inspect_file(data: bytes) -> dict:
    """Validate integrity and canonical codes before returning metadata."""
    return _read(data)[0]
