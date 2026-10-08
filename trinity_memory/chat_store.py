"""Encrypted graph-memory store for chats (issue #136).

A thin sqlite layer. Every decision is made by native t27 code:

* the rules are the functions generated from the vendored specs
  ``specs/memory/graph/{scope,crypt,temporal}.t27`` (gHashTag/t27 epic #7828):
  ``may_read``, ``may_write``, ``may_erase``, ``keep_raw``, ``raw_expired``,
  ``mirror_allowed``, ``key_transition_ok``, ``must_rotate``,
  ``service_token_ok``, ``fact_is_current``, ``may_invalidate``,
  ``retired_invalid_at``;
* the cryptography is ``t27/chat_crypto.t27``: the "TGE1" ChaCha20-Poly1305
  envelope with the "TGA1" associated data rebuilt from the row's own scope,
  HMAC search tokens, HKDF and HS256 service tokens.

This module only moves bytes between sqlite, the KMS and those functions.
Text is never written to the database: the ``records`` table holds the scope,
the four times and the envelope. A group's data key is stored only wrapped by
the KMS; forgetting a group destroys that key in the KMS and in the table.

``LocalKms`` is the in-process KMS used by tests and local runs. In production
the KMS lives outside the app host (cloud KMS or Vault), logs every unwrap, and
must refuse to unwrap a destroyed group's key even when an old wrapped copy
comes back from a backup.
"""
from __future__ import annotations

import base64
import ctypes as C
import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field

from . import _native as n

U8P = C.POINTER(C.c_uint8)

KIND_EPISODE, KIND_ENTITY, KIND_FACT = 1, 2, 3
AGENT, PERSON, OPERATOR = 1, 2, 3
HOST_SELF_HOSTED, HOST_ZEP_CLOUD, HOST_OTHER = 1, 2, 3
PURPOSE_FACTS_ONLY, PURPOSE_REPLY_CONTEXT = 1, 2
KEY_ACTIVE, KEY_RETIRED, KEY_DESTROYED = 1, 2, 3
FIELD_ENTITY_NAME, FIELD_TERM = 1, 2
RAW_TTL_DEFAULT = 2592000
MAX_ID = (1 << 63) - 1
INDEX_INFO = b"TG index v1"

OPEN_STATUS = {-1: "HEADER", -2: "KEY_DESTROYED", -3: "KEY_VERSION", -4: "KMS_DENIED", -5: "TAG", -6: "CAPACITY"}


class ScopeError(PermissionError):
    """The scope rules of scope.t27 refuse the operation."""


class OpenError(ValueError):
    """An envelope did not open; ``status`` is the crypt.t27 name."""

    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


class KmsDenied(PermissionError):
    pass


def _fn(name, result, *arguments):
    return n.function(name, result, tuple(arguments))


def _rule(name, *arguments):
    return _fn(name, C.c_bool, *arguments)


_may_read = _rule("may_read", C.c_uint8, C.c_uint64, C.c_uint64, C.c_uint64, C.c_uint64, C.c_uint64, C.c_uint64)
_may_write = _rule("may_write", C.c_uint8, C.c_uint64, C.c_uint64, C.c_uint64, C.c_uint64, C.c_uint64, C.c_uint64)
_may_erase = _rule("may_erase", C.c_uint8, C.c_uint64, C.c_uint64, C.c_uint64, C.c_uint64)
_keep_raw = _rule("keep_raw", C.c_uint8)
_raw_ttl_ok = _rule("raw_ttl_ok", C.c_uint64)
_raw_expired = _rule("raw_expired", C.c_uint64, C.c_uint64, C.c_uint64)
_mirror_allowed = _rule("mirror_allowed", C.c_bool, C.c_uint8)
_key_transition_ok = _rule("key_transition_ok", C.c_uint8, C.c_uint8)
_must_rotate = _rule("must_rotate", C.c_uint64)
_service_token_ok = _rule("service_token_ok", C.c_uint64, C.c_uint64, C.c_uint64)
_fact_is_current = _rule("fact_is_current", C.c_uint64, C.c_uint64, C.c_uint64)
_may_invalidate = _rule("may_invalidate", C.c_uint64, C.c_uint64, C.c_uint64)
_retired_invalid_at = _fn("retired_invalid_at", C.c_uint64, C.c_uint64, C.c_uint64, C.c_uint64)
_env_seal = _fn("cm_env_seal", C.c_int64, U8P, C.c_uint32, U8P, C.c_uint8, C.c_uint64, C.c_uint64,
                C.c_uint64, C.c_uint64, U8P, C.c_size_t, U8P, C.c_size_t)
