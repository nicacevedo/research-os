"""Assembling what a writer is allowed to write from.

Deterministic throughout, and the filtering is the point. A Claim reaches the
packet only if it is *accepted* and R0's acceptance rule actually holds for it
right now -- a concluded human approve Review bound to its current digest and to
every Evidence and Experiment it reaches. The rule is not restated here; it is
called, from the one place that implements it, so the packet and the validator
cannot drift.

A Claim that was asked for and did not qualify is *reported*, not dropped. A
writer that silently did not receive it would write around a hole; a researcher
who asked for it deserves to be told whether it is not accepted, or its approval
has gone stale, and which.

Contrary Evidence travels with the Claim it contradicts, marked. Omitting it
would be the single most damaging thing this pipeline could do, and it is
invisible unless the packet names it.

Literature arrives as structured entries with a generated citation key. The
writer must cite by that key, which turns "did this citation exist" from a fuzzy
name-and-year comparison into a set difference.
"""

from __future__ import annotations

import re
from pathlib import Path

from research_os.automation.promptdata import (
    LITERATURE_FENCE,
    STATEMENT_FENCE,
    prompt_safe,
    prompt_safe_block,
    render_data_block,
)
from research_os.capsule import validate_project
from research_os.digests import subject_digest
from research_os.errors import PaperPacketError
from research_os.literature.store import LiteratureStore
from research_os.models import (
    Claim,
    ClaimStatus,
    Evidence,
    Experiment,
    Reviewable,
    ScientificObject,
)
from research_os.paper.models import (
    ClaimEntry,
    EvidenceEntry,
    ExperimentEntry,
    LiteratureEntry,
    SourcePacket,
)
from research_os.validate import claim_approval, referenced_experiments

MAX_LABEL_CHARS = 300
MAX_STATEMENT_CHARS = 3_000

_CITATION_KEY_RE = re.compile(r"[^A-Za-z0-9]+")


def build_source_packet(
    project_path: Path,
    *,
    claim_ids: list[str] | None = None,
    literature_keys: list[str] | None = None,
    limitations: list[str] | None = None,
    literature_store: LiteratureStore | None = None,
) -> SourcePacket:
    """Assemble the sources one writing task may use.

    ``claim_ids`` selects which accepted Claims to include; omitting it includes
    every accepted Claim that currently qualifies. Either way, qualification is
    re-established now rather than trusted from the file, because a Claim that
    was accepted last week and whose Evidence has since changed is exactly the
    thing a manuscript must not quote.
    """

    report = validate_project(project_path)
    if report.project is None or report.capsule is None:
        raise PaperPacketError(
            f"{project_path} has no Research Capsule, so there is no accepted "
            "science to write from"
        )
    project_id = report.project.id
    by_id: dict[str, ScientificObject] = {item.id: item for item in report.objects}

    requested = list(claim_ids) if claim_ids else None
    claims: list[ClaimEntry] = []
    excluded: dict[str, str] = {}
    for candidate in _candidate_claims(by_id, requested):
        entry, reason = _claim_entry(candidate, by_id, project_id)
        if entry is None:
            excluded[candidate.id] = reason
            continue
        claims.append(entry)
    for missing in sorted(set(requested or []) - set(by_id)):
        excluded[missing] = "this project has no object with that id"

    evidence, experiments = _linked_sources(claims, by_id, project_id)
    literature = _literature_entries(literature_keys or [], literature_store)

    return SourcePacket(
        project_id=project_id,
        project_path=str(report.git_root),
        claims=sorted(claims, key=lambda item: item.claim_id),
        evidence=sorted(evidence, key=lambda item: item.evidence_id),
        experiments=sorted(experiments, key=lambda item: item.experiment_id),
        literature=sorted(literature, key=lambda item: item.citation_key),
        limitations=[item for item in (limitations or []) if item.strip()],
        excluded_claims=excluded,
    )


def _candidate_claims(
    by_id: dict[str, ScientificObject], requested: list[str] | None
) -> list[Claim]:
    if requested is None:
        return sorted(
            (item for item in by_id.values() if isinstance(item, Claim)),
            key=lambda item: item.id,
        )
    found: list[Claim] = []
    for claim_id in requested:
        candidate = by_id.get(claim_id)
        if isinstance(candidate, Claim):
            found.append(candidate)
    return found


