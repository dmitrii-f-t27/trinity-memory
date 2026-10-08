# Chat memory: an encrypted graph-memory store

Issue [#136](https://github.com/dmitrii-f-t27/trinity-memory/issues/136). The executable side of the
privacy-first graph memory of [gHashTag/t27#7828](https://github.com/gHashTag/t27/issues/7828), for the
chat agent of gHashTag/999-multibots-telegraf
([#3876](https://github.com/gHashTag/999-multibots-telegraf/issues/3876),
[#3877](https://github.com/gHashTag/999-multibots-telegraf/issues/3877)).

## What runs where

| Part | File | Language |
|---|---|---|
| Who may read, write, erase; raw TTL; mirror | `specs/memory/graph/scope.t27` (vendored, t27#7881) | t27 spec, compiled into the native library |
| Key states, envelope and associated-data layout, open order, service tokens | `specs/memory/graph/crypt.t27` (vendored, t27#7882) | t27 spec, compiled into the native library |
| When a fact is current; a newer fact retires an older one | `specs/memory/graph/temporal.t27` (vendored, t27#7830) | t27 spec, compiled into the native library |
| ChaCha20-Poly1305, SHA-256, HMAC, HKDF, the `TGE1` envelope | `t27/chat_crypto.t27` | t27 |
| sqlite tables, the KMS call, ctypes | `trinity_memory/chat_store.py` | Python, no rule of its own |

The vendored specs are pinned by `specs/memory/graph/upstream.lock.json` (git blob SHA per file);
`tests/test_chat_store.py` fails when a file differs from its pin. Change them upstream in gHashTag/t27
and copy them back; never edit them here.

## Model

- **Scope** `(owner, user, thread)`: `owner` is the account the agent serves, `user` the person it talks
  to, `thread` one dialog (0 = group level: entities and facts). A group is one `(owner, user)` pair.
- **Principals**: an agent reads its pair's group-level records and its own thread; a person reads and
  erases everything about themself; an operator reads nothing and may carry out an erase.
  The read filter is built from the principal, and each row is checked again with `may_read`.
- **Keys**: one 32-byte data key per group and version, stored only wrapped by the KMS. The KMS decides
  and logs every unwrap. Sealing uses the active key; a retired key still opens; a key is rotated after
  2^32 seals.
- **Envelope** `TGE1`: `"TGE1" | 1 | 1 | 0 0 | key_version u32 | nonce[12] | ciphertext | tag[16]`.
  The associated data `TGA1` (kind, owner, user, thread, record id, key version) is rebuilt from the row
  when it is read and never stored, so a ciphertext copied into another row does not open.
- **Search**: HMAC-SHA256 tokens of normalized words under an index key derived from the data key
  (HKDF, info `TG index v1`); they die with the key.
- **Retention**: raw episodes expire after the TTL (default 30 days, at most 365); `purge()` deletes
  them. With purpose `PURPOSE_FACTS_ONLY` raw messages are not kept at all.
- **Erase on request**: `erase()` deletes the group's rows and tokens, destroys every key version in
  the KMS and in the table, and vacuums the file (`secure_delete` is on). An envelope restored from an
  older backup then fails with `KMS_DENIED` or `KEY_DESTROYED`.
- **Mirror** to an external graph store: off by default, only after the owner opts in and only to a
  self-hosted store; Zep Cloud is refused.
- **Service tokens** for that store: HS256 with `exp`, at most 15 minutes; a token without `exp` is
  refused.

## What it protects against, and what not

Protects: a database dump, a backup or volume copy, read-only database access, a row or ciphertext moved
between groups, threads or records, and reading after erasure (crypto-shredding).

Does not protect: whoever can change and deploy the agent code or run it with the KMS role reads
plaintext while the agent works; LLM extraction and embeddings need plaintext, and embeddings leak
content. Keeping operators out needs client-side processing or an attested enclave (slices 4 and 6 of
t27#7828; separation of duties is 999-multibots-telegraf#3878).

`LocalKms` is for tests and local runs: its key-encryption key lives in process memory. In production
the KMS lives outside the app host (cloud KMS or Vault transit), the app has only the role to unwrap,
and a destroyed group's key must stay unrecoverable there even when an old wrapped copy comes back from
a backup (one KMS key per group, or a deny list kept by the KMS).

## Use

```python
from trinity_memory.chat_store import ChatMemoryStore, LocalKms, Principal

store = ChatMemoryStore("memory.db", LocalKms())
agent = Principal.agent(owner=11, user=22, thread=33)
store.add_episode(agent, "Anna said she moves to Lisbon in May")
fact = store.add_fact(agent, "Anna lives in Porto", valid_at=1_700_000_000)
store.supersede(agent, fact, "Anna lives in Lisbon", valid_at=1_780_000_000)
store.recall(agent)            # current facts and unexpired episodes, decrypted
store.search(agent, "Lisbon")  # record ids by HMAC token
store.purge()                  # raw TTL
store.erase(Principal.person(22))
```

Ids are opaque integers in `1..2^63-1` (hash the Telegram ids with a server key before they arrive).

## Verification

- `python -m unittest tests.test_chat_store`: every vector of `conformance/memory_graph_{scope,temporal,crypt}.json`
  through the native library (expectations from independent Python restatements and a pure-Python
  ChaCha20-Poly1305, `tools/graph_memory_vectors.py`), the published RFC 8439, FIPS 180-4, RFC 4231 and
  RFC 5869 vectors, and the acceptance tests of #3876 and #3877.
- `tests/native_chat_crypto.c` (in `make t27-test`): the same primitives and every open status under
  ASan and UBSan.
- `make check-specs`: the three vendored specs typecheck, generate, pass their test blocks and match
  their seals on the pinned compiler.

Not done here: the TypeScript wiring in 999-multibots-telegraf (needs the maintainer's `only-t27`
label), LLM extraction (slice 4), dedup/index/search ranking (slice 5), attestation (slice 6).
