"""Command-line interface for Research OS."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from research_os import __version__, diagnostics
from research_os.automation.commands import add_auto_parser
from research_os.automation.commands import dispatch as auto_dispatch
from research_os.capsule import (
    init_project,
    load_project_identity,
    project_status,
    resolve_object,
    validate_project,
)
from research_os.digests import subject_digest
from research_os.errors import (
    EXIT_ERROR,
    EXIT_OK,
    CapsuleCreatedRegistryFailedError,
    CapsuleError,
    Finding,
    ResearchOSError,
    ReviewBlockedError,
)
from research_os.experiment.commands import add_experiment_parser
from research_os.experiment.commands import dispatch as experiment_dispatch
from research_os.insights.commands import add_insight_parser
from research_os.insights.commands import dispatch as insight_dispatch
from research_os.literature.commands import add_lit_parser
from research_os.literature.commands import dispatch as lit_dispatch
from research_os.models import Reviewable, Verdict
from research_os.paper.commands import add_paper_parser
from research_os.paper.commands import dispatch as paper_dispatch
from research_os.proposal.commands import add_propose_parser
from research_os.proposal.commands import dispatch as propose_dispatch
from research_os.registry import (
    legacy_registry_path,
    legacy_registry_present,
    list_projects,
    register_project,
)
from research_os.research.commands import add_research_parser
from research_os.research.commands import dispatch as research_dispatch
from research_os.review import (
    EvidenceEntry,
    ExperimentEntry,
    ReviewPacket,
    build_review,
    build_review_packet,
    write_review,
)
from research_os.runtime.commands import add_runtime_parser
from research_os.runtime.commands import dispatch as runtime_dispatch


def _doctor(args: argparse.Namespace) -> int:
    """Report what this machine can do, and exit non-zero only if something broke.

    An absent capability -- one provider, no cluster, no declared experiments --
    is a WARN and exits zero. A researcher who wires ``doctor`` into a script
    should be told about real breakage and not about the shape of their setup.
    """

    report = diagnostics.collect(include_storage=not args.no_storage)
    if args.json:
        print(json.dumps(report.payload(), ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(diagnostics.render(report, verbose=args.verbose), end="")
    return EXIT_OK if report.ok else EXIT_ERROR


def _storage(args: argparse.Namespace) -> int:
    if args.reclaim:
        print(diagnostics.render_reclaimed(diagnostics.reclaim()), end="")
        return EXIT_OK
    report = diagnostics.Report(usage=list(diagnostics.storage_usage()))
    if args.json:
        print(json.dumps(report.payload(), ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(diagnostics.render(report, verbose=True), end="")
    return EXIT_OK


def _init_project(args: argparse.Namespace) -> int:
    git_root, project = init_project(
        args.path,
        project_id=args.id,
        title=args.title,
    )
    try:
        register_project(project, git_root)
    except Exception as exc:
        raise CapsuleCreatedRegistryFailedError(
            f"Capsule creation succeeded at {git_root / '.research'} but "
            f"registration failed: {exc}. Retry with researchctl "
            f"register-project {git_root}."
        ) from exc
    print(f"Initialized project {project.id} at {git_root}")
    _note_legacy_registry_if_present()
    return EXIT_OK


def _register_project(args: argparse.Namespace) -> int:
    git_root, project = load_project_identity(args.path)
    entry = register_project(project, git_root)
    print(f"Registered project {entry.project_id} at {entry.path}")
    _note_legacy_registry_if_present()
    return EXIT_OK


def _note_legacy_registry_if_present() -> None:
    """Mention a still-present legacy SQLite registry after a successful write.

    Informational only: it never affects the exit code and never touches the
    legacy file, which is why this runs after registration succeeds rather than
    guarding it.
    """

    if legacy_registry_present():
        print(
            f"note: a legacy SQLite project registry remains at "
            f"{legacy_registry_path()}. It is never read and can be deleted.",
            file=sys.stderr,
        )


def _validate_project(args: argparse.Namespace) -> int:
    report = validate_project(args.path)
    if args.json:
        print(_validation_json(report.findings, ok=report.ok))
    else:
        print(_validation_human(report.findings, ok=report.ok), end="")
    return EXIT_OK if report.ok else EXIT_ERROR


def _projects(_args: argparse.Namespace) -> int:
    for entry in list_projects():
        print(
            f"{entry.availability:9}  {entry.project_id}  {entry.path}  "
            f"{entry.status}  {entry.title}"
        )
    return EXIT_OK


def _status(args: argparse.Namespace) -> int:
    report = project_status(args.path)
    print(f"project_id: {report.project.id}")
    print(f"title: {report.project.title}")
    print(f"status: {report.project.status}")
    for object_type, count in report.object_counts.items():
        print(f"{object_type}: {count}")
    print(f"errors: {report.error_count}")
    print(f"warnings: {report.warning_count}")
    return EXIT_OK


def _digest(args: argparse.Namespace) -> int:
    report = validate_project(args.path)
    obj = resolve_object(report, args.object_id)
    if not isinstance(obj, Reviewable):
        raise CapsuleError(
            f"{obj.id} is a {obj.type}; reviews are not reviewable subjects "
            "and have no semantic digest"
        )
    assert report.project is not None  # resolve_object guarantees this
    print(subject_digest(obj, project_id=report.project.id))
    return EXIT_OK


def _is_interactive() -> bool:
    """Return whether a person is driving this invocation.

    The single seam the review command consults, so the guard can be exercised
    deliberately in tests rather than depending on how a runner happens to wire
    standard input.
    """

    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _review(args: argparse.Namespace) -> int:
    if not _is_interactive():
        raise CapsuleError(
            "researchctl review requires an interactive terminal; a "
            "non-human process must not record a human review."
        )
    report = validate_project(args.path)
    packet = build_review_packet(report, args.object_id)
    print(_render_packet(packet), end="")

    verdict = _prompt_verdict()
    if verdict is None:
        return _cancelled()
    findings = _prompt_findings()
    if findings is None:
        return _cancelled()
    if not _confirm(
        f"Write {packet.review_id} as a concluded human {verdict.value} "
        f"review of {packet.claim.id}?"
    ):
        return _cancelled()

    review = build_review(packet, verdict=verdict, findings=findings)
    target = write_review(packet, review)
    print(f"Wrote {target.relative_to(packet.git_root).as_posix()}")

    after = validate_project(packet.git_root)
    print(_validation_human(after.findings, ok=after.ok), end="")
    return EXIT_OK if after.ok else EXIT_ERROR


def _cancelled() -> int:
    print("cancelled: no review written", file=sys.stderr)
    return EXIT_ERROR


_VERDICTS: tuple[tuple[str, Verdict | None], ...] = (
    ("approve", Verdict.APPROVE),
    ("revise", Verdict.REVISE),
    ("reject", Verdict.REJECT),
    ("cancel", None),
)


def _prompt_verdict() -> Verdict | None:
    """Prompt until the reviewer names one verdict, or cancels.

    Accepts a full word or an unambiguous prefix. A bare ``r`` matches both
    ``revise`` and ``reject``, so it is re-prompted rather than guessed.
    """

    while True:
        raw = _ask("Verdict [approve/revise/reject/cancel]: ")
        if raw is None:
            return None
        answer = raw.strip().lower()
        matches = [item for item in _VERDICTS if item[0].startswith(answer)]
        if answer and len(matches) == 1:
            return matches[0][1]
        print(
            "  please answer approve, revise, reject, or cancel",
            file=sys.stderr,
        )


def _prompt_findings() -> str | None:
    """Collect the reviewer's findings, terminated by an empty line."""

    while True:
        print("Findings (required; end with an empty line):")
        lines: list[str] = []
        while True:
            raw = _ask("> ")
            if raw is None:
                return None
            if not raw.strip():
                break
            lines.append(raw.rstrip())
        findings = "\n".join(lines).strip()
        if findings:
            return findings
        print("  findings are required for a concluded review", file=sys.stderr)


