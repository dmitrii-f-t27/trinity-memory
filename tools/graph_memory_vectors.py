"""Vectors for the graph-memory specs vendored under specs/memory/graph (issue #136).

Imported by tools/generate-spec-vectors.py, which writes and checks
conformance/memory_graph_{scope,crypt,temporal}.json. Every expectation here is
computed by an independent Python restatement of the rule (and, for the
envelope, a pure-Python ChaCha20-Poly1305 written from RFC 8439), never by the
native library; tests/test_chat_store.py replays the vectors through the
functions the native library generates from the specs and from
t27/chat_crypto.t27. The RFC 8439, RFC 4231 and RFC 5869 vectors are copied
from the published documents.
"""
import hashlib
import hmac
import itertools
import struct

CREATED_AT = "2026-10-08T00:00:00Z"
GENERATOR = "tools/generate-spec-vectors.py (tools/graph_memory_vectors.py)"

# ---------------------------------------------------------------------------
# scope.t27

AGENT, PERSON, OPERATOR = 1, 2, 3
RAW_TTL_DEFAULT, RAW_TTL_MAX, DAY = 30 * 86400, 365 * 86400, 86400


def same_group(po, pu, ro, ru):
    return po != 0 and pu != 0 and po == ro and pu == ru


def may_read(pk, po, pu, pt, ro, ru, rt):
    if ro == 0 or ru == 0:
        return False
    if pk == AGENT:
        return pt != 0 and same_group(po, pu, ro, ru) and rt in (0, pt)
    if pk == PERSON:
        return pu != 0 and pu == ru
    return False


def may_write(pk, po, pu, pt, ro, ru, rt):
    return pk == AGENT and pt != 0 and same_group(po, pu, ro, ru) and rt in (0, pt)


def may_erase(pk, po, pu, ro, ru):
    if ro == 0 or ru == 0:
        return False
    if pk == PERSON:
        return pu != 0 and pu == ru
    if pk == AGENT:
        return same_group(po, pu, ro, ru)
    return pk == OPERATOR


def raw_ttl_ok(ttl):
    return 0 < ttl <= RAW_TTL_MAX


def raw_expired(created_at, ttl, now):
    if not raw_ttl_ok(ttl):
        return True
    return now >= created_at and now - created_at >= ttl


def vector(fn, args, expected):
    return {"fn": fn, "input": list(args), "expected": expected}


def build_scope():
    principals = [(1, 2, 5), (1, 2, 0), (0, 2, 0), (1, 0, 5), (0, 0, 0)]
    records = [(1, 2, 5), (1, 2, 0), (1, 2, 6), (9, 2, 5), (1, 9, 5), (0, 2, 5), (1, 0, 5), (9, 2, 0)]
    vectors = []
    for kind, p, r in itertools.product(range(5), principals, records):
        vectors.append(vector("may_read", (kind, *p, *r), may_read(kind, *p, *r)))
        vectors.append(vector("may_write", (kind, *p, *r), may_write(kind, *p, *r)))
    for kind, p, r in itertools.product(range(5), [(1, 2), (0, 2), (1, 0), (0, 0)], [(1, 2), (9, 2), (1, 9), (0, 2), (1, 0)]):
        vectors.append(vector("may_erase", (kind, *p, *r), may_erase(kind, *p, *r)))
    for purpose in range(4):
        vectors.append(vector("keep_raw", (purpose,), purpose == 2))
    for ttl in (0, 1, 100, RAW_TTL_DEFAULT, RAW_TTL_MAX, RAW_TTL_MAX + 1):
        vectors.append(vector("raw_ttl_ok", (ttl,), raw_ttl_ok(ttl)))
        for now in (999, 1000, 1099, 1100, 1000 + RAW_TTL_DEFAULT - 1, 1000 + RAW_TTL_DEFAULT):
            vectors.append(vector("raw_expired", (1000, ttl, now), raw_expired(1000, ttl, now)))
    for opted, host in itertools.product((False, True), range(5)):
        vectors.append(vector("mirror_allowed", (opted, host), opted and host == 1))
    return {
        "module": "MemoryGraphScope",
        "spec_path": "specs/memory/graph/scope.t27",
        "schema_version": 2,
        "format_family": "Conformance",
        "vector_name": "Graph memory scope",
        "description": "Who may read, write and erase a record of the graph memory; raw TTL and mirror rules. "
                       "Expectations are independent Python restatements; tests/test_chat_store.py replays them "
                       "through the functions the native library generates from the spec.",
        "created_at": CREATED_AT,
        "generator": GENERATOR,
        "constants": {"principals": {"agent": 1, "person": 2, "operator": 3},
                      "hosts": {"self_hosted": 1, "zep_cloud": 2, "other": 3},
                      "purposes": {"facts_only": 1, "reply_context": 2},
                      "raw_ttl_default_seconds": RAW_TTL_DEFAULT, "raw_ttl_max_seconds": RAW_TTL_MAX,
                      "mirror_default_on": False},
        "invariants": [{"id": "default_ttl_is_thirty_days", "condition": "RAW_TTL_DEFAULT == 30 * DAY_SECONDS"},
                       {"id": "max_ttl_is_a_year", "condition": "RAW_TTL_MAX == 365 * DAY_SECONDS"}],
        "vectors": vectors,
    }


