"""The commands a researcher actually types, driven through the real CLI.

Real parsing, real exit codes, a real database. Two properties get particular
attention, for the reasons the runtime's CLI test gives and one more:

- every view renders on an empty portfolio, because the first time somebody
  runs ``researchctl portfolio status`` there is nothing in it;
- nothing here can promote, accept or approve anything -- the command set has
  no verb for it, and that is asserted rather than assumed;
- and every listing says what kind of object it is showing, because
  ``researchctl ideas`` and ``.research/ideas/`` are different things with the
  same English word.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from research_os.cli import main
from research_os.portfolio.models import (
    EvidenceKind,
    EvidenceStrength,
    IdeaStatus,
    ReviewerRole,
)
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.config import DSN_ENV
from research_os.runtime.db import Database
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import portfolio, record_review, seed_idea
from tests.runtime_graph_helpers import make_capsule
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_project", "runtime_xdg"]


def run_cli(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr("sys.argv", ["researchctl", *argv])
    try:
        main()
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    return 0


@pytest.fixture
def cli(
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    monkeypatch.setenv(DSN_ENV, pg_dsn)
    repo = make_capsule(tmp_path / "project")
    RuntimeStore(runtime_db).upsert_project(
        project_id="alpha-project", repo_path=str(repo)
    )
    return "alpha-project"


# --------------------------------------------------------- the empty case --
@pytest.mark.parametrize(
    "argv",
    [
        ("portfolio", "status", "alpha-project"),
        ("portfolio", "top", "alpha-project"),
        ("portfolio", "digest", "alpha-project"),
        ("portfolio", "digest", "alpha-project", "list"),
        ("ideas", "list", "alpha-project"),
        ("ideas", "rejected", "alpha-project"),
        ("ideas", "validated", "alpha-project"),
        ("ideas", "human-ready", "alpha-project"),
        ("seed", "list", "alpha-project"),
    ],
)
def test_every_view_renders_on_an_empty_portfolio(
    cli: str, monkeypatch: pytest.MonkeyPatch, capsys, argv: tuple[str, ...]
) -> None:
    assert run_cli(monkeypatch, *argv) == 0
    assert capsys.readouterr().out


# ------------------------------------------------------------ what it says --
def test_a_listing_says_which_kind_of_idea_it_is_showing(
    cli: str, portfolio: PortfolioStore, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """`researchctl ideas` and `.research/ideas/IDEA-0001.yaml` are different.

    A researcher reading a list that did not say so would reasonably assume
    these were their capsule's objects, which is the one confusion this
    layer's entire naming discipline exists to prevent.
    """

    idea, _ = seed_idea(portfolio, cli)
    assert run_cli(monkeypatch, "ideas", "list", cli) == 0
    out = capsys.readouterr().out
    assert "PIDEA" in out
    assert "IDEA-0001" in out
    assert idea.idea_id in out


def test_a_validated_idea_is_shown_with_what_it_rests_on(
    cli: str, portfolio: PortfolioStore, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The word VALIDATED never appears alone.

    Beside it: how many executions, how many retrieved sources, and how many
    distinct reviewer models -- computed, not asserted. On this machine the
    last of those is usually one, and the line says what that means.
    """

    idea, _ = seed_idea(portfolio, cli)
    portfolio.add_evidence(
        idea_id=idea.idea_id,
        idea_version=1,
        kind=EvidenceKind.LITERATURE,
        strength=EvidenceStrength.SUPPORTS,
        summary="a retrieved source",
        literature_key="openalex:W1",
    )
    record_review(
        portfolio,
        idea_id=idea.idea_id,
        version=1,
        role=ReviewerRole.METHODOLOGY,
        provider="claude",
        family="anthropic",
        model="opus",
    )
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.VALIDATED)

    assert run_cli(monkeypatch, "ideas", "validated", cli) == 0
    out = capsys.readouterr().out
    assert "sources 1" in out
    assert "not independent review" in out


