"""Typed, citable, noncanonical runtime findings.

The smallest representation that lets a person answer "what is this proposed
scientific change actually resting on" without reading chat history.

**What a finding is not.** Not a scientific fact, not a capsule object, not
something with a status a person could accept. It has no acceptance rule, it
never reaches `.research/`, and it participates in no scientific digest. The
runtime mints them constantly and nothing about that changes what the project
holds to be true.

**What a finding is for.** Before this existed, an autonomous result reached the
v1 proposal layer as prose concatenated into the natural-language ``goal``. So
the grounding allowlist the proposal validator checks citations against -- which
already had a ``finding_ids`` field, and which already refused an unsupplied
citation -- was handed an empty list, and a proposal's real basis was whatever a
model had written into a sentence. A finding gives that basis an identifier, a
digest, and edges to the artifacts, capsule objects, literature keys and
experiment job it came from, so this chain is traversable:

```text
proposed scientific change
  -> proposal item          (cites finding_id, checked against the allowlist)
  -> runtime finding        (this)
  -> artifact / capsule object / literature key / experiment job
  -> bytes, by content hash
```

**Immutable, and deduplicated by content.** Nothing updates a finding. A
finding that turns out to be wrong is superseded by a later one, because a
citation is worth nothing if the thing cited can change after being cited. The
digest is computed from the finding's *content and its references*, not from the
time it was made, so the runtime recomputing an unchanged frontier on every
cycle produces one finding rather than one per cycle.

**``summary`` is untrusted text.** It is usually a model's sentence about a
model's output. It is rendered through :mod:`research_os.automation.promptdata`
behind a fence wherever it reaches a prompt, and through
:mod:`research_os.textsafe` wherever it reaches a terminal. Nothing reads it for
instructions and nothing dispatches on it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: The most references of one kind one finding may carry.
#:
#: A bound rather than a limit that will be hit: a finding citing two hundred
#: artifacts is not a finding, it is a dump, and it would arrive in a proposal
#: prompt as two hundred identifiers a worker is invited to cite at random.
MAX_REFS_PER_KIND = 32

#: The most characters of a finding's summary that are stored.
#:
#: Generous, because the summary is what a person reads first, and small enough
#: that a bounded set of findings cannot become the largest thing in a prompt.
MAX_SUMMARY_CHARS = 4_000

#: The most findings one proposal may be grounded in.
#:
#: Twelve, matching `proposal.planner.MAX_ITEMS`, for the same reason it gives:
#: a set a researcher cannot read in one sitting is a set they will skim, and
#: skimming a grounding allowlist is how an ungrounded proposal gets promoted.
MAX_PACKET_FINDINGS = 12


class FindingKind(StrEnum):
    """Where a finding came from.

    A closed set with a matching check constraint, for the usual reason: a kind
    the database rejects is a crash in the code path that was recording
    something, and a kind Python does not know is a row nothing can render.

    Deliberately about *provenance*, not about significance. There is no
    ``important`` or ``confirmed`` kind, because a runtime that could label its
    own findings confirmed would be doing the one thing it must not.
    """

    LITERATURE = "literature"
    EXPERIMENT = "experiment"
    INTERPRETATION = "interpretation"
    CODE = "code"
    REVIEW = "review"
    INSPECTION = "inspection"
    FRONTIER = "frontier"
    OTHER = "other"


class RuntimeFinding(BaseModel):
    """One thing the runtime observed, with enough provenance to audit it.

    Not every field is populated for every finding, and that is deliberate: a
    literature finding has no ``experiment_job_id`` and an experiment finding
    has no ``literature_keys``. What is *not* optional is
    :attr:`digest`-determining content, because that is what makes two runs of
    the same observation one finding.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    finding_id: str = ""
    """Assigned by the store when the finding is written. Empty until then."""

    project_id: str
    kind: FindingKind
    summary: str

    source_run_id: str | None = None
    source_cycle: int | None = None
    source_work_id: str | None = None
    source_action: str | None = None

    artifact_ids: tuple[str, ...] = ()
    capsule_refs: tuple[str, ...] = ()
    literature_keys: tuple[str, ...] = ()

    experiment_job_id: str | None = None
    spec_digest: str | None = None

    created_at: str | None = None
    """Set by the store. Not part of the digest, so a repeat is a repeat."""

    @field_validator("summary")
    @classmethod
    def _non_blank_summary(cls, value: str) -> str:
        collapsed = " ".join(value.split())
        if not collapsed:
            raise ValueError(
                "a finding needs a summary; an empty one is a citable identifier "
                "for nothing, which is worse than no finding at all"
            )
        return collapsed[:MAX_SUMMARY_CHARS]

    @field_validator("artifact_ids", "capsule_refs", "literature_keys")
    @classmethod
    def _bounded_unique_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(dict.fromkeys(item.strip() for item in value if item.strip()))
        if len(cleaned) > MAX_REFS_PER_KIND:
            raise ValueError(
                f"a finding may carry at most {MAX_REFS_PER_KIND} references of "
                f"one kind; this one has {len(cleaned)}"
            )
        return cleaned

    @property
    def digest(self) -> str:
        """A stable hash of what this finding says and what it rests on.

        Identified by content, deliberately, and every excluded field is
        excluded for a stated reason:

        - ``finding_id`` and ``created_at`` are assigned by the store
          afterwards, so including them would make the digest depend on its own
          result;
        - ``source_work_id`` is which worker happened to run the action, which
          is not part of the observation;
        - ``source_run_id`` and ``source_cycle`` are which cycle noticed it. The
          runtime recomputes an unchanged frontier on every cycle, and if the
          cycle index were in the digest, seven cycles over one frontier would
          mint seven citable identifiers for one fact -- which is the same
          defect as the seven-cycle pilot in ``docs/RUNTIME.md`` §16, one layer
          down.

        The row still records the run and cycle that first observed it. What is
        excluded from the *digest* is only what decides whether two observations
        are the same observation.
        """

        material = json.dumps(
            {
                "v": 1,
                "project_id": self.project_id,
                "kind": str(self.kind),
                "summary": self.summary,
                "source_action": self.source_action or "",
                "artifact_ids": sorted(self.artifact_ids),
                "capsule_refs": sorted(self.capsule_refs),
                "literature_keys": sorted(self.literature_keys),
                "experiment_job_id": self.experiment_job_id or "",
                "spec_digest": self.spec_digest or "",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def references(self) -> tuple[tuple[str, str], ...]:
        """Every edge, as ``(kind, ref)`` pairs, for the refs table."""

        return (
            *(("artifact", value) for value in self.artifact_ids),
            *(("capsule_object", value) for value in self.capsule_refs),
            *(("literature_key", value) for value in self.literature_keys),
        )


class FindingPacket(BaseModel):
    """A bounded set of findings, as it is handed to the proposal layer.

    A type rather than a list because two things have to be true of the set as a
    whole and neither is a property of one finding: it is bounded, and the ids
    in it are exactly the ids the proposal grounding allowlist will contain.
    Passing a bare list made it possible to render one set into the prompt and
    allow a different one, which is the failure the allowlist exists to prevent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    findings: tuple[RuntimeFinding, ...] = Field(default_factory=tuple)

    @field_validator("findings")
    @classmethod
    def _bounded_and_identified(
        cls, value: tuple[RuntimeFinding, ...]
    ) -> tuple[RuntimeFinding, ...]:
        if len(value) > MAX_PACKET_FINDINGS:
            raise ValueError(
                f"a proposal may be grounded in at most {MAX_PACKET_FINDINGS} "
                f"findings; this packet has {len(value)}. More than that is not "
                f"grounding, it is a corpus, and a worker handed a corpus of "
                f"identifiers will cite from it decoratively"
            )
        missing = [item.summary[:40] for item in value if not item.finding_id]
        if missing:
            raise ValueError(
                "every finding in a proposal packet must already be stored and "
                "have an id, because the id is what the proposal cites and what "
                "the allowlist checks: " + "; ".join(missing)
            )
        seen = [item.finding_id for item in value]
        if len(set(seen)) != len(seen):
            raise ValueError("a proposal packet lists the same finding twice")
        return value

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(item.finding_id for item in self.findings)

    @property
    def digest(self) -> str:
        """A stable hash of the packet, for the proposal's basis snapshot.

        What it pins is "this proposal was grounded in exactly these findings,
        each saying exactly this". Recomputed before a human promotion, so a
        proposal whose grounding has been superseded fails closed rather than
        being promoted on a basis that no longer holds.
        """

        material = json.dumps(
            {
                "v": 1,
                "findings": sorted(
                    [item.finding_id, item.digest] for item in self.findings
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def rendered(self) -> tuple[dict[str, object], ...]:
        """The packet as plain data, for a fenced prompt block.

        Everything a worker needs in order to cite a finding correctly, and
        nothing it could mistake for an instruction. The caller renders this
        through :mod:`research_os.automation.promptdata`.
        """

        return tuple(
            {
                "finding_id": item.finding_id,
                "kind": str(item.kind),
                "summary": item.summary,
                "rests_on_artifacts": list(item.artifact_ids),
                "rests_on_capsule_objects": list(item.capsule_refs),
                "rests_on_literature": list(item.literature_keys),
                "experiment_job_id": item.experiment_job_id or "",
            }
            for item in self.findings
        )


def packet_from(findings: Sequence[RuntimeFinding]) -> FindingPacket:
    """Build a packet, preserving order and refusing an unstored finding."""

    return FindingPacket(findings=tuple(findings))