_env_open = _fn("cm_env_open", C.c_int64, U8P, C.c_uint8, C.c_uint32, C.c_bool, C.c_uint8, C.c_uint64,
                C.c_uint64, C.c_uint64, C.c_uint64, U8P, C.c_size_t, U8P, C.c_size_t)
_env_key_version = _fn("cm_env_key_version", C.c_uint32, U8P, C.c_size_t)
_hmac = _fn("cm_hmac_sha256", None, U8P, C.c_size_t, U8P, C.c_size_t, U8P)
_hkdf = _fn("cm_hkdf_sha256", C.c_int32, U8P, C.c_size_t, U8P, C.c_size_t, U8P, C.c_size_t, U8P, C.c_size_t)
_token = _fn("cm_token", None, U8P, C.c_uint8, U8P, C.c_size_t, U8P)


def _buf(data: bytes):
    return n.octets(data)


def seal(key: bytes, key_version: int, nonce: bytes, kind: int, owner: int, user: int, thread: int,
         record: int, plaintext: bytes) -> bytes:
    out = n.buffer(len(plaintext) + 40)
    size = _env_seal(_buf(key), key_version, _buf(nonce), kind, owner, user, thread, record,
                     _buf(plaintext), len(plaintext), out, len(plaintext) + 40)
    if size < 0:
        raise ValueError(f"cm_env_seal refused: {size}")
    return bytes(out[:size])


def open_envelope(key: bytes | None, key_state: int, held_version: int, kms_ok: bool, kind: int,
                  owner: int, user: int, thread: int, record: int, envelope: bytes) -> bytes:
    capacity = max(1, len(envelope))
    out = n.buffer(capacity)
    status = _env_open(_buf(key or bytes(32)), key_state, held_version, kms_ok, kind, owner, user, thread,
                       record, _buf(envelope), len(envelope), out, capacity)
    if status < 0:
        raise OpenError(OPEN_STATUS.get(status, str(status)))
    return bytes(out[:status])


def hmac_sha256(key: bytes, message: bytes) -> bytes:
    out = n.buffer(32)
    _hmac(_buf(key), len(key), _buf(message), len(message), out)
    return bytes(out[:32])


def hkdf_sha256(ikm: bytes, info: bytes, length: int = 32, salt: bytes = b"") -> bytes:
    out = n.buffer(length)
    if _hkdf(_buf(salt), len(salt), _buf(ikm), len(ikm), _buf(info), len(info), out, length) != 0:
        raise ValueError("HKDF length over 8160 bytes")
    return bytes(out[:length])


def search_token(index_key: bytes, field_id: int, term: str) -> bytes:
    data = term.encode("utf-8")
    out = n.buffer(16)
    _token(_buf(index_key), field_id, _buf(data), len(data), out)
    return bytes(out[:16])


def terms(text: str) -> list[str]:
    """Normalized search terms: lower-case words of two or more letters or digits."""
    seen = []
    for word in re.findall(r"\w{2,}", text.lower()):
        if word not in seen:
            seen.append(word)
    return seen


