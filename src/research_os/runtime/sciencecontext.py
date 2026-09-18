"""What this project has learned, and has not yet accepted.

The gap this closes. A cycle that finished a literature audit registered its
artifact, recorded a :class:`~research_os.runtime.findings.RuntimeFinding`, and
linked the provenance -- all of it correct, all of it durable. Then the next
cycle's planner was told ``findings_available: 1`` and nothing else, because
:func:`research_os.runtime.graphs.cycle.plan_one_action` passed a *count*. The
reasoning for the count is in the commit that added it and is not silly: the
planner chooses an action rather than reasoning about evidence, and handing it a
finding's text invites it to plan from the content.

But the cost was that completed scientific work vanished. The frontier is
derived from capsule files only, and the capsule cannot change without a human
promotion, so a planner seeing only the frontier sees a project where the audit
never happened. It then plans the audit again, and the proposal it eventually
writes describes the literature as unavailable while the literature artifact
sits in the repository. The thesis pilot did exactly that.

The fix is not to put more of the repository in the prompt. It is to give the
planner the one thing it was missing and to type it precisely:

.. code-block:: text

    canonical           the frontier, from capsule files, changed only by a person
    noncanonical        findings: what this runtime observed, accepted by nobody
    outstanding         proposals: decisions already in front of a person

Three labelled categories, each bounded, the middle one fenced as untrusted
autonomous output. A finding here is evidence *that work was done and what it
said*; it is not a claim, it has no status a person could accept, and the
planner is told so in the same sentence it is handed them.

**Why it cannot become an accepted claim by this route.** Nothing in this module
writes. It reads two tables and returns dictionaries. The only path from a
finding to canonical state still runs through ``propose_capsule_change`` and a
human promotion, and :mod:`research_os.runtime.kernel` still holds no handle
that could write a capsule.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from research_os.runtime.findings import RuntimeFinding

#: The most findings the planner is shown.
#:
#: Twelve, matching :data:`research_os.runtime.findings.MAX_PACKET_FINDINGS`,
#: for the reason that constant gives: a set nobody can read in one sitting is
#: a set that gets skimmed. The *count* of everything is reported separately,
#: so a project with two hundred findings does not look like a project with
#: twelve.
MAX_PLANNER_FINDINGS = 12

#: Characters of each summary the planner is shown.
#:
#: Enough for a handler's sentence about what it found, short enough that
#: twelve of them cannot become the largest thing in the prompt. The full text
#: is in the finding, which the proposal layer reads in full.
MAX_PLANNER_SUMMARY_CHARS = 600

#: Characters of each excerpt the planner is shown.
#:
#: Shorter than the proposal layer's allowance, and shorter on purpose. The
#: planner picks a *verb*; it does not weigh evidence. What it needs from an
#: excerpt is enough to tell "the literature audit found eight works on
#: working-set methods" from "the literature audit found nothing", which is a
#: sentence, not a document. The proposal worker -- the one actually asked to
#: ground a conclusion -- reads the longer form.
MAX_PLANNER_EXCERPT_CHARS = 400

#: The most outstanding proposals the planner is shown.
MAX_PLANNER_PROPOSALS = 6

#: The most preregistered designs the planner is shown.
#:
#: The reason this category exists at all: `design_experiment` is deliberately
#: not in `FINDING_FOR_ACTION` -- "a specification is a plan, and its
#: preregistration artifact is already durable and already looked up by digest"
#: -- but *looked up by digest* only helps a caller who has the digest, and a
#: fresh planner does not. So a preregistered design was invisible to the next
#: cycle, while the frontier went on reporting the hypothesis as untested,
#: because a preregistration is not a capsule object and only a person can make
#: it one.
#:
#: The live thesis runtime did exactly that six times in ninety minutes: six
#: `design_experiment` calls, six *distinct* spec digests -- variations on one
#: test of HYP-0002 -- and about four dollars of model spend, none of it wrong
#: and none of it new. Deduplication by digest could not stop it because each
#: design differed.
MAX_PLANNER_PREREGISTRATIONS = 6


@dataclass(frozen=True, slots=True)
class NoncanonicalScience:
    """The bounded, explicitly-not-accepted half of a planner's context.

    A type rather than two lists because the *totals* are part of the meaning:
    "twelve findings shown of two hundred" tells a planner something that
    twelve findings alone does not, and the sentence that says so has to be
    built from both numbers at once.
    """

    findings: tuple[Mapping[str, object], ...]
    proposals: tuple[Mapping[str, object], ...]
    preregistrations: tuple[Mapping[str, object], ...]
    findings_total: int
    proposals_total: int
    preregistrations_total: int

    @property
    def empty(self) -> bool:
        return not self.findings and not self.proposals and not self.preregistrations

    def census(self) -> str:
        """One line of counts, for a plain prompt field outside every fence.

        Controller-authored text: a planner deciding whether there is anything
        to propose from reads this, and no model's output can influence it.
        """

        parts = [
            (f"{self.findings_total} completed finding(s), {len(self.findings)} shown")
        ]
        if self.proposals_total:
            parts.append(
                f"{self.proposals_total} proposal(s) already awaiting a human "
                f"decision, {len(self.proposals)} shown"
            )
        else:
            parts.append("no proposal has been put to a human yet")
        if self.preregistrations_total:
            parts.append(
                f"{self.preregistrations_total} experiment design(s) already "
                f"preregistered, {len(self.preregistrations)} shown"
            )
        return "; ".join(parts)


def finding_view(
    finding: RuntimeFinding, *, cited_by: Sequence[str] = ()
) -> dict[str, object]:
    """One finding as plain data for a fenced block.

    ``summary`` is a model's sentence and is truncated here rather than at the
    fence, so the truncation is visible in what the block contains rather than
    being a property of how it was serialised.

    ``noncanonical`` is in every entry on purpose. It is redundant with the
    fence banner and with the instruction, and a planner that skims will still
    see it next to the text it is skimming.
    """

    summary = finding.summary.strip()
    truncated = len(summary) > MAX_PLANNER_SUMMARY_CHARS
    return {
        "finding_id": finding.finding_id,
        "noncanonical": True,
        "kind": str(finding.kind),
        "produced_by_action": finding.source_action or "",
        "cycle": finding.source_cycle if finding.source_cycle is not None else -1,
        "summary": summary[:MAX_PLANNER_SUMMARY_CHARS] + ("..." if truncated else ""),
        "excerpt": _clipped(finding.excerpt, MAX_PLANNER_EXCERPT_CHARS),
        "rests_on_artifacts": list(finding.artifact_ids),
        "rests_on_capsule_objects": list(finding.capsule_refs),
        "rests_on_literature": list(finding.literature_keys),
        "experiment_job_id": finding.experiment_job_id or "",
        "already_cited_by_proposals": list(cited_by),
    }


def _clipped(text: str, limit: int) -> str:
    """Clip visibly, so a planner can tell short from shortened."""

    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit] + "..."


def preregistration_view(
    row: Mapping[str, object], *, hypothesis: str = ""
) -> dict[str, object]:
    """One preregistered design as plain data for a fenced block.

    ``hypothesis`` is what actually stops the loop -- "a design for HYP-0002
    already exists" is the sentence a planner needs, and the digest alone does
    not say it.
    """

    return {
        "spec_digest": str(row["spec_digest"])[:16],
        "noncanonical": True,
        "preregistered_at": row["created_at"],
        "tests_hypothesis": hypothesis,
        "artifact_id": row["artifact_id"],
    }


def noncanonical_science(
    store: Any, *, project_id: str, artifacts: Any = None
) -> NoncanonicalScience:
    """Assemble what this project has learned but not yet had accepted.

    Two reads and no writes. The findings are newest-first and bounded; each
    carries the proposals that already cite it, so a planner can tell "this was
    observed and nobody has been asked about it" from "this was observed and a
    person is already deciding about it" -- the distinction that decides whether
    another ``propose_capsule_change`` would be useful or duplicative.
    """

    shown = store.list_findings(project_id=project_id, limit=MAX_PLANNER_FINDINGS)
    total = store.count_findings(project_id=project_id)
    proposals = store.created_proposals(
        project_id=project_id, limit=MAX_PLANNER_PROPOSALS
    )
    proposals_total = store.count_created_proposals(project_id=project_id)

    # Designs already frozen, and the hypothesis each one tests. The document
    # is read for the bounded set only, and a document that cannot be read
    # yields an empty hypothesis rather than an error: this is planning
    # context, and losing one field of it must not fail a cycle.
    prereg_rows = store.stored_preregistrations(
        project_id=project_id, limit=MAX_PLANNER_PREREGISTRATIONS
    )
    preregistrations = tuple(
        preregistration_view(
            row, hypothesis=_hypothesis_of(artifacts, str(row["artifact_id"]))
        )
        for row in prereg_rows
    )
    preregistrations_total = store.count_stored_preregistrations(project_id=project_id)

    # Which proposals cite which finding. Built from the proposals actually
    # shown, so the edge a planner reads is one it can also see the other end
    # of; a citation by a proposal too old to be listed would be an identifier
    # pointing at nothing.
    cited_by: dict[str, list[str]] = {}
    for row in proposals:
        proposal_id = str(row["proposal_id"])
        for finding in store.proposal_findings(proposal_id, cited_only=True):
            cited_by.setdefault(finding.finding_id, []).append(proposal_id)

    return NoncanonicalScience(
        findings=tuple(
            finding_view(item, cited_by=tuple(cited_by.get(item.finding_id, ())))
            for item in shown
        ),
        proposals=proposals,
        preregistrations=preregistrations,
        findings_total=total,
        proposals_total=proposals_total,
        preregistrations_total=preregistrations_total,
    )


def _hypothesis_of(artifacts: Any, artifact_id: str) -> str:
    """The hypothesis a stored preregistration names, or "" if unreadable."""

    if artifacts is None:
        return ""
    try:
        document = json.loads(artifacts.get_text(artifact_id))
    except Exception:  # noqa: BLE001 - planning context, never a cycle failure
        return ""
    value = document.get("hypothesis") if isinstance(document, dict) else None
    return str(value)[:64] if value else ""
