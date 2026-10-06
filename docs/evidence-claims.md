# Evidence claims

A closed issue counts as covered by `.t27` only when a sealed spec, committed
vectors and an executed check back it (CLAUDE.md, "Правило цвета"). Where the
work is a measurement or a report rather than a byte-level contract, the check
is a claim: `tools/evidence/<name>.py` plus a spec, vectors and an evidence
manifest. `tools/evidence-proof.py` replays every claim; `make evidence-proof`
runs it in CI.

## Files of one claim `<name>`

| File | Role |
|---|---|
| `specs/memory/<name>_evidence.t27` | The acceptance rule, sealed with the pinned compiler (`t27c seal --save`). It states what must be counted and links the issues in its header (`// Coverage: https://github.com/dmitrii-f-t27/trinity-memory/issues/N`). |
| `conformance/memory_<name>_evidence.json` | The truth table of the rule, written by `tools/evidence-proof.py --claim <name> --write-vectors`. |
| `tools/evidence/<name>.py` | `CLAIM = {...}`: issues, spec, vectors, accept function, expected counts, manifest, bound files, and `counts(root)`. |
| `<evidence dir>/evidence-manifest.json` | sha256 and size of every bound evidence file and source, written by `--write-manifest`. |

## The acceptance rule

`<prefix>_accept(source_current: bool, evidence_exact: bool, replay_passed: bool, n1: u32, ...) -> bool`
is true only when all three flags are true and every count equals the constant
the spec names. The first three arguments are fixed; the counts are yours.
Copy the shape of `specs/memory/attention_evidence.t27`: constants, the accept
function, and a test with the passing case and one failing case per argument.

## `counts(root)`

It must run the existing offline verifiers of the evidence (never hardware, never
the network) and return the counts they establish, as a tuple of ints in the
order of the accept function. It raises if a verifier fails. It never returns
numbers it read from a report without recomputing them from the retained raw data
when the repository holds that data. If a number cannot be recomputed here, do
not make it a count.

Helper modules next to a claim must start with an underscore (`_replay.py`):
`tools/evidence-proof.py` loads every other `tools/evidence/*.py` as a claim.

## Honesty rules

- The spec header says exactly what is replayed and what is not (for example
  "retained measurements, not a new board run").
- A claim covers an issue only if the verifiers really check the issue's claim.
  If the retained data is missing, a verifier hard-codes a private path, or the
  claim is a process, a release or an upstream matter, there is no claim.
- Never edit a verifier to make it pass. Fixing a path or a missing input is fine
  when the fix is explained in the claim's spec header.
- Binding: list every evidence file, verifier and source the counts depend on in
  `bind` (globs relative to the repository root). `evidence-proof.py` rejects a
  changed, missing or unlisted file.

## Commands

    export T27_ROOT=<checkout of gHashTag/t27 at native/compiler.lock, built>
    $T27_ROOT/target/release/t27c seal --save specs/memory/<name>_evidence.t27
    python3 tools/evidence-proof.py --claim <name> --write-vectors --write-manifest
    python3 tools/evidence-proof.py --claim <name>
    sh tools/check-specs.sh        # every spec under specs/ must pass the gate