def mirror_allowed(opted_in: bool, host: int) -> bool:
    return bool(_mirror_allowed(bool(opted_in), host))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def mint_service_token(secret: bytes, now: int, ttl: int = 900, issuer: str = "vibee-render") -> str:
    """HS256 token for the external store that expires (crypt.t27 service_token_ok)."""
    claims = {"iss": issuer, "iat": int(now), "exp": int(now) + int(ttl)}
    if not _service_token_ok(claims["iat"], claims["exp"], int(now)):
        raise ValueError("service tokens expire within 900 s")
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    return f"{head}.{body}.{_b64(hmac_sha256(secret, f'{head}.{body}'.encode()))}"


def verify_service_token(token: str, secret: bytes, now: int) -> bool:
    try:
        head, body, signature = token.split(".")
        if not secrets.compare_digest(_unb64(signature), hmac_sha256(secret, f"{head}.{body}".encode())):
            return False
        if json.loads(_unb64(head)).get("alg") != "HS256":
            return False
        claims = json.loads(_unb64(body))
        return bool(_service_token_ok(int(claims.get("iat", 0)), int(claims.get("exp", 0)), int(now)))
    except (ValueError, TypeError, json.JSONDecodeError):
        return False


@dataclass(frozen=True)
class Principal:
    kind: int
    owner: int = 0
    user: int = 0
    thread: int = 0

    @classmethod
    def agent(cls, owner: int, user: int, thread: int) -> "Principal":
        return cls(AGENT, owner, user, thread)

    @classmethod
    def person(cls, user: int) -> "Principal":
        return cls(PERSON, 0, user, 0)

    @classmethod
    def operator(cls) -> "Principal":
        return cls(OPERATOR)


@dataclass
class LocalKms:
    """In-process KMS: a key-encryption key in memory, a role check and a log.

    Wrapping is the same TGE1 envelope under the KEK, bound to (owner, user,
    version). ``destroy`` makes the group's key version unrecoverable here,
    including from an old wrapped copy.
    """

    roles: frozenset = frozenset({"agent"})
    kek: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    log: list = field(default_factory=list)
    destroyed: set = field(default_factory=set)

    def generate(self, owner: int, user: int, version: int) -> tuple[bytes, bytes]:
        dek = secrets.token_bytes(32)
        wrapped = seal(self.kek, 1, secrets.token_bytes(12), 0, owner, user, 0, version, dek)
        self.log.append(("generate", owner, user, version, None, True))
        return dek, wrapped

    def unwrap(self, wrapped: bytes, owner: int, user: int, version: int, role: str) -> bytes:
        granted = role in self.roles and (owner, user, version) not in self.destroyed
        self.log.append(("unwrap", owner, user, version, role, granted))
        if not granted:
            raise KmsDenied(f"KMS refused {role} for ({owner}, {user}, v{version})")
        return open_envelope(self.kek, KEY_ACTIVE, 1, True, 0, owner, user, 0, version, wrapped)

    def destroy(self, owner: int, user: int, version: int) -> None:
        self.destroyed.add((owner, user, version))
        self.log.append(("destroy", owner, user, version, None, True))


SCHEMA = """
CREATE TABLE IF NOT EXISTS keys (
    owner INTEGER NOT NULL, user INTEGER NOT NULL, version INTEGER NOT NULL,
    state INTEGER NOT NULL, wrapped BLOB, seals INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (owner, user, version));
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY, kind INTEGER NOT NULL, owner INTEGER NOT NULL, user INTEGER NOT NULL,
    thread INTEGER NOT NULL, created_at INTEGER NOT NULL, valid_at INTEGER NOT NULL DEFAULT 0,
    invalid_at INTEGER NOT NULL DEFAULT 0, expired_at INTEGER NOT NULL DEFAULT 0,
    key_version INTEGER NOT NULL, envelope BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS records_scope ON records (owner, user, thread);
CREATE TABLE IF NOT EXISTS tokens (record INTEGER NOT NULL, token BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS tokens_token ON tokens (token);
CREATE TABLE IF NOT EXISTS mirror (owner INTEGER PRIMARY KEY, opted_in INTEGER NOT NULL, host INTEGER NOT NULL);
"""


