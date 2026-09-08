"""Command-line interface for Research OS."""

from __future__ import annotations

import argparse
import shutil
import sys

from research_os import __version__
from research_os.errors import EXIT_ERROR, EXIT_OK
from research_os.paths import xdg_dir_issue, xdg_dirs


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


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="researchctl",
        description="Research OS command-line interface.",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("version", help="Show the Research OS version.")
    subparsers.add_parser("doctor", help="Check the local Research OS environment.")

    args = parser.parse_args()

    if args.command == "version":
        print(__version__)
        return

    if args.command == "doctor":
        raise SystemExit(_doctor())

    parser.print_help()


if __name__ == "__main__":
    main()
