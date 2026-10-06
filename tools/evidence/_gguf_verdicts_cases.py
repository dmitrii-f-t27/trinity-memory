#!/usr/bin/env python3
"""Committed GGUF header fixtures for the gguf_verdicts evidence claim.

Every header is written here byte by byte from the GGUF definition (struct.pack),
with no use of t27 or of trinity_memory. The expected verdicts are written by
hand from two sources that do not run t27: the runtime tables in
specs/runtimes/*.json (type id -> block weights and bytes) and the checks of the
pinned readers (llama.cpp gguf.cpp offsets/type rules, the PrismML fork's
Hadamard rules), the same ones tests/test_live.py and tests/native_live.c
assert. The legacy group-128 case is also the one the real readers refused in
reports/live/legacy-q2_0-2026-09-29/scan-replay.json.

  python3 tools/evidence/_gguf_verdicts_cases.py      # rewrites the fixture file
"""
import json
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path('fixtures/gguf-verdicts/cases.json')
RUNTIMES = ['llama.cpp', 'prismml', 'bitnet.cpp', 'mortar.cpp']  # runtime ids 1..4


def string(value):
    raw = value.encode()
    return struct.pack('<Q', len(raw)) + raw


def kv(name, vtype, payload):
    return string(name) + struct.pack('<I', vtype) + payload


def gguf(records, keys=(), arch='qwen35', alignment=32):
    keys = ([('general.architecture', 8, string(arch))] if arch else []) + list(keys)
    out = bytearray(struct.pack('<IIQQ', 0x46554747, 3, len(records), len(keys)))
    for name, vtype, payload in keys:
        out += kv(name, vtype, payload)
    for name, dims, ggml_type, offset in records:
        out += string(name) + struct.pack('<I', len(dims))
        out += b''.join(struct.pack('<Q', d) for d in dims) + struct.pack('<IQ', ggml_type, offset)
    start = (len(out) + alignment - 1) // alignment * alignment
    return bytes(out) + bytes(start - len(out))