def _confirm(question: str) -> bool:
    raw = _ask(f"{question} [y/N]: ")
    if raw is None:
        return False
    return raw.strip().lower() in {"y", "yes"}


def _ask(prompt: str) -> str | None:
    """Read one line, treating end-of-input or interruption as a cancellation."""

    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _render_packet(packet: ReviewPacket) -> str:
    """Render everything the reviewer is about to bind.

    Digests print in full: the digest is the artifact being bound, and an
    abbreviated hash invites a false sense of having checked it.
    """

    claim = packet.claim
    lines = [
        "",
        f"Review packet \u2014 project {packet.project_id}",
        "",
        f"claim         {claim.id}  (status: {claim.status})",
        f"title         {claim.title}",
        f"statement     {claim.statement}",
        f"digest        {packet.claim_digest}",
    ]
    for reference, title in packet.hypotheses:
        suffix = f"  {title}" if title else ""
        lines.append(f"hypotheses    {reference}{suffix}")

    lines.extend(_render_evidence("supporting evidence", packet.supporting))
    lines.extend(_render_evidence("contrary evidence", packet.contrary))
    lines.extend(_render_experiments(packet.experiments))

    if claim.contrary_evidence_addressed is not None:
        lines.extend(
            [
                "",
                "contrary evidence addressed",
                f"  {claim.contrary_evidence_addressed}",
            ]
        )
    if packet.warnings:
        lines.append("")
        lines.append("warnings")
        for finding in packet.warnings:
            lines.append(f"  {_format_finding(finding)}")

    count = packet.evidence_count
    plural = "digest" if count == 1 else "digests"
    experiments = packet.experiment_count
    experiment_plural = "digest" if experiments == 1 else "digests"
    lines.extend(
        [
            "",
            f"This review will bind the claim digest, all {count} evidence {plural},",
            f"and all {experiments} experiment {experiment_plural} shown above.",
            "Changing any of that content afterwards invalidates the approval.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_evidence(heading: str, entries: Sequence[EvidenceEntry]) -> list[str]:
    if not entries:
        return ["", f"{heading} (none)"]
    lines = ["", f"{heading} ({len(entries)})"]
    for entry in entries:
        lines.append(f"  {entry.id}  {entry.status}  {entry.kind}")
        lines.append(f"    title       {entry.title}")
        lines.append(f"    statement   {entry.statement}")
        for label, value in entry.pointers():
            lines.append(f"    {label:<11} {value}")
        lines.append(f"    digest      {entry.digest}")
    return lines


def _render_experiments(entries: Sequence[ExperimentEntry]) -> list[str]:
    """Render the Experiments whose content this review binds.

    Experiment-derived evidence names an Experiment by id, so without this
    section a reviewer would approve a claim resting on experimental content
    they were never shown.
    """

    if not entries:
        return ["", "experiments (none)"]
    lines = ["", f"experiments ({len(entries)})"]
    for entry in entries:
        lines.append(f"  {entry.id}  {entry.status}")
        lines.append(f"    title           {entry.title}")
        lines.append(f"    purpose         {entry.purpose}")
        for label, value in entry.fields():
            lines.append(f"    {label:<15} {value}")
        if entry.predictions:
            lines.append(f"    predictions ({len(entry.predictions)})")
            for hypothesis, discriminates, outcome in entry.predictions:
                lines.append(
                    f"      {hypothesis}  discriminates: "
                    f"{'yes' if discriminates else 'no'}"
                )
                lines.append(f"        {outcome}")
        if entry.provenance:
            lines.append("    provenance")
            for label, value in entry.provenance:
                lines.append(f"      {label:<13} {value}")
        if entry.artifacts:
            lines.append(f"    artifacts ({len(entry.artifacts)})")
            for artifact in entry.artifacts:
                lines.append(f"      {artifact}")
        lines.append(f"    digest          {entry.digest}")
    return lines


def _validation_human(findings: Sequence[Finding], *, ok: bool) -> str:
    if not findings:
        return "OK\n"
    lines = [_format_finding(item) for item in findings]
    if ok:
        lines.append("OK")
    return "\n".join(lines) + "\n"


def _format_finding(item: Finding) -> str:
    object_id = item.object_id or "-"
    source = item.source or "-"
    return f"{item.severity}  {item.code}  {object_id}  {source}  {item.message}"


def _validation_json(findings: Sequence[Finding], *, ok: bool) -> str:
    payload = {
        "ok": ok,
        "findings": [_finding_dict(item) for item in findings],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)


def _finding_dict(item: Finding) -> dict[str, str | None]:
    return {
        "severity": str(item.severity),
        "code": item.code,
        "message": item.message,
        "object_id": item.object_id,
        "field": item.field,
        "reference": item.reference,
        "source": item.source,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="researchctl",
        description="Research OS command-line interface.",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("version", help="Show the Research OS version.")

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Check what this machine can actually do. Makes no network request.",
    )
    doctor_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show every check and every store, including the empty ones.",
    )
    doctor_parser.add_argument(
        "--json", action="store_true", help="Emit the report as JSON."
    )
    doctor_parser.add_argument(
        "--no-storage",
        action="store_true",
        help="Skip measuring runtime storage. Faster on a large state directory.",
    )

    storage_parser = subparsers.add_parser(
        "storage",
        help="Report what runtime state is using, and release what is reclaimable.",
    )
    storage_parser.add_argument(
        "--reclaim",
        action="store_true",
        help=(
            "Release the worktrees and check environments finished runs still "
            "hold. Records, ledgers and branches are kept."
        ),
    )
    storage_parser.add_argument(
        "--json", action="store_true", help="Emit the measurement as JSON."
    )

    init_parser = subparsers.add_parser(
        "init-project",
        help="Create a Research Capsule in an existing Git repository.",
    )
    init_parser.add_argument("path", nargs="?", default=".")
    init_parser.add_argument(
        "--id",
        dest="id",
        help="Project id. Required when the repository directory name is not a valid slug.",
    )
    init_parser.add_argument(
        "--title",
        dest="title",
        help="Project title. Defaults to the repository directory name.",
    )

    register_parser = subparsers.add_parser(
        "register-project",
        help="Register an existing Research Capsule in the project registry.",
    )
    register_parser.add_argument("path", nargs="?", default=".")

    validate_parser = subparsers.add_parser(
        "validate-project",
        help="Validate a Research Capsule without modifying files.",
    )
    validate_parser.add_argument("path", nargs="?", default=".")
    validate_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a deterministic JSON validation report.",
    )

    subparsers.add_parser(
        "projects",
        help="List registered projects without modifying the registry.",
    )

    status_parser = subparsers.add_parser(
        "status",
        help="Show file-derived capsule status.",
    )
    status_parser.add_argument("path", nargs="?", default=".")

    digest_parser = subparsers.add_parser(
        "digest",
        help="Print the current project-scoped semantic digest of an object.",
    )
    digest_parser.add_argument("object_id", metavar="OBJECT-ID")
    digest_parser.add_argument("path", nargs="?", default=".")

    review_parser = subparsers.add_parser(
        "review",
        help="Record a human Review of a claim through an interactive prompt.",
    )
    review_parser.add_argument("object_id", metavar="CLAIM-ID")
    review_parser.add_argument("path", nargs="?", default=".")

    add_research_parser(subparsers)
    add_runtime_parser(subparsers)
    add_auto_parser(subparsers)
    add_lit_parser(subparsers)
    add_propose_parser(subparsers)
    add_experiment_parser(subparsers)
    add_insight_parser(subparsers)
    add_paper_parser(subparsers)
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "version":
        print(__version__)
        return

    if args.command is None:
        parser.print_help()
        return

    try:
        if args.command == "doctor":
            code = _doctor(args)
        elif args.command == "storage":
            code = _storage(args)
        elif args.command == "init-project":
            code = _init_project(args)
        elif args.command == "register-project":
            code = _register_project(args)
        elif args.command == "validate-project":
            code = _validate_project(args)
        elif args.command == "projects":
            code = _projects(args)
        elif args.command == "status":
            code = _status(args)
        elif args.command == "digest":
            code = _digest(args)
        elif args.command == "review":
            code = _review(args)
        elif args.command == "research":
            code = research_dispatch(args)
        elif args.command == "runtime":
            code = runtime_dispatch(args)
        elif args.command == "auto":
            code = auto_dispatch(args)
        elif args.command == "lit":
            code = lit_dispatch(args)
        elif args.command == "propose":
            code = propose_dispatch(args)
        elif args.command == "experiment":
            code = experiment_dispatch(args)
        elif args.command == "insight":
            code = insight_dispatch(args)
        elif args.command == "paper":
            code = paper_dispatch(args)
        else:
            parser.print_help()
            return
    except ReviewBlockedError as exc:
        if exc.findings:
            print(_validation_human(exc.findings, ok=False), end="")
        print(str(exc), file=sys.stderr)
        raise SystemExit(EXIT_ERROR) from None
    except ResearchOSError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(EXIT_ERROR) from None

    raise SystemExit(code)


if __name__ == "__main__":
    main()
