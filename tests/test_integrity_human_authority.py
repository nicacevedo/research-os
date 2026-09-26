"""INV-10 -- the autonomous layers cannot grant themselves human authority.

Promotion, a researcher's pause, and a budget or ceiling expansion are human
acts. The promotion doors are already proved unreachable from the runtime and
the portfolio by ``tests/test_runtime_authority.py`` and
``tests/test_portfolio_authority.py``; this file adds the other two, by
parsing the packages rather than trusting a sentence:

- no autonomous module writes a portfolio bound override, marks a ceiling as
  a person's (``explicit=True``), or lifts the operational blocks
  ``portfolio resume`` lifts -- those calls live only in the CLI command
  modules a person runs;
- the tick never resumes a pause the researcher made.

The M4 revival (``tick._revive_budget_parks``) is the case this exists to
keep honest: it *reads* the ceiling a person raised, and the scan below is
what shows it cannot raise one.
"""

from __future__ import annotations

import ast
from pathlib import Path

from research_os.portfolio.config import load_config
from research_os.portfolio.models import PortfolioStatus
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.db import Database
from tests.portfolio_helpers import portfolio
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg
from tests.test_portfolio_tick import _tick

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_project", "runtime_xdg"]

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "research_os"
AUTONOMOUS = ("runtime", "portfolio")
#: The command modules a person runs. Everything else under the two packages
#: is reachable from `researchd` without a person present.
HUMAN_ENTRY_POINTS = frozenset({"runtime/commands.py", "portfolio/commands.py"})


def _autonomous_modules() -> list[tuple[str, ast.Module]]:
    found: list[tuple[str, ast.Module]] = []
    for package in AUTONOMOUS:
        for path in sorted((PACKAGE / package).rglob("*.py")):
            relative = path.relative_to(PACKAGE).as_posix()
            if relative in HUMAN_ENTRY_POINTS:
                continue
            found.append((relative, ast.parse(path.read_text(encoding="utf-8"))))
    assert len(found) > 40, "the scan found too little to mean anything"
    return found


def _calls(tree: ast.Module, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == name
    ]


def test_no_autonomous_module_writes_a_portfolio_bound() -> None:
    offenders = [
        f"{module}:{call.lineno}"
        for module, tree in _autonomous_modules()
        for call in _calls(tree, "upsert_state")
        if any(keyword.arg == "bounds" for keyword in call.keywords)
    ]
    assert offenders == [], f"a bound override written without a person: {offenders}"


def test_no_autonomous_module_marks_a_ceiling_as_a_persons() -> None:
    offenders = [
        f"{module}:{call.lineno}"
        for module, tree in _autonomous_modules()
        for call in _calls(tree, "set_limit")
        if any(
            keyword.arg == "explicit"
            and not (
                isinstance(keyword.value, ast.Constant) and keyword.value.value is False
            )
            for keyword in call.keywords
        )
    ]
    assert offenders == [], f"an explicit ceiling set without a person: {offenders}"


def test_no_autonomous_module_lifts_the_blocks_a_person_resumes() -> None:
    offenders = [
        f"{module}:{call.lineno}"
        for module, tree in _autonomous_modules()
        for call in _calls(tree, "unblock_ideas")
    ]
    assert offenders == [], offenders


def test_the_scan_sees_the_human_entry_points_it_excludes() -> None:
    """The exclusions are real files that do these things, not a typo that hides one."""

    for relative in HUMAN_ENTRY_POINTS:
        assert (PACKAGE / relative).is_file(), relative
    commands = ast.parse(
        (PACKAGE / "portfolio/commands.py").read_text(encoding="utf-8")
    )
    assert _calls(commands, "unblock_ideas")
    runtime_commands = ast.parse(
        (PACKAGE / "runtime/commands.py").read_text(encoding="utf-8")
    )
    assert _calls(runtime_commands, "set_limit")


def test_the_tick_never_resumes_a_researchers_pause(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    portfolio.upsert_state(project_id=runtime_project)
    portfolio.set_portfolio_status(
        project_id=runtime_project,
        status=PortfolioStatus.PAUSED_BY_RESEARCHER,
        detail="a person paused it",
    )
    for _ in range(2):
        report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
        assert report.status is PortfolioStatus.PAUSED_BY_RESEARCHER
    state = portfolio.get_state(runtime_project)
    assert state is not None and state.status is PortfolioStatus.PAUSED_BY_RESEARCHER
    assert load_config() is not None
