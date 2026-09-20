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
import re
import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest

from research_os.runtime.db import Database
from research_os.runtime.migrations import migrate

#: Every table the runtime owns. Truncated between tests, in one statement so
#: foreign keys do not dictate an order.
RUNTIME_TABLES = (
    # The portfolio layer, listed first only because it references the runtime
    # and not the other way round. `truncate ... cascade` would reach most of
    # these through `projects` anyway; they are named explicitly because
    # "it happens to cascade" is not a property anyone checks, and
    # `test_portfolio_store.py::test_every_table_is_truncated_between_tests`
    # asserts this tuple covers the live schema.
    "portfolio_digests",
    "portfolio_seeds",
    "portfolio_state",
    "idea_objections",
    "idea_reviews",
    "idea_evidence",
    "idea_actions",
    "idea_edges",
    "idea_versions",
    "ideas",
    "artifact_links",
    "artifacts",
    "capsule_observations",
    "experiment_interpretations",
    "runtime_nomination_links",
    "runtime_proposal_links",
    "runtime_proposal_reservations",
    "runtime_finding_refs",
    "runtime_findings",
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
def runtime_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every Research OS directory into ``tmp_path``.

    Requested by :func:`runtime_db`, and worth keeping even though
    ``isolate_research_os_state`` now redirects every root for every test: the
    runtime reaches the XDG homes in more places than is obvious -- the
    artifact store, experiment run directories, the notification inbox, the
    literature index, the disposable database's cluster -- and a runtime test
    usually wants to *look* at one of those directories, which means knowing
    which one it is.

    It used to be the only thing standing between those tests and the real
    machine, because the autouse fixture of the day only *unset* the overrides
    and the resolver fell back to ``Path.home()``. That was not hypothetical:
    before this fixture existed the experiment tests wrote seven job
    directories into the real data home, and the derived-index chaos test was
    one line away from deleting the researcher's actual literature database --
    stopped by an unrelated schema check, which is not a safety mechanism.
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
    return mapping["RESEARCH_OS_DATA_HOME"]


@pytest.fixture
def runtime_db(pg_dsn: str, runtime_xdg: Path) -> Iterator[Database]:
    """A migrated, empty database, and a handle to it.

    Depends on :func:`runtime_xdg` so that no test using it can touch the
    researcher's real directories, whatever else it does.
    """

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


@pytest.fixture
def throwaway_dsn(pg_dsn: str) -> Iterator[str]:
    """A fresh, empty database that is dropped afterwards.

    For tests that change ``schema_migrations`` or run migrations from nothing.
    The session database is shared and ``runtime_db`` truncates only the
    runtime tables -- deliberately, because re-migrating per test would cost
    seconds -- so a test that tampers with a *checksum* leaks into every later
    test that calls ``migrate()``.

    That is not hypothetical: it broke five CLI tests, and only after they were
    run in a different order. In the default alphabetical order the CLI tests
    happen to run before the schema tests, so the suite passed by luck.
    """

    name = f"research_os_probe_{secrets.token_hex(4)}"
    with Database(pg_dsn) as admin, admin.autocommit() as conn:
        conn.execute(f'create database "{name}"')
    try:
        yield _with_database(pg_dsn, name)
    finally:
        with Database(pg_dsn) as admin, admin.autocommit() as conn:
            conn.execute(f'drop database if exists "{name}" with (force)')


def _with_database(dsn: str, name: str) -> str:
    """Point a DSN at a different database, whatever form it is in."""

    if "dbname=" in dsn:
        return re.sub(r"dbname=\S+", f"dbname={name}", dsn)
    if "://" in dsn:
        scheme, _, rest = dsn.partition("://")
        authority, _, tail = rest.partition("/")
        path, _, query = tail.partition("?")
        del path
        return f"{scheme}://{authority}/{name}" + (f"?{query}" if query else "")
    return f"{dsn} dbname={name}"
