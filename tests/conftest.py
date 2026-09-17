"""Isolation helpers for Research OS tests.

The important thing this file does is redirect *every* Research OS writable
root into a pytest-owned temporary directory, for every test, by default.

It used to do the opposite. ``isolate_xdg_env`` only *unset* the four
``RESEARCH_OS_*_HOME`` overrides, so that one test could not leak an override
into the next. But :func:`research_os.paths._override_or_default` falls back to
``Path.home()`` when an override is absent, which meant "isolated" was
implemented as "resolve to the researcher's real directories". Any test that
did not opt into ``automation_home``, ``runtime_xdg`` or ``data_home`` wrote
real records into ``~/.local/state/research-os``, and 774 of them accumulated
there before anyone counted: 355 assessments and 419 research runs, each one a
pytest temporary path recorded as a project.

So the default is now inverted. Redirection is the default and requires no
opt-in; the per-family fixtures below remain because tests want a *handle* on
the directory, not because the redirect depends on them.

Two guards keep it that way, and both check behaviour rather than environment
variables. :func:`_assert_state_is_isolated` calls the real resolver after
every test and fails if any Research OS root resolved outside pytest's
``basetemp``. The session hooks at the bottom of this file inventory the
researcher's real directories before and after the run, and fail the run if
anything appeared in them. ``tests/test_state_isolation.py`` proves the guards
themselves are not vacuous.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

#: The four environment variables that relocate Research OS' writable roots.
STATE_ENV_VARS = (
    "RESEARCH_OS_CONFIG_HOME",
    "RESEARCH_OS_DATA_HOME",
    "RESEARCH_OS_CACHE_HOME",
    "RESEARCH_OS_STATE_HOME",
)

#: Kept under the old name because two test docstrings still cite it.
XDG_ENV_VARS = STATE_ENV_VARS

#: Every function that derives a writable path from one of the four roots.
#: Named as ``module:function`` and resolved lazily, because importing all of
#: Research OS to check one test's paths would dominate the suite's runtime.
_PATH_ENTRY_POINTS = (
    "research_os.paths:config_home",
    "research_os.paths:data_home",
    "research_os.paths:cache_home",
    "research_os.paths:state_home",
    "research_os.assessment.store:assessments_root",
    "research_os.research.store:research_root",
    "research_os.proposal.store:proposals_root",
    "research_os.experiment.store:experiments_root",
    "research_os.paper.store:drafts_root",
    "research_os.automation.store:runs_root",
    "research_os.automation.store:worktrees_root",
    "research_os.automation.store:locks_root",
    "research_os.insights.store:insights_root",
    "research_os.insights.store:nominations_root",
    "research_os.literature.store:database_root",
    "research_os.registry:registry_path",
    "research_os.registry:legacy_registry_path",
    "research_os.runtime.notify:inbox_path",
    "research_os.runtime.executors:runs_root",
    "research_os.automation.config:config_path",
    "research_os.experiment.config:config_path",
    "research_os.literature.config:config_path",
    "research_os.runtime.config:config_path",
    "research_os.runtime.config:artifacts_root",
    "research_os.runtime.config:dev_db_root",
)


def resolve_path_entry_points() -> dict[str, Path]:
    """Return every Research OS writable path, as the product resolves it now.

    Shared with ``tests/test_state_isolation.py`` so that the guard and the
    test that proves the guard works cannot drift apart.
    """

    import importlib

    resolved: dict[str, Path] = {}
    for entry in _PATH_ENTRY_POINTS:
        module_name, _, attr = entry.partition(":")
        function = getattr(importlib.import_module(module_name), attr)
        resolved[entry] = Path(function())
    return resolved


def _is_within(path: Path, root: Path) -> bool:
    """Whether ``path`` is ``root`` or below it, symlinks resolved."""

    try:
        resolved = path.resolve()
    except OSError:  # pragma: no cover - resolve() does not raise on Linux
        resolved = path
    return resolved == root or root in resolved.parents


def _assert_state_is_isolated(basetemp: Path) -> None:
    """Fail unless every Research OS writable root is under ``basetemp``.

    Behavioural: it calls the product's own path functions rather than
    inspecting the environment, so a resolver that ignores the overrides, or a
    new path family that forgets them, is caught the same way.
    """

    escaped = {
        entry: path
        for entry, path in resolve_path_entry_points().items()
        if not _is_within(path, basetemp)
    }
    if escaped:
        listing = "\n".join(
            f"  {entry} -> {path}" for entry, path in sorted(escaped.items())
        )
        raise AssertionError(
            "Research OS writable paths resolved outside the pytest temporary "
            f"root {basetemp}:\n{listing}\n"
            "A test must not be able to reach the researcher's real state. Use "
            "the default isolation, or an explicitly temporary HOME if the test "
            "is about default resolution."
        )


@pytest.fixture(autouse=True)
def isolate_research_os_state(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Path]:
    """Redirect all four Research OS roots into a pytest-owned directory.

    Autouse and unconditional. A test that wants a *handle* on one of the
    directories should use ``automation_home``, ``runtime_xdg`` or
    ``data_home``, all of which redirect again inside ``tmp_path``; a test that
    wants no Research OS state at all still gets this, because "no state" is
    not something a test can promise about the code it calls.

    Patches through its own :class:`pytest.MonkeyPatch` rather than the
    ``monkeypatch`` fixture, because that fixture is one shared object: a test
    calling ``monkeypatch.undo()`` would otherwise revert this redirect along
    with its own patches and silently de-isolate itself.
    ``tests/test_crash_recovery.py`` already had to work around that hazard.
    """

    root = tmp_path_factory.mktemp("research-os-home")
    patcher = pytest.MonkeyPatch()
    for name, leaf in zip(
        STATE_ENV_VARS, ("config", "data", "cache", "state"), strict=True
    ):
        directory = root / leaf
        directory.mkdir(parents=True, exist_ok=True)
        patcher.setenv(name, str(directory))
    # Stashed for the hook below, which has no access to the factory.
    _BASETEMP.append(tmp_path_factory.getbasetemp().resolve())
    try:
        yield root
    finally:
        patcher.undo()


#: One entry, appended by the fixture above. A list rather than a global so the
#: hook can tell "no test has run yet" from "basetemp is None".
_BASETEMP: list[Path] = []


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Iterator[None]:
    """Check the invariant right after the test body, before any teardown.

    A fixture finalizer would be too late: pytest's ``monkeypatch`` tears down
    before an autouse fixture that does not depend on it, so the environment a
    finalizer sees is the baseline rather than whatever the test left behind.
    This hook runs while the test's own patches are still in effect, which is
    the state that matters -- a test that points a root at a real directory is
    caught here and nowhere else.
    """

    result = yield
    if _BASETEMP:
        _assert_state_is_isolated(_BASETEMP[-1])
    return result


@pytest.fixture
def data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Temporary RESEARCH_OS_DATA_HOME for registry-writing tests."""
    path = tmp_path / "xdg-data"
    path.mkdir()
    monkeypatch.setenv("RESEARCH_OS_DATA_HOME", str(path))
    return path


