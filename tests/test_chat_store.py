"""Graph-memory store (issue #136): spec vectors through the native library, then the store.

The vectors in conformance/memory_graph_{scope,temporal,crypt}.json are computed by
independent Python restatements (tools/graph_memory_vectors.py). Here every one is
replayed through the functions the native library generates from the vendored specs
(specs/memory/graph) and from t27/chat_crypto.t27. The store tests are the acceptance
criteria of gHashTag/999-multibots-telegraf#3876 and #3877.
"""
import ctypes as C
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from trinity_memory import _native as n
from trinity_memory import chat_store as cs

ROOT = Path(__file__).resolve().parents[1]
U8P = C.POINTER(C.c_uint8)
BOOL, U8, U32, U64, I64 = C.c_bool, C.c_uint8, C.c_uint32, C.c_uint64, C.c_int64
SIGNATURES = {
    "may_read": (BOOL, (U8, U64, U64, U64, U64, U64, U64)),
    "may_write": (BOOL, (U8, U64, U64, U64, U64, U64, U64)),
    "may_erase": (BOOL, (U8, U64, U64, U64, U64)),
    "keep_raw": (BOOL, (U8,)),
    "raw_ttl_ok": (BOOL, (U64,)),
    "raw_expired": (BOOL, (U64, U64, U64)),
    "mirror_allowed": (BOOL, (BOOL, U8)),
    "fact_is_current": (BOOL, (U64, U64, U64)),
    "fact_true_at": (BOOL, (U64, U64, U64)),
    "known_as_of": (BOOL, (U64, U64, U64)),
    "may_invalidate": (BOOL, (U64, U64, U64)),
    "retired_invalid_at": (U64, (U64, U64, U64)),
    "key_transition_ok": (BOOL, (U8, U8)),
    "is_key_state": (BOOL, (U8,)),
    "may_seal": (BOOL, (U8,)),
    "may_open": (BOOL, (U8,)),
    "forget": (U8, (U8,)),
    "must_rotate": (BOOL, (U64,)),
    "plaintext_bytes": (I64, (U32,)),
    "header_ok": (BOOL, (U8, U8, U8, U8, U8, U8, U8, U8, U32, U32)),
    "open_status": (C.c_int32, (BOOL, U8, BOOL, BOOL, BOOL)),
    "service_token_ok": (BOOL, (U64, U64, U64)),
}


def document(name):
    return json.loads((ROOT / "conformance" / name).read_text(encoding="utf-8"))


def octets(hex_text):
    return bytes.fromhex(hex_text)