# ---------------------------------------------------------------------------
# temporal.t27 (slice 1, gHashTag/t27#7830)

def fact_is_current(invalid_at, expired_at, now):
    return not (expired_at and expired_at <= now) and not (invalid_at and invalid_at <= now)


def fact_true_at(valid_at, invalid_at, t):
    return not (valid_at and valid_at > t) and not (invalid_at and invalid_at <= t)


def known_as_of(created_at, expired_at, as_of):
    return created_at <= as_of and not (expired_at and expired_at <= as_of)


def may_invalidate(old_valid_at, old_expired_at, new_valid_at):
    if old_expired_at:
        return False
    return old_valid_at == 0 or new_valid_at == 0 or new_valid_at > old_valid_at


def retired_invalid_at(old_invalid_at, new_valid_at, now):
    end = new_valid_at or now
    return old_invalid_at if old_invalid_at and old_invalid_at < end else end


def build_temporal():
    times = (0, 100, 200, 500)
    vectors = []
    for a, b, c in itertools.product(times, repeat=3):
        vectors.append(vector("fact_is_current", (a, b, c), fact_is_current(a, b, c)))
        vectors.append(vector("fact_true_at", (a, b, c), fact_true_at(a, b, c)))
        vectors.append(vector("known_as_of", (a, b, c), known_as_of(a, b, c)))
        vectors.append(vector("may_invalidate", (a, b, c), may_invalidate(a, b, c)))
        vectors.append(vector("retired_invalid_at", (a, b, c or 900), retired_invalid_at(a, b, c or 900)))
    return {
        "module": "MemoryGraphTemporal",
        "spec_path": "specs/memory/graph/temporal.t27",
        "schema_version": 2,
        "format_family": "Conformance",
        "vector_name": "Graph memory time rules",
        "description": "Current, true-at, known-as-of and the retirement of an older fact (Graphiti's bi-temporal "
                       "rules), over every triple of four times. Independent Python restatements.",
        "created_at": CREATED_AT,
        "generator": GENERATOR,
        "constants": {"unset": 0},
        "invariants": [],
        "vectors": vectors,
    }


# ---------------------------------------------------------------------------
# crypt.t27 and the envelope of t27/chat_crypto.t27

KEY_ACTIVE, KEY_RETIRED, KEY_DESTROYED = 1, 2, 3
MASK32 = 0xFFFFFFFF


def _rotl(x, n):
    return ((x << n) | (x >> (32 - n))) & MASK32


