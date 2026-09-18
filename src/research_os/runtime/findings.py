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
import re
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

#: The most characters of a finding's producer-authored excerpt that are stored.
#:
#: The gap it closes. A `critique_hypotheses` finding reached the proposal layer
#: as ``"6 alternative explanation(s) for 5 target(s)"`` -- 44 characters -- while
#: the six alternatives themselves, eleven kilobytes of them, sat in an immutable
#: artifact the proposal worker had an id for and no way to read. The worker did
#: the right thing and refused to ground anything in text it could not see; the
#: proposal it wrote says so, in ``PR-002``:
#:
#:     Only that summary is available to this proposal; the text of the six
#:     alternatives is not.
#:
#: Safe, and incomplete: the system had produced the evidence and then hidden it
#: from itself.
#:
#: Two thousand characters, chosen against the two failure modes either side. Too
#: small and the excerpt is a second summary, which is what already failed. Too
#: large and twelve findings become the prompt -- so this is bounded such that a
#: full :data:`MAX_PACKET_FINDINGS` packet stays a readable fraction of a
#: proposal prompt, and the rendering layers clip further still.
MAX_EXCERPT_CHARS = 2_000

#: Characters of an excerpt that reach a prompt through :meth:`rendered`.
#:
#: Smaller than what is stored. The stored form is the record; a prompt is a
#: working set, and a full packet of stored excerpts would be twenty-four
#: thousand characters of one block. Clipped here rather than at the fence so
#: the truncation is a property of what the block contains rather than of how
#: it was serialised -- the same reason
#: :func:`research_os.runtime.sciencecontext.finding_view` clips there.
MAX_RENDERED_EXCERPT_CHARS = 1_500

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


class FindingRefKind(StrEnum):
    """What one provenance edge of a finding points at.

    Mirrored by ``runtime_finding_refs_kind_ck``. It was three string literals
    in the SQL and three more in :meth:`RuntimeFinding.refs`, with nothing
    holding them together -- an adversarial review noted that
    ``tests/test_runtime_schema.py`` proves every *other* value list in the
    schema matches a Python enum, so this one and the proposal reservation
    statuses were the two the proof did not cover.
    """

    ARTIFACT = "artifact"
    CAPSULE_OBJECT = "capsule_object"
    LITERATURE_KEY = "literature_key"


