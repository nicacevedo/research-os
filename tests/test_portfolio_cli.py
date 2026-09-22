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
    ActionStatus,
    EvidenceKind,
    EvidenceStrength,
    IdeaStatus,
    OperationalState,
    ReviewerRole,
    Stage,
)
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.config import DSN_ENV
from research_os.runtime.db import Database
from research_os.runtime.failures import FailureClass
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


# ------------------------------------------------- starting a portfolio --
def test_enabling_a_portfolio_puts_it_on_the_tick(
    cli: str,
    runtime_db: Database,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The one command without which none of this layer ever runs.

    `ensure_schedule` existed from the first commit and had no production
    caller: every test reached it directly, so a seeded project acquired state
    and no cadence, and `researchd` never ticked it. The headline property --
    the portfolio continues without the researcher advancing cycles -- was
    unreachable from the command line.
    """

    schedules = RuntimeStore(runtime_db).list_schedules()
    assert not [row for row in schedules if row.kind == "PORTFOLIO_TICK_DUE"]

    assert run_cli(monkeypatch, "portfolio", "enable", cli) == 0

    rows = [
        row
        for row in RuntimeStore(runtime_db).list_schedules()
        if row.kind == "PORTFOLIO_TICK_DUE" and row.project_id == cli
    ]
    assert len(rows) == 1
    assert rows[0].payload["project_id"] == cli
    out = capsys.readouterr().out
    assert rows[0].schedule_id in out
    # It says what it is about to spend before it spends it.
    assert "budget" in out


def test_enabling_twice_is_one_schedule(
    cli: str,
    runtime_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idempotent, because a researcher will type it again to check."""

    assert run_cli(monkeypatch, "portfolio", "enable", cli) == 0
    assert run_cli(monkeypatch, "portfolio", "enable", cli) == 0
    rows = [
        row
        for row in RuntimeStore(runtime_db).list_schedules()
        if row.kind == "PORTFOLIO_TICK_DUE" and row.project_id == cli
    ]
    assert len(rows) == 1


def test_enable_does_not_reverse_a_pause(
    cli: str,
    runtime_db: Database,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A pause is the researcher's decision; `enable` is not its undo.

    Otherwise the obvious command to type after coming back to a quiet
    portfolio would silently restart the thing they stopped.
    """

    assert (
        run_cli(monkeypatch, "portfolio", "pause", cli, "--reason", "thesis week") == 0
    )
    capsys.readouterr()
    assert run_cli(monkeypatch, "portfolio", "enable", cli) == 0
    out = capsys.readouterr().out
    assert "thesis week" in out
    assert "resume" in out
    state = PortfolioStore(runtime_db).get_state(cli)
    assert state is not None
    assert str(state.status) == "PAUSED_BY_RESEARCHER"


def test_status_says_when_nothing_will_ever_tick_it(
    cli: str,
    runtime_db: Database,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A RUNNING portfolio with no schedule looks healthy and is not."""

    seed_idea(PortfolioStore(runtime_db), cli)
    assert run_cli(monkeypatch, "portfolio", "status", cli) == 0
    before = capsys.readouterr().out
    assert "not scheduled" in before

    assert run_cli(monkeypatch, "portfolio", "enable", cli) == 0
    capsys.readouterr()
    assert run_cli(monkeypatch, "portfolio", "status", cli) == 0
    after = capsys.readouterr().out
    assert "not scheduled" not in after


def test_a_seed_on_a_portfolio_nothing_ticks_says_so(
    cli: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`seed add` used to promise a "next pass" that was never coming."""

    assert run_cli(monkeypatch, "seed", "add", cli, "--text", "try a warm start") == 0
    out = capsys.readouterr().out
    assert "no next pass yet" in out
    assert "portfolio enable" in out

    assert run_cli(monkeypatch, "portfolio", "enable", cli) == 0
    capsys.readouterr()
    assert run_cli(monkeypatch, "seed", "add", cli, "--text", "and a cold one") == 0
    assert "no next pass yet" not in capsys.readouterr().out


# ------------------------------------------------ what the first dogfood hit --
def test_a_seed_before_the_portfolio_exists_says_what_to_run(
    runtime_db: Database,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Not a foreign-key violation. The fixture above is why this needs its own.

    Every other test in this file uses the ``cli`` fixture, which calls
    ``upsert_project`` -- so the operational ``projects`` row that
    ``portfolio_state`` references always existed, and the path a researcher
    actually takes after ``register-project`` was never exercised. The first
    dogfood took it, following this layer's own documented command order, and
    got

        insert or update on table "portfolio_state" violates foreign key
        constraint "portfolio_state_project_id_fkey"

    from a command set in which every other failure explains itself.
    """

    monkeypatch.setenv(DSN_ENV, pg_dsn)
    code = run_cli(monkeypatch, "seed", "add", "unregistered", "--text", "a direction")
    out = capsys.readouterr().out

    assert code != 0
    assert "foreign key" not in out
    assert "portfolio enable unregistered" in out
    # And it must not have started anything on the way to explaining itself.
    assert PortfolioStore(runtime_db).get_state("unregistered") is None


def test_status_does_not_call_a_portfolio_whose_work_is_failing_healthy(
    cli: str,
    runtime_db: Database,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nine ideas, no tracks, every advance failing -- and it printed RUNNING.

    The first dogfood's routing defect failed every ``portfolio_advance_idea``
    item, and this command said nothing about it. The failures were in
    ``researchctl runtime status``, which is a different layer's view.
    """

    from research_os.portfolio.allocation import ADVANCE_IDEA
    from research_os.runtime.failures import FailureClass
    from research_os.runtime.queue import WorkQueue

    assert run_cli(monkeypatch, "portfolio", "enable", cli) == 0
    capsys.readouterr()

    queue = WorkQueue(runtime_db)
    item = queue.enqueue(
        project_id=cli,
        kind=ADVANCE_IDEA,
        payload={"idea_id": "PIDEA-x"},
        max_attempts=1,
        dedup_key="advance:PIDEA-x",
    ).item
    owner = "worker-under-test"
    queue.claim(owner=owner, lease_seconds=60, limit=1)
    queue.fail(
        item.work_id,
        owner=owner,
        failure_class=FailureClass.CODE_EXCEPTION,
        error="KeyError: <ModelRole.NOVELTY_SCREENER: 'novelty_screener'>",
        force_terminal=True,
    )

    assert run_cli(monkeypatch, "portfolio", "status", cli) == 0
    out = capsys.readouterr().out
    assert "failed" in out
    assert ADVANCE_IDEA in out
    assert "NOVELTY_SCREENER" in out
    assert "not making progress" in out

    # And once work succeeds again, the same eight rows must stop reading as a
    # diagnosis. The soak's first project spent an hour being told it was not
    # making progress while it advanced nine ideas.
    later = queue.enqueue(
        project_id=cli,
        kind=ADVANCE_IDEA,
        payload={"idea_id": "PIDEA-y"},
        max_attempts=1,
        dedup_key="advance:PIDEA-y",
    ).item
    queue.claim(owner=owner, lease_seconds=60, limit=1)
    queue.succeed(later.work_id, owner=owner, result={})

    assert run_cli(monkeypatch, "portfolio", "status", cli) == 0
    after = capsys.readouterr().out
    assert "history rather than a diagnosis" in after
    assert "not making progress" not in after


def test_status_json_carries_the_failures_too(
    cli: str,
    runtime_db: Database,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A machine reading this must see what a person reading it sees."""

    import json as _json

    assert run_cli(monkeypatch, "portfolio", "enable", cli) == 0
    capsys.readouterr()
    assert run_cli(monkeypatch, "portfolio", "status", cli, "--json") == 0
    payload = _json.loads(capsys.readouterr().out)

    assert payload["failed_work"] == 0
    assert payload["failures"] == []


def test_show_says_the_idea_is_blocked_and_what_refused_it(
    cli: str, portfolio: PortfolioStore, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The one command for reading one idea did not mention either.

    Five real ideas sat at `BLOCKED_EXTERNAL` overnight, one of them after
    five failed attempts at the same stage and two careful refusals naming
    the capability the project would have to add. `ideas show` printed
    `next: evidence -- get the evidence this kind of idea would be settled
    by`, which is what `select_stage` would choose and not what happened.
    A researcher reading it would wait for work that has already been tried
    and has already reported why it cannot be done.
    """

    idea, _ = seed_idea(portfolio, cli)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    action = portfolio.open_action(
        idea_id=idea.idea_id,
        idea_version=1,
        stage=Stage.EVIDENCE,
        basis_digest="basis-blocked",
    )
    portfolio.complete_action(
        action_id=action.action_id,
        status=ActionStatus.FAILED,
        detail="no declared command can measure peak memory",
        failure_class=str(FailureClass.CAPABILITY_DENIED),
        operational_state=OperationalState.BLOCKED_EXTERNAL,
    )

    assert run_cli(monkeypatch, "ideas", "show", idea.idea_id) == 0
    out = capsys.readouterr().out
    assert "BLOCKED_EXTERNAL" in out
    assert "capability_denied" in out
    assert "no declared command can measure peak memory" in out


def test_show_stays_quiet_about_an_idea_that_is_simply_working(
    cli: str, portfolio: PortfolioStore, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Positive control: an idle idea gains neither section.

    Asserting the *absence of the line*, not the absence of the word: a
    first draft of this checked only that "BLOCKED" was missing, which a
    view printing "state: IDLE" on every idea would have passed.
    """

    idea, _ = seed_idea(portfolio, cli)
    assert run_cli(monkeypatch, "ideas", "show", idea.idea_id) == 0
    out = capsys.readouterr().out
    assert "state:" not in out
    assert "last attempt" not in out


def test_show_drops_a_failure_the_stage_has_since_overcome(
    cli: str, portfolio: PortfolioStore, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A refusal that has been answered is history, not the current state.

    The idea whose evidence stage this reproduces was refused five times
    for a capability the project had not declared. The researcher declared
    it, `portfolio resume` lifted the block, and the stage then ran a
    contained measurement and wrote an interpreted experiment. `ideas show`
    went on opening with "last attempt at evidence failed
    (capability_denied); tried 5 times" and the whole stale refusal --
    directly under a `next:` line that had moved on to the review board.

    `portfolio status` already draws this distinction in words; this view
    did not draw it at all.
    """

    idea, _ = seed_idea(portfolio, cli)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    for attempt in range(2):
        refused = portfolio.open_action(
            idea_id=idea.idea_id,
            idea_version=1,
            stage=Stage.EVIDENCE,
            basis_digest=f"basis-refused-{attempt}",
        )
        portfolio.complete_action(
            action_id=refused.action_id,
            status=ActionStatus.FAILED,
            detail="no declared command can run this sweep",
            failure_class=str(FailureClass.CAPABILITY_DENIED),
            operational_state=OperationalState.IDLE,
        )
    measured = portfolio.open_action(
        idea_id=idea.idea_id,
        idea_version=1,
        stage=Stage.EVIDENCE,
        basis_digest="basis-measured",
    )
    portfolio.complete_action(
        action_id=measured.action_id,
        status=ActionStatus.SUCCEEDED,
        detail="the measurement ran and was read",
        operational_state=OperationalState.IDLE,
    )

    assert run_cli(monkeypatch, "ideas", "show", idea.idea_id) == 0
    out = capsys.readouterr().out
    assert "last attempt" not in out
    assert "capability_denied" not in out


def test_show_still_reports_a_stage_that_is_still_failing(
    cli: str, portfolio: PortfolioStore, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Negative control, and the reason the filter is per stage.

    A cheap stage succeeding must not silence an expensive one that is
    still refusing -- which is what an aggregate "has anything succeeded
    since" test would have done, and is the defect in the other direction.
    """

    idea, _ = seed_idea(portfolio, cli)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    measured = portfolio.open_action(
        idea_id=idea.idea_id,
        idea_version=1,
        stage=Stage.EVIDENCE,
        basis_digest="basis-measured",
    )
    portfolio.complete_action(
        action_id=measured.action_id,
        status=ActionStatus.SUCCEEDED,
        detail="the measurement ran and was read",
        operational_state=OperationalState.IDLE,
    )
    stuck = portfolio.open_action(
        idea_id=idea.idea_id,
        idea_version=1,
        stage=Stage.DISCOVER,
        basis_digest="basis-stuck",
    )
    portfolio.complete_action(
        action_id=stuck.action_id,
        status=ActionStatus.FAILED,
        detail="the sharpened title was 201 characters",
        failure_class=str(FailureClass.MODEL_OUTPUT_INVALID),
        operational_state=OperationalState.IDLE,
    )

    assert run_cli(monkeypatch, "ideas", "show", idea.idea_id) == 0
    out = capsys.readouterr().out
    assert "last attempt at discover failed" in out
    assert "the sharpened title was 201 characters" in out