def chacha20_block(key, counter, nonce):
    state = [0x61707865, 0x3320646E, 0x79622D32, 0x6B206574, *struct.unpack("<8I", key), counter,
             *struct.unpack("<3I", nonce)]
    x = list(state)

    def quarter(a, b, c, d):
        x[a] = (x[a] + x[b]) & MASK32; x[d] = _rotl(x[d] ^ x[a], 16)
        x[c] = (x[c] + x[d]) & MASK32; x[b] = _rotl(x[b] ^ x[c], 12)
        x[a] = (x[a] + x[b]) & MASK32; x[d] = _rotl(x[d] ^ x[a], 8)
        x[c] = (x[c] + x[d]) & MASK32; x[b] = _rotl(x[b] ^ x[c], 7)

    for _ in range(10):
        quarter(0, 4, 8, 12); quarter(1, 5, 9, 13); quarter(2, 6, 10, 14); quarter(3, 7, 11, 15)
        quarter(0, 5, 10, 15); quarter(1, 6, 11, 12); quarter(2, 7, 8, 13); quarter(3, 4, 9, 14)
    return struct.pack("<16I", *((a + b) & MASK32 for a, b in zip(x, state)))


def chacha20_xor(key, counter, nonce, data):
    out = bytearray()
    for i in range(0, len(data), 64):
        block = chacha20_block(key, counter + i // 64, nonce)
        out += bytes(a ^ b for a, b in zip(data[i:i + 64], block))
    return bytes(out)


def poly1305(key, message):
    r = int.from_bytes(key[:16], "little") & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
    s = int.from_bytes(key[16:], "little")
    p, acc = (1 << 130) - 5, 0
    for i in range(0, len(message), 16):
        chunk = message[i:i + 16] + b"\x01"
        acc = (acc + int.from_bytes(chunk, "little")) * r % p
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, "little")


def _pad16(data):
    return data + bytes(-len(data) % 16)


def aead_seal(key, nonce, aad, plaintext):
    otk = chacha20_block(key, 0, nonce)[:32]
    ciphertext = chacha20_xor(key, 1, nonce, plaintext)
    mac_data = _pad16(aad) + _pad16(ciphertext) + struct.pack("<QQ", len(aad), len(ciphertext))
    return ciphertext, poly1305(otk, mac_data)


def aad_bytes(kind, owner, user, thread, record, key_version):
    return b"TGA1" + bytes([kind, 0, 0, 0]) + struct.pack("<QQQQI", owner, user, thread, record, key_version)


def envelope(key, key_version, nonce, kind, owner, user, thread, record, plaintext):
    ciphertext, tag = aead_seal(key, nonce, aad_bytes(kind, owner, user, thread, record, key_version), plaintext)
    return b"TGE1" + bytes([1, 1, 0, 0]) + struct.pack("<I", key_version) + nonce + ciphertext + tag


def open_status(header, state, version_match, kms_ok, tag_ok):
    if not header:
        return -1
    if state not in (KEY_ACTIVE, KEY_RETIRED):
        return -2
    if not version_match:
        return -3
    if not kms_ok:
        return -4
    return 0 if tag_ok else -5


def key_transition_ok(a, b):
    return (a, b) in {(1, 2), (1, 3), (2, 3)}


def plaintext_bytes(n):
    return n - 40 if 40 <= n <= 40 + 1048576 else -1


def header_ok(m0, m1, m2, m3, version, alg, r0, r1, key_version, total):
    return (bytes([m0, m1, m2, m3]) == b"TGE1" and version == 1 and alg == 1 and r0 == 0 and r1 == 0
            and key_version != 0 and plaintext_bytes(total) >= 0)


def service_token_ok(iat, exp, now):
    return exp != 0 and iat != 0 and exp > iat and exp - iat <= 900 and iat <= now + 30 and now < exp


def token(index_key, field, term):
    return hmac.new(index_key, b"TGT1" + bytes([field]) + term.encode(), hashlib.sha256).digest()[:16]


