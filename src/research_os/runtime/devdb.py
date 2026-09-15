"""A disposable local PostgreSQL, for a machine that has none.

The runtime needs PostgreSQL and this workstation has no system service, no
container runtime and no root. That is not an unusual situation for a
researcher's laptop, and requiring any of the three would mean the runtime could
not be tried without an afternoon of setup.

``pgserver`` is a wheel containing a real PostgreSQL binary distribution. This
module initialises a cluster under the state home and hands back a DSN. Three
things it deliberately is not:

**It is not how the runtime finds its database.** The runtime reads a DSN and
knows nothing about where it came from. Production is a real PostgreSQL with a
real backup; this is for trying things and for tests.

**It is not a dependency of the runtime.** ``pgserver`` is in the dev group and
imported here only. A production install does not have it and does not need it.

**It is not a service.** The cluster runs while something holds a handle to it,
and ``stop`` shuts it down. Nothing is registered with the host.
"""

from __future__ import annotations

import logging
from pathlib import Path

from research_os.errors import ResearchOSError
from research_os.runtime.config import dev_db_root

LOG = logging.getLogger("research_os.runtime.devdb")

DATABASE_NAME = "research_os"


class DevDatabaseError(ResearchOSError):
    """Raised when the disposable local database cannot be managed."""


def _pgserver():
    try:
        import pgserver
    except ModuleNotFoundError as exc:
        raise DevDatabaseError(
            "The disposable local database needs the `pgserver` dev dependency. "
            "Install it with `uv sync --all-groups`, or set "
            "RESEARCH_OS_RUNTIME_DSN to a PostgreSQL you already have."
        ) from exc
    return pgserver


def cluster_path() -> Path:
    return dev_db_root() / "cluster"


def start(*, cleanup: str | None = None) -> str:
    """Start (or attach to) the disposable cluster and return a DSN.

    ``cleanup_mode=None`` by default, so the server keeps running after this
    process exits -- which is what a person starting it from a terminal wants.
    ``stop`` is how it ends.
    """

    pgserver = _pgserver()
    root = cluster_path()
    root.parent.mkdir(parents=True, exist_ok=True)
    try:
        server = pgserver.get_server(str(root), cleanup_mode=cleanup)
        existing = server.psql(
            f"select 1 from pg_database where datname = '{DATABASE_NAME}'"
        )
        if "1" not in str(existing):
            server.psql(f"create database {DATABASE_NAME}")
        return str(server.get_uri(database=DATABASE_NAME))
    except DevDatabaseError:
        raise
    except Exception as exc:  # noqa: BLE001 - pgserver raises several unrelated types
        raise DevDatabaseError(f"could not start the local database: {exc}") from None


def status() -> tuple[bool, str]:
    """Return ``(running, detail)`` without starting anything."""

    root = cluster_path()
    if not root.exists():
        return False, f"no cluster at {root}"
    pid_file = root / "postmaster.pid"
    if not pid_file.is_file():
        return False, f"a cluster exists at {root} but is not running"
    return True, f"running, cluster at {root}"


def stop() -> str:
    pgserver = _pgserver()
    root = cluster_path()
    if not root.exists():
        return f"no cluster at {root}"
    try:
        server = pgserver.get_server(str(root), cleanup_mode=None)
        server.cleanup()
    except Exception as exc:  # noqa: BLE001 - pgserver raises several unrelated types
        raise DevDatabaseError(f"could not stop the local database: {exc}") from None
    return f"stopped the cluster at {root}"
