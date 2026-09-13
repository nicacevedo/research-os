"""Human-facing rendering of the literature subsystem.

Every string here that came from a provider is somebody else's text, so every
renderer returns through :func:`~research_os.textsafe.terminal_safe`. The stored
record keeps the bytes; the view a person reads cannot move their cursor.

What the renderers insist on showing, because leaving it out would be the
dishonest part: which providers were actually reached and which were skipped and
why, which provider supplied the fields of a merged work, whether a work is
retracted, and whether a stored file's text could be extracted at all.
"""

from __future__ import annotations

from research_os.literature.evaluation import EvaluationReport
from research_os.literature.models import (
    ExtractionStatus,
    SearchResult,
    SourceProbe,
    WorkRecord,
)
from research_os.literature.service import RetrievalReport
from research_os.literature.store import LiteratureStore
from research_os.textsafe import terminal_safe


def render_sources(probes: dict[str, SourceProbe]) -> str:
    """Render provider readiness without ever printing a credential."""

    lines = ["", "Literature sources", ""]
    for name in sorted(probes):
        probe = probes[name]
        credential = "not required"
        if probe.credential_env:
            state = "present" if probe.credential_present else "absent"
            credential = f"optional, {state} in {probe.credential_env}"
        lines.extend(
            [
                f"{name}",
                f"  status            {probe.status}",
                f"  endpoint          {probe.base_url}",
                f"  credential        {credential}",
                "  contact address   "
                + ("configured" if probe.contact_configured else "not configured"),
                f"  rate limits       {probe.rate_limit_note or 'unstated'}",
                f"  detail            {probe.detail}",
                "",
            ]
        )
    return terminal_safe("\n".join(lines) + "\n")


def render_retrieval(report: RetrievalReport) -> str:
    """Render one retrieval, naming every provider that did not answer."""

    lines = [
        "",
        f"Retrieval for: {report.query}",
        "",
        f"providers reached  {', '.join(report.reached) or 'none'}",
        f"works ingested     {len(report.work_keys)}",
        f"complete           {'yes' if report.complete else 'no'}",
        "",
    ]
    for outcome in report.outcomes:
        lines.append(
            f"  {outcome.provider:10} {outcome.status:14} "
            f"{outcome.records} record(s), {len(outcome.ingested)} ingested"
        )
        if outcome.rate_limit_note:
            lines.append(f"             {outcome.rate_limit_note}")
        if outcome.detail:
            lines.append(f"             {outcome.detail}")
    if report.skipped:
        lines.extend(
            [
                "",
                "This retrieval is incomplete. The providers above that did not",
                "answer were not searched, which is different from having searched",
                "them and found nothing.",
            ]
        )
    lines.append("")
    return terminal_safe("\n".join(lines) + "\n")


def render_results(results: list[SearchResult], *, query: str) -> str:
    """Render locally ranked search results."""

    lines = ["", f"{len(results)} result(s) for: {query}", ""]
    if not results:
        lines.extend(
            [
                "Nothing in the local index matched. The index only holds what has",
                "been retrieved: try 'researchctl lit retrieve' first.",
                "",
            ]
        )
        return terminal_safe("\n".join(lines) + "\n")
    for position, item in enumerate(results, start=1):
        work = item.work
        authors = ", ".join(entry.name for entry in work.authors[:3])
        if len(work.authors) > 3:
            authors += f" +{len(work.authors) - 3}"
        lines.append(f"{position:2}. {work.title or '(untitled)'}")
        lines.append(
            f"    {authors or 'no authors recorded'}  "
            f"({work.publication_year or 'n.d.'})  {work.venue or 'no venue'}"
        )
        lines.append(
            f"    {work.key}   score {item.score:.4f}   matched {item.matched}"
        )
        if work.is_retracted:
            lines.append(
                f"    RETRACTED: {work.retraction_note or 'reported withdrawn'}"
            )
        if item.snippet:
            lines.append(f"    {item.snippet}")
        lines.append("")
    return terminal_safe("\n".join(lines) + "\n")


def render_work(work: WorkRecord, store: LiteratureStore) -> str:
    """Render one work in full, including where each field came from."""

    lines = [
        "",
        f"{work.title or '(untitled)'}",
        "",
        f"work_key       {work.key}",
        f"year           {work.publication_year or 'unknown'}",
        f"venue          {work.venue or 'unknown'}",
        f"type           {work.work_type or 'unknown'}",
        f"doi            {work.doi or '-'}",
        f"arxiv          {work.arxiv_id or '-'}",
        f"openalex       {work.openalex_id or '-'}",
        f"cited by       {work.cited_by_count if work.cited_by_count is not None else 'unknown'}",
        f"open access    {work.open_access_url or '-'}",
        f"first seen     {work.first_seen}",
        f"last updated   {work.last_updated}",
    ]
    if work.is_retracted:
        lines.extend(
            [
                "",
                "RETRACTED OR WITHDRAWN",
                f"  {work.retraction_note or 'a provider reports this work was withdrawn'}",
                "  This work is still a record of what was once believed. Citing it",
                "  as established requires saying that it was withdrawn.",
            ]
        )
    lines.extend(["", "authors"])
    if not work.authors:
        lines.append("  (none recorded)")
    lines.extend(
        f"  {item.position + 1}. {item.name}"
        + (f"  [{item.orcid}]" if item.orcid else "")
        for item in work.authors
    )

    lines.extend(["", "field provenance"])
    if not work.provenance:
        lines.append("  (none recorded)")
    for item in work.provenance:
        state = "superseded" if item.superseded else "in force  "
        lines.append(
            f"  {state}  {item.field:18} {item.provider:10} {item.retrieved_at}"
        )

    records = store.source_records(work.key)
    lines.extend(["", f"archived provider payloads ({len(records)})"])
    for record in records:
        lines.append(
            f"  {record.provider:10} {record.retrieved_at}  "
            f"sha256 {record.payload_sha256[:16]}"
        )
        if record.request_url:
            lines.append(f"             {record.request_url}")

    files = store.files(work.key)
    lines.extend(["", f"stored files ({len(files)})"])
    if not files:
        lines.append("  (none)")
    for item in files:
        lines.append(
            f"  {item.media_type:18} {item.byte_size:>10} bytes  "
            f"{item.extraction_status}"
        )
        lines.append(f"             sha256 {item.file_sha256}")
        if item.extraction_status is not ExtractionStatus.EXTRACTED and (
            item.extraction_detail
        ):
            lines.append(f"             {item.extraction_detail}")

    if work.abstract:
        lines.extend(["", "abstract", f"  {work.abstract}"])
    lines.append("")
    return terminal_safe("\n".join(lines) + "\n")


def render_evaluation(report: EvaluationReport) -> str:
    from research_os.literature.evaluation import render_report

    return terminal_safe(render_report(report))