def hkdf(ikm, info, length=32, salt=b""):
    prk = hmac.new(salt or bytes(32), ikm, hashlib.sha256).digest()
    out, t, n = b"", b"", 1
    while len(out) < length:
        t = hmac.new(prk, t + info + bytes([n]), hashlib.sha256).digest()
        out += t
        n += 1
    return out[:length]


RFC_8439 = {
    "chacha20_block_2_3_2": {
        "key": bytes(range(32)).hex(), "counter": 1, "nonce": "000000090000004a00000000",
        "block": "10f1e7e4d13b5915500fdd1fa32071c4c7d1f4c733c068030422aa9ac3d46c4e"
                 "d2826446079faa0914c2d705d98b02a2b5129cd1de164eb9cbd083e8a2503c4e"},
    "poly1305_2_5_2": {
        "key": "85d6be7857556d337f4452fe42d506a80103808afb0db2fd4abff6af4149f51b",
        "message": b"Cryptographic Forum Research Group".hex(), "tag": "a8061dc1305136c6c22b8baf0c0127a9"},
    "aead_2_8_2": {
        "key": "808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f",
        "nonce": "070000004041424344454647", "aad": "50515253c0c1c2c3c4c5c6c7",
        "plaintext": b"Ladies and Gentlemen of the class of '99: If I could offer you only one tip for the future, "
                     b"sunscreen would be it.".hex(),
        "ciphertext": "d31a8d34648e60db7b86afbc53ef7ec2a4aded51296e08fea9e2b5a736ee62d63dbea45e8ca9671282fafb69da92728b"
                      "1a71de0a9e060b2905d6a5b67ecd3b3692ddbd7f2d778b8c9803aee328091b58fab324e4fad675945585808b4831d7bc"
                      "3ff4def08e4b7a9de576d26586cec64b6116",
        "tag": "1ae10b594f09e26a7e902ecbd0600691"},
}
RFC_4231 = [
    {"case": 2, "key": b"Jefe".hex(), "data": b"what do ya want for nothing?".hex(),
     "hmac": "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"},
    {"case": 6, "key": "aa" * 131, "data": b"Test Using Larger Than Block-Size Key - Hash Key First".hex(),
     "hmac": "60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54"},
]
RFC_5869 = [
    {"case": 1, "ikm": "0b" * 22, "salt": "000102030405060708090a0b0c", "info": "f0f1f2f3f4f5f6f7f8f9", "length": 42,
     "okm": "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865"},
    {"case": 3, "ikm": "0b" * 22, "salt": "", "info": "", "length": 42,
     "okm": "8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879ec3454e5f3c738d2d9d201395faa4b61a96c8"},
]
FIPS_180_4 = [
    {"message": "", "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"},
    {"message": b"abc".hex(), "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"},
    {"message": b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq".hex(),
     "sha256": "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"},
]