#: The shape a producer-stated identity must have.
#:
#: ``<producer>:v<version>:<sha256 hex>``. The version is there so a producer
#: can change what it hashes without the new keys colliding with the old ones,
#: and the digest is there because a key without one is almost certainly a
#: constant -- which merges every finding of its kind onto one row.
_SEMANTIC_KEY_RE = re.compile(r"[a-z_]+:v[0-9]+:[0-9a-f]{64}")


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

    excerpt: str = ""
    """Bounded scientific substance, selected by the handler that produced it.

    **Producer-authored, deliberately.** The generic prompt layer does not read
    artifacts. It cannot: an artifact is bytes with a media type, and a layer
    that scraped them would have to guess which part of a ten-kilobyte review
    document is the finding, on every schema any handler might ever write. The
    handler already knows -- it built the structure a moment earlier -- so it
    selects the excerpt and this field carries it.

    **Noncanonical, like everything else here.** An excerpt is a model's output
    quoted verbatim. It is rendered fenced and untrusted everywhere it reaches a
    prompt, it is labelled as noncanonical in the same entry that carries it, and
    it can no more become a Claim than the summary beside it can. What it changes
    is only that a worker asked to ground a proposal in this finding can now read
    what the finding *said*.

    **Part of the digest, when present.** Two observations that said different
    things are different observations. Absent from the digest material when
    empty, so every finding recorded before this field existed keeps the digest
    it was cited by -- an excerpt appearing later must not silently restate a
    finding a proposal already rests on.
    """

    source_run_id: str | None = None
    source_cycle: int | None = None
    source_work_id: str | None = None
    source_action: str | None = None

    artifact_ids: tuple[str, ...] = ()
    capsule_refs: tuple[str, ...] = ()
    literature_keys: tuple[str, ...] = ()

    experiment_job_id: str | None = None
    spec_digest: str | None = None

    semantic_key: str = ""
    """What makes two of these the same observation, when content will not do.

    **Why content is not always enough.** The digest below is over what the
    finding says and what it rests on, which is right whenever the content *is*
    the observation. It is wrong for a handler whose result contains a model's
    prose. A frontier assessment reached over identical scientific state,
    concluding the identical thing, produces a differently worded rationale and
    a differently hashed artifact -- so it hashes differently, so it becomes a
    second citable identifier for one observation. Twenty of them fill all
    :data:`research_os.runtime.sciencecontext.MAX_PLANNER_FINDINGS` slots of
    the planner's window -- that constant, not the one in this module, which
    bounds a *proposal's* grounding packet -- and push the project's real
    findings out of it, with nothing about the project having changed.

    **So the producer may state the identity instead.** When this is set, the
    digest is computed from it and from the finding's project, kind and
    producing action, and from nothing else. The summary, the excerpt and the
    references are still stored, still read and still cited -- they are simply
    not what decides whether this is a new finding.

    **The obligation that comes with setting it.** Two findings with equal keys
    are permanently the same finding. A handler that sets one must put
    everything material into it and nothing that varies without the observation
    varying. See :data:`research_os.runtime.actions.base.SEMANTIC_KEY`, and
    :func:`research_os.runtime.actions.review.assessment_identity` for the one
    handler that does -- asserted structurally, because a second producer
    setting a key badly would merge citable findings with nothing to notice.
    The column is ``sql/0017_finding_semantic_identity.sql``; the shape is
    enforced by the validator below, so a constant is refused rather than
    silently collapsing everything of its kind.

    **Repeats keep the first wording, deliberately.** ``record_finding``
    returns the existing row, so the stored prose is the first occurrence's. A
    finding is immutable and superseded rather than updated -- a citation is
    worth nothing if the thing cited can change after being cited -- and if a
    later assessment's substance differs, its key differs and it is a new
    finding rather than a rewrite of this one.
    """

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

    @field_validator("excerpt")
    @classmethod
    def _bounded_excerpt(cls, value: str) -> str:
        """Clip to the stored bound. Blank is valid -- most findings have none.

        Newlines survive, unlike in ``summary``: an excerpt is structured
        material a person reads as a list, and collapsing it to one line would
        make six alternative explanations into one paragraph. The prompt
        serializer renders it inert either way.
        """

        return value.strip()[:MAX_EXCERPT_CHARS]

    @field_validator("semantic_key")
    @classmethod
    def _well_formed_semantic_key(cls, value: str) -> str:
        """Refuse a key that would merge findings it should not.

        The failure this prevents, and it is permanent when it happens: a
        producer that sets a *constant* -- ``"assess_frontier"`` rather than a
        digest of what it assessed -- collapses every finding of that
        ``(project, kind, source_action)`` onto the first row, citably, with no
        error and nothing to notice. Prose in
        :data:`research_os.runtime.actions.base.SEMANTIC_KEY` states the
        obligation; this enforces the part of it a schema can.

        ``<producer>:v<n>:<64 hex>`` -- the shape the one producer emits. The
        digest is what makes a key specific to what was assessed, so requiring
        one makes "I hashed the inputs" the only accepted answer. Blank stays
        valid and means the finding is identified by its content, which is
        every other handler.
        """

        collapsed = value.strip()
        if not collapsed:
            return ""
        if _SEMANTIC_KEY_RE.fullmatch(collapsed) is None:
            raise ValueError(
                f"{collapsed[:64]!r} is not a well-formed semantic key. A key "
                "states what makes two findings the same observation, and two "
                "findings with equal keys are the same finding permanently -- "
                "so it must be <producer>:v<n>:<64 hex digest of the material "
                "assessed>, not a constant. See "
                "research_os.runtime.actions.base.SEMANTIC_KEY."
            )
        return collapsed

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

        if self.semantic_key:
            # Identity as the producer stated it. A separate material shape
            # rather than one more key in the one below, because the point is
            # that the content fields are *excluded*: including them would
            # leave the digest moving with the prose, which is the whole
            # defect. Versioned separately so a change to either shape cannot
            # silently restate findings already cited under the other.
            keyed = json.dumps(
                {
                    "v": "semantic-1",
                    "project_id": self.project_id,
                    "kind": str(self.kind),
                    "source_action": self.source_action or "",
                    "semantic_key": self.semantic_key,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            return hashlib.sha256(keyed.encode("utf-8")).hexdigest()

        material = json.dumps(
            {
                "v": 1,
                "project_id": self.project_id,
                "kind": str(self.kind),
                "summary": self.summary,
                # Present only when non-empty, so a finding recorded before
                # excerpts existed hashes to what it always hashed to.
                **({"excerpt": self.excerpt} if self.excerpt else {}),
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
            *((FindingRefKind.ARTIFACT.value, value) for value in self.artifact_ids),
            *(
                (FindingRefKind.CAPSULE_OBJECT.value, value)
                for value in self.capsule_refs
            ),
            *(
                (FindingRefKind.LITERATURE_KEY.value, value)
                for value in self.literature_keys
            ),
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
    def digest(self) -> str | None:
        """A stable hash of the packet, or ``None`` when nothing was supplied.

        ``None`` for an empty packet, matching
        :func:`research_os.proposal.planner.supplied_findings_digest` and for
        the reason that function gives: a real-looking 64-hex digest of the
        empty set is indistinguishable from "we forgot to record it". An
        adversarial review found a recovered proposal reporting exactly that --
        ``grounded_in_findings: []`` beside a confident digest -- for a proposal
        whose stored allowlist named two findings.

        What it pins is "this proposal was grounded in exactly these findings,
        each saying exactly this". Recomputed before a human promotion, so a
        proposal whose grounding has been superseded fails closed rather than
        being promoted on a basis that no longer holds.
        """

        if not self.findings:
            return None
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
                "excerpt": _clip(item.excerpt, MAX_RENDERED_EXCERPT_CHARS),
                "excerpt_is_noncanonical": True,
                "rests_on_artifacts": list(item.artifact_ids),
                "rests_on_capsule_objects": list(item.capsule_refs),
                "rests_on_literature": list(item.literature_keys),
                "experiment_job_id": item.experiment_job_id or "",
            }
            for item in self.findings
        )


def _clip(text: str, limit: int) -> str:
    """Clip visibly, so a reader can tell short from shortened."""

    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def packet_from(findings: Sequence[RuntimeFinding]) -> FindingPacket:
    """Build a packet, preserving order and refusing an unstored finding."""

    return FindingPacket(findings=tuple(findings))