def _check_id(value: int, name: str, allow_zero: bool = False) -> int:
    if type(value) is not int or value < 0 or value > MAX_ID or (value == 0 and not allow_zero):
        raise ValueError(f"{name} must be an integer in 1..2^63-1")
    return value


class ChatMemoryStore:
    def __init__(self, path: str, kms, raw_ttl: int = RAW_TTL_DEFAULT, clock=time.time,
                 purpose: int = PURPOSE_REPLY_CONTEXT, role: str = "agent"):
        if not _raw_ttl_ok(raw_ttl):
            raise ValueError("raw TTL must be 1..365 days")
        self.kms, self.raw_ttl, self.clock, self.purpose, self.role = kms, raw_ttl, clock, purpose, role
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA secure_delete = ON")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def _now(self) -> int:
        return int(self.clock())

    # Keys -------------------------------------------------------------------

    def _active_key(self, owner: int, user: int) -> tuple[int, bytes]:
        row = self.db.execute("SELECT version, wrapped, seals FROM keys WHERE owner=? AND user=? AND state=?",
                              (owner, user, KEY_ACTIVE)).fetchone()
        if row and _must_rotate(row[2]):
            assert _key_transition_ok(KEY_ACTIVE, KEY_RETIRED)
            self.db.execute("UPDATE keys SET state=? WHERE owner=? AND user=? AND version=?",
                            (KEY_RETIRED, owner, user, row[0]))
            row = None
        if row:
            version = row[0]
            dek = self.kms.unwrap(row[1], owner, user, version, self.role)
        else:
            last = self.db.execute("SELECT MAX(version) FROM keys WHERE owner=? AND user=?", (owner, user)).fetchone()[0]
            version = (last or 0) + 1
            dek, wrapped = self.kms.generate(owner, user, version)
            self.db.execute("INSERT INTO keys (owner, user, version, state, wrapped) VALUES (?, ?, ?, ?, ?)",
                            (owner, user, version, KEY_ACTIVE, wrapped))
        self.db.execute("UPDATE keys SET seals = seals + 1 WHERE owner=? AND user=? AND version=?", (owner, user, version))
        return version, dek

    def _key(self, owner: int, user: int, version: int, role: str):
        """(key bytes or None, state, kms_ok) for opening an envelope of that version."""
        row = self.db.execute("SELECT state, wrapped FROM keys WHERE owner=? AND user=? AND version=?",
                              (owner, user, version)).fetchone()
        if row is None or row[0] == KEY_DESTROYED or row[1] is None:
            return None, KEY_DESTROYED, False
        try:
            return self.kms.unwrap(row[1], owner, user, version, role), row[0], True
        except KmsDenied:
            return None, row[0], False

    def _index_keys(self, owner: int, user: int, role: str) -> list[bytes]:
        keys = []
        for (version,) in self.db.execute("SELECT version FROM keys WHERE owner=? AND user=? AND state IN (?, ?)",
                                          (owner, user, KEY_ACTIVE, KEY_RETIRED)).fetchall():
            dek, _, ok = self._key(owner, user, version, role)
            if ok:
                keys.append(hkdf_sha256(dek, INDEX_INFO))
        return keys

    # Writes -----------------------------------------------------------------

    def _write(self, principal: Principal, kind: int, thread: int, text: str, valid_at: int = 0,
               tokens_of: str | None = None) -> int:
        owner, user = _check_id(principal.owner, "owner"), _check_id(principal.user, "user")
        if not _may_write(principal.kind, principal.owner, principal.user, principal.thread, owner, user, thread):
            raise ScopeError("scope.t27 may_write refuses this record")
        now = self._now()
        with self.db:
            version, dek = self._active_key(owner, user)
            while True:
                record = secrets.randbelow(MAX_ID) + 1
                if not self.db.execute("SELECT 1 FROM records WHERE id=?", (record,)).fetchone():
                    break
            envelope = seal(dek, version, secrets.token_bytes(12), kind, owner, user, thread, record, text.encode("utf-8"))
            self.db.execute("INSERT INTO records (id, kind, owner, user, thread, created_at, valid_at, key_version, envelope) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (record, kind, owner, user, thread, now, valid_at, version, envelope))
            index_key = hkdf_sha256(dek, INDEX_INFO)
            field_id = FIELD_ENTITY_NAME if kind == KIND_ENTITY else FIELD_TERM
            for term in terms(tokens_of if tokens_of is not None else text):
                self.db.execute("INSERT INTO tokens (record, token) VALUES (?, ?)",
                                (record, search_token(index_key, field_id, term)))
        return record

    def add_episode(self, principal: Principal, text: str) -> int | None:
        """Keep a raw message only when the reply needs recent wording (scope.t27 keep_raw)."""
        if not _keep_raw(self.purpose):
            return None
        return self._write(principal, KIND_EPISODE, principal.thread, text)

    def add_entity(self, principal: Principal, name: str, summary: str = "") -> int:
        return self._write(principal, KIND_ENTITY, 0, json.dumps({"name": name, "summary": summary}), tokens_of=name)

    def add_fact(self, principal: Principal, sentence: str, valid_at: int = 0) -> int:
        return self._write(principal, KIND_FACT, 0, sentence, valid_at=valid_at)

    def supersede(self, principal: Principal, old: int, sentence: str, valid_at: int = 0) -> int:
        """A newer fact retires an older one by the temporal.t27 rules; nothing is deleted."""
        row = self._row(principal, old)
        if row["kind"] != KIND_FACT:
            raise ValueError("only facts are superseded")
        new = self.add_fact(principal, sentence, valid_at)
        now = self._now()
        if _may_invalidate(row["valid_at"], row["expired_at"], valid_at):
            with self.db:
                self.db.execute("UPDATE records SET invalid_at=?, expired_at=? WHERE id=?",
                                (_retired_invalid_at(row["invalid_at"], valid_at, now), now, old))
        return new

    # Reads ------------------------------------------------------------------

    def _row(self, principal: Principal, record: int) -> dict:
        names = ("id", "kind", "owner", "user", "thread", "created_at", "valid_at", "invalid_at",
                 "expired_at", "key_version", "envelope")
        found = self.db.execute(f"SELECT {', '.join(names)} FROM records WHERE id=?", (record,)).fetchone()
        if found is None:
            raise KeyError(record)
        row = dict(zip(names, found))
        if not _may_read(principal.kind, principal.owner, principal.user, principal.thread,
                         row["owner"], row["user"], row["thread"]):
            raise ScopeError("scope.t27 may_read refuses this record")
        return row

    def _open(self, row: dict) -> str:
        version = _env_key_version(_buf(row["envelope"]), len(row["envelope"]))
        dek, state, kms_ok = self._key(row["owner"], row["user"], version, self.role)
        return open_envelope(dek, state, version, kms_ok, row["kind"], row["owner"], row["user"], row["thread"],
                             row["id"], row["envelope"]).decode("utf-8")

    def read(self, principal: Principal, record: int) -> str:
        return self._open(self._row(principal, record))

    def _visible_ids(self, principal: Principal) -> list[int]:
        # The filter is built from the principal, never from the request (scope.t27).
        if principal.kind == AGENT:
            rows = self.db.execute("SELECT id FROM records WHERE owner=? AND user=? AND thread IN (0, ?)",
                                   (principal.owner, principal.user, principal.thread)).fetchall()
        elif principal.kind == PERSON:
            rows = self.db.execute("SELECT id FROM records WHERE user=?", (principal.user,)).fetchall()
        else:
            rows = []
        return [r[0] for r in rows]

    def recall(self, principal: Principal) -> list[dict]:
        """Current facts and entities, and unexpired episodes, that the principal may read."""
        now, out = self._now(), []
        for record in self._visible_ids(principal):
            row = self._row(principal, record)
            if row["kind"] == KIND_FACT and not _fact_is_current(row["invalid_at"], row["expired_at"], now):
                continue
            if row["kind"] == KIND_EPISODE and _raw_expired(row["created_at"], self.raw_ttl, now):
                continue
            out.append({"id": record, "kind": row["kind"], "thread": row["thread"], "text": self._open(row)})
        return sorted(out, key=lambda item: (item["kind"], item["id"]))

    def search(self, principal: Principal, term: str) -> list[int]:
        if principal.kind != AGENT:
            return []
        word = terms(term)
        if not word:
            return []
        candidates = set()
        for index_key in self._index_keys(principal.owner, principal.user, self.role):
            for field_id in (FIELD_TERM, FIELD_ENTITY_NAME):
                token = search_token(index_key, field_id, word[0])
                candidates.update(r[0] for r in self.db.execute("SELECT record FROM tokens WHERE token=?", (token,)))
        visible = set(self._visible_ids(principal))
        return sorted(candidates & visible)

    # Retention and erasure --------------------------------------------------

    def purge(self) -> int:
        """Delete raw episodes whose age reached the TTL (scope.t27 raw_expired)."""
        now, gone = self._now(), []
        for record, created_at in self.db.execute("SELECT id, created_at FROM records WHERE kind=?", (KIND_EPISODE,)).fetchall():
            if _raw_expired(created_at, self.raw_ttl, now):
                gone.append(record)
        with self.db:
            for record in gone:
                self.db.execute("DELETE FROM tokens WHERE record=?", (record,))
                self.db.execute("DELETE FROM records WHERE id=?", (record,))
        return len(gone)

    def erase(self, principal: Principal, owner: int | None = None, user: int | None = None) -> int:
        """Erase on request: delete the group's rows and destroy its keys (crypto-shredding).

        A person erases every group about themself; an agent or an operator names the group.
        """
        if principal.kind == PERSON:
            groups = self.db.execute("SELECT DISTINCT owner, user FROM keys WHERE user=?", (principal.user,)).fetchall()
        else:
            groups = [(principal.owner if owner is None else owner, principal.user if user is None else user)]
        removed = 0
        for g_owner, g_user in groups:
            if not _may_erase(principal.kind, principal.owner, principal.user, g_owner or 0, g_user or 0):
                raise ScopeError("scope.t27 may_erase refuses this group")
        with self.db:
            for g_owner, g_user in groups:
                ids = [r[0] for r in self.db.execute("SELECT id FROM records WHERE owner=? AND user=?", (g_owner, g_user))]
                for record in ids:
                    self.db.execute("DELETE FROM tokens WHERE record=?", (record,))
                removed += self.db.execute("DELETE FROM records WHERE owner=? AND user=?", (g_owner, g_user)).rowcount
                for version, state in self.db.execute("SELECT version, state FROM keys WHERE owner=? AND user=?",
                                                      (g_owner, g_user)).fetchall():
                    if state != KEY_DESTROYED:
                        assert _key_transition_ok(state, KEY_DESTROYED)
                        self.kms.destroy(g_owner, g_user, version)
                self.db.execute("UPDATE keys SET state=?, wrapped=NULL WHERE owner=? AND user=?",
                                (KEY_DESTROYED, g_owner, g_user))
        self.db.execute("VACUUM")
        return removed

    # Mirror -----------------------------------------------------------------

    def set_mirror(self, owner: int, opted_in: bool, host: int) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO mirror (owner, opted_in, host) VALUES (?, ?, ?)",
                            (_check_id(owner, "owner"), int(bool(opted_in)), host))

    def mirror_allowed(self, owner: int) -> bool:
        row = self.db.execute("SELECT opted_in, host FROM mirror WHERE owner=?", (owner,)).fetchone()
        return mirror_allowed(bool(row[0]), row[1]) if row else mirror_allowed(False, HOST_SELF_HOSTED)