def build_crypt():
    vectors = []
    for a, b in itertools.product(range(5), repeat=2):
        vectors.append(vector("key_transition_ok", (a, b), key_transition_ok(a, b)))
    for state in range(5):
        vectors.append(vector("is_key_state", (state,), state in (1, 2, 3)))
        vectors.append(vector("may_seal", (state,), state == 1))
        vectors.append(vector("may_open", (state,), state in (1, 2)))
        vectors.append(vector("forget", (state,), 0 if state == 0 else 3))
    for seals in (0, 1, (1 << 32) - 1, 1 << 32, (1 << 32) + 1):
        vectors.append(vector("must_rotate", (seals,), seals >= 1 << 32))
    for size in (0, 1, 39, 40, 41, 1048616, 1048617):
        vectors.append(vector("plaintext_bytes", (size,), plaintext_bytes(size)))
    good = [0x54, 0x47, 0x45, 0x31, 1, 1, 0, 0, 1, 45]
    vectors.append(vector("header_ok", good, True))
    for index, bad in [(0, 0x55), (1, 0x48), (2, 0x46), (3, 0x32), (4, 2), (5, 2), (6, 1), (7, 1), (8, 0), (9, 39)]:
        args = list(good)
        args[index] = bad
        vectors.append(vector("header_ok", args, header_ok(*args)))
    vectors.append(vector("header_ok", good[:9] + [40], True))
    for header, state, version, kms, tag in itertools.product((False, True), range(5), (False, True), (False, True), (False, True)):
        vectors.append(vector("open_status", (header, state, version, kms, tag), open_status(header, state, version, kms, tag)))
    for iat, exp, now in [(1000, 1900, 1000), (1000, 1900, 1899), (1000, 1900, 1900), (1000, 1901, 1000),
                          (1000, 0, 1000), (0, 900, 100), (1000, 1000, 999), (1030, 1100, 1000), (1031, 1100, 1000)]:
        vectors.append(vector("service_token_ok", (iat, exp, now), service_token_ok(iat, exp, now)))
    scopes = [(3, 1, 2, 0, 9, 1), (1, 258, 7, 5, 6699, 16909060), (2, (1 << 63) - 1, 1, 0, 1, 4294967295)]
    for scope in scopes:
        vectors.append({"fn": "aad", "input": list(scope), "expected": aad_bytes(*scope).hex()})
    key = bytes(range(0x80, 0xA0))
    for index, text in enumerate([b"", b"x", bytes(range(64)), bytes(range(65)),
                                  "Анна работает в Acme с 2026 года".encode()]):
        nonce = bytes([index]) + bytes(range(1, 12))
        scope = (3, 11, 22, 0, 1000 + index)
        env = envelope(key, 1, nonce, *scope, text)
        vectors.append({"fn": "envelope", "key": key.hex(), "key_version": 1, "nonce": nonce.hex(),
                        "scope": list(scope), "plaintext": text.hex(), "expected": env.hex()})
    env = envelope(key, 1, bytes(12), 1, 11, 22, 33, 44, b"secret")
    moves = [("same_row", (1, 11, 22, 33, 44), 1, 1, True, 6), ("other_kind", (3, 11, 22, 33, 44), 1, 1, True, -5),
             ("other_owner", (1, 12, 22, 33, 44), 1, 1, True, -5), ("other_user", (1, 11, 23, 33, 44), 1, 1, True, -5),
             ("other_thread", (1, 11, 22, 34, 44), 1, 1, True, -5), ("other_record", (1, 11, 22, 33, 45), 1, 1, True, -5),
             ("kms_denied", (1, 11, 22, 33, 44), 1, 1, False, -4), ("retired_key", (1, 11, 22, 33, 44), 2, 1, True, 6),
             ("destroyed_key", (1, 11, 22, 33, 44), 3, 1, True, -2), ("other_version", (1, 11, 22, 33, 44), 1, 2, True, -3)]
    for name, scope, state, held, kms, expected in moves:
        vectors.append({"fn": "open", "id": name, "key": key.hex(), "envelope": env.hex(), "scope": list(scope),
                        "key_state": state, "held_version": held, "kms_ok": kms, "expected": expected})
    flipped = bytearray(env)
    flipped[30] ^= 1
    vectors.append({"fn": "open", "id": "flipped_ciphertext", "key": key.hex(), "envelope": bytes(flipped).hex(),
                    "scope": [1, 11, 22, 33, 44], "key_state": 1, "held_version": 1, "kms_ok": True, "expected": -5})
    for name, cut in (("bad_magic", 0), ("bad_version", 4), ("bad_reserved", 6)):
        broken = bytearray(env)
        broken[cut] ^= 1
        vectors.append({"fn": "open", "id": name, "key": key.hex(), "envelope": bytes(broken).hex(),
                        "scope": [1, 11, 22, 33, 44], "key_state": 1, "held_version": 1, "kms_ok": True, "expected": -1})
    index_key = hkdf(key, b"TG index v1")
    vectors.append({"fn": "index_key", "dek": key.hex(), "info": b"TG index v1".hex(), "expected": index_key.hex()})
    for field, term in ((1, "anna"), (2, "acme"), (2, "работает"), (2, "")):
        vectors.append({"fn": "token", "index_key": index_key.hex(), "field": field, "term": term,
                        "expected": token(index_key, field, term).hex()})
    for name, case in RFC_8439.items():
        vectors.append({"fn": "rfc8439", "id": name, **case})
    vectors += [{"fn": "rfc4231", **case} for case in RFC_4231]
    vectors += [{"fn": "rfc5869", **case} for case in RFC_5869]
    vectors += [{"fn": "fips180_4", **case} for case in FIPS_180_4]
    return {
        "module": "MemoryGraphCrypt",
        "spec_path": "specs/memory/graph/crypt.t27",
        "schema_version": 2,
        "format_family": "Conformance",
        "vector_name": "Graph memory sealing",
        "description": "Key states, envelope and associated-data layout, open status order, service tokens, and "
                       "exact TGE1 envelopes from a pure-Python ChaCha20-Poly1305 (RFC 8439), with the published "
                       "RFC 8439, RFC 4231, RFC 5869 and FIPS 180-4 vectors that t27/chat_crypto.t27 must reproduce.",
        "created_at": CREATED_AT,
        "generator": GENERATOR,
        "constants": {"key_states": {"active": 1, "retired": 2, "destroyed": 3}, "alg_chacha20_poly1305": 1,
                      "key_bytes": 32, "nonce_bytes": 12, "tag_bytes": 16, "envelope_header_bytes": 24,
                      "envelope_overhead_bytes": 40, "envelope_max_plaintext_bytes": 1048576, "aad_bytes": 44,
                      "token_bytes": 16, "max_seals_per_key": 1 << 32, "service_token_max_ttl": 900,
                      "service_token_skew": 30,
                      "open_status": {"ok": 0, "header": -1, "key_destroyed": -2, "key_version": -3,
                                      "kms_denied": -4, "tag": -5}},
        "invariants": [{"id": "header_is_contiguous", "condition": "ENV_NONCE_OFFSET + NONCE_BYTES == ENV_HEADER_BYTES"},
                       {"id": "overhead_is_header_and_tag", "condition": "ENV_HEADER_BYTES + TAG_BYTES == ENV_OVERHEAD_BYTES"},
                       {"id": "aad_is_contiguous", "condition": "AAD_KEY_VERSION_OFFSET + 4 == AAD_BYTES"},
                       {"id": "token_is_a_hmac_prefix", "condition": "TOKEN_BYTES <= HMAC_BYTES"}],
        "vectors": vectors,
    }


def self_check():
    """The pure-Python primitives reproduce the published vectors before any expectation is written."""
    case = RFC_8439["chacha20_block_2_3_2"]
    assert chacha20_block(bytes.fromhex(case["key"]), case["counter"], bytes.fromhex(case["nonce"])).hex() == case["block"]
    case = RFC_8439["poly1305_2_5_2"]
    assert poly1305(bytes.fromhex(case["key"]), bytes.fromhex(case["message"])).hex() == case["tag"]
    case = RFC_8439["aead_2_8_2"]
    ct, tag = aead_seal(bytes.fromhex(case["key"]), bytes.fromhex(case["nonce"]), bytes.fromhex(case["aad"]),
                        bytes.fromhex(case["plaintext"]))
    assert (ct.hex(), tag.hex()) == (case["ciphertext"], case["tag"])
    for case in RFC_5869:
        assert hkdf(bytes.fromhex(case["ikm"]), bytes.fromhex(case["info"]), case["length"],
                    bytes.fromhex(case["salt"])).hex() == case["okm"]


OUTPUTS = {"memory_graph_scope.json": build_scope, "memory_graph_temporal.json": build_temporal,
           "memory_graph_crypt.json": build_crypt}