def test_a_rejected_idea_says_why_it_stopped(
    cli: str, portfolio: PortfolioStore, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The half of a portfolio nobody usually writes down."""

    idea, _ = seed_idea(portfolio, cli)
    portfolio.set_status(
        idea_id=idea.idea_id,
        status=IdeaStatus.REJECTED,
        retire_reason="Theorem 3 of a 2013 paper already states this",
    )
    assert run_cli(monkeypatch, "ideas", "rejected", cli) == 0
    out = capsys.readouterr().out
    assert "Theorem 3" in out


def test_show_says_what_the_idea_would_do_next(
    cli: str, portfolio: PortfolioStore, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    idea, _ = seed_idea(portfolio, cli)
    assert run_cli(monkeypatch, "ideas", "show", idea.idea_id) == 0
    out = capsys.readouterr().out
    assert "next:" in out
    assert "falsifier:" in out


def test_show_refuses_an_id_that_is_not_an_idea(
    cli: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    code = run_cli(monkeypatch, "ideas", "show", "PIDEA-20260101T000000Z-00000000")
    assert code != 0
    assert "not an idea" in capsys.readouterr().out


def test_lineage_shows_where_an_idea_came_from(
    cli: str, portfolio: PortfolioStore, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from research_os.portfolio.models import EdgeKind, IdeaOrigin
    from tests.portfolio_helpers import idea_fields

    parent, _ = seed_idea(portfolio, cli, title="parent")
    child, _ = portfolio.create_idea(
        project_id=cli,
        origin=IdeaOrigin.BRANCH,
        fields=idea_fields(title="child"),
        parent_idea_id=parent.idea_id,
        edge_kind=EdgeKind.SPECIALIZES,
        origin_role="brancher",
    )
    assert run_cli(monkeypatch, "ideas", "lineage", child.idea_id) == 0
    out = capsys.readouterr().out
    assert parent.idea_id in out
    assert "SPECIALIZES" in out


# --------------------------------------------------------------- control --
def test_pause_and_resume_are_recorded(
    cli: str, portfolio: PortfolioStore, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    assert run_cli(monkeypatch, "portfolio", "pause", cli, "--reason", "thinking") == 0
    capsys.readouterr()
    assert portfolio.get_state(cli).status.name == "PAUSED_BY_RESEARCHER"

    assert run_cli(monkeypatch, "portfolio", "resume", cli) == 0
    capsys.readouterr()
    assert portfolio.get_state(cli).status.name == "RUNNING"


def test_a_seed_is_recorded_and_described_as_a_direction_not_a_claim(
    cli: str, portfolio: PortfolioStore, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """What `seed add` tells the researcher matters.

    A seed is a direction to push, and the system may well conclude it is
    already known or not worth pursuing. Saying so is what stops a rejection
    reading as the system disagreeing with them.
    """

    assert (
        run_cli(
            monkeypatch,
            "seed",
            "add",
            cli,
            "--text",
            "Compare column generation with fully-corrective Frank-Wolfe.",
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "SEED-" in out
    assert "not a claim" in out
    assert len(portfolio.pending_seeds(project_id=cli)) == 1


def test_status_reports_what_losing_the_database_would_cost(
    cli: str, portfolio: PortfolioStore, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The stated exposure, as a number rather than an assumption.

    ADR-0002 says ideas curated to the autonomous branch survive and ideas
    changed since do not. This is where a researcher can see how many that is.
    """

    seed_idea(portfolio, cli)
    assert run_cli(monkeypatch, "portfolio", "status", cli) == 0
    out = capsys.readouterr().out
    assert "not yet written to Git" in out
    assert "losing the" in out


def test_status_as_json_is_machine_readable(
    cli: str, portfolio: PortfolioStore, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import json

    seed_idea(portfolio, cli)
    assert run_cli(monkeypatch, "portfolio", "status", cli, "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["project"] == cli
    assert payload["counts"]["CANDIDATE"] == 1


# ------------------------------------------------------------- authority --
def test_the_portfolio_command_set_has_no_verb_that_accepts_science(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asserted against the parser, because the guarantee is an absence.

    Promotion and review are human acts with commands of their own
    (`researchctl propose promote`, `researchctl review`). A portfolio verb
    that did either would be this layer acquiring scientific authority by
    convenience, one command at a time.
    """

    import argparse

    from research_os.portfolio.commands import add_parsers

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    add_parsers(subparsers)

    verbs: set[str] = set()

    def _collect(action: argparse.Action) -> None:
        for name, sub in getattr(action, "choices", {}).items():
            verbs.add(name)
            for nested in sub._subparsers._group_actions if sub._subparsers else ():
                _collect(nested)

    _collect(subparsers)
    forbidden = {"promote", "accept", "approve", "review", "merge", "publish", "push"}
    assert not (verbs & forbidden), sorted(verbs & forbidden)
