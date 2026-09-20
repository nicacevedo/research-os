# ADR 0002 — The autonomous bank is a separate branch, and is not science

**Status:** accepted
**Date:** 2026-09-20
**Context:** the autonomous discovery portfolio layer

## Decision

The idea bank is materialised by a deterministic Curator into
`.research-os/autonomous/` on a dedicated per-project Git branch,
`research-os/autonomous`, written from a dedicated worktree outside the
researcher's ordinary checkout. The researcher's canonical branch is never
written by this system. Nothing under `.research/` is written by this system.

The authority chain is:

```text
PostgreSQL (working store)
  → deterministic Curator
  → research-os/autonomous            durable, human-readable, NOT science
  → [ human act: propose promote / review ]
  → .research/ on the canonical branch   science
```

## Why this and not the alternatives

**Not writing ideas into `.research/`.** `DESIGN_INVARIANTS.md` 6 says
Git-tracked capsule files are scientific truth. An Idea is explicitly *not*
scientific truth — it is a candidate direction, most of which will be rejected.
Putting a few hundred autonomous candidates into the capsule would make the
statement "capsule files are what this project holds" false, and would do it
by accretion rather than by any single reviewable decision.

**Not leaving the bank only in PostgreSQL.** `ARCHITECTURE.md` §14 promises that
deleting the operational database loses no science. If the bank existed only
there, the promise would either become false or would require redefining the
bank as disposable — and §29 of the brief requires that nothing explored is ever
deleted. A Git branch is durable, diffable, human-readable and already the
system's idiom for "state a person should be able to read".

**Not the same branch with a subdirectory.** A person merging the autonomous
branch would then be merging into their working history by accident. A separate
branch makes adoption an explicit act.

**`.research-os/` and not `.research/`.** One character, deliberately: the
kernel validator reads `.research/`, and an autonomous tree that the validator
would try to parse is an autonomous tree that can produce a validation failure
in the researcher's project. The Curator refuses any path under `.research/`.

## Consequences

- Deleting the operational database loses ideas generated since the last
  curation, and nothing else. That window is bounded by curating after every
  disposition, and `researchctl portfolio status` reports the uncurated count
  so the exposure is a number rather than an assumption.
- Adopting an idea is a deliberate human act with an existing command, not a
  merge nobody reviewed.
- The Curator must be the sole serialized writer, which is why it takes a
  PostgreSQL session advisory lock rather than a file lock: a Curator killed
  mid-commit must release by dying.

## Reversibility

Moderate. The branch could be relocated or the layout changed with a re-render,
because the branch is a *view* and PostgreSQL is the working store. What would
be expensive to reverse is the opposite choice — having written ideas into
`.research/` and later needing to extract them from a researcher's history.