def _claim_entry(
    claim: Claim,
    by_id: dict[str, ScientificObject],
    project_id: str,
) -> tuple[ClaimEntry | None, str]:
    """Return the packet entry for one Claim, or why it may not be written from."""

    if claim.status is not ClaimStatus.ACCEPTED:
        return None, (
            f"this claim is {claim.status}, not accepted. A manuscript may only "
            "state what the project has actually accepted."
        )
    approval = claim_approval(claim, by_id, project_id)
    if approval.review is None:
        if approval.stale:
            reason = (
                "its human approval no longer matches the current subject, "
                "evidence, and experiment digests: the science changed after it "
                "was approved"
            )
        elif approval.incomplete_evidence:
            reason = "its human approval does not bind every linked evidence object"
        elif approval.incomplete_experiments:
            reason = (
                "its human approval does not bind every experiment its evidence "
                "relies on"
            )
        else:
            reason = "it has no concluded human approve review bound to its digest"
        return None, reason
    review = approval.review
    return (
        ClaimEntry(
            claim_id=claim.id,
            title=claim.title,
            statement=claim.statement,
            digest=subject_digest(claim, project_id=project_id),
            approval_review_id=review.id,
            approved_findings=review.findings or "",
            supporting_evidence=list(claim.supporting_evidence or []),
            contrary_evidence=list(claim.contrary_evidence or []),
            contrary_evidence_addressed=claim.contrary_evidence_addressed,
            hypotheses=list(claim.hypotheses or []),
        ),
        "",
    )


def _linked_sources(
    claims: list[ClaimEntry],
    by_id: dict[str, ScientificObject],
    project_id: str,
) -> tuple[list[EvidenceEntry], list[ExperimentEntry]]:
    """Return the Evidence and Experiments the supplied Claims actually reach."""

    contrary_ids: set[str] = set()
    wanted: set[str] = set()
    for claim in claims:
        wanted.update(claim.supporting_evidence)
        wanted.update(claim.contrary_evidence)
        contrary_ids.update(claim.contrary_evidence)

    evidence: list[EvidenceEntry] = []
    experiment_ids: set[str] = set()
    for evidence_id in sorted(wanted):
        candidate = by_id.get(evidence_id)
        if not isinstance(candidate, Evidence):
            continue
        evidence.append(
            EvidenceEntry(
                evidence_id=candidate.id,
                title=candidate.title,
                statement=candidate.statement,
                kind=str(candidate.kind),
                status=str(candidate.status),
                digest=_digest(candidate, project_id),
                experiment_id=candidate.experiment,
                citation=candidate.citation,
                notes=candidate.notes,
                contrary=evidence_id in contrary_ids,
            )
        )
        if candidate.experiment:
            experiment_ids.add(candidate.experiment)

    for claim in claims:
        source = by_id.get(claim.claim_id)
        if isinstance(source, Claim):
            experiment_ids.update(referenced_experiments(source, by_id))

    experiments: list[ExperimentEntry] = []
    for experiment_id in sorted(experiment_ids):
        candidate = by_id.get(experiment_id)
        if not isinstance(candidate, Experiment):
            continue
        experiments.append(
            ExperimentEntry(
                experiment_id=candidate.id,
                title=candidate.title,
                purpose=candidate.purpose,
                status=str(candidate.status),
                digest=_digest(candidate, project_id),
                primary_metrics=list(candidate.primary_metrics or []),
                decision_rule=candidate.decision_rule,
                predictions=[
                    f"{item.hypothesis}: {item.predicted_outcome}"
                    + ("  [discriminates]" if item.discriminates else "")
                    for item in (candidate.predictions or [])
                ],
                provenance=(
                    candidate.provenance.model_dump(mode="json")
                    if candidate.provenance is not None
                    else {}
                ),
                result_manifest=candidate.result_manifest,
                artifacts=list(candidate.artifacts or []),
            )
        )
    return evidence, experiments


def _digest(item: ScientificObject, project_id: str) -> str:
    return (
        subject_digest(item, project_id=project_id)
        if isinstance(item, Reviewable)
        else ""
    )


