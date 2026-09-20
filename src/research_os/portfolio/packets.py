"""The frozen packet a reviewer reads, and what it deliberately does not hold.

A :class:`ReviewPacket` is the whole input to an independent review. It is a
frozen dataclass with **no field for another reviewer's verdict**, and that
absence is how "do not expose Reviewer A's verdict to Reviewer B" stops being
an instruction a caller must remember. Adding such a field would be a visible
change to a type three tests assert the shape of.

It also carries a digest. Two reviewers of one idea version read identical
bytes, the digest is stored on each review row, and a reviewer that somehow
read something else is therefore detectable after the fact rather than only
suspected.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from research_os.portfolio.models import (
    IdeaEvidence,
    IdeaObjection,
    IdeaVersion,
)

PACKET_DIGEST_VERSION = "pidea-packet-v1"

#: How many evidence rows and objections a packet may quote.
#:
#: A bound rather than a limit that will be hit. A reviewer handed sixty
#: evidence rows reads the first few; the rest make the prompt large and the
#: review no better. Exceeding it is recorded in ``notes`` so the reviewer is
#: told what it is not seeing, which is different from not showing it.
MAX_PACKET_EVIDENCE = 20
MAX_PACKET_OBJECTIONS = 12


@dataclass(frozen=True, slots=True)
class ReviewPacket:
    """Everything one reviewer is given about one idea version.

    Note what is **not** here, each for a different reason:

    - ``reviews`` / ``verdicts``: three reviewers reading each other would be
      one reviewer with three names.
    - ``quality_tier`` / ``adjudication_type``: both are computed from rows,
      and telling a reviewer the answer the gate will reach invites it to
      agree.
    - ``origin_call_id`` or anything naming the producer: a reviewer that knew
      which model wrote the idea could defer to it.
    """

    idea_id: str
    version: int
    content_digest: str
    title: str
    research_question: str
    core_idea: str
    mechanism: str
    why_it_matters: str
    falsifier: str
    closest_prior_work: str
    claimed_difference: str
    assumptions: tuple[str, ...] = ()
    alternative_explanations: tuple[str, ...] = ()
    open_uncertainties: tuple[str, ...] = ()
    evidence: tuple[tuple[str, str, str], ...] = ()
    standing_objections: tuple[tuple[str, str], ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def digest(self) -> str:
        payload = {
            "idea": self.idea_id,
            "version": self.version,
            "content": self.content_digest,
            "title": self.title,
            "research_question": self.research_question,
            "core_idea": self.core_idea,
            "mechanism": self.mechanism,
            "why_it_matters": self.why_it_matters,
            "falsifier": self.falsifier,
            "closest_prior_work": self.closest_prior_work,
            "claimed_difference": self.claimed_difference,
            "assumptions": list(self.assumptions),
            "alternative_explanations": list(self.alternative_explanations),
            "open_uncertainties": list(self.open_uncertainties),
            "evidence": [list(item) for item in self.evidence],
            "objections": [list(item) for item in self.standing_objections],
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return f"{PACKET_DIGEST_VERSION}:{hashlib.sha256(encoded).hexdigest()}"

    def idea_block(self) -> list[str]:
        """The idea, as lines for a fenced data block."""

        lines = [
            f"title: {self.title}",
            f"research question: {self.research_question}",
            f"core idea: {self.core_idea}",
            f"mechanism: {self.mechanism or '(none stated)'}",
            f"why it matters: {self.why_it_matters or '(none stated)'}",
            f"falsifier: {self.falsifier or '(none stated)'}",
            f"closest prior work: {self.closest_prior_work or '(none stated)'}",
            f"claimed difference: {self.claimed_difference or '(none stated)'}",
        ]
        for label, values in (
            ("assumption", self.assumptions),
            ("alternative explanation", self.alternative_explanations),
            ("open uncertainty", self.open_uncertainties),
        ):
            lines.extend(f"{label}: {item}" for item in values)
        lines.extend(f"note: {item}" for item in self.notes)
        return lines

    def evidence_block(self) -> list[str]:
        return [
            f"[{kind}/{strength}] {summary}"
            for kind, strength, summary in self.evidence
        ] or ["(no evidence recorded)"]

    def objections_block(self) -> list[str]:
        return [
            f"[{severity}] {summary}" for severity, summary in self.standing_objections
        ] or ["(no standing objections)"]


def build_packet(
    *,
    version: IdeaVersion,
    evidence: Sequence[IdeaEvidence],
    objections: Sequence[IdeaObjection],
) -> ReviewPacket:
    """Freeze one idea version and its support into a reviewer's whole input.

    Ordered deterministically by id, so two reviewers of one version read
    identical bytes and the digest means something.
    """

    ordered_evidence = sorted(evidence, key=lambda item: item.evidence_id)
    ordered_objections = sorted(
        (item for item in objections if item.open),
        key=lambda item: (item.raised_at_version, item.objection_id),
    )
    notes: list[str] = []
    if len(ordered_evidence) > MAX_PACKET_EVIDENCE:
        notes.append(
            f"{len(ordered_evidence) - MAX_PACKET_EVIDENCE} further evidence "
            f"row(s) exist and are not quoted here"
        )
    if len(ordered_objections) > MAX_PACKET_OBJECTIONS:
        notes.append(
            f"{len(ordered_objections) - MAX_PACKET_OBJECTIONS} further standing "
            f"objection(s) exist and are not quoted here"
        )
    return ReviewPacket(
        idea_id=version.idea_id,
        version=version.version,
        content_digest=version.content_digest,
        title=version.title,
        research_question=version.research_question,
        core_idea=version.core_idea,
        mechanism=version.mechanism,
        why_it_matters=version.why_it_matters,
        falsifier=version.falsifier,
        closest_prior_work=version.closest_prior_work,
        claimed_difference=version.claimed_difference,
        assumptions=version.assumptions,
        alternative_explanations=version.alternative_explanations,
        open_uncertainties=version.open_uncertainties,
        evidence=tuple(
            (str(item.kind), str(item.strength), item.summary)
            for item in ordered_evidence[:MAX_PACKET_EVIDENCE]
        ),
        standing_objections=tuple(
            (str(item.severity), item.summary)
            for item in ordered_objections[:MAX_PACKET_OBJECTIONS]
        ),
        notes=tuple(notes),
    )