@pytest.fixture
def automation_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every Research OS directory into ``tmp_path``.

    Automation runs write runtime state, create worktrees, and read the project
    registry. The redirect is no longer what keeps those off the real machine
    -- ``isolate_research_os_state`` does that -- but a test that inspects the
    directory needs to know which one it is.
    """

    root = tmp_path / "xdg"
    mapping = {
        "RESEARCH_OS_CONFIG_HOME": root / "config",
        "RESEARCH_OS_DATA_HOME": root / "data",
        "RESEARCH_OS_CACHE_HOME": root / "cache",
        "RESEARCH_OS_STATE_HOME": root / "state",
    }
    for name, path in mapping.items():
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(path))
    return mapping["RESEARCH_OS_STATE_HOME"]


# --- the researcher's real directories --------------------------------------
# Defence in depth behind the per-test guard: that one proves no test *resolved*
# a path outside the temporary root, this one proves nothing *appeared* in the
# real directories, including by a route that does not go through
# ``research_os.paths`` at all.


def _real_state_roots() -> tuple[Path, ...]:
    home = Path(os.path.expanduser("~"))
    return (
        home / ".config" / "research-os",
        home / ".local" / "share" / "research-os",
        home / ".cache" / "research-os",
        home / ".local" / "state" / "research-os",
    )


def _real_state_inventory() -> set[str]:
    """Every entry in the researcher's real Research OS directories.

    Recursive, except that automation worktrees are inventoried only down to
    the run that owns them: a worktree is a checkout of a real repository and
    holds tens of thousands of files that say nothing about contamination,
    while a *new* worktree still shows up as a new run-level entry.
    """

    found: set[str] = set()
    for root in _real_state_roots():
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            relative = Path(dirpath).relative_to(root)
            if len(relative.parts) >= 2 and relative.parts[0] == "worktrees":
                dirnames[:] = []
                filenames = []
            for name in list(dirnames) + list(filenames):
                found.add(str(root / relative / name))
    return found


def pytest_configure(config: pytest.Config) -> None:
    config.stash_real_state = _real_state_inventory()  # type: ignore[attr-defined]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    before: set[str] = getattr(session.config, "stash_real_state", set())
    added = sorted(_real_state_inventory() - before)
    session.config.stash_real_state_added = added  # type: ignore[attr-defined]
    if added and exitstatus == 0:
        session.exitstatus = 1


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    added: list[str] = getattr(config, "stash_real_state_added", [])
    if not added:
        return
    terminalreporter.section(
        "Research OS real-state contamination", red=True, bold=True
    )
    terminalreporter.write_line(
        f"{len(added)} entries appeared in the researcher's real Research OS "
        "directories during this run:"
    )
    for path in added[:40]:
        terminalreporter.write_line(f"  {path}")
    if len(added) > 40:
        terminalreporter.write_line(f"  ... and {len(added) - 40} more")


# --- autonomous runtime (R5) ------------------------------------------------
# Imported rather than redefined so that a test module can also import them
# directly; pytest only discovers fixtures that are named in a conftest.
from tests.runtime_helpers import (  # noqa: F401
    pg_dsn,
    runtime_db,
    runtime_project,
    runtime_xdg,
    throwaway_dsn,
)