def tensors_file(tensors, block_bytes, keys=(), arch='qwen35'):
    """Tensors of 1024 x 4 weights stored in `group`-weight blocks of block_bytes."""
    offset, records = 0, []
    for name, ggml_type, group in tensors:
        records.append((name, [1024, 4], ggml_type, offset))
        offset += 4 * (1024 // group) * block_bytes
        offset = (offset + 31) // 32 * 32
    header = gguf(records, keys, arch)
    return header, len(header) + offset


def hadamard(names, block=1024):
    array = struct.pack('<IQ', 8, len(names)) + b''.join(string(n) for n in names)
    return [('prism.hadamard.version', 4, struct.pack('<I', 1)),
            ('prism.hadamard.block_size', 4, struct.pack('<I', block)),
            ('prism.hadamard.transform', 8, string('normalized-sylvester-walsh-hadamard')),
            ('prism.hadamard.axis', 8, string('input-last-dimension')),
            ('prism.hadamard.sign_mode', 8, string('identity')),
            ('prism.hadamard.weight_names', 9, array)]


def case(name, header, size, expect, native, file_verdict, fits=None):
    """expect: runtime -> dict(verdict, status?, record?, expected?, found?)."""
    return {'name': name, 'header': header.hex(), 'file_size': size, 'expect': expect,
            'native': native, 'file_verdict': file_verdict, 'fits': fits or {}}


def cases():
    out = []
    # 1. #50: type 42 declared, 128-weight groups of 34 bytes stored.
    h, s = tensors_file([('blk.0.a.weight', 42, 128), ('blk.0.b.weight', 42, 128)], 34)
    out.append(case('legacy-q2_0-declared-42-stored-group128', h, s, {
        'llama.cpp': {'verdict': 'refuses', 'status': 'offsets', 'record': 1, 'expected': 1152, 'found': 1088},
        'prismml': {'verdict': 'refuses', 'status': 'offsets', 'record': 1, 'expected': 1152, 'found': 1088},
        'bitnet.cpp': {'verdict': 'refuses', 'status': 'offsets'},
        'mortar.cpp': {'verdict': 'accepts'}},
        'llama.cpp', 'refused', {'PQ2_0': 2}))
    # 1b. the same legacy layout with a non-ternary F32 tensor between two Q2_0 ones.
    h = gguf([('blk.0.a.weight', [1024, 4], 42, 0), ('blk.0.norm.weight', [1024], 0, 1088),
              ('blk.0.b.weight', [1024, 4], 42, 1088 + 4096)])
    out.append(case('legacy-q2_0-with-f32-between', h, len(h) + 1088 + 4096 + 1088, {
        'llama.cpp': {'verdict': 'refuses', 'status': 'offsets', 'record': 1, 'expected': 1152, 'found': 1088},
        'prismml': {'verdict': 'refuses', 'status': 'offsets', 'record': 1, 'expected': 1152, 'found': 1088}},
        'llama.cpp', 'refused', {'PQ2_0': 2}))
    # 2. the same bytes under the fork's own id 142.
    h, s = tensors_file([('blk.0.ffn_down.weight', 142, 128), ('blk.0.ffn_up.weight', 142, 128)], 34)
    out.append(case('fork-pq2_0-id142', h, s, {
        'llama.cpp': {'verdict': 'refuses', 'status': 'type', 'record': 0},
        'prismml': {'verdict': 'accepts'},
        'bitnet.cpp': {'verdict': 'refuses', 'status': 'type', 'record': 0},
        'mortar.cpp': {'verdict': 'accepts'}},
        'prismml', 'ok'))
    # 3. the group-64 Q2_0 the stock reader and the fork read.
    h, s = tensors_file([('blk.0.a.weight', 42, 64), ('blk.0.b.weight', 42, 64)], 18)
    out.append(case('q2_0-group64-id42', h, s, {
        'llama.cpp': {'verdict': 'accepts'}, 'prismml': {'verdict': 'accepts'},
        'bitnet.cpp': {'verdict': 'refuses', 'status': 'offsets'},
        'mortar.cpp': {'verdict': 'refuses', 'status': 'offsets'}},
        'llama.cpp', 'ok'))
    # 4. mortar.cpp: id 143 is G8_0 (32 weights, 16 bytes); the fork reads it as PTQ1_0.
    records = [('a.weight', [1024, 4], 42, 0), ('token_embd.weight', [1024, 4], 143, 1088),
               ('b.weight', [1024, 4], 42, 1088 + 2048)]
    h = gguf(records)
    out.append(case('mortar-g8_0-id143', h, len(h) + 1088 + 2048 + 1088, {
        'llama.cpp': {'verdict': 'refuses'}, 'prismml': {'verdict': 'refuses'},
        'bitnet.cpp': {'verdict': 'refuses'}, 'mortar.cpp': {'verdict': 'accepts'}},
        'mortar.cpp', 'ok'))
    # 5. id 143 with the fork's PTQ1_0 bytes (28 per 128 weights) stays the fork's.
    h = gguf([('a.weight', [1024, 4], 142, 0), ('b.weight', [1024, 4], 143, 1088)])
    out.append(case('fork-ptq1_0-id143', h, len(h) + 1088 + 896, {
        'prismml': {'verdict': 'accepts'}, 'mortar.cpp': {'verdict': 'refuses'}},
        'prismml', 'ok'))
    # 6. silent: a declared Hadamard rotation is ignored outside the fork.
    h, s = tensors_file([('blk.0.attn_q.weight', 1, 1)], 2, keys=hadamard(['blk.0.attn_q.weight']))
    out.append(case('hadamard-declared-ignored-outside-fork', h, s, {
        'llama.cpp': {'verdict': 'ignores_rotation'}, 'prismml': {'verdict': 'accepts'},
        'bitnet.cpp': {'verdict': 'ignores_rotation'}, 'mortar.cpp': {'verdict': 'ignores_rotation'}},
        'prismml', 'no_ternary_layout'))  # native: a file declaring prism.hadamard.* is the fork's (tlv_native)
    # 7. the fork refuses an invalid Hadamard block size (12 is not a power of two).
    h, s = tensors_file([('blk.0.attn_q.weight', 1, 1)], 2, keys=hadamard(['blk.0.attn_q.weight'], block=12))
    out.append(case('hadamard-invalid-block-refused-by-fork', h, s, {
        'prismml': {'verdict': 'refuses', 'status': 'hadamard_block', 'record': 0},
        'llama.cpp': {'verdict': 'ignores_rotation'}},
        'prismml', 'refused'))  # the fork it is written for refuses it
    # 8. a TQ2_0 row (1320 weights) that is not whole 256-weight blocks.
    h = gguf([('blk.0.ffn_down.weight', [1320, 5120], 35, 0)], arch='qwen2')
    out.append(case('row-not-whole-blocks', h, len(h) + 5120 * 330, {
        'llama.cpp': {'verdict': 'refuses', 'status': 'row', 'record': 0}},
        'llama.cpp', 'refused'))
    # 9. a byte-swapped version is the reader's endianness refusal.
    out.append(case('endian', bytes.fromhex('474755460000000300000000000000000000000000000000'), 10 ** 6, {
        'llama.cpp': {'verdict': 'refuses', 'status': 'endian'}, 'prismml': {'verdict': 'refuses', 'status': 'endian'}},
        'llama.cpp', 'other_runtime'))  # a file for the other byte order is other software
    # 10. not GGUF at all.
    out.append(case('bad-magic', bytes(64), 10 ** 6, {
        'llama.cpp': {'verdict': 'refuses', 'status': 'magic'}, 'bitnet.cpp': {'verdict': 'refuses', 'status': 'magic'}},
        'llama.cpp', 'refused'))
    # 11/12. a header cut short of the file's end asks for more bytes; a file that ends inside it is refused.
    h, s = tensors_file([('a', 42, 64)], 18)
    out.append(case('header-truncated-no-verdict', h[:30], s, {
        'llama.cpp': {'verdict': 'no_verdict', 'status': 'truncated'}}, 'llama.cpp', 'undecided'))
    out.append(case('file-ends-inside-header', h[:30], 30, {
        'llama.cpp': {'verdict': 'refuses', 'status': 'short'}}, 'llama.cpp', 'refused'))
    # 13. an architecture the loader does not map.
    h, s = tensors_file([('a', 42, 64)], 18, arch='no-such-arch')
    out.append(case('unknown-architecture', h, s, {
        'llama.cpp': {'verdict': 'refuses', 'status': 'arch'}}, 'other', 'other_runtime'))
    return out


def document():
    return {'schema': 'trinity-gguf-verdict-fixtures-v1', 'runtimes': RUNTIMES, 'cases': cases()}


if __name__ == '__main__':
    (ROOT / OUT).write_text(json.dumps(document(), indent=1) + '\n')
