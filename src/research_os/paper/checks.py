"""Deterministic checks on a draft, before any model is asked what it thinks.

A writing reviewer is useful for judgement -- is this overstated, does the
discussion follow from the results. It is the wrong tool for bookkeeping, and
bookkeeping is where the dangerous failures live: a citation key that resolves
to nothing, a Claim id that names an object this project does not hold, a number
that appears in the manuscript and in none of the sources.

Those are set differences, and this module computes them. Every one of them is
exact, reproducible, and free. What reaches the reviewer afterwards is a draft
whose bookkeeping has already been established, so its attention goes to the
part only judgement can settle.

Four checks, and the reasons they are the four:

**manifest_is_grounded** -- the writer declares what it used, and every
declaration must have been supplied. A manifest naming a Claim the packet did
not contain means the writer reached outside its sources.

**citations_resolve** -- every citation token in the prose must be a key the
packet supplied. This is the fabricated-citation check, and it is a set
difference precisely because the packet generates the keys.

**identifiers_resolve** -- every ``CLAIM-``/``EVI-``/``EXP-`` shaped token in the
prose must be in the packet. A draft that references an object id it was not
given is asserting something about this project that this project may not hold.

**numbers_are_supported** -- every number in the prose must appear somewhere in
the sources. This is the noisiest check and it is deliberately a warning rather
than a blocker, because section numbering and years produce false positives; but
a results section quoting a figure that exists in none of its evidence is worth
a human's eyes every time.
"""

from __future__ import annotations

import re
from pathlib import Path

from research_os.paper.models import (
    NUMBER_RE,
    GroundingIssue,
    GroundingReport,
    SectionKind,
    SourceManifest,
    SourcePacket,
)

#: How a citation appears in prose.
#:
#: Both common forms, because a manuscript may be Markdown or LaTeX and the
#: writer should not have to be told which this project uses. Pandoc-style
#: ``[@key]`` and ``\cite{key}`` with its usual variants.
CITATION_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\[@([A-Za-z0-9_:+-]+)(?:[;,][^\]]*)?\]"),
    re.compile(r"\\cite[tp]?\*?(?:\[[^\]]*\])*\{([^}]+)\}"),
    re.compile(r"\\(?:autocite|parencite|textcite)\{([^}]+)\}"),
)

#: A scientific-object id as it appears in prose.
OBJECT_ID_RE = re.compile(r"\b(Q|IDEA|HYP|ASM|CLAIM|DEC|EXP|REV|EVI)-[0-9]{4,}\b")

#: Numbers that are almost never a result, and would otherwise be reported
#: constantly.
#:
#: Small integers appear as section numbers, list indices, and counts of things.
#: Four-digit numbers in a plausible year range appear as years. Excluding them
#: is what keeps the numeric check readable enough to be read.
_TRIVIAL_INTEGERS = frozenset(str(item) for item in range(21))


def check_draft(
    *,
    packet: SourcePacket,
    manifest: SourceManifest,
    written: dict[str, str],
    section: SectionKind,
) -> GroundingReport:
    """Run every deterministic check over one draft and report what it found.

    ``written`` maps each changed path to its full text. The checks read the
    text rather than the diff, because a draft that *moved* an unsupported
    sentence rather than adding it is no better grounded for having been moved.
    """

    issues: list[GroundingIssue] = []
    issues.extend(_check_manifest(packet, manifest))

    prose = "\n".join(written.values())
    cited = _cited_keys(prose)
    issues.extend(_check_citations(packet, manifest, cited))

    referenced = sorted({match.group(0) for match in OBJECT_ID_RE.finditer(prose)})
    issues.extend(_check_identifiers(packet, referenced))

    unsupported = _unsupported_numbers(packet, prose)
    issues.extend(_check_numbers(section, unsupported))

    issues.extend(_check_contrary_evidence(packet, manifest, prose))

    return GroundingReport(
        draft_id=manifest.draft_id,
        issues=issues,
        checked_paths=sorted(written),
        cited_keys=sorted(cited),
        referenced_object_ids=referenced,
        unsupported_numbers=unsupported,
    )


def _check_manifest(
    packet: SourcePacket, manifest: SourceManifest
) -> list[GroundingIssue]:
    """Refuse a manifest that declares something the packet did not supply."""

    issues: list[GroundingIssue] = []
    for label, declared, supplied in (
        ("claim", manifest.claim_ids, packet.claim_ids),
        ("evidence", manifest.evidence_ids, packet.evidence_ids),
        ("experiment", manifest.experiment_ids, packet.experiment_ids),
        ("citation key", manifest.citation_keys, packet.citation_keys),
    ):
        invented = sorted(set(declared) - supplied)
        if invented:
            issues.append(
                GroundingIssue(
                    check="manifest_is_grounded",
                    severity="blocker",
                    message=(
                        f"the manifest declares {label}(s) that were not supplied "
                        "to this writing task"
                    ),
                    detail=", ".join(invented),
                )
            )
    if not manifest.written_paths:
        issues.append(
            GroundingIssue(
                check="manifest_is_grounded",
                severity="major",
                message="the manifest names no written path",
            )
        )
    return issues


