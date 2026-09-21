"""The PostgreSQL connection layer, and the failure classification around it.

Three decisions are worth stating, because each of them is a bug this runtime
would otherwise have.

**No transaction is ever held open across a wait.** Not across a model call, not
across ``sbatch``, not across a human decision, not across a test run. A work
item is claimed in one short transaction, executed with no transaction at all,
and completed in another short transaction. A runtime that holds a row lock
while a frontier model thinks for ninety seconds is a runtime whose queue stops
when one provider gets slow, and whose ``VACUUM`` never catches up.

**Connections are pooled but conversations are not.** :meth:`Database.tx` hands
out a connection, runs one unit of work and gives it back. Callers never keep
one. That makes worker crashes boring: the pool notices the connection is gone,
the server rolls the transaction back, and the lease expiry does the rest.

**A dropped connection is a *transient* failure, not a task failure.** The
difference decides whether the work is retried or marked permanently broken, so
it is classified here, once, next to the driver, rather than guessed at by each
caller. :func:`classify_db_error` is the only place that reads psycopg's
exception hierarchy.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Self

from research_os.errors import ResearchOSError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from psycopg import Connection

LOG = logging.getLogger("research_os.runtime.db")


class RuntimeDatabaseError(ResearchOSError):
    """Raised when the operational database cannot be reached or used."""


class TransientDatabaseError(RuntimeDatabaseError):
    """A database failure that retrying the same work may well survive.

    Connection loss, server restart, serialization failure, deadlock. The work
    item stays claimable; the attempt is not counted against a permanent
    failure class.
    """


def _require_psycopg() -> Any:
    try:
        import psycopg
        import psycopg.types.json
    except ModuleNotFoundError as exc:  # pragma: no cover - install-time path
        raise RuntimeDatabaseError(
            "The autonomous runtime needs psycopg. Install the runtime extra: "
            "`uv sync --extra runtime`."
        ) from exc
    return psycopg


def jsonb(value: Any) -> Any:
    """Wrap a value as PostgreSQL ``jsonb``, importing the driver on use.

    A module-level ``from psycopg.types.json import Jsonb`` would make importing
    ``research_os.runtime.store`` -- and therefore ``research_os.cli``, which
    registers the runtime commands -- require psycopg. That would quietly break
    the promise in ``runtime/__init__.py`` that a kernel-only install still
    works with two dependencies, and it did: the test that was supposed to catch
    it used the ``find_module`` import hook, which Python 3.12 ignores, so it
    passed while blocking nothing.

    The import is a dict lookup in ``sys.modules`` after the first call.
    """

    psycopg = _require_psycopg()
    return psycopg.types.json.Jsonb(value)


def classify_db_error(exc: BaseException) -> RuntimeDatabaseError:
    """Map a driver exception onto this runtime's two database error classes.

    Anything that means "the server went away or asked you to try again" is
    transient. Everything else -- a syntax error, a constraint violation, a
    missing table -- is a defect in this program and must not be retried in a
    loop, because retrying it produces the same error forever while burning the
    attempt budget that real transient failures need.
    """

    psycopg = _require_psycopg()
    transient = (
        psycopg.OperationalError,
        psycopg.errors.SerializationFailure,
        psycopg.errors.DeadlockDetected,
        psycopg.errors.AdminShutdown,
        psycopg.errors.CannotConnectNow,
        psycopg.errors.CrashShutdown,
        psycopg.errors.ConnectionException,
    )
    if isinstance(exc, transient):
        return TransientDatabaseError(str(exc).strip() or exc.__class__.__name__)
    if isinstance(exc, psycopg.Error):
        return RuntimeDatabaseError(str(exc).strip() or exc.__class__.__name__)
    # Not a database error at all: `tx()` wraps the whole `with` body, so any
    # exception raised by *caller* code inside it arrives here. Reclassifying
    # it is deliberate -- it must not be retried as though the server had
    # blinked -- but rendering it as `str(exc)` alone erased which exception it
    # was, and the first dogfood paid for that. A missing routing-table entry
    # surfaced to the operator as
    #
    #     RuntimeDatabaseError: <ModelRole.NOVELTY_SCREENER: 'novelty_screener'>
    #
    # in `researchctl runtime status`: a database error naming an enum member,
    # with the word `KeyError` nowhere on the screen. Keeping the class name is
    # the whole difference between that and a message that says what happened.
    return RuntimeDatabaseError(
        f"{exc.__class__.__name__}: {exc}" if str(exc) else exc.__class__.__name__
    )


def _reset_session(conn: Any) -> None:
    """Return a connection to the pool with no session state of its own.

    Only advisory locks matter here today, and they matter a lot: see the
    ``reset`` comment in :meth:`Database._ensure_pool`.
    """

    try:
        conn.execute("select pg_advisory_unlock_all()")
        # The pool requires the connection back out of a transaction. A
        # non-autocommit connection opens one on the first statement, so the
        # reset has to close it -- without this, psycopg discards every
        # connection it resets and the pool churns.
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - a failed reset must not kill the pool
        LOG.warning("could not reset a pooled connection: %s", exc)
        try:
            conn.rollback()
        except Exception as rollback_exc:  # noqa: BLE001 - nothing more to try
            LOG.warning("could not roll back a pooled connection: %s", rollback_exc)


class Database:
    """A pooled PostgreSQL handle.

    The pool is opened lazily on first use so that constructing a
    :class:`Database` in a CLI command that turns out not to need one costs
    nothing and fails nothing.
    """

    __slots__ = ("_dsn", "_max_size", "_min_size", "_pool")

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 8) -> None:
        if not dsn:
            raise RuntimeDatabaseError("Database requires a DSN")
        self._dsn = dsn
        self._pool: Any = None
        self._min_size = min_size
        self._max_size = max_size

    @property
    def dsn(self) -> str:
        return self._dsn

    def _ensure_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ModuleNotFoundError as exc:  # pragma: no cover - install-time path
            raise RuntimeDatabaseError(
                "The autonomous runtime needs psycopg[pool]. Install the runtime "
                "extra: `uv sync --extra runtime`."
            ) from exc
        try:
            pool = ConnectionPool(
                self._dsn,
                min_size=self._min_size,
                max_size=self._max_size,
                open=True,
                timeout=30.0,
                # `lock_timeout` so an advisory lock taken with wait=True can
                # never block a worker forever, and a statement timeout as a
                # backstop against a pathological query holding a connection.
                kwargs={
                    "row_factory": dict_row,
                    "autocommit": False,
                    "options": "-c lock_timeout=30s -c idle_in_transaction_session_timeout=60s",
                },
                # psycopg's own reset only normalises transaction status; it does
                # not clear *session* state, and a session-scoped
                # `pg_advisory_lock` is session state. Without this, a lock whose
                # unlock was skipped -- a signal between acquiring and entering
                # the try block -- is held by a connection that returns to the
                # pool and is never released until the process exits.
                reset=_reset_session,
            )
            pool.wait(timeout=30.0)
        except Exception as exc:  # noqa: BLE001 - classified, then re-raised
            raise classify_db_error(exc) from None
        self._pool = pool
        return pool

    @contextmanager
    def tx(self) -> Iterator[Connection]:
        """Run one unit of work in one transaction, then give the connection back.

        Commits on clean exit, rolls back on any exception. There is no nesting
        and no "keep this open for a while": if two statements must be atomic
        they belong in the same ``with`` block, and if a wait sits between them
        they must not be atomic at all.
        """

        pool = self._ensure_pool()
        try:
            with pool.connection() as conn:
                yield conn
        except ResearchOSError:
            raise
        except Exception as exc:  # noqa: BLE001 - classified, then re-raised
            raise classify_db_error(exc) from None

    @contextmanager
    def autocommit(self) -> Iterator[Connection]:
        """A connection in autocommit mode, for statements that refuse a transaction."""

        pool = self._ensure_pool()
        try:
            with pool.connection() as conn:
                conn.set_autocommit(True)
                try:
                    yield conn
                finally:
                    conn.set_autocommit(False)
        except ResearchOSError:
            raise
        except Exception as exc:  # noqa: BLE001 - classified, then re-raised
            raise classify_db_error(exc) from None

    def ping(self) -> str:
        """Return the server version, or raise a classified error."""
        with self.tx() as conn:
            row = conn.execute("select version() as v").fetchone()
        return str(row["v"]) if row else ""

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
