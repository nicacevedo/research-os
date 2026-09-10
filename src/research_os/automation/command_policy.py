"""Authorisation policy for planner-originated acceptance commands.

The controller runs acceptance commands itself, after a write-enabled worker has
modified the code. Which command shapes a plan may name is therefore policy, not
a suggestion, and an executable name is far too coarse a unit to express it:
``python`` and ``python3`` run arbitrary source with ``-c``, ``git`` reaches the
network with ``push``, and ``uv run`` re-opens both.

So this module authorises whole argument vectors against a small explicit
grammar rather than authorising ``argv[0]``. It is deliberately not a shell
policy engine. It understands three programs, a fixed set of options for each,
and repository-relative path arguments; anything it does not recognise is
refused. A narrow grammar that occasionally rejects a legitimate flag is the
intended trade, because the alternative failure is executing a command the
controller never meant to allow.

Nothing here interprets shell metacharacters, because nothing ever runs through
a shell: ``subprocess.run`` is always given a list. Tokens carrying shell syntax
are still refused, since their only purpose in a plan is to be misread later.

Controller-injected observations such as ``git diff --check HEAD`` do not come
from a planner and deliberately do not use this path.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from research_os.automation.models import AcceptanceCommand
from research_os.errors import CommandPolicyError

RUNNER = "uv"
PYTEST = "pytest"
RUFF = "ruff"

#: Every program a planner may name, as a bare program or after ``uv run``.
SUPPORTED_PROGRAMS: tuple[str, ...] = (RUNNER, PYTEST, RUFF)

#: The command forms this policy accepts, for prompts, reports, and docs.
SUPPORTED_COMMAND_FORMS: tuple[str, ...] = (
    "pytest [options] [test paths]",
    "ruff check [options] [paths]",
    "ruff format --check [options] [paths]",
    "uv run pytest [options] [test paths]",
    "uv run ruff check [options] [paths]",
    "uv run ruff format --check [options] [paths]",
)

_PYTEST_FLAGS: frozenset[str] = frozenset(
    {
        "-q",
        "-qq",
        "--quiet",
        "-v",
        "-vv",
        "--verbose",
        "-x",
        "--exitfirst",
        "--no-header",
        "--no-summary",
        "--strict-config",
        "--strict-markers",
    }
)
_TRACEBACK_STYLES: frozenset[str] = frozenset(
    {"auto", "long", "short", "line", "native", "no"}
)

_RUFF_FLAGS: frozenset[str] = frozenset(
    {
        "-q",
        "--quiet",
        "--no-cache",
        "--exit-non-zero-on-fix",
    }
)
_RUFF_CHECK_ONLY_FLAGS: frozenset[str] = frozenset({"--no-fix"})
_RUFF_FORMAT_ONLY_FLAGS: frozenset[str] = frozenset({"--diff"})

# One token of an argument vector. No whitespace, no quoting, no shell syntax:
# '=' for option values, ':' for pytest node ids, brackets for parametrised ids.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.,:=\[\]/-]+$")

# The selector part of a pytest node id, after the file path.
_SELECTOR_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.,\[\]-]*$")


def authorize_planner_commands(
    commands: Iterable[AcceptanceCommand],
    allowed_programs: tuple[str, ...],
) -> None:
    """Raise ``CommandPolicyError`` naming the first unauthorised command."""

    for command in commands:
        authorize_planner_argv(command.argv, allowed_programs=allowed_programs)


def authorize_planner_argv(
    argv: Sequence[str],
    *,
    allowed_programs: tuple[str, ...],
) -> None:
    """Raise ``CommandPolicyError`` unless ``argv`` is a supported command form.

    ``allowed_programs`` may only narrow this grammar, never widen it: a program
    named in configuration that has no grammar here is still refused.
    """

    if not argv:
        raise CommandPolicyError("an acceptance command must not be empty")

    rest = list(argv)
    if rest[0] == RUNNER:
        _require(
            RUNNER in allowed_programs,
            argv,
            f"{RUNNER!r} is not in this project's allowed check programs",
        )
        _require(
            len(rest) >= 3 and rest[1] == "run",
            argv,
            f"the only supported {RUNNER!r} form is '{RUNNER} run <program> ...'",
        )
        rest = rest[2:]

    program = rest[0]
    _require(
        program in SUPPORTED_PROGRAMS and program != RUNNER,
        argv,
        f"{program!r} is not an authorised acceptance-command program; "
        f"supported forms: {'; '.join(SUPPORTED_COMMAND_FORMS)}",
    )
    _require(
        program in allowed_programs,
        argv,
        f"{program!r} is not in this project's allowed check programs",
    )

    for token in argv:
        _assert_plain_token(token, argv)

    if program == PYTEST:
        _authorize_pytest(rest[1:], argv)
    else:
        _authorize_ruff(rest[1:], argv)


def _authorize_pytest(args: list[str], argv: Sequence[str]) -> None:
    for token in args:
        if not token.startswith("-"):
            _assert_safe_target(token, argv)
            continue
        if token in _PYTEST_FLAGS:
            continue
        if token.startswith("--maxfail="):
            _require(
                token[len("--maxfail=") :].isdigit(),
                argv,
                "--maxfail= takes a whole number",
            )
            continue
        if token.startswith("--tb="):
            _require(
                token[len("--tb=") :] in _TRACEBACK_STYLES,
                argv,
                f"--tb= takes one of: {', '.join(sorted(_TRACEBACK_STYLES))}",
            )
            continue
        raise _refused(
            argv,
            f"pytest option {token!r} is not authorised; supported options: "
            f"{', '.join(sorted(_PYTEST_FLAGS))}, --maxfail=N, --tb=STYLE",
        )


def _authorize_ruff(args: list[str], argv: Sequence[str]) -> None:
    _require(
        bool(args) and args[0] in {"check", "format"},
        argv,
        "ruff must be invoked as 'ruff check ...' or 'ruff format --check ...'",
    )
    subcommand = args[0]
    rest = args[1:]
    if subcommand == "format":
        _require(
            bool(rest) and rest[0] == "--check",
            argv,
            "'ruff format' is only authorised as 'ruff format --check', so it "
            "reports formatting instead of rewriting the worker's diff",
        )
        rest = rest[1:]
        permitted = _RUFF_FLAGS | _RUFF_FORMAT_ONLY_FLAGS
    else:
        permitted = _RUFF_FLAGS | _RUFF_CHECK_ONLY_FLAGS

    for token in rest:
        if not token.startswith("-"):
            _assert_safe_target(token, argv)
            continue
        if token in permitted:
            continue
        raise _refused(
            argv,
            f"ruff {subcommand} option {token!r} is not authorised; supported "
            f"options: {', '.join(sorted(permitted))}",
        )


def _assert_plain_token(token: str, argv: Sequence[str]) -> None:
    """Reject anything that is not a plain, shell-free argument token."""

    _require(bool(token), argv, "an acceptance command argument must not be empty")
    _require(
        _TOKEN_RE.fullmatch(token) is not None,
        argv,
        f"argument {token!r} contains characters this policy does not accept; "
        "acceptance commands are plain argument vectors with no shell syntax",
    )


def _assert_safe_target(token: str, argv: Sequence[str]) -> None:
    """Reject a path argument that could point outside the isolated worktree."""

    path, _, selector = token.partition("::")
    if selector:
        for part in selector.split("::"):
            _require(
                _SELECTOR_RE.fullmatch(part) is not None,
                argv,
                f"{token!r} is not a usable test selector",
            )
    _require(
        not path.startswith("/"),
        argv,
        f"{token!r} is an absolute path; acceptance commands run inside the "
        "isolated worktree and may only name paths relative to it",
    )
    _require(bool(path), argv, f"{token!r} names no path")
    segments = [item for item in path.split("/") if item != ""]
    _require(
        ".." not in segments,
        argv,
        f"{token!r} traverses out of the worktree with '..'",
    )


def _require(condition: bool, argv: Sequence[str], detail: str) -> None:
    if not condition:
        raise _refused(argv, detail)


def _refused(argv: Sequence[str], detail: str) -> CommandPolicyError:
    return CommandPolicyError(
        f"acceptance command {' '.join(argv)!r} is not authorised: {detail}"
    )
