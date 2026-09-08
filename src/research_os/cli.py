"""Command-line interface for Research OS."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from research_os import __version__


def _doctor() -> int:
    checks: list[tuple[str, bool, str]] = []

    py_ok = sys.version_info[:2] == (3, 12)
    checks.append(
        ("python", py_ok, f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    )

    for command in ("git", "sqlite3"):
        path = shutil.which(command)
        checks.append((command, path is not None, path or "not found"))

    home = Path.home()
    paths = {
        "config": home / ".config" / "research-os",
        "data": home / ".local" / "share" / "research-os",
        "cache": home / ".cache" / "research-os",
        "state": home / ".local" / "state" / "research-os",
    }

    for name, path in paths.items():
        checks.append((name, path.is_dir(), str(path)))

    failed = False

    for name, ok, detail in checks:
        status = "PASS" if ok else "FAIL"
        print(f"{status:4}  {name:10}  {detail}")
        failed = failed or not ok

    return 1 if failed else 0


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