def _cited_keys(prose: str) -> set[str]:
    """Return every citation key the prose uses, in any supported form."""

    found: set[str] = set()
    for pattern in CITATION_RES:
        for match in pattern.finditer(prose):
            for key in match.group(1).split(","):
                cleaned = key.strip()
                if cleaned:
                    found.add(cleaned)
    return found


def _check_citations(
    packet: SourcePacket, manifest: SourceManifest, cited: set[str]
) -> list[GroundingIssue]:
    """The fabricated-citation check. A set difference, by construction."""

    issues: list[GroundingIssue] = []
    fabricated = sorted(cited - packet.citation_keys)
    if fabricated:
        issues.append(
            GroundingIssue(
                check="citations_resolve",
                severity="blocker",
                message=(
                    "the draft cites keys that name no work supplied to this "
                    "task. A citation that resolves to nothing is a fabricated "
                    "citation"
                ),
                detail=", ".join(fabricated),
            )
        )
    undeclared = sorted(cited & packet.citation_keys - set(manifest.citation_keys))
    if undeclared:
        issues.append(
            GroundingIssue(
                check="citations_resolve",
                severity="minor",
                message=(
                    "the draft cites works the manifest does not list, so the "
                    "provenance record is incomplete"
                ),
                detail=", ".join(undeclared),
            )
        )
    retracted = sorted(
        item.citation_key
        for item in packet.literature
        if item.is_retracted and item.citation_key in cited
    )
    if retracted:
        issues.append(
            GroundingIssue(
                check="citations_resolve",
                severity="major",
                message=(
                    "the draft cites retracted work. That is legitimate, but it "
                    "must say the work was retracted wherever it is cited"
                ),
                detail=", ".join(retracted),
            )
        )
    return issues


def _check_identifiers(
    packet: SourcePacket, referenced: list[str]
) -> list[GroundingIssue]:
    """Refuse a draft that names a capsule object it was not given."""

    supplied = packet.claim_ids | packet.evidence_ids | packet.experiment_ids
    unknown = sorted(set(referenced) - supplied)
    if not unknown:
        return []
    return [
        GroundingIssue(
            check="identifiers_resolve",
            severity="blocker",
            message=(
                "the draft names scientific objects that were not supplied to "
                "this task. It is asserting something about this project that "
                "this task has no basis for"
            ),
            detail=", ".join(unknown),
        )
    ]


def _unsupported_numbers(packet: SourcePacket, prose: str) -> list[str]:
    """Return numbers in the prose that appear nowhere in the sources."""

    vocabulary = packet.numeric_vocabulary()
    found: set[str] = set()
    for token in NUMBER_RE.findall(prose):
        if token in vocabulary or token in _TRIVIAL_INTEGERS:
            continue
        if token.isdigit() and 1500 <= int(token) <= 2200:
            continue  # a year, and years appear constantly in prose about papers
        found.add(token)
    return sorted(found)


def _check_numbers(
    section: SectionKind, unsupported: list[str]
) -> list[GroundingIssue]:
    """Report numbers with no source. Louder in a results section.

    A warning rather than a blocker because the false positives are real:
    section numbers, equation constants, and figure counts all look like
    results. But a results section quoting a figure that appears in none of its
    evidence is worth a human's eyes every single time, so there it is raised to
    major.
    """

    if not unsupported:
        return []
    severity = "major" if section is SectionKind.RESULTS else "minor"
    return [
        GroundingIssue(
            check="numbers_are_supported",
            severity=severity,
            message=(
                "the draft states numbers that appear in none of its sources. "
                "Check each one: a figure this task was not given is either the "
                "writer's arithmetic or an invention"
            ),
            detail=", ".join(unsupported),
        )
    ]


def _check_contrary_evidence(
    packet: SourcePacket, manifest: SourceManifest, prose: str
) -> list[GroundingIssue]:
    """Refuse a draft that states a Claim while omitting what contradicts it.

    The single most damaging thing a writer could do, and the one a reader is
    least likely to notice, because what is missing leaves no trace. It is
    checkable exactly when the packet marks contrary Evidence, which it does.
    """

    issues: list[GroundingIssue] = []
    for claim in packet.claims:
        if not claim.contrary_evidence:
            continue
        if claim.claim_id not in manifest.claim_ids and claim.claim_id not in prose:
            continue
        omitted = [
            evidence_id
            for evidence_id in claim.contrary_evidence
            if evidence_id not in manifest.evidence_ids and evidence_id not in prose
        ]
        if omitted:
            issues.append(
                GroundingIssue(
                    check="contrary_evidence_is_kept",
                    severity="blocker",
                    message=(
                        f"the draft uses {claim.claim_id} but accounts for none "
                        "of the evidence that contradicts it"
                    ),
                    detail=", ".join(omitted),
                )
            )
    return issues


def read_written(worktree: Path, paths: list[str]) -> dict[str, str]:
    """Return the text of every path the draft changed, for checking.

    Reads the file rather than the diff, because a draft that moved an
    unsupported sentence instead of adding it is no better grounded for having
    moved it. A path that cannot be read is reported as empty rather than
    raising: the checks then see no support for anything in it, which is the
    safe direction to fail.
    """

    found: dict[str, str] = {}
    for relative in paths:
        target = worktree / relative
        if target.is_symlink() or not target.is_file():
            continue
        try:
            found[relative] = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            found[relative] = ""
    return found