class GraphSpecVectors(unittest.TestCase):
    def replay(self, name, minimum):
        doc = document(name)
        self.assertTrue((ROOT / doc["spec_path"]).is_file())
        counted = 0
        for vector in doc["vectors"]:
            if vector["fn"] not in SIGNATURES:
                continue
            result, arguments = SIGNATURES[vector["fn"]]
            got = n.call(vector["fn"], result, arguments, *vector["input"])
            self.assertEqual(got, vector["expected"], f"{vector['fn']}{tuple(vector['input'])}")
            counted += 1
        self.assertGreaterEqual(counted, minimum)
        return doc

    def test_scope_vectors(self):
        self.replay("memory_graph_scope.json", 400)

    def test_temporal_vectors(self):
        self.replay("memory_graph_temporal.json", 300)

    def test_crypt_rule_vectors(self):
        self.replay("memory_graph_crypt.json", 100)

    def test_aad_bytes_match_spec_and_store(self):
        aad = n.function("cm_aad", None, (U8, U64, U64, U64, U64, U32, U8P))
        aad_byte = n.function("aad_byte", U8, (U32, U8, U64, U64, U64, U64, U32))
        for vector in document("memory_graph_crypt.json")["vectors"]:
            if vector["fn"] != "aad":
                continue
            out = n.buffer(44)
            aad(*vector["input"], out)
            self.assertEqual(bytes(out[:44]).hex(), vector["expected"])
            spec = bytes(aad_byte(i, *vector["input"]) for i in range(44))
            self.assertEqual(spec.hex(), vector["expected"], "crypt.t27 aad_byte")

    def test_envelopes_are_exact(self):
        rows = [v for v in document("memory_graph_crypt.json")["vectors"] if v["fn"] == "envelope"]
        self.assertEqual(len(rows), 5)
        for v in rows:
            kind, owner, user, thread, record = v["scope"]
            env = cs.seal(octets(v["key"]), v["key_version"], octets(v["nonce"]), kind, owner, user, thread, record,
                          octets(v["plaintext"]))
            self.assertEqual(env.hex(), v["expected"])
            self.assertEqual(cs.open_envelope(octets(v["key"]), 1, 1, True, kind, owner, user, thread, record, env).hex(),
                             v["plaintext"])

    def test_open_status_through_the_envelope(self):
        rows = [v for v in document("memory_graph_crypt.json")["vectors"] if v["fn"] == "open"]
        self.assertGreaterEqual(len(rows), 13)
        for v in rows:
            try:
                got = len(cs.open_envelope(octets(v["key"]), v["key_state"], v["held_version"], v["kms_ok"],
                                           *v["scope"], octets(v["envelope"])))
            except cs.OpenError as error:
                got = {name: code for code, name in cs.OPEN_STATUS.items()}[error.status]
            self.assertEqual(got, v["expected"], v["id"])

    def test_tokens_and_index_key(self):
        vectors = document("memory_graph_crypt.json")["vectors"]
        for v in vectors:
            if v["fn"] == "index_key":
                self.assertEqual(cs.hkdf_sha256(octets(v["dek"]), octets(v["info"])).hex(), v["expected"])
            if v["fn"] == "token":
                self.assertEqual(cs.search_token(octets(v["index_key"]), v["field"], v["term"]).hex(), v["expected"])

    def test_published_vectors(self):
        block = n.function("cm_chacha20_block", None, (U8P, U32, U8P, U8P))
        poly = n.function("cm_poly1305", None, (U8P, U8P, C.c_size_t, U8P))
        aead = n.function("cm_aead_seal", None, (U8P, U8P, U8P, C.c_size_t, U8P, C.c_size_t, U8P, U8P))
        sha = n.function("cm_sha256", None, (U8P, C.c_size_t, U8P))
        seen = set()
        for v in document("memory_graph_crypt.json")["vectors"]:
            if v["fn"] == "rfc8439" and v["id"] == "chacha20_block_2_3_2":
                out = n.buffer(64)
                block(n.octets(octets(v["key"])), v["counter"], n.octets(octets(v["nonce"])), out)
                self.assertEqual(bytes(out[:64]).hex(), v["block"])
            elif v["fn"] == "rfc8439" and v["id"] == "poly1305_2_5_2":
                message, out = octets(v["message"]), n.buffer(16)
                poly(n.octets(octets(v["key"])), n.octets(message), len(message), out)
                self.assertEqual(bytes(out[:16]).hex(), v["tag"])
            elif v["fn"] == "rfc8439":
                aad, plaintext = octets(v["aad"]), octets(v["plaintext"])
                ct, tag = n.buffer(len(plaintext)), n.buffer(16)
                aead(n.octets(octets(v["key"])), n.octets(octets(v["nonce"])), n.octets(aad), len(aad),
                     n.octets(plaintext), len(plaintext), ct, tag)
                self.assertEqual((bytes(ct[:len(plaintext)]).hex(), bytes(tag[:16]).hex()), (v["ciphertext"], v["tag"]))
            elif v["fn"] == "rfc4231":
                self.assertEqual(cs.hmac_sha256(octets(v["key"]), octets(v["data"])).hex(), v["hmac"])
            elif v["fn"] == "rfc5869":
                self.assertEqual(cs.hkdf_sha256(octets(v["ikm"]), octets(v["info"]), v["length"], octets(v["salt"])).hex(),
                                 v["okm"])
            elif v["fn"] == "fips180_4":
                message, out = octets(v["message"]), n.buffer(32)
                sha(n.octets(message), len(message), out)
                self.assertEqual(bytes(out[:32]).hex(), v["sha256"])
            else:
                continue
            seen.add(v["fn"])
        self.assertEqual(seen, {"rfc8439", "rfc4231", "rfc5869", "fips180_4"})

    def test_vendored_specs_match_their_upstream_pin(self):
        lock = json.loads((ROOT / "specs/memory/graph/upstream.lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock["repository"], "gHashTag/t27")
        for path, blob in lock["files"].items():
            data = (ROOT / path).read_bytes()
            got = subprocess.run(["git", "hash-object", "--stdin"], input=data, capture_output=True, check=True).stdout
            self.assertEqual(got.decode().strip(), blob, path)
        self.assertEqual(sorted(lock["files"]), sorted(p.relative_to(ROOT).as_posix()
                                                       for p in (ROOT / "specs/memory/graph").glob("*.t27")))

    def test_generator_output_is_committed(self):
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "generate-spec-vectors.py"), "--check"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class Clock:
    def __init__(self, now=1_800_000_000):
        self.now = now

    def __call__(self):
        return self.now


class ChatStoreAcceptance(unittest.TestCase):
    SECRET = "Anna's door code is BLUE-HORSE-42"

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = str(self.dir / "memory.db")
        self.kms, self.clock = cs.LocalKms(), Clock()
        self.store = cs.ChatMemoryStore(self.path, self.kms, raw_ttl=7 * 86400, clock=self.clock)
        self.agent = cs.Principal.agent(11, 22, 33)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.dir)

    def test_dump_and_backup_show_only_ciphertext_and_the_agent_still_answers(self):
        episode = self.store.add_episode(self.agent, self.SECRET)
        fact = self.store.add_fact(self.agent, "Anna works at Acme", valid_at=1_700_000_000)
        backup = self.dir / "backup.db"
        shutil.copy(self.path, backup)
        for path in (self.path, backup):
            raw = Path(path).read_bytes()
            self.assertNotIn(b"BLUE-HORSE", raw)
            self.assertNotIn(b"Acme", raw)
        dump = "\n".join(sqlite3.connect(self.path).iterdump())
        self.assertNotIn("BLUE-HORSE", dump)
        self.assertNotIn(self.kms.kek.hex(), dump)
        self.assertEqual(self.store.read(self.agent, episode), self.SECRET)
        self.assertEqual([r["text"] for r in self.store.recall(self.agent)], [self.SECRET, "Anna works at Acme"])
        self.assertEqual(self.store.search(self.agent, "ACME"), [fact])

    def test_decrypt_without_the_kms_role_fails(self):
        episode = self.store.add_episode(self.agent, self.SECRET)
        denied = cs.ChatMemoryStore(self.path, cs.LocalKms(roles=frozenset(), kek=self.kms.kek), clock=self.clock)
        with self.assertRaises(cs.OpenError) as caught:
            denied.read(self.agent, episode)
        self.assertEqual(caught.exception.status, "KMS_DENIED")
        stolen = cs.ChatMemoryStore(self.path, cs.LocalKms(), clock=self.clock)  # a dump without the real KMS
        with self.assertRaises(cs.OpenError):
            stolen.read(self.agent, episode)
        self.assertTrue(any(entry[0] == "unwrap" and not entry[5] for entry in denied.kms.log))
        denied.close()
        stolen.close()

    def test_scope_is_built_from_the_principal(self):
        self.store.add_episode(self.agent, self.SECRET)
        other_thread = self.store.add_episode(cs.Principal.agent(11, 22, 34), "other dialog")
        foreign = self.store.add_episode(cs.Principal.agent(99, 22, 33), "told to another owner")
        texts = [r["text"] for r in self.store.recall(self.agent)]
        self.assertEqual(texts, [self.SECRET])
        with self.assertRaises(cs.ScopeError):
            self.store.read(self.agent, foreign)
        with self.assertRaises(cs.ScopeError):
            self.store.read(self.agent, other_thread)
        with self.assertRaises(cs.ScopeError):
            self.store.add_episode(cs.Principal.agent(11, 22, 0), "no thread")
        self.assertEqual(self.store.recall(cs.Principal.operator()), [])
        with self.assertRaises(cs.ScopeError):
            self.store.read(cs.Principal.operator(), foreign)
        person = sorted(r["text"] for r in self.store.recall(cs.Principal.person(22)))
        self.assertEqual(person, sorted([self.SECRET, "other dialog", "told to another owner"]))

    def test_a_row_moved_to_another_scope_does_not_open(self):
        mine = self.store.add_episode(self.agent, self.SECRET)
        theirs = self.store.add_episode(cs.Principal.agent(11, 23, 33), "their text")
        envelope = self.store.db.execute("SELECT envelope FROM records WHERE id=?", (mine,)).fetchone()[0]
        with self.store.db:  # an attacker with write access copies my ciphertext into their row
            self.store.db.execute("UPDATE records SET envelope=? WHERE id=?", (envelope, theirs))
        with self.assertRaises(cs.OpenError) as caught:
            self.store.read(cs.Principal.agent(11, 23, 33), theirs)
        self.assertIn(caught.exception.status, ("TAG", "KEY_VERSION"))
        with self.store.db:  # same group, another record id
            self.store.db.execute("UPDATE records SET id=? WHERE id=?", (mine + 1, mine))
        with self.assertRaises(cs.OpenError) as caught:
            self.store.read(self.agent, mine + 1)
        self.assertEqual(caught.exception.status, "TAG")

    def test_raw_ttl_purge_keeps_facts(self):
        old = self.store.add_episode(self.agent, "old message")
        fact = self.store.add_fact(self.agent, "Anna works at Acme")
        self.clock.now += 7 * 86400 - 1
        fresh = self.store.add_episode(self.agent, "fresh message")
        self.assertEqual(self.store.purge(), 0)
        self.clock.now += 1
        self.assertEqual(self.store.purge(), 1)
        ids = [r[0] for r in self.store.db.execute("SELECT id FROM records")]
        self.assertNotIn(old, ids)
        self.assertIn(fact, ids)
        self.assertIn(fresh, ids)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM tokens WHERE record=?", (old,)).fetchone()[0], 0)
        with self.assertRaises(ValueError):
            cs.ChatMemoryStore(self.path, self.kms, raw_ttl=0)

    def test_facts_only_purpose_keeps_no_raw_text(self):
        store = cs.ChatMemoryStore(self.path, self.kms, clock=self.clock, purpose=cs.PURPOSE_FACTS_ONLY)
        self.assertIsNone(store.add_episode(self.agent, self.SECRET))
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM records").fetchone()[0], 0)
        store.close()

    def test_erase_on_request_leaves_no_rows_and_shreds_backups(self):
        episode = self.store.add_episode(self.agent, self.SECRET)
        self.store.add_fact(self.agent, "Anna works at Acme")
        self.store.add_episode(cs.Principal.agent(77, 22, 1), "same person, other owner")
        keep = self.store.add_episode(cs.Principal.agent(11, 23, 33), "another person")
        backup = self.dir / "before-erase.db"
        shutil.copy(self.path, backup)
        self.assertEqual(self.store.erase(cs.Principal.person(22)), 3)
        rows = self.store.db.execute("SELECT user FROM records").fetchall()
        self.assertEqual(rows, [(23,)])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM tokens t JOIN records r ON t.record = r.id "
                                               "WHERE r.user = 22").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT state, wrapped FROM keys WHERE user=22").fetchall(),
                         [(cs.KEY_DESTROYED, None), (cs.KEY_DESTROYED, None)])
        self.assertEqual(self.store.read(cs.Principal.agent(11, 23, 33), keep), "another person")
        restored = cs.ChatMemoryStore(str(backup), self.kms, clock=self.clock)
        with self.assertRaises(cs.OpenError) as caught:
            restored.read(self.agent, episode)
        self.assertEqual(caught.exception.status, "KMS_DENIED")
        restored.close()
        with self.assertRaises(cs.ScopeError):
            self.store.erase(cs.Principal.agent(11, 22, 33), owner=12, user=22)

    def test_a_newer_fact_retires_the_older_one(self):
        old = self.store.add_fact(self.agent, "Anna works at Acme", valid_at=1_700_000_000)
        self.store.supersede(self.agent, old, "Anna works at Globex", valid_at=1_750_000_000)
        texts = [r["text"] for r in self.store.recall(self.agent)]
        self.assertEqual(texts, ["Anna works at Globex"])
        row = self.store.db.execute("SELECT invalid_at, expired_at FROM records WHERE id=?", (old,)).fetchone()
        self.assertEqual(row, (1_750_000_000, self.clock.now))
        self.assertEqual(self.store.read(self.agent, old), "Anna works at Acme")

    def test_mirror_is_opt_in_and_self_hosted(self):
        self.assertFalse(self.store.mirror_allowed(11))
        self.store.set_mirror(11, True, cs.HOST_ZEP_CLOUD)
        self.assertFalse(self.store.mirror_allowed(11))
        self.store.set_mirror(11, True, cs.HOST_SELF_HOSTED)
        self.assertTrue(self.store.mirror_allowed(11))
        self.store.set_mirror(11, False, cs.HOST_SELF_HOSTED)
        self.assertFalse(self.store.mirror_allowed(11))

    def test_service_tokens_expire(self):
        secret = b"zep-auth-secret"
        token = cs.mint_service_token(secret, 1_800_000_000)
        self.assertTrue(cs.verify_service_token(token, secret, 1_800_000_000 + 899))
        self.assertFalse(cs.verify_service_token(token, secret, 1_800_000_000 + 900))
        self.assertFalse(cs.verify_service_token(token, b"other", 1_800_000_000))
        with self.assertRaises(ValueError):
            cs.mint_service_token(secret, 1_800_000_000, ttl=901)
        head, body, _ = token.split(".")
        forever = cs._b64(json.dumps({"iss": "vibee-render", "iat": 1_800_000_000}).encode())
        unsigned = f"{head}.{forever}"
        self.assertFalse(cs.verify_service_token(f"{unsigned}.{cs._b64(cs.hmac_sha256(secret, unsigned.encode()))}",
                                                 secret, 1_800_000_000))


if __name__ == "__main__":
    unittest.main()
