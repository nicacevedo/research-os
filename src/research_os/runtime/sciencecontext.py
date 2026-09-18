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
writes. It *reads* -- two operational tables, the proposal documents those
tables name, and (for the basis check only) the project's own capsule -- and
returns dictionaries. An earlier version of this paragraph said "two tables",
which stopped being true when proposal items and basis freshness were added,
and a security review was right to point at it: the honest statement is that
the reads have grown and the writes are still none.

The only path from a finding to canonical state still runs through
``propose_capsule_change`` and a human promotion, and
:mod:`research_os.runtime.kernel` still holds no handle that could write a
capsule.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from research_os.runtime.findings import RuntimeFinding

LOG = logging.getLogger("research_os.runtime.sciencecontext")

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

#: The most proposed items shown for one proposal.
#:
#: The gap this closes, found by running the thing. A real frontier assessment
#: ranked "audit whether the existing proposals already cover the open
#: questions" as its highest-value action and then reported that it could not
#: perform it -- because a proposal reached the prompt as
#: ``{proposal_id, run_id, created_at, cited_findings, offered_findings}`` and
#: nothing else. Five fields, none of them scientific. "Does a proposal already
#: ask about Q-0004" is not answerable from a count of findings, so the
#: assessment either guessed or asked for file access it must not have.
#:
#: What makes it answerable is the one thing a proposed item carries that the
#: reservation row does not: ``addresses``, the capsule object ids the item is
#: about. With those, coverage is a set intersection the reader can do, and the
#: two live thesis proposals -- twelve items and eleven -- become a map from
#: open question to the item already asking about it.
#:
#: Twelve, matching ``proposal.planner.MAX_ITEMS``: a proposal cannot hold more
#: than that, so a bound below it would silently hide items and make a covered
#: question look uncovered. The *count* is reported separately either way.
MAX_PROPOSAL_ITEMS = 12

