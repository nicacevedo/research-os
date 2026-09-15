"""A real PostgreSQL for the runtime tests, and the fixtures around it.

The runtime's interesting properties are all properties of a *database*:
``for update skip locked`` handing two workers different rows, a unique index
resolving a race between two producers, a lease that cannot be renewed after it
expires, an advisory lock that is released when a connection dies. None of that
can be tested against a mock, and testing it against SQLite would be testing a
different system.

So these tests use PostgreSQL. Getting one is the awkward part, and the answer
here deliberately does not require the developer to have a system service, a
container runtime, or root: ``pgserver`` is a wheel containing a real PostgreSQL
binary distribution, and it initialises a cluster in a temporary directory and
runs it on a unix socket. It is a dev-group dependency and the runtime itself
never imports it -- the runtime only ever sees a DSN.

One server per test session (initdb costs about ten seconds), one database
inside it, and every runtime table truncated between tests. Truncation rather
than a fresh database per test because ``create database`` is the expensive
operation in PostgreSQL and truncating eleven small tables is not.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from research_os.runtime.db import Database
from research_os.runtime.migrations import migrate

#: Every table the runtime owns. Truncated between tests, in one statement so
#: foreign keys do not dictate an order.
RUNTIME_TABLES = (
    "artifact_links",
    "artifacts",
    "approvals",
    "budget_reservations",
    "budgets",
    "events",
    "external_jobs",
    "model_calls",
    "provider_status",
    "schedules",
    "tool_invocations",
    "work_items",
    "research_runs",
    "projects",
)


def pgserver_available() -> str:
    """Return "" when a test server can be started, or the reason it cannot."""

    try:
        import pgserver  # noqa: F401
    except ModuleNotFoundError:
        return "pgserver is not installed (uv sync --all-groups)"
    if os.environ.get("RESEARCH_OS_SKIP_PG_TESTS"):
        return "RESEARCH_OS_SKIP_PG_TESTS is set"
    return ""


@pytest.fixture(scope="session")
def pg_dsn(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A session-wide PostgreSQL, and a DSN for a database inside it."""

    reason = pgserver_available()
    if reason:
        pytest.skip(reason)
    import pgserver

    root: Path = tmp_path_factory.mktemp("pgdata")
    server = pgserver.get_server(str(root / "cluster"), cleanup_mode="stop")
    base = server.get_uri()
    server.psql("create database research_os_test")
    dsn = server.get_uri(database="research_os_test")
    # Prove the database is reachable before any test blames itself for it.
    with Database(dsn) as db:
        db.ping()
        migrate(db)
    yield dsn
    del base


@pytest.fixture
def runtime_db(pg_dsn: str) -> Iterator[Database]:
    """A migrated, empty database, and a handle to it."""

    with Database(pg_dsn) as db:
        with db.tx() as conn:
            conn.execute(
                f"truncate {', '.join(RUNTIME_TABLES)} restart identity cascade"
            )
        yield db


@pytest.fixture
def runtime_project(runtime_db: Database) -> str:
    """One registered project, because almost every row references one."""

    from research_os.runtime.store import RuntimeStore

    RuntimeStore(runtime_db).upsert_project(
        project_id="demo-project", repo_path="/tmp/demo-project", title="Demo"
    )
    return "demo-project"
