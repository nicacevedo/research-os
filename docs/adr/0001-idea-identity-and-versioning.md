# ADR 0001 — Idea identity and versioning

**Status:** accepted
**Date:** 2026-09-20
**Context:** the autonomous discovery portfolio layer

## Decision

An Idea has a stable identity (`ideas.idea_id`) and an append-only sequence of
immutable versions (`idea_versions`). Scientific content lives only in
versions. Each version carries two SHA-256 digests over canonical JSON:

- `content_digest`, over the scientifically material fields;
- `canonical_digest`, over a normalised projection used for deduplication.

A review binds `(idea_id, version, reviewed_content_digest)`. A review whose
`reviewed_content_digest` differs from the idea's current version's is **stale**
and does not count towards any gate.

## Why this and not the alternatives

**Not a mutable row with an edit history table.** The property that matters is
not "we can see what changed" but "a review cannot silently come to be about
something it did not read". A mutable row plus an audit log gives the first and
not the second: the gate would still read the row, and the row would have moved.

**Not one digest per reviewer dimension.** A novelty review arguably survives a
mechanism change and a methodology review arguably survives a rewording of the
claimed difference. Per-dimension digests would express that and would be
wrong in the unsafe direction whenever the partition is imperfect. One digest
over-invalidates, which costs a re-review, and never under-invalidates, which
would cost a false promotion. Version creation is controlled — only `REVISE`,
`discover` and merge append one — so over-invalidation does not thrash.

**Not a model-assigned identity.** A stochastic identity means the same idea has
different identities on two runs, which breaks replay, restart and dedup
simultaneously. The model's opinion about sameness is recorded as an *edge*
with provenance.

## Consequences

- Reviews go stale automatically and mechanically; nothing has to remember to
  invalidate them.
- Deduplication has a deterministic key that survives restart, reconnect,
  replay and model nondeterminism.
- `idea_versions` grows monotonically. For a portfolio of a few hundred ideas
  with a handful of versions each this is thousands of small rows, which is
  nothing; if it ever is not, versions are prunable *below the current one*
  without losing the gate's correctness, and that is the escape hatch.

## Reversibility

Low cost to extend (add fields to the digest input with a digest version tag),
high cost to reverse (every recorded review's binding becomes meaningless).
Hence an ADR.