#: Characters of a proposed item's title shown.
#:
#: A title, not a statement. The reader is deciding whether a question is
#: already in front of a person, and the item's ``addresses`` answers that; the
#: title is there so the answer is legible rather than a list of identifiers.
#: The statements are in the proposal, which is what the researcher reads.
#:
#: **Eighty, and the number is measured rather than chosen.** An independent
#: security review found the first version of this block silently clipped: a
#: proposal view is *one* block entry, ``prompt_safe_block`` clips each entry
#: at ``DEFAULT_FIELD_CHARS`` (2 000), and twelve items at ``indent=2`` with a
#: 160-character title serialise to 6 550 characters -- so **three** of the
#: twelve reached the prompt, the JSON was cut mid-object, and nothing said so.
#: Both live thesis proposals hold eleven and twelve items, so the coverage
#: intersection this block exists for was being computed over a quarter of the
#: data while ``census`` asserted all of it was there.
#:
#: Three changes together make it fit, and the test at the *rendered* layer is
#: what keeps it fitting: this bound, compact serialisation at both render
#: sites, and an explicit per-block limit on the template
#: (:data:`research_os.runtime.prompts.PROPOSAL_BLOCK_CHARS`). Worst case now
#: measured at 3 595 characters for twelve items.
MAX_PROPOSAL_TITLE_CHARS = 80


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

    @property
    def proposed_items_shown(self) -> int:
        """How many proposed items the blocks actually carry."""

        return sum(len(tuple(row.get("items") or ())) for row in self.proposals)

    @property
    def proposed_items_total(self) -> int:
        """How many the readable proposals hold, shown or not."""

        return sum(
            int(row.get("items_total") or 0)
            for row in self.proposals
            if not row.get("items_unavailable")
        )

    @property
    def proposals_unreadable(self) -> int:
        """How many shown proposals could not be opened."""

        return sum(1 for row in self.proposals if row.get("items_unavailable"))

    def census(self) -> str:
        """One line of counts, for a plain prompt field outside every fence.

        Controller-authored text: a planner deciding whether there is anything
        to propose from reads this, and no model's output can influence it.
        """

        parts = [
            (f"{self.findings_total} completed finding(s), {len(self.findings)} shown")
        ]
        if self.proposals_total:
            # "N shown" and not "N", because a bound that is silently hit is
            # the defect a security review found one layer down: the reader was
            # told every item was present while nine of twelve had been clipped
            # out of the rendered block, and read the missing ones as absent
            # rather than as unseen.
            items = (
                f"{self.proposed_items_shown} proposed item(s)"
                if self.proposed_items_shown == self.proposed_items_total
                else (
                    f"{self.proposed_items_shown} of "
                    f"{self.proposed_items_total} proposed item(s)"
                )
            )
            parts.append(
                f"{self.proposals_total} proposal(s) already awaiting a human "
                f"decision, {len(self.proposals)} shown with {items} and the "
                f"capsule objects each one addresses"
            )
            # Said in the controller's own words, outside every fence, because
            # a reader that cannot tell "nothing addresses this question" from
            # "I could not read what addresses it" will read the second as the
            # first -- which is how an assessment concludes a direction is
            # uncovered when it is already in front of a person.
            unreadable = self.proposals_unreadable
            if unreadable:
                parts.append(
                    f"{unreadable} of those proposal(s) could not be read, so "
                    "coverage cannot be established for them"
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


def proposal_view(
    row: Mapping[str, object],
    *,
    repo_path: str | None = None,
    project_id: str | None = None,
) -> dict[str, object]:
    """One outstanding proposal as plain data, including what it proposes about.

    **What the row alone could not say.** ``created_proposals`` returns the
    reservation: an id, the run that made it, when, and how many findings it
    cited. Every field is operational. A reader asked "is Q-0004 already in
    front of the researcher" cannot answer it from any of them, and a real
    frontier assessment ranked that exact audit first and then reported it had
    no way to perform it.

    So the items are read here, from the proposal store, by the id the
    reservation gives. Each item contributes the two things that decide
    coverage -- the capsule objects it ``addresses``, and whether a person has
    already acted on it -- plus a title so the answer is legible to a person
    reading the prompt rather than a list of identifiers.

    **Read-only, and never fatal.** This is planning context. A proposal
    directory that has been moved, a document this build cannot parse, a
    permission error: each yields the row with ``items_unavailable`` set and
    the reader told plainly that coverage could not be established, rather
    than failing a cycle over a decision aid. Reporting the absence matters as
    much as reporting the items: "no item addresses Q-0004" and "I could not
    read the items" are different facts, and a reader that cannot tell them
    apart will read the second as the first.

    ``repo_path`` is supplied by a caller that knows where the project is --
    the run's own repository, from the runtime's context, never the path
    recorded inside the proposal document. With it, the basis check runs and
    the reader learns whether the science the proposal rests on has moved
    since it was written; without it, the basis is reported as unchecked.
    """

    from research_os.proposal.store import ProposalStore  # lazy: scientific layer

    view = dict(row)
    view["noncanonical"] = True
    proposal_id = str(row.get("proposal_id") or "")
    try:
        store = ProposalStore.open(proposal_id)
        proposal = store.load()
        decided = store.decided_item_ids()
    except Exception as exc:  # noqa: BLE001 - planning context, never a failure
        return _unreadable(view, _why_unreadable(exc))

    # The row was scoped by project; the *document* was not.
    #
    # `created_proposals` filters on `project_id`, but the document is then
    # read out of a store shared by every project in one state home, and a
    # reserved id is a 32-bit digest of the reservation key
    # (`proposal.store.reserved_proposal_id`). A collision would put another
    # project's items, titles and addresses into this project's coverage
    # calculation. Unlikely, and free to exclude. Checked only when the
    # document records an id, because proposals written before that field
    # existed record none and refusing those would hide real ones.
    document_project = getattr(proposal, "project_id", None)
    if project_id and document_project and str(document_project) != project_id:
        return _unreadable(
            view,
            f"this document belongs to a different project ({document_project})",
        )

    items = list(proposal.items)
    shown = items[:MAX_PROPOSAL_ITEMS]
    view["items_total"] = len(items)
    #: How many of them this entry carries. Reported beside the total, so a
    #: proposal with more items than the bound reads as *partially* shown
    #: rather than as fully shown -- the same distinction ``items_unavailable``
    #: keeps for a proposal that could not be read at all, and for the same
    #: reason: a reader that cannot tell "nothing addresses this" from "I was
    #: not shown everything" will treat the second as the first.
    view["items_shown"] = len(shown)
    view["items"] = [
        # Four fields and a title, and the omissions are deliberate. This block
        # exists so a reader can compute coverage; `importance`, `confidence`
        # and each item's evidence `basis` are adjudication aids for the
        # researcher reading the proposal itself, and carrying them here cost
        # about 900 characters across twelve items -- which is what pushed the
        # entry past the clip that lost nine of them.
        {
            "item_id": item.item_id,
            "kind": str(item.kind),
            "title": _clipped(item.title, MAX_PROPOSAL_TITLE_CHARS),
            # The field coverage is computed from. Capsule object ids, sorted,
            # so two renderings of one item are the same text.
            "addresses": sorted(str(target) for target in item.addresses if target),
            # "A person has already acted on this" -- promoted or declined. An
            # item nobody has decided is the only kind still worth ranking.
            "decided_by_human": item.item_id in decided,
        }
        for item in shown
    ]
    view["base_commit"] = proposal.base_commit or ""
    view["basis"] = _basis_line(proposal, repo_path=repo_path)
    return view


def _unreadable(view: dict[str, object], reason: str) -> dict[str, object]:
    """The view for a proposal whose items could not be established.

    One shape for every cause, because the reader's response to all of them is
    the same: coverage for this proposal is *unknown*. What must not happen is
    an empty ``items`` list with no marker beside it, which reads identically
    to "this proposal addresses nothing".
    """

    view["items_unavailable"] = reason
    view["items"] = []
    view["items_total"] = 0
    view["items_shown"] = 0
    view["basis"] = "unchecked: the proposal could not be read"
    return view


def _why_unreadable(exc: BaseException) -> str:
    """Why a read failed, in a form that can safely enter a prompt.

    **The exception class, and deliberately not its message.** An independent
    audit of the first version found the boundary this module claims being
    false on exactly this path. ``ProposalStore.open`` raises ``no proposal
    <id> under <proposals_root()>``, an absolute path under the state home;
    formatting ``str(exc)`` put it into ``items_unavailable``, which is
    serialised into the prompt -- so a role told in the same prompt that it has
    no filesystem was handed the researcher's home directory name, and a test
    asserted that it arrived.

    A class name is producer-controlled and path-free, and it is what the
    reader needs: "the items could not be read" is the fact that changes its
    reasoning, and *which* directory could not be read is not. The full
    exception goes to the log, where a person debugging it is looking and a
    model provider is not.
    """

    LOG.info("proposal context unavailable: %s", exc, exc_info=False)
    return type(exc).__name__


def _basis_line(proposal: object, *, repo_path: str | None) -> str:
    """Whether the science this proposal rests on still holds, in one line.

    Three answers and not two, because ``basis_status`` has three and
    collapsing them is how "we never checked" gets read as "we checked and it
    is fine". A proposal written before basis snapshots existed is
    ``unchecked``; so is one this caller has no repository for.
    """

    if not repo_path:
        return "unchecked: no repository path was supplied to this reader"
    from pathlib import Path

    from research_os.proposal.basis import basis_status  # lazy: scientific layer

    try:
        status = basis_status(proposal, project_path=Path(repo_path))  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 - planning context, never a failure
        # Path-free by construction: `_why_unreadable` reports the class only.
        # This branch is reachable with the *caller's* repository in the
        # exception -- a capsule mid-checkout, a missing directory -- and that
        # path must not enter a prompt either.
        return f"unchecked: the basis check failed ({_why_unreadable(exc)})"
    if not status.checkable:
        return f"unchecked: {' '.join(status.reason.split())[:200]}"
    if status.stale:
        changed = ", ".join(status.changed_objects[:6]) or "see the proposal"
        return (
            "STALE: the scientific objects it rests on have changed since it "
            f"was written ({changed}). Promoting it needs a regenerated "
            "proposal, so it is not a live decision as it stands."
        )
    return "current: the scientific objects it rests on are unchanged"


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
    store: Any,
    *,
    project_id: str,
    artifacts: Any = None,
    repo_path: str | None = None,
    exclude_finding_actions: Sequence[str] = (),
) -> NoncanonicalScience:
    """Assemble what this project has learned but not yet had accepted.

    Reads only, no writes. The findings are newest-first and bounded; each
    carries the proposals that already cite it, so a planner can tell "this was
    observed and nobody has been asked about it" from "this was observed and a
    person is already deciding about it" -- the distinction that decides whether
    another ``propose_capsule_change`` would be useful or duplicative.

    ``exclude_finding_actions`` leaves out findings a particular *caller* must
    not be shown. There is one such caller: the frontier assessment, which must
    not be handed its own previous assessments in a block the instruction
    describes as completed work. Its own prior recommendation and rationale
    read as established findings, and the role whose answer decides whether
    more money is spent and whether the system stops for a person is the last
    one that should be anchored on what it said last time. The planner passes
    nothing and sees everything.

    ``repo_path`` is the run's own repository. It is used for one thing --
    whether each proposal's scientific basis still holds -- and a caller that
    does not have it gets proposals whose basis is reported ``unchecked``,
    which is honest and still useful. The path comes from the runtime's
    context, never from a proposal document: a reader that dereferenced a path
    it read out of a stored document would be letting the document choose what
    gets opened.
    """

    shown = store.list_findings(
        project_id=project_id,
        limit=MAX_PLANNER_FINDINGS,
        exclude_actions=exclude_finding_actions,
    )
    total = store.count_findings(
        project_id=project_id, exclude_actions=exclude_finding_actions
    )
    proposal_rows = store.created_proposals(
        project_id=project_id, limit=MAX_PLANNER_PROPOSALS
    )
    proposals_total = store.count_created_proposals(project_id=project_id)
    # The items, not just the reservation. See `proposal_view`: without them a
    # reader cannot answer "is this question already in front of a person",
    # which is the question the frontier assessment is for.
    proposals = tuple(
        proposal_view(row, repo_path=repo_path, project_id=project_id)
        for row in proposal_rows
    )

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
    for row in proposal_rows:
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
