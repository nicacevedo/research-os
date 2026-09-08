"""Command-line interface for Research OS."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Sequence

from research_os import __version__
from research_os.capsule import (
    init_project,
    load_project_identity,
    project_status,
    validate_project,
)
from research_os.errors import (
    EXIT_ERROR,
    EXIT_OK,
    CapsuleCreatedRegistryFailedError,
    Finding,
    ResearchOSError,
)
from research_os.paths import xdg_dir_issue, xdg_dirs
from research_os.registry import list_projects, register_project


def _doctor() -> int:
    checks: list[tuple[str, bool, str]] = []

    py_ok = sys.version_info[:2] == (3, 12)
    checks.append(
        (
            "python",
            py_ok,
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        )
    )

    for command in ("git", "sqlite3"):
        path = shutil.which(command)
        checks.append((command, path is not None, path or "not found"))

    for name, path in xdg_dirs().items():
        issue = xdg_dir_issue(path)
        if issue is None:
            checks.append((name, True, str(path)))
        else:
            checks.append((name, False, f"{path} ({issue})"))

    failed = False

    for name, ok, detail in checks:
        status = "PASS" if ok else "FAIL"
        print(f"{status:4}  {name:10}  {detail}")
        failed = failed or not ok

    return EXIT_ERROR if failed else EXIT_OK


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
    return EXIT_OK


def _register_project(args: argparse.Namespace) -> int:
    git_root, project = load_project_identity(args.path)
    entry = register_project(project, git_root)
    print(f"Registered project {entry.project_id} at {entry.path}")
    return EXIT_OK


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
    print(
        "state.sqlite: "
        f"{'present' if report.state_sqlite_present else 'absent'}"
    )
    return EXIT_OK


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
    subparsers.add_parser("doctor", help="Check the local Research OS environment.")

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
            code = _doctor()
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
        else:
            parser.print_help()
            return
    except ResearchOSError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(EXIT_ERROR) from None

    raise SystemExit(code)


if __name__ == "__main__":
    main()