def citation_key_for(
    *,
    first_author: str | None,
    year: int | None,
    work_key: str,
) -> str:
    """Return the token a writer must use to cite one work.

    Deterministic from the work, and generated here rather than taken from the
    provider: the writer has to cite by exactly this string, so checking a
    citation becomes a set membership test rather than an attempt to decide
    whether "Roe et al. 2021" refers to the same paper as "Roe & Smith, 2021".
    """

    surname = _CITATION_KEY_RE.sub("", (first_author or "anon").split()[-1]).lower()
    stamp = str(year) if year else "nd"
    suffix = _CITATION_KEY_RE.sub("", work_key)[-6:].lower()
    return f"{surname or 'anon'}{stamp}{suffix}"


def _literature_entries(
    work_keys: list[str], store: LiteratureStore | None
) -> list[LiteratureEntry]:
    """Return the works a writer may cite, or refuse a key that is not indexed."""

    if not work_keys:
        return []
    opened = store if store is not None else LiteratureStore.open()
    entries: list[LiteratureEntry] = []
    for key in work_keys:
        work = opened.work(key)
        if work is None:
            raise PaperPacketError(
                f"no work {key} is in the local literature index, so it cannot be "
                "cited. Retrieve it first with 'researchctl lit fetch'."
            )
        entries.append(
            LiteratureEntry(
                work_key=work.key,
                citation_key=citation_key_for(
                    first_author=work.first_author,
                    year=work.publication_year,
                    work_key=work.key,
                ),
                title=work.title,
                authors=[item.name for item in work.authors],
                year=work.publication_year,
                venue=work.venue,
                doi=work.doi,
                is_retracted=work.is_retracted,
                retraction_note=work.retraction_note,
            )
        )
    return entries


