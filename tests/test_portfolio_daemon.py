"""The portfolio reaching the control plane, through the machinery that exists.

The chain under test is the one ``sql/0001_runtime.sql`` argues for:

```text
schedules row  ->  PORTFOLIO_TICK_DUE event  ->  work item  ->  handler
```

and not a new pass in the daemon's deliberate ordering. A scheduled action
passing through the queue gets the same provenance, budget, locking and failure
handling as one a person asked for; a second execution path would get none of
them.

The other property here is the direction of dependency. The daemon has no
import of ``research_os.portfolio`` -- asserted in
``tests/test_runtime_layering.py`` -- so it can only run a portfolio because
something *composed* it with one. These tests do that composition explicitly,
the way ``research_os.service`` does for ``researchd``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from research_os.portfolio import extensions as portfolio_extensions
from research_os.portfolio.allocation import ADVANCE_IDEA, EXPLORE
from research_os.portfolio.config import load_config
from research_os.portfolio.models import IdeaStatus
from research_os.portfolio.store import PortfolioStore
from research_os.portfolio.tick import ensure_schedule
from research_os.runtime import extensions as runtime_extensions
from research_os.runtime.clock import Clock
from research_os.runtime.daemon import Daemon
from research_os.runtime.db import Database
from research_os.runtime.queue import WorkQueue
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import portfolio, seed_idea
from tests.runtime_graph_helpers import ScriptedRouter, make_capsule, make_config
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_project", "runtime_xdg"]


class FrozenClock(Clock):
    def now(self):  # type: ignore[override]
        from datetime import UTC, datetime

        return datetime(2026, 9, 20, 12, 0, tzinfo=UTC)

    def sleep(self, seconds: float) -> None:
        del seconds


@pytest.fixture
def plane(runtime_db: Database, pg_dsn: str, tmp_path: Path, runtime_project: str):
    """A control plane composed with the portfolio, as `researchd` is."""

    portfolio_extensions.register()
    repo = make_capsule(tmp_path / "project")
    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id=runtime_project, repo_path=str(repo))
    router = ScriptedRouter(
        answers={
            "blind_explorer": {
                "candidates": [
                    {
                        "title": "an explored direction",
                        "research_question": "Does the bound hold under ties?",
                        "core_idea": "Ties break the monotonicity the bound needs.",
                        "falsifier": "Exhibit a tied instance where it still holds.",
                    }
                ]
            },
        },
        store=store,
    )
    daemon = Daemon(
        config=make_config(pg_dsn, tmp_path / "artifacts"),
        db=runtime_db,
        repo_for=lambda _project: repo,
        models=lambda _run, _project, _work: router,
        clock=FrozenClock(),
        owner="portfolio-test-worker",
    )
    return {
        "daemon": daemon,
        "db": runtime_db,
        "dsn": pg_dsn,
        "repo": repo,
        "store": store,
        "router": router,
        "queue": WorkQueue(runtime_db),
        "project": runtime_project,
    }


# --------------------------------------------------------- registration --
def test_every_portfolio_work_kind_has_a_handler() -> None:
    portfolio_extensions.register()
    registered = set(runtime_extensions.registered())
    assert {
        "portfolio_tick",
        ADVANCE_IDEA,
        EXPLORE,
        "portfolio_curate",
        "portfolio_digest",
    } <= registered


def test_registering_a_kind_the_runtime_owns_is_refused() -> None:
    """Two layers claiming one kind is a composition error, not a preference.

    The version that silently took the last registration would make which
    handler runs depend on import order.
    """

    from research_os.runtime.extensions import ExtensionError, register_work

    with pytest.raises(ExtensionError, match="runtime work kind"):
        register_work("run_cycle", lambda context: {})


def test_registering_the_same_kind_twice_from_elsewhere_is_refused() -> None:
    from research_os.runtime.extensions import ExtensionError, register_work

    portfolio_extensions.register()
    with pytest.raises(ExtensionError, match="already registered"):
        register_work("portfolio_tick", lambda context: {})


# ------------------------------------------------------------ the chain --
def test_a_schedule_becomes_an_event_becomes_work_becomes_a_tick(
    plane, portfolio: PortfolioStore
) -> None:
    """The whole path, end to end, with nothing bypassing the queue."""

    daemon: Daemon = plane["daemon"]
    store: RuntimeStore = plane["store"]
    project = plane["project"]

    ensure_schedule(db=plane["db"], project_id=project, config=load_config())
    seed_idea(portfolio, project)

    # Pass one: the schedule fires and produces an event.
    first = daemon.tick()
    assert first.schedules_fired >= 1
    kinds = {item.kind for item in store.list_events(limit=50)}
    assert "PORTFOLIO_TICK_DUE" in kinds

    # Pass two: the event becomes a work item and the handler runs it.
    seen: list[str] = []
    for _ in range(6):
        report = daemon.tick()
        seen.append(str(report.payload()))
        if not report.did_something:
            break
    work = plane["queue"]
    kinds_run = {
        item.kind
        for item in store.list_events(limit=200)
        if item.kind.startswith("WORK_")
    }
    assert work.counts_by_status() or kinds_run, seen

    # And the portfolio's own state records that it ticked.
    state = portfolio.get_state(project)
    assert state is not None
    assert state.last_tick_at is not None


def test_an_unknown_work_kind_is_failed_rather_than_left_claimable(
    plane,
) -> None:
    """A kind nobody handles must not sit in the queue forever.

    The runtime already does this for its own kinds; the extension path has to
    behave the same, or a typo in a registration would produce work that is
    claimed, released, claimed, released.
    """

    from research_os.runtime.models import WorkStatus

    queue: WorkQueue = plane["queue"]
    queue.enqueue(
        project_id=plane["project"],
        kind="portfolio_something_nobody_registered",
        payload={},
    )
    plane["daemon"].tick()
    items = queue.list_for_run(run_id=None) if False else None
    del items
    counts = queue.counts_by_status()
    assert counts.get(WorkStatus.FAILED, 0) >= 1


def test_the_explorer_handler_creates_ideas_through_the_queue(
    plane, portfolio: PortfolioStore
) -> None:
    """One explorer work item, and candidate directions afterwards."""

    queue: WorkQueue = plane["queue"]
    project = plane["project"]
    queue.enqueue(
        project_id=project,
        kind=EXPLORE,
        payload={"explorer": "blind_explorer", "reason": "test"},
    )
    for _ in range(4):
        report = plane["daemon"].tick()
        if report.work_succeeded or report.work_failed:
            break
    ideas = portfolio.list_ideas(project_id=project)
    assert ideas, "the explorer produced nothing through the daemon"
    assert ideas[0].status is IdeaStatus.CANDIDATE
    assert ideas[0].origin.name == "BLIND_EXPLORER"


def test_a_tick_work_item_runs_the_deterministic_pipeline(
    plane, portfolio: PortfolioStore
) -> None:
    queue: WorkQueue = plane["queue"]
    project = plane["project"]
    seed_idea(portfolio, project)
    queue.enqueue(project_id=project, kind="portfolio_tick", payload={})
    for _ in range(4):
        report = plane["daemon"].tick()
        if report.work_succeeded or report.work_failed:
            break
    assert report.work_succeeded >= 1
    assert portfolio.get_state(project).last_tick_at is not None


def test_the_daemon_still_runs_with_no_portfolio_composed(
    runtime_db: Database, pg_dsn: str, tmp_path: Path, runtime_project: str
) -> None:
    """Deleting this layer leaves a runtime that still works.

    The property the whole extension arrangement exists for, and the reason
    the daemon consults a registry rather than importing anything: a
    researcher who does not want autonomous discovery is not carrying it.
    """

    store = RuntimeStore(runtime_db)
    repo = make_capsule(tmp_path / "project")
    store.upsert_project(project_id=runtime_project, repo_path=str(repo))
    daemon = Daemon(
        config=make_config(pg_dsn, tmp_path / "artifacts"),
        db=runtime_db,
        repo_for=lambda _project: repo,
        models=lambda _run, _project, _work: ScriptedRouter(),
        clock=FrozenClock(),
        owner="bare-worker",
    )
    report = daemon.tick()
    assert report is not None