def render_source_packet(packet: SourcePacket) -> str:
    """Render the packet as the text a writer receives.

    The project's own accepted science is not fenced, because it is not
    untrusted: every claim, evidence and experiment entry was assembled by the
    controller from files a human wrote and approved. What that part is instead
    is *exhaustive* -- the writer is told, in the same breath, that this is
    everything it may use.

    Literature is different and is fenced. An independent reviewer caught the
    original justification here, which said the citation check made it trusted:
    that check only proves the key is in the local index, and the title,
    authors, venue and retraction note are whatever a publisher's API returned.
    Every field does go through ``prompt_safe``, so none of it can open a line
    of its own -- but "cannot escape" is not the same claim as "is trusted", and
    the writer is the one worker here with a worktree.
    """

    lines = [
        "SOURCES YOU MAY USE",
        "",
        "This is everything. A statement you cannot ground in one of these",
        "entries does not belong in the draft.",
        "",
        f"project: {prompt_safe(packet.project_id)}",
        f"generated: {packet.generated_at}",
        "",
        "## Accepted claims",
    ]
    if not packet.claims:
        lines.append(
            "(none - this project has accepted nothing that qualifies, so there "
            "is nothing to state)"
        )
    for claim in packet.claims:
        lines.extend(
            [
                "",
                f"- {claim.claim_id}  (approved by {claim.approval_review_id})",
                f"  title: {prompt_safe(claim.title, limit=MAX_LABEL_CHARS)}",
                "  statement: "
                + prompt_safe(claim.statement, limit=MAX_STATEMENT_CHARS),
                f"  digest: {claim.digest}",
            ]
        )
        if claim.supporting_evidence:
            lines.append(f"  supported by: {', '.join(claim.supporting_evidence)}")
        if claim.contrary_evidence:
            lines.append(
                f"  CONTRARY EVIDENCE: {', '.join(claim.contrary_evidence)}"
                "   - this must not be omitted from the draft"
            )
        if claim.contrary_evidence_addressed:
            lines.append(
                "  how the contrary evidence was addressed: "
                + prompt_safe(
                    claim.contrary_evidence_addressed, limit=MAX_STATEMENT_CHARS
                )
            )
        if claim.approved_findings:
            lines.append(
                "  reviewer findings at approval: "
                + prompt_safe(claim.approved_findings, limit=MAX_STATEMENT_CHARS)
            )

    lines.extend(["", "## Evidence"])
    if not packet.evidence:
        lines.append("(none)")
    for item in packet.evidence:
        marker = "  CONTRARY" if item.contrary else ""
        lines.extend(
            [
                "",
                f"- {item.evidence_id}  [{item.kind}/{item.status}]{marker}",
                f"  title: {prompt_safe(item.title, limit=MAX_LABEL_CHARS)}",
                "  statement: "
                + prompt_safe(item.statement, limit=MAX_STATEMENT_CHARS),
                f"  digest: {item.digest}",
            ]
        )
        if item.experiment_id:
            lines.append(f"  from experiment: {item.experiment_id}")
        if item.citation:
            lines.append(
                f"  citation: {prompt_safe(item.citation, limit=MAX_LABEL_CHARS)}"
            )
        if item.notes:
            lines.append(
                "  notes: " + prompt_safe(item.notes, limit=MAX_STATEMENT_CHARS)
            )

    lines.extend(["", "## Experiments"])
    if not packet.experiments:
        lines.append("(none)")
    for item in packet.experiments:
        lines.extend(
            [
                "",
                f"- {item.experiment_id}  [{item.status}]",
                f"  title: {prompt_safe(item.title, limit=MAX_LABEL_CHARS)}",
                "  purpose: " + prompt_safe(item.purpose, limit=MAX_STATEMENT_CHARS),
                f"  digest: {item.digest}",
            ]
        )
        if item.primary_metrics:
            lines.append(f"  primary metrics: {', '.join(item.primary_metrics)}")
        if item.decision_rule:
            lines.append(
                "  decision rule: "
                + prompt_safe(item.decision_rule, limit=MAX_STATEMENT_CHARS)
            )
        for prediction in item.predictions:
            lines.append(
                "  prediction: " + prompt_safe(prediction, limit=MAX_STATEMENT_CHARS)
            )
        if item.provenance:
            lines.append(
                "  provenance: "
                + ", ".join(
                    f"{key}={prompt_safe(str(value), limit=MAX_LABEL_CHARS)}"
                    for key, value in sorted(item.provenance.items())
                )
            )

    lines.extend(["", "## Literature you may cite"])
    if not packet.literature:
        lines.append("(none - do not cite anything)")
    literature_lines: list[str] = []
    for item in packet.literature:
        authors = ", ".join(item.authors[:4]) or "no authors recorded"
        if len(item.authors) > 4:
            authors += f" +{len(item.authors) - 4}"
        literature_lines.extend(
            [
                "",
                f"- cite as: [{item.citation_key}]",
                f"  {prompt_safe(item.title, limit=MAX_LABEL_CHARS)}",
                (
                    f"  {prompt_safe(authors, limit=MAX_LABEL_CHARS)} "
                    f"({item.year or 'n.d.'})"
                ),
                f"  {prompt_safe(item.venue or 'no venue', limit=MAX_LABEL_CHARS)}"
                + (f"   doi:{item.doi}" if item.doi else ""),
            ]
        )
        if item.is_retracted:
            literature_lines.append(
                "  RETRACTED: "
                + prompt_safe(
                    item.retraction_note or "this work was withdrawn",
                    limit=MAX_STATEMENT_CHARS,
                )
                + " - do not cite it as established without saying so"
            )
    if literature_lines:
        lines.append(render_data_block(LITERATURE_FENCE, literature_lines))

    lines.extend(["", "## Unresolved limitations"])
    # The one newline-preserving render in this package that used to sit outside
    # a data block. A limitation comes from a human today -- ``--limitation`` on
    # the CLI, never a model -- but ``prompt_safe_block`` keeps line breaks, so a
    # multi-line limitation could stand a second "SOURCES YOU MAY USE" heading
    # in a packet that reaches the writer, the writing reviewer and the repair
    # worker. "Human-only today" is the reasoning four earlier findings in this
    # class were justified by, so it is fenced like everything else.
    if not packet.limitations:
        lines.append("(none were supplied)")
    else:
        limitation_lines: list[str] = []
        for item in packet.limitations:
            body = prompt_safe_block(item, limit=MAX_STATEMENT_CHARS).split("\n")
            limitation_lines.append(f"- {body[0]}")
            limitation_lines.extend(f"  {line}" for line in body[1:])
        lines.append(render_data_block(STATEMENT_FENCE, limitation_lines))

    if packet.excluded_claims:
        lines.extend(["", "## Claims that were asked for and withheld"])
        lines.extend(
            f"- {claim_id}: {prompt_safe(reason, limit=MAX_STATEMENT_CHARS)}"
            for claim_id, reason in sorted(packet.excluded_claims.items())
        )
        lines.append("  Do not write around these. They are not available to state.")
    return "\n".join(lines) + "\n"
